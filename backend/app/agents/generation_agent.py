"""
ChisCode — Generation Agent (Phase 3)
======================================
SSE streaming generator with LangGraph-style nodes, smart stack suggestion,
Human-in-the-Loop (HITL) stack selection, and GitHub push on confirm.

Pipeline:
  [node_analyze] → SSE: stack_suggestion →
  HITL: user picks stack →
  [node_generate] → SSE: file events →
  [node_quality_check] →
  SSE: complete (awaiting_confirmation) →
  User clicks Confirm →
  [node_github] → SSE: github_done

HITL mechanic:
  - Agent writes status="awaiting_stack_selection" to MongoDB + yields SSE
  - Frontend shows stack picker modal
  - User POSTs to /projects/{id}/select-stack
  - That endpoint updates MongoDB with chosen stack and status="stack_selected"
  - Client resumes the SSE stream (reconnects) OR the confirm flow proceeds
  
  For simplicity in Phase 3: HITL is done in a SINGLE request —
  the /generate endpoint streams analyze → yields stack_suggestion → 
  PAUSES (returns from generator waiting for /select-stack) then client
  calls /generate/resume which streams generation onwards.
  
  Implemented as two separate SSE endpoints:
    POST /projects/generate          → analyze only, ends with stack_suggestion event
    POST /projects/{id}/generate/run → generate+validate, needs stack already in DB
"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from typing import AsyncGenerator

from bson import ObjectId
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mistralai import ChatMistralAI

from app.agents.stack_advisor import suggest_stacks
from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import projects_collection

logger = get_logger(__name__)


# ── LLM factory ───────────────────────────────────────────────

def _llm(temperature: float = 0.15) -> ChatMistralAI:
    return ChatMistralAI(
        model=settings.codestral_model,
        api_key=settings.codestral_api_key,
        base_url=settings.codestral_base_url,
        temperature=temperature,
        max_tokens=8192,
    )


# ── MongoDB helpers ───────────────────────────────────────────

async def _set(project_id: str, fields: dict) -> None:
    fields["updated_at"] = datetime.now(tz=timezone.utc)
    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {"$set": fields},
    )


async def _log(project_id: str, message: str) -> None:
    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {
            "$push": {"generation_log": message},
            "$set":  {"updated_at": datetime.now(tz=timezone.utc)},
        },
    )


# ── SSE ───────────────────────────────────────────────────────

def _sse(event: str, **data) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


# ═══════════════════════════════════════════════════════════════
# NODE 1 — Analyze + Stack Suggestion
# ═══════════════════════════════════════════════════════════════

async def node_analyze_stream(
    project_id: str,
    prompt:     str,
    project_name: str,
) -> AsyncGenerator[str, None]:
    """
    Analyzes the prompt → builds spec → suggests 3 tech stacks → HITL pause.
    Yields SSE events. Ends with 'stack_suggestion' event carrying 3 options.
    The frontend shows a picker; user calls /select-stack to resume.
    """
    await _set(project_id, {"status": "analyzing"})
    await _log(project_id, "🔍 Analyzing requirements...")
    yield _sse("status", status="analyzing", message="Analyzing your requirements...")

    # ── Analyze prompt ────────────────────────────────────────
    try:
        spec, file_plan_hint = await _analyze_prompt(prompt, project_name)
    except Exception as exc:
        msg = f"Analysis failed: {exc}"
        await _set(project_id, {"status": "failed", "error_message": msg})
        await _log(project_id, f"❌ {msg}")
        yield _sse("error", message=msg)
        return

    complexity = spec.get("complexity", "moderate")
    app_type   = spec.get("app_type", "web_app")
    features   = spec.get("features", [])

    await _set(project_id, {"spec": spec, "status": "analyzing"})
    await _log(project_id, f"✅ App analyzed: {spec.get('app_name')} ({complexity})")
    yield _sse("log", message=f"App: {spec.get('app_name')} · {complexity} · {len(file_plan_hint)} files planned")

    # ── Suggest stacks ────────────────────────────────────────
    yield _sse("log", message="🧠 Evaluating best tech stacks for your app...")
    try:
        stack_options = await suggest_stacks(prompt, app_type, complexity, features)
    except Exception as exc:
        logger.warning("Stack suggestion failed", error=str(exc))
        stack_options = []

    # ── HITL pause — wait for user to pick stack ──────────────
    await _set(project_id, {
        "status":             "awaiting_stack_selection",
        "stack_options":      stack_options,
        "spec":               spec,
        "file_plan_hint":     file_plan_hint,
    })
    await _log(project_id, "⏸ Waiting for stack selection...")
    yield _sse(
        "stack_suggestion",
        options=stack_options,
        message="Pick your tech stack to continue",
        project_id=project_id,
    )
    # Stream ends here — frontend resumes with /generate/run after user picks


# ═══════════════════════════════════════════════════════════════
# NODE 2 — Generate + Quality Check (runs after HITL)
# ═══════════════════════════════════════════════════════════════

async def node_generate_stream(
    project_id: str,
) -> AsyncGenerator[str, None]:
    """
    Reads spec + chosen stack from MongoDB, generates all files, quality checks.
    Called after user selects a stack via /select-stack endpoint.
    """
    # Load project state from DB
    doc = await projects_collection().find_one({"_id": ObjectId(project_id)})
    if not doc:
        yield _sse("error", message="Project not found")
        return

    if doc.get("status") != "stack_selected":
        yield _sse("error", message=f"Expected status 'stack_selected', got '{doc.get('status')}'")
        return

    spec          = doc.get("spec", {})
    chosen_stack  = doc.get("stack", {})
    file_plan     = doc.get("file_plan_hint") or _default_file_plan(spec, chosen_stack)
    prompt        = doc.get("original_prompt", "")

    await _set(project_id, {"status": "generating", "file_plan": file_plan})
    yield _sse("status", status="generating", message=f"Generating {len(file_plan)} files...")

    # ── Build system prompt ───────────────────────────────────
    stack_desc = _stack_description(chosen_stack)
    system_prompt = (
        f"You are an expert {stack_desc} developer.\n"
        f"App: {spec.get('app_name')} — {spec.get('description')}\n"
        f"Features: {', '.join(spec.get('features', []))}\n"
        f"Stack: {stack_desc}\n\n"
        f"Rules:\n"
        f"- Output ONLY raw file content — no markdown fences, no explanation\n"
        f"- Write complete, production-ready code — no placeholders, no TODOs\n"
        f"- Use modern best practices for every language and framework in the stack\n"
        f"- HTML files: full <!DOCTYPE html> structure, responsive, styled\n"
        f"- Python files: syntactically valid, typed where appropriate\n"
        f"- JS/TS files: ES2022+, proper imports/exports\n"
        f"- Include helpful inline comments\n"
        f"- with requirements, .env, and Dockerfile if required/n"
    )

    # ── Generate files ────────────────────────────────────────
    file_tree: dict[str, str] = {}

    for filename in file_plan:
        await _log(project_id, f"📝 {filename}...")
        yield _sse("log", message=f"📝 Generating {filename}...")

        context = ""
        for prev in list(file_tree.keys())[-2:]:
            context += f"\n\n# {prev} (excerpt):\n{file_tree[prev][:400]}"

        try:
            response = await _llm(temperature=0.2).ainvoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=(
                    f"Generate file: {filename}"
                    + (f"\n\nContext from files already generated:{context}" if context else "")
                )),
            ])
            content = _strip_fences(response.content)

            if not content or len(content.strip()) < 10:
                content = f"# {filename}\n# Generation returned empty\n"
                await _log(project_id, f"⚠️ {filename} empty")
                yield _sse("log", message=f"⚠️ {filename} came back empty")
            else:
                file_tree[filename] = content
                await _set(project_id, {"file_tree": file_tree})
                await _log(project_id, f"✅ {filename} ({len(content):,} chars)")
                yield _sse("file", filename=filename, size=len(content))

        except Exception as exc:
            logger.error("File generation error", filename=filename, error=str(exc))
            await _log(project_id, f"❌ {filename}: {exc}")
            yield _sse("log", message=f"❌ {filename} failed")

    # ── Quality check ─────────────────────────────────────────
    await _set(project_id, {"status": "quality_check"})
    yield _sse("status", status="quality_check", message="Running quality checks...")

    issues = _quality_check(file_tree, file_plan)
    if issues:
        for issue in issues:
            await _log(project_id, f"⚠️ {issue}")
        yield _sse("issues", issues=issues)
        yield _sse("log", message=f"⚠️ {len(issues)} quality issue(s) noted")
    else:
        await _log(project_id, "✅ Quality check passed")
        yield _sse("log", message="✅ Quality check passed")

    # ── Complete ──────────────────────────────────────────────
    file_count = len(file_tree)
    await _set(project_id, {
        "status":    "awaiting_confirmation",
        "file_tree": file_tree,
    })
    await _log(project_id, f"🚀 Done — {file_count} files ready for review")
    yield _sse("complete", file_count=file_count, message=f"{file_count} files ready — review and confirm to push to GitHub")


# ═══════════════════════════════════════════════════════════════
# NODE 3 — GitHub Push (called on confirm)
# ═══════════════════════════════════════════════════════════════

async def node_github_stream(
    project_id:      str,
    github_token:    str,
    commit_message:  str,
    private_repo:    bool = False,
) -> AsyncGenerator[str, None]:
    """
    Creates GitHub repo + pushes all files + saves repo URL.
    Called from confirm endpoint as a StreamingResponse.
    """
    from app.services.github_service import GitHubService, GitHubError

    doc = await projects_collection().find_one({"_id": ObjectId(project_id)})
    if not doc:
        yield _sse("error", message="Project not found")
        return

    file_tree    = doc.get("file_tree", {})
    spec         = doc.get("spec", {})
    project_name = doc.get("name", "chiscode-project")

    if not file_tree:
        yield _sse("error", message="No files to push — generate first")
        return

    await _set(project_id, {"status": "committing"})
    yield _sse("status", status="committing", message="Pushing to GitHub...")

    gh = GitHubService(github_token)

    try:
        # Sanitize repo name
        repo_name = re.sub(r"[^a-zA-Z0-9_.-]", "-", project_name.lower())
        repo_name = re.sub(r"-+", "-", repo_name).strip("-")[:100]

        yield _sse("log", message=f"📦 Creating repository: {repo_name}...")

        result = await gh.create_repo_and_push(
            repo_name=repo_name,
            description=spec.get("description", "")[:255],
            file_tree=file_tree,
            commit_message=commit_message or f"Initial commit — generated by ChisCode",
            private=private_repo,
        )

        repo_url   = result["repo_url"]
        commit_sha = result["commit_sha"]
        owner      = result["owner"]

        await _set(project_id, {
            "status":           "complete",
            "github_repo_url":  repo_url,
            "github_repo_name": repo_name,
            "github_owner":     owner,
            "current_version":  1,
        })
        await _log(project_id, f"🎉 Pushed to GitHub: {repo_url}")

        yield _sse("log",         message=f"✅ Repository created: {repo_url}")
        yield _sse("github_done", repo_url=repo_url, commit_sha=commit_sha,
                                  message="Code pushed to GitHub successfully!")

    except GitHubError as exc:
        msg = f"GitHub push failed: {exc}"
        await _set(project_id, {"status": "failed", "error_message": msg})
        await _log(project_id, f"❌ {msg}")
        yield _sse("error", message=msg)

    except Exception as exc:
        msg = f"Unexpected error during GitHub push: {exc}"
        await _set(project_id, {"status": "failed", "error_message": msg})
        await _log(project_id, f"❌ {msg}")
        yield _sse("error", message=msg)


# ═══════════════════════════════════════════════════════════════
# NODE 4 — Iteration PR (Phase 4)
# ═══════════════════════════════════════════════════════════════

async def node_iterate_stream(
    project_id:   str,
    github_token: str,
    iterate_prompt: str,
    version:      int,
) -> AsyncGenerator[str, None]:
    """
    Re-generates changed files based on iterate_prompt, pushes to a
    feature branch, and opens a PR against main.
    """
    from app.services.github_service import GitHubService, GitHubError

    doc = await projects_collection().find_one({"_id": ObjectId(project_id)})
    if not doc:
        yield _sse("error", message="Project not found")
        return

    file_tree    = dict(doc.get("file_tree", {}))
    spec         = doc.get("spec", {})
    stack        = doc.get("stack", {})
    repo_name    = doc.get("github_repo_name", "")
    owner        = doc.get("github_owner", "")

    if not repo_name or not owner:
        yield _sse("error", message="Project has no GitHub repo — confirm first")
        return

    await _set(project_id, {"status": "generating"})
    yield _sse("status", status="generating", message="Generating iteration changes...")
    await _log(project_id, f"🔄 Iteration: {iterate_prompt[:80]}...")

    stack_desc    = _stack_description(stack)
    changed_files: dict[str, str] = {}

    # Ask Codestral which files need changing + regenerate them
    try:
        plan_response = await _llm(0.1).ainvoke([
            SystemMessage(content="You are a code editor. List which files need changes. Return ONLY JSON: {\"files\": [\"path1\", \"path2\"]}"),
            HumanMessage(content=(
                f"Existing files: {list(file_tree.keys())}\n"
                f"Change request: {iterate_prompt}\n"
                f"Return JSON list of files that need to change."
            )),
        ])
        raw = _strip_fences(plan_response.content)
        files_to_change = json.loads(raw).get("files", list(file_tree.keys())[:3])
    except Exception:
        files_to_change = list(file_tree.keys())[:3]

    yield _sse("log", message=f"📋 Files to update: {', '.join(files_to_change)}")

    system_prompt = (
        f"You are an expert {stack_desc} developer.\n"
        f"Modify the file according to the change request. Output ONLY the complete new file content.\n"
        f"App: {spec.get('app_name')} — {spec.get('description')}\n"
        f"Stack: {stack_desc}\n"
    )

    for filename in files_to_change:
        yield _sse("log", message=f"✏️ Updating {filename}...")
        try:
            existing = file_tree.get(filename, "")
            response = await _llm(0.2).ainvoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=(
                    f"File: {filename}\n"
                    f"Change request: {iterate_prompt}\n\n"
                    f"Current content:\n{existing[:3000]}\n\n"
                    f"Return the complete updated file."
                )),
            ])
            content = _strip_fences(response.content)
            if content and len(content.strip()) > 10:
                changed_files[filename] = content
                file_tree[filename]     = content
                yield _sse("file", filename=filename, size=len(content))
        except Exception as exc:
            yield _sse("log", message=f"❌ {filename}: {exc}")

    if not changed_files:
        yield _sse("error", message="No files were updated")
        return

    # Quality check on changed files only
    issues = _quality_check(changed_files, list(changed_files.keys()))
    if issues:
        yield _sse("issues", issues=issues)

    # Push to GitHub PR
    yield _sse("status", status="committing", message="Opening Pull Request...")
    gh = GitHubService(github_token)
    branch_name = f"chiscode/iteration-v{version}"

    try:
        result = await gh.push_iteration_pr(
            owner=owner,
            repo=repo_name,
            branch_name=branch_name,
            file_tree=changed_files,
            commit_message=f"feat: {iterate_prompt[:72]}",
            pr_title=f"ChisCode Iteration v{version}: {iterate_prompt[:60]}",
            pr_body=(
                f"## Changes\n{iterate_prompt}\n\n"
                f"**Files changed:** {', '.join(changed_files.keys())}\n\n"
                f"_Generated by ChisCode_"
            ),
        )

        await _set(project_id, {
            "status":    "awaiting_confirmation",
            "file_tree": file_tree,
        })
        await _log(project_id, f"✅ PR opened: {result['pr_url']}")
        yield _sse("github_done",
                   pr_url=result["pr_url"],
                   commit_sha=result["commit_sha"],
                   message=f"PR opened — review and merge on GitHub")

    except GitHubError as exc:
        yield _sse("error", message=f"GitHub error: {exc}")



# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

async def _analyze_prompt(prompt: str, project_name: str) -> tuple[dict, list[str]]:
    response = await _llm(0.1).ainvoke([
        SystemMessage(content="""Analyze the app idea. Return ONLY valid JSON.
{
  "app_type": "web_app|api|cli|mobile_web|dashboard|landing_page|e_commerce|real_time_app|data_app|game|ai_app",
  "app_name": "snake_case",
  "description": "one sentence",
  "features": ["feat1", "feat2"],
  "auth_required": false,
  "database_needed": false,
  "api_needed": false,
  "mobile_responsive": true,
  "complexity": "simple|moderate|complex",
  "file_plan": ["path/file.ext"]
}
file_plan: all files for a working app. simple=4-6, moderate=7-14, complex=15-25. Always README.md."""),
        HumanMessage(content=f"App idea: {prompt}"),
    ])
    raw = _strip_fences(response.content)
    try:
        data = json.loads(raw)
    except Exception:
        data = {
            "app_type": "web_app", "app_name": re.sub(r"\s+", "_", project_name).lower(),
            "description": prompt[:150], "features": [], "auth_required": False,
            "database_needed": False, "api_needed": False, "mobile_responsive": True,
            "complexity": "simple",
            "file_plan": ["index.html", "style.css", "app.js", "README.md"],
        }
    file_plan = data.pop("file_plan", ["index.html", "README.md"])
    return data, file_plan


def _default_file_plan(spec: dict, stack: dict) -> list[str]:
    backend  = (stack.get("backend") or "").lower()
    frontend = (stack.get("frontend") or "").lower()
    files    = ["README.md"]
    if "fastapi" in backend or "python" in backend:
        files += ["main.py", "requirements.txt"]
    if "react" in frontend or "next" in frontend:
        files += ["src/App.jsx", "src/index.js", "package.json", "index.html"]
    elif "html" in frontend or not frontend:
        files += ["index.html", "style.css", "app.js"]
    return files


def _stack_description(stack: dict) -> str:
    parts = [v for v in [
        stack.get("frontend", ""),
        stack.get("backend", ""),
        stack.get("database", ""),
    ] if v and v.lower() not in ("none", "")]
    extras = stack.get("extras", [])
    if extras:
        parts.extend(extras[:2])
    return " + ".join(parts) if parts else "HTML/CSS/JS"


def _quality_check(file_tree: dict[str, str], file_plan: list[str]) -> list[str]:
    issues = []
    missing = [f for f in file_plan if f not in file_tree]
    if missing:
        issues.append(f"Missing: {', '.join(missing)}")
    for path, content in file_tree.items():
        if not content or len(content.strip()) < 20:
            issues.append(f"{path} is empty")
    for path, content in file_tree.items():
        if path.endswith(".py") and content:
            try:
                ast.parse(content)
            except SyntaxError as e:
                issues.append(f"Syntax error in {path} line {e.lineno}: {e.msg}")
    for path, content in file_tree.items():
        if path.endswith(".html") and content:
            if "<!doctype" not in content.lower() and "<html" not in content.lower():
                issues.append(f"{path} missing DOCTYPE/html root")
    if not any(p.lower() == "readme.md" for p in file_tree):
        issues.append("README.md missing")
    return issues


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ── Legacy compat ─────────────────────────────────────────────

async def run_generation_agent(
    project_id: str, user_id: str, prompt: str,
    project_name: str, preferred_stack: dict | None = None,
) -> None:
    async for _ in generate_project_stream(
        project_id, user_id, prompt, project_name, preferred_stack
    ):
        pass


async def generate_project_stream(
    project_id: str, user_id: str, prompt: str,
    project_name: str, preferred_stack: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Legacy single-stream flow (no HITL). Used as fallback."""
    async for chunk in node_analyze_stream(project_id, prompt, project_name):
        yield chunk