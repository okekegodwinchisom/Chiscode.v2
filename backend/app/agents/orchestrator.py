"""
ChisCode — MCP Orchestrator
============================
LangGraph StateGraph workflows that wire MCP tools together.

Every node in the graph calls an MCP tool via _call_tool().
This means the graph is purely a coordination layer — all
actual work happens inside the MCP server tools.

Four workflows:
  1. generate_workflow  — full project generation with HITL
  2. iterate_workflow   — iterate on existing project, commit to main
  3. github_workflow    — standalone repo creation + push
  4. playwright_test    — Daytona sandbox + browser test + self-heal

State flows through a typed ProjectState TypedDict.
SSE events are yielded from run_workflow() as strings.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.logging import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════

class ProjectState(TypedDict, total=False):
    # Inputs
    project_id:           str
    user_id:              str
    prompt:               str
    project_name:         str
    github_token:         str

    # Analysis
    spec:                 dict
    stack:                dict
    stack_options:        list[dict]
    file_plan:            list[str]

    # Generation
    file_tree:            dict[str, str]
    issues:               list[str]

    # GitHub
    repo_url:             str
    commit_sha:           str
    pr_url:               str
    github_owner:         str
    github_repo_name:     str

    # Preview / Daytona
    preview_url:          str
    daytona_workspace_id: str
    preview_screenshot:   str

    # Iterate
    iterate_prompt:       str
    version:              int

    # Control flow
    status:               str
    error:                str | None
    logs:                 list[str]

    # SSE queue
    _sse_queue:           asyncio.Queue


# ═══════════════════════════════════════════════════════════════
# MCP Client helper
# ═══════════════════════════════════════════════════════════════

async def _call_tool(tool: str, params: dict) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"http://localhost:7860/api/v1/mcp/tools/{tool}",
            json={"params": params},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"MCP tool '{tool}' error: {data['error']}")
        return data["result"]


def _sse(event: str, **data) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


async def _push(state: ProjectState, event: str, **data) -> None:
    """Push SSE event to queue + append to logs."""
    msg = data.get("message", "")
    if msg:
        state.setdefault("logs", []).append(msg)
    q = state.get("_sse_queue")
    if q:
        await q.put(_sse(event, **data))


def _strip_fences(text: str) -> str:
    import re
    text = text.strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════
# WORKFLOW 1 — Generate
# Node: analyze
# ═══════════════════════════════════════════════════════════════

async def node_analyze(state: ProjectState) -> ProjectState:
    await _push(state, "status", status="analyzing", message="🔍 Analyzing requirements...")

    try:
        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields":     {"status": "analyzing"},
        })

        result = await _call_tool("analyze_prompt", {
            "prompt":       state["prompt"],
            "project_name": state.get("project_name", "my-app"),
        })
        spec = result["spec"]

        await _push(state, "log", message="🧠 Evaluating best tech stacks...")
        stacks = await _call_tool("stack_advisor", {
            "prompt":     state["prompt"],
            "app_type":   spec.get("app_type", "web_app"),
            "complexity": spec.get("complexity", "moderate"),
            "features":   spec.get("features", []),
        })

        state["spec"]          = spec
        state["stack_options"] = stacks["options"]
        state["status"]        = "awaiting_stack_selection"

        await _push(state, "stack_suggestion",
                    stacks=stacks["options"],
                    message="Pick your tech stack to continue",
                    project_id=state["project_id"])
        logger.info("✅ stack_suggestion event sent")

        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields": {
                "spec":          spec,
                "stack_options": stacks["options"],
                "status":        "awaiting_stack_selection",
            },
        })
    except Exception as exc:
        state["error"]  = str(exc)
        state["status"] = "failed"
        await _push(state, "error", message=f"Analysis failed: {exc}")
        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields": {"status": "failed", "error_message": str(exc)},
        })

    return state


# ═══════════════════════════════════════════════════════════════
# Node: scaffold_files
# ═══════════════════════════════════════════════════════════════

async def node_scaffold(state: ProjectState) -> ProjectState:
    """Generate file plan from stack + spec."""
    try:
        result = await _call_tool("file_scaffold", {
            "spec":  state.get("spec", {}),
            "stack": state.get("stack", {}),
        })
        state["file_plan"] = result["files"]

        logger.debug("file_plan",
                     project_id=state["project_id"],
                     files=result["files"])

        await _push(state, "log",
                    message=f"📋 {result['count']} files planned: {', '.join(result['files'])}")
        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields": {"file_plan_hint": result["files"], "status": "generating"},
        })
    except Exception as exc:
        state["error"]  = str(exc)
        state["status"] = "failed"
        await _push(state, "error", message=f"Scaffold failed: {exc}")
    return state


# ═══════════════════════════════════════════════════════════════
# Node: generate_files (parallel)
# ═══════════════════════════════════════════════════════════════

async def node_generate(state: ProjectState) -> ProjectState:
    """Generate all files in parallel via MCP code_generator tool."""
    spec      = state.get("spec", {})
    stack     = state.get("stack", {})
    file_plan = state.get("file_plan", [])

    if not file_plan:
        state["error"]  = "No file plan — run scaffold first"
        state["status"] = "failed"
        return state

    parts = [v for k, v in stack.items()
             if k in ("frontend", "backend", "database") and v and v.lower() != "none"]
    extras = stack.get("extras", [])
    if extras:
        parts.extend(extras[:3])
    stack_desc = " + ".join(parts) if parts else "HTML/CSS/JS"

    system_prompt = (
        f"You are an expert {stack_desc} developer.\n"
        f"App: {spec.get('app_name', 'App')} — {spec.get('description', '')}\n"
        f"Features: {', '.join(spec.get('features', []))}\n"
        f"Stack: {stack_desc}\n\n"
        f"Rules:\n"
        f"- Output ONLY raw file content — no markdown fences, no explanation\n"
        f"- Write complete, production-ready code — no placeholders\n"
        f"- Use modern best practices for every language in the stack\n"
        f"- HTML: full <!DOCTYPE html>, responsive, styled\n"
        f"- Python: syntactically valid, typed\n"
        f"- JS/TS: ES2022+, proper imports/exports\n"
    )

    await _push(state, "status", status="generating",
                message=f"⚡ Generating {len(file_plan)} files in parallel...")

    file_tree: dict[str, str] = {}
    queue: asyncio.Queue = asyncio.Queue()

    async def _gen_one(filename: str) -> None:
        try:
            result = await _call_tool("code_generator", {
                "filename":      filename,
                "system_prompt": system_prompt,
                "user_prompt": (
                    f"Generate the file: {filename}\n\n"
                    f"This file is part of a {stack_desc} project.\n"
                    f"Full project structure:\n"
                    + "\n".join(f"  - {f}" for f in file_plan)
                    + f"\n\nGenerate ONLY the content for {filename}. "
                    f"Ensure imports and references match the other files in this project."
                ),
            })
            await queue.put(("ok", filename, result["content"]))
        except Exception as exc:
            await queue.put(("error", filename, str(exc)))

    tasks     = [asyncio.create_task(_gen_one(f)) for f in file_plan]
    completed = 0

    while completed < len(file_plan):
        flag, filename, payload = await queue.get()
        completed += 1
        if flag == "ok":
            file_tree[filename] = payload
            await _push(state, "file", filename=filename,
                        size=len(payload), progress=f"{completed}/{len(file_plan)}")
            await _call_tool("project_write", {
                "project_id": state["project_id"],
                "fields":     {"file_tree": file_tree},
            })
        else:
            await _push(state, "log", message=f"❌ {filename} failed: {payload}")

    await asyncio.gather(*tasks, return_exceptions=True)

    state["file_tree"] = file_tree
    return state


# ═══════════════════════════════════════════════════════════════
# Node: quality_check
# ═══════════════════════════════════════════════════════════════

async def node_quality(state: ProjectState) -> ProjectState:
    await _push(state, "status", status="quality_check",
                message="🔎 Running quality checks...")

    try:
        result = await _call_tool("quality_checker", {
            "file_tree": state.get("file_tree", {}),
            "file_plan": state.get("file_plan", []),
        })
        issues = result.get("issues", [])
        state["issues"] = issues

        if issues:
            await _push(state, "issues", issues=issues)
            await _push(state, "log", message=f"⚠ {len(issues)} quality note(s)")
        else:
            await _push(state, "log", message="✅ Quality check passed")

        file_count      = result.get("file_count", len(state.get("file_tree", {})))
        state["status"] = "complete"

        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields": {
                "status":    "complete",
                "file_tree": state.get("file_tree", {}),
            },
        })
        await _push(state, "complete", file_count=file_count,
                    message=f"{file_count} files ready — review and confirm to push to GitHub")

    except Exception as exc:
        state["error"]  = str(exc)
        state["status"] = "failed"
        await _push(state, "error", message=f"Quality check failed: {exc}")

    return state

# ═══════════════════════════════════════════════════════════════
# Node: playwright_test (Daytona sandbox + self-heal)
# ═══════════════════════════════════════════════════════════════

async def node_playwright_test(state: ProjectState) -> ProjectState:
    """
    1. Spin up Daytona sandbox with generated files
    2. Wait for app to be live
    3. Run Playwright against the live URL
    4. If console errors → Codestral fixes → redeploy to same sandbox
    5. Save screenshot + preview URL to MongoDB
    6. Sandbox auto-destroys after 10 minutes
    """
    await _push(state, "status", status="testing",
                message="🚀 Spinning up sandbox environment...")

    project_id   = state["project_id"]
    file_tree    = state.get("file_tree", {})
    stack        = state.get("stack", {})
    spec         = state.get("spec", {})
    project_name = state.get("project_name", spec.get("app_name", "app"))

    # ── Step 1: Create Daytona sandbox ───────────────────────
    workspace_id = None
    preview_url  = None

    try:
        from app.services.daytona_service import DaytonaService
        daytona  = DaytonaService()
        sandbox  = await daytona.create_sandbox(
            project_id=project_id,
            project_name=project_name,
            file_tree=file_tree,
            stack=stack,
        )
        workspace_id = sandbox["workspace_id"]
        preview_url  = sandbox["preview_url"]

        await _push(state, "log", message=f"✅ Sandbox ready: {preview_url}")
        await _call_tool("project_write", {
            "project_id": project_id,
            "fields": {
                "preview_url":          preview_url,
                "daytona_workspace_id": workspace_id,
            },
        })

    except Exception as exc:
        await _push(state, "log",
                    message=f"⚠ Sandbox creation failed: {exc} — skipping preview")
        return state

    # ── Step 2: Playwright test + self-heal loop ──────────────
    MAX_HEAL_ATTEMPTS = 3
    heal_attempt      = 0
    current_file_tree = dict(file_tree)

    while heal_attempt <= MAX_HEAL_ATTEMPTS:
        label = f" (attempt {heal_attempt})" if heal_attempt else ""
        await _push(state, "status", status="testing",
                    message=f"🎭 Running browser test{label}...")

        # Run Playwright
        try:
            from app.services.preview_service import _run_playwright_on_preview
            pw_result = await _run_playwright_on_preview(
                preview_url, timeout_ms=20000
            )
        except Exception as exc:
            await _push(state, "log",
                        message=f"⚠ Playwright unavailable: {exc}")
            break

        if not pw_result.get("success"):
            await _push(state, "log",
                        message=f"⚠ Browser test skipped: {pw_result.get('reason', 'unknown')}")
            break

        console_errors = pw_result.get("console_errors", [])
        screenshot_b64 = pw_result.get("screenshot_b64")

        # Save screenshot + preview URL
        if screenshot_b64:
            await _call_tool("project_write", {
                "project_id": project_id,
                "fields": {
                    "preview_screenshot": screenshot_b64,
                    "preview_url":        preview_url,
                },
            })
            await _push(state, "preview_ready",
                        preview_url=preview_url,
                        message=f"📸 Live preview ready: {preview_url}")

        # No errors — done
        if not console_errors:
            await _push(state, "log",
                        message="✅ Browser test passed — no console errors")
            state["file_tree"]  = current_file_tree
            state["preview_url"] = preview_url
            break

        # Errors found
        heal_attempt += 1
        if heal_attempt > MAX_HEAL_ATTEMPTS:
            await _push(state, "log",
                        message=f"⚠ {len(console_errors)} errors remain after "
                                f"{MAX_HEAL_ATTEMPTS} fix attempts")
            await _push(state, "issues", issues=console_errors[:5])
            break

        await _push(state, "log",
                    message=f"🐛 {len(console_errors)} console errors — "
                            f"self-healing ({heal_attempt}/{MAX_HEAL_ATTEMPTS})...")

        # ── Self-heal: fix errors with Codestral ─────────────
        current_file_tree = await _heal_console_errors(
            state=state,
            file_tree=current_file_tree,
            console_errors=console_errors,
            project_id=project_id,
            stack=stack,
            spec=spec,
        )

        # ── Redeploy fixed files to same sandbox ──────────────
        try:
            await _push(state, "log", message="♻ Redeploying fixed files...")
            from app.services.daytona_service import (
                DaytonaService,
                _detect_start_command,
            )
            daytona   = DaytonaService()
            await daytona._upload_files(workspace_id, current_file_tree)
            start_cmd, _ = _detect_start_command(current_file_tree, stack)
            await daytona._exec_command(workspace_id, start_cmd)
            await asyncio.sleep(5)  # wait for restart

        except Exception as exc:
            await _push(state, "log", message=f"⚠ Redeploy failed: {exc}")
            break

    await _push(state, "log",
                message="⏱ Sandbox will auto-shutdown in 10 minutes")
    return state


# ═══════════════════════════════════════════════════════════════
# Self-heal helper
# ═══════════════════════════════════════════════════════════════

async def _heal_console_errors(
    state:          ProjectState,
    file_tree:      dict[str, str],
    console_errors: list[str],
    project_id:     str,
    stack:          dict,
    spec:           dict,
) -> dict[str, str]:
    """
    Ask Codestral which files caused the errors and fix them.
    Returns updated file_tree.
    """
    parts      = [v for k, v in stack.items()
                  if k in ("frontend", "backend", "database")
                  and v and v.lower() != "none"]
    stack_desc = " + ".join(parts) if parts else "HTML/CSS/JS"
    error_text = "\n".join(f"- {e}" for e in console_errors[:10])

    # Ask Codestral which files to fix
    try:
        plan_result = await _call_tool("code_generator", {
            "filename": "_heal_plan.json",
            "system_prompt": (
                "You are a debugger. Given browser console errors and a list of "
                "project files, identify which files are causing the errors.\n"
                "Return ONLY valid JSON: {\"files\": [\"path1\", \"path2\"], "
                "\"explanation\": \"brief reason\"}"
            ),
            "user_prompt": (
                f"Console errors:\n{error_text}\n\n"
                f"Project files: {list(file_tree.keys())}\n\n"
                f"Which files need to be fixed to resolve these errors?"
            ),
        })
        raw          = _strip_fences(plan_result.get("content", ""))
        heal_plan    = json.loads(raw)
        files_to_fix = heal_plan.get("files", [])
        explanation  = heal_plan.get("explanation", "")
        if explanation:
            await _push(state, "log", message=f"🔍 {explanation}")

    except Exception:
        files_to_fix = [
            f for f in file_tree
            if f.endswith((".html", ".js", ".css", ".jsx", ".ts", ".tsx", ".py"))
        ][:3]

    if not files_to_fix:
        return file_tree

    await _push(state, "log", message=f"🔧 Fixing: {', '.join(files_to_fix)}")

    updated_tree  = dict(file_tree)
    queue: asyncio.Queue = asyncio.Queue()

    async def _fix_one(filename: str) -> None:
        content = file_tree.get(filename, "")
        try:
            result = await _call_tool("code_generator", {
                "filename": filename,
                "system_prompt": (
                    f"You are an expert {stack_desc} developer and debugger.\n"
                    f"Fix the file to resolve the browser console errors.\n"
                    f"Output ONLY the complete fixed file content.\n"
                ),
                "user_prompt": (
                    f"File: {filename}\n\n"
                    f"Console errors to fix:\n{error_text}\n\n"
                    f"Current content:\n{content[:4000]}\n\n"
                    f"Return the complete fixed file."
                ),
            })
            await queue.put(("ok", filename, result["content"]))
        except Exception as exc:
            await queue.put(("error", filename, str(exc)))

    tasks     = [asyncio.create_task(_fix_one(f)) for f in files_to_fix]
    completed = 0

    while completed < len(files_to_fix):
        flag, filename, payload = await queue.get()
        completed += 1
        if flag == "ok" and len(payload.strip()) > 10:
            updated_tree[filename] = payload
            await _push(state, "file", filename=filename,
                        message=f"🔧 Fixed: {filename}")
        else:
            await _push(state, "log", message=f"❌ Could not fix: {filename}")

    await asyncio.gather(*tasks, return_exceptions=True)

    await _call_tool("project_write", {
        "project_id": project_id,
        "fields":     {"file_tree": updated_tree},
    })

    return updated_tree


# ═══════════════════════════════════════════════════════════════
# Node: github_push
# ═══════════════════════════════════════════════════════════════

async def node_github_push(state: ProjectState) -> ProjectState:
    await _push(state, "status", status="committing",
                message="📡 Pushing to GitHub...")

    try:
        import re
        project_name = state.get("project_name", "chiscode-project")
        repo_name    = re.sub(r"[^a-zA-Z0-9_.-]", "-", project_name.lower())
        repo_name    = re.sub(r"-+", "-", repo_name).strip("-")[:100]
        spec         = state.get("spec", {})

        await _push(state, "log",
                    message=f"📦 Creating repository: {repo_name}...")

        result = await _call_tool("github_push", {
            "github_token":   state["github_token"],
            "repo_name":      repo_name,
            "description":    spec.get("description", "")[:255],
            "file_tree":      state.get("file_tree", {}),
            "commit_message": state.get("commit_message",
                                        "Initial commit — generated by ChisCode"),
            "private":        False,
        })

        state["repo_url"]   = result["repo_url"]
        state["commit_sha"] = result["commit_sha"]
        state["status"]     = "complete"

        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields": {
                "status":           "complete",
                "github_repo_url":  result["repo_url"],
                "github_repo_name": repo_name,
                "github_owner":     result["owner"],
                "current_version":  1,
            },
        })

        await _push(state, "log",
                    message=f"✅ Repository: {result['repo_url']}")
        await _push(state, "github_done",
                    repo_url=result["repo_url"],
                    commit_sha=result["commit_sha"],
                    message="Code pushed to GitHub successfully!")

    except Exception as exc:
        state["error"]  = str(exc)
        state["status"] = "failed"
        await _push(state, "error", message=f"GitHub push failed: {exc}")
        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields": {"status": "failed", "error_message": str(exc)},
        })

    return state


# ═══════════════════════════════════════════════════════════════
# Node: iterate
# ═══════════════════════════════════════════════════════════════

async def node_iterate(state: ProjectState) -> ProjectState:
    """Plan + generate changed files, commit directly to main."""
    iterate_prompt = state.get("iterate_prompt", "")
    version        = state.get("version", 2)

    await _push(state, "status", status="generating",
                message="🔄 Planning changes...")

    # ── Load project doc ──────────────────────────────────────
    try:
        project = await _call_tool("project_read", {
            "project_id": state["project_id"]
        })
        if not state.get("github_owner"):
            state["github_owner"]     = project.get("github_owner", "")
        if not state.get("github_repo_name"):
            state["github_repo_name"] = project.get("github_repo_name", "")
        if not state.get("file_tree"):
            state["file_tree"]        = project.get("file_tree", {})
        if not state.get("spec"):
            state["spec"]             = project.get("spec", {})
        if not state.get("stack"):
            state["stack"]            = project.get("stack", {})
    except Exception as exc:
        await _push(state, "error", message=f"Failed to load project: {exc}")
        state["error"]  = str(exc)
        state["status"] = "failed"
        return state

    # ── Validate ──────────────────────────────────────────────
    if not state.get("github_token"):
        await _push(state, "error",
                    message="GitHub token not found — connect GitHub first")
        state["error"]  = "github_token missing"
        state["status"] = "failed"
        return state

    if not state.get("github_owner") or not state.get("github_repo_name"):
        await _push(state, "error",
                    message="No GitHub repo linked — push to GitHub first")
        state["error"]  = "github_owner or github_repo_name missing"
        state["status"] = "failed"
        return state

    # ── Plan files to change ──────────────────────────────────
    file_tree = state.get("file_tree", {})
    try:
        plan_result = await _call_tool("code_generator", {
            "filename": "_plan.json",
            "system_prompt": (
                "You are a senior software engineer planning a code change.\n"
                "When given a change request, think through it properly:\n"
                "- Some changes only need existing files modified\n"
                "- Some changes require brand new files to be created\n"
                "- Some changes need both modifications AND new files\n"
                "Always return the COMPLETE list of files needed.\n"
                "Return ONLY valid JSON: {\"files\": [\"path1\", \"path2\"]}"
            ),
            "user_prompt": (
                f"Current project files:\n"
                f"{chr(10).join(f'  - {f}' for f in file_tree.keys())}\n\n"
                f"Change request: {iterate_prompt}\n\n"
                f"List every file that needs to be modified or created."
            ),
        })
        raw             = _strip_fences(plan_result.get("content", ""))
        files_to_change = json.loads(raw).get("files", list(file_tree.keys())[:3])
    except Exception as e:
        logger.warning("file plan parse failed", error=str(e))
        files_to_change = list(file_tree.keys())[:3]

    await _push(state, "log",
                message=f"📋 Files to update: {', '.join(files_to_change)}")

    stack      = state.get("stack", {})
    parts      = [v for k, v in stack.items()
                  if k in ("frontend", "backend", "database")
                  and v and v.lower() != "none"]
    stack_desc = " + ".join(parts) if parts else "HTML/CSS/JS"

    changed_files: dict[str, str] = {}
    queue: asyncio.Queue = asyncio.Queue()

    async def _patch_one(filename: str) -> None:
        existing = file_tree.get(filename, "")
        is_new   = filename not in file_tree
        try:
            result = await _call_tool("code_generator", {
                "filename":      filename,
                "system_prompt": (
                    f"You are an expert {stack_desc} developer.\n"
                    f"{'Create this new file' if is_new else 'Modify this existing file'} "
                    f"according to the change request.\n"
                    f"Output ONLY the complete file content.\n"
                ),
                "user_prompt": (
                    f"File: {filename}\n"
                    f"Change request: {iterate_prompt}\n\n"
                    + (f"Current content:\n{existing[:3000]}\n\n" if not is_new else "")
                    + f"Return the complete {'new' if is_new else 'updated'} file."
                ),
            })
            await queue.put(("ok", filename, result["content"]))
        except Exception as exc:
            await queue.put(("error", filename, str(exc)))

    tasks     = [asyncio.create_task(_patch_one(f)) for f in files_to_change]
    completed = 0
    while completed < len(files_to_change):
        flag, filename, payload = await queue.get()
        completed += 1
        if flag == "ok" and len(payload.strip()) > 10:
            changed_files[filename] = payload
            file_tree[filename]     = payload
            await _push(state, "file", filename=filename, size=len(payload))
        else:
            await _push(state, "log", message=f"❌ {filename} failed")

    await asyncio.gather(*tasks, return_exceptions=True)

    if not changed_files:
        state["error"]  = "No files were updated"
        state["status"] = "failed"
        await _push(state, "error", message="No files were updated")
        return state

    # ── Quality check ─────────────────────────────────────────
    qc = await _call_tool("quality_checker", {
        "file_tree": changed_files,
        "file_plan": list(changed_files.keys()),
    })
    if qc.get("issues"):
        await _push(state, "issues", issues=qc["issues"])

    # ── Commit to main ────────────────────────────────────────
    await _push(state, "status", status="committing",
                message="⬆ Committing changes to main...")
    try:
        from app.services.github_service import GitHubService
        gh = GitHubService(state["github_token"])
        await gh.push_files(
            owner=state["github_owner"],
            repo=state["github_repo_name"],
            file_tree=file_tree,
            commit_message=f"feat: {iterate_prompt[:72]}",
            branch="main",
        )

        state["file_tree"] = file_tree
        state["status"]    = "complete"

        await _call_tool("project_write", {
            "project_id": state["project_id"],
            "fields": {
                "status":          "complete",
                "file_tree":       file_tree,
                "current_version": version,
            },
        })

        repo_url = (
            f"https://github.com/{state['github_owner']}"
            f"/{state['github_repo_name']}"
        )
        await _push(state, "github_done",
                    commit_sha="",
                    repo_url=repo_url,
                    message=f"✅ {len(file_tree)} files committed to main "
                            f"({len(changed_files)} updated)")

    except Exception as exc:
        state["error"] = str(exc)
        await _push(state, "error", message=f"Commit failed: {exc}")

    return state


# ═══════════════════════════════════════════════════════════════
# Graph builders
# ═══════════════════════════════════════════════════════════════

def _route_after_analyze(state: ProjectState) -> Literal["end"]:
    """Always pause after analyze for HITL stack selection."""
    return "end"


def _route_after_generate(state: ProjectState) -> Literal["quality", "end"]:
    return "end" if state.get("error") else "quality"


def _route_after_quality(state: ProjectState) -> Literal["playwright_test", "end"]:
    """After quality check, run Playwright test if no errors."""
    return "end" if state.get("error") else "playwright_test"


def build_analyze_graph() -> StateGraph:
    """Phase 1: analyze prompt → suggest stacks. Pauses for HITL."""
    g = StateGraph(ProjectState)
    g.add_node("analyze", node_analyze)
    g.add_edge(START,     "analyze")
    g.add_edge("analyze", END)
    return g.compile()


def build_generate_graph() -> StateGraph:
    """Phase 2: scaffold → generate → quality → playwright test."""
    g = StateGraph(ProjectState)
    g.add_node("scaffold",        node_scaffold)
    g.add_node("generate",        node_generate)
    g.add_node("quality",         node_quality)
    g.add_node("playwright_test", node_playwright_test)  # ← new

    g.add_edge(START,      "scaffold")
    g.add_edge("scaffold", "generate")
    g.add_conditional_edges("generate", _route_after_generate,
                            {"quality": "quality", "end": END})
    g.add_conditional_edges("quality", _route_after_quality,
                            {"playwright_test": "playwright_test", "end": END})
    g.add_edge("playwright_test", END)
    return g.compile()


def build_github_graph() -> StateGraph:
    """Phase 3: push files to GitHub."""
    g = StateGraph(ProjectState)
    g.add_node("github_push", node_github_push)
    g.add_edge(START,         "github_push")
    g.add_edge("github_push", END)
    return g.compile()


def build_iterate_graph() -> StateGraph:
    """Iterate: plan → patch → quality → commit to main."""
    g = StateGraph(ProjectState)
    g.add_node("iterate", node_iterate)
    g.add_edge(START,     "iterate")
    g.add_edge("iterate", END)
    return g.compile()


# ═══════════════════════════════════════════════════════════════
# Public SSE runner
# ═══════════════════════════════════════════════════════════════

async def run_workflow(
    graph:         StateGraph,
    initial_state: ProjectState,
) -> AsyncGenerator[str, None]:
    """
    Run a LangGraph workflow, yielding SSE strings as events arrive.
    Injects a queue into state so nodes can push events mid-execution.
    """
    queue: asyncio.Queue = asyncio.Queue()
    initial_state["_sse_queue"] = queue

    async def _run():
        try:
            await graph.ainvoke(initial_state)
        except Exception as exc:
            await queue.put(_sse("error", message=f"Workflow error: {exc}"))
        finally:
            await queue.put(None)  # sentinel

    task = asyncio.create_task(_run())

    while True:
        item = await queue.get()
        if item is None:
            break
        yield item

    await task