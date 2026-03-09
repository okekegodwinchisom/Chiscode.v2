"""
ChisCode — Generation Agent (SSE streaming, no background task)
===============================================================
Runs synchronously inside a FastAPI StreamingResponse so the browser
receives Server-Sent Events in real time — no WebSocket, no polling,
no duplicate log lines.

Pipeline:  analyze → generate (file by file) → quality_check → complete

Quality check (replaces self-heal):
  - All planned files were generated
  - No file is empty (< 20 chars)
  - Python files parse without SyntaxError
  - HTML files contain a valid document root
  Reports issues in the log but always marks complete — the user can
  iterate if something looks wrong.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from typing import AsyncGenerator, Any

from bson import ObjectId
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mistralai import ChatMistralAI

from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import projects_collection

logger = get_logger(__name__)


# ── Codestral LLM ─────────────────────────────────────────────────

def _llm(temperature: float = 0.15) -> ChatMistralAI:
    return ChatMistralAI(
        model=settings.codestral_model,
        api_key=settings.codestral_api_key,
        base_url=settings.codestral_base_url,
        temperature=temperature,
        max_tokens=8192,
    )


# ── MongoDB helpers ───────────────────────────────────────────────

async def _set(project_id: str, fields: dict) -> None:
    fields["updated_at"] = datetime.now(tz=timezone.utc)
    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {"$set": fields},
    )


async def _log(project_id: str, message: str) -> None:
    """Append one line to generation_log in MongoDB."""
    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {
            "$push": {"generation_log": message},
            "$set":  {"updated_at": datetime.now(tz=timezone.utc)},
        },
    )


# ── SSE helper ────────────────────────────────────────────────────

def _sse(event: str, **data) -> str:
    """Format a Server-Sent Event string."""
    return f"data: {json.dumps({'event': event, **data})}\n\n"


# ── Main generator ────────────────────────────────────────────────

async def generate_project_stream(
    project_id:      str,
    user_id:         str,
    prompt:          str,
    project_name:    str,
    preferred_stack: dict | None = None,
) -> AsyncGenerator[str, None]:
    """
    AsyncGenerator that yields SSE strings and drives the full
    generation pipeline. Consumed by a FastAPI StreamingResponse.

    SSE event types:
      log      — plain progress message
      status   — project status changed  {status, message}
      file     — one file generated      {filename, size}
      issues   — quality check warnings  {issues: [...]}
      complete — done                    {file_count}
      error    — fatal error             {message}
    """

    # ── 1. Analyze ────────────────────────────────────────────
    await _set(project_id, {"status": "analyzing"})
    await _log(project_id, "🔍 Analyzing requirements...")
    yield _sse("status", status="analyzing", message="Analyzing your requirements...")

    try:
        spec, file_plan, stack = await _analyze(prompt, project_name, preferred_stack)
    except Exception as exc:
        msg = f"Analysis failed: {exc}"
        await _set(project_id, {"status": "failed", "error_message": msg})
        await _log(project_id, f"❌ {msg}")
        yield _sse("error", message=msg)
        return

    await _set(project_id, {"spec": spec, "stack": stack, "status": "generating"})
    await _log(project_id, f"✅ Spec ready — {len(file_plan)} files planned")
    yield _sse("log", message=f"Spec: {spec.get('app_name')} ({spec.get('complexity')}, {len(file_plan)} files)")
    yield _sse("status", status="generating", message="Generating files...")

    # ── 2. Generate files ─────────────────────────────────────
    file_tree: dict[str, str] = {}

    stack_hint = ""
    if preferred_stack:
        parts = [v for v in preferred_stack.values() if v]
        if parts:
            stack_hint = f"Stack: {', '.join(parts)}."

    system_prompt = (
        f"You are an expert full-stack developer.\n"
        f"App: {spec.get('app_name')} — {spec.get('description')}\n"
        f"Features: {', '.join(spec.get('features', []))}\n"
        f"{stack_hint}\n\n"
        f"Rules:\n"
        f"- Output ONLY raw file content — no markdown fences, no explanation\n"
        f"- Write complete, production-ready code — no placeholders, no TODOs\n"
        f"- Use modern best practices\n"
        f"- Add concise inline comments where helpful\n"
        f"- HTML must be fully structured with <!DOCTYPE>, <head>, <body>\n"
        f"- Python must be syntactically valid\n"
    )

    for filename in file_plan:
        await _log(project_id, f"📝 {filename}...")
        yield _sse("log", message=f"📝 Generating {filename}...")

        context_snippets = ""
        for prev_path in list(file_tree.keys())[-2:]:
            snippet = file_tree[prev_path][:400]
            context_snippets += f"\n\n# {prev_path} (excerpt):\n{snippet}"

        try:
            response = await _llm(temperature=0.2).ainvoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=(
                    f"Generate file: {filename}"
                    + (f"\n\nContext:{context_snippets}" if context_snippets else "")
                )),
            ])
            content = _strip_fences(response.content)

            if not content or len(content.strip()) < 10:
                content = f"# {filename}\n# TODO: generation returned empty\n"
                await _log(project_id, f"⚠️ {filename} empty — placeholder inserted")
                yield _sse("log", message=f"⚠️ {filename} empty")
            else:
                file_tree[filename] = content
                await _set(project_id, {"file_tree": file_tree})
                await _log(project_id, f"✅ {filename} ({len(content)} chars)")
                yield _sse("file", filename=filename, size=len(content))

        except Exception as exc:
            logger.error("File generation error", filename=filename, error=str(exc))
            await _log(project_id, f"❌ {filename} failed: {exc}")
            yield _sse("log", message=f"❌ {filename} failed")

    # ── 3. Quality check ──────────────────────────────────────
    await _set(project_id, {"status": "quality_check"})
    yield _sse("status", status="quality_check", message="Running quality checks...")

    issues = _quality_check(file_tree, file_plan)

    if issues:
        for issue in issues:
            await _log(project_id, f"⚠️ {issue}")
        yield _sse("issues", issues=issues)
        yield _sse("log", message=f"⚠️ {len(issues)} issue(s) noted — you can iterate to fix")
    else:
        await _log(project_id, "✅ Quality check passed")
        yield _sse("log", message="✅ Quality check passed")

    # ── 4. Complete ───────────────────────────────────────────
    file_count = len(file_tree)
    await _set(project_id, {
        "status":    "awaiting_confirmation",
        "file_tree": file_tree,
    })
    await _log(project_id, f"🚀 Done — {file_count} files ready")
    yield _sse("complete", file_count=file_count, message=f"{file_count} files ready")


# ── Analyze helper ────────────────────────────────────────────────

async def _analyze(
    prompt:          str,
    project_name:    str,
    preferred_stack: dict | None,
) -> tuple[dict, list[str], dict]:

    response = await _llm(temperature=0.1).ainvoke([
        SystemMessage(content="""You are a senior software architect.
Return ONLY a valid JSON object — no markdown, no explanation.

{
  "app_type": "web_app | api | cli | mobile_web",
  "app_name": "snake_case_name",
  "description": "one sentence",
  "features": ["feature1", "feature2"],
  "auth_required": true,
  "database_needed": true,
  "api_needed": true,
  "mobile_responsive": true,
  "complexity": "simple | moderate | complex",
  "stack": {
    "frontend": "HTML/CSS/JS",
    "backend": "FastAPI",
    "database": "SQLite"
  },
  "file_plan": ["src/main.py", "frontend/index.html", "README.md"]
}

file_plan: ALL files for a working deployable app.
simple=4-6 files, moderate=7-14, complex=15-25.
Always include README.md. Use realistic relative paths. No binary files."""),
        HumanMessage(content=f"App idea: {prompt}"),
    ])

    raw = _strip_fences(response.content)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {
            "app_type": "web_app",
            "app_name": re.sub(r"\s+", "_", project_name).lower(),
            "description": prompt[:150],
            "features": [],
            "auth_required": False,
            "database_needed": False,
            "api_needed": False,
            "mobile_responsive": True,
            "complexity": "simple",
            "stack": {"frontend": "HTML/CSS/JS", "backend": "", "database": ""},
            "file_plan": ["index.html", "style.css", "app.js", "README.md"],
        }

    file_plan = data.pop("file_plan", ["index.html", "README.md"])
    stack     = data.pop("stack", {})

    if preferred_stack:
        for k, v in preferred_stack.items():
            if v:
                stack[k] = v

    return data, file_plan, stack


# ── Quality check ─────────────────────────────────────────────────

def _quality_check(file_tree: dict[str, str], file_plan: list[str]) -> list[str]:
    issues = []

    missing = [f for f in file_plan if f not in file_tree]
    if missing:
        issues.append(f"Missing: {', '.join(missing)}")

    for path, content in file_tree.items():
        if not content or len(content.strip()) < 20:
            issues.append(f"{path} is empty")
        elif "# TODO: generation returned empty" in content:
            issues.append(f"{path} is a placeholder")

    for path, content in file_tree.items():
        if path.endswith(".py") and content:
            try:
                ast.parse(content)
            except SyntaxError as e:
                issues.append(f"Syntax error in {path} line {e.lineno}: {e.msg}")

    for path, content in file_tree.items():
        if path.endswith(".html") and content:
            low = content.lower()
            if "<!doctype" not in low and "<html" not in low:
                issues.append(f"{path} missing DOCTYPE/html root")

    if not any(p.lower() == "readme.md" for p in file_tree):
        issues.append("README.md missing")

    return issues


# ── Strip markdown fences ─────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json|python|javascript|html|css|bash|yaml|sql|\w*)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ── Legacy entry point (kept for import compatibility) ────────────

async def run_generation_agent(
    project_id:      str,
    user_id:         str,
    prompt:          str,
    project_name:    str,
    preferred_stack: dict | None = None,
) -> None:
    """Drains the SSE generator without streaming — used if called as background task."""
    async for _ in generate_project_stream(
        project_id, user_id, prompt, project_name, preferred_stack
    ):
        pass
