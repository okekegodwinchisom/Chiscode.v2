"""
ChisCode — Core AI Generation Agent
=====================================
LangGraph state machine that turns a natural language prompt into a
production-ready file tree using Codestral (Mistral's code model).

Pipeline:
  analyze → plan → generate → validate → self_heal? → complete

Each node:
  1. Updates project status in MongoDB
  2. Streams a log message over the project's WebSocket
  3. Either advances the graph or retries via self-heal loop

Self-healing:
  If generated code fails validation (syntax / structural checks),
  the agent sends the error back to Codestral and asks it to fix the
  specific file. Max MAX_HEAL_ATTEMPTS retries before marking failed.

Streaming:
  ws_broadcast() is called after every meaningful step so the dashboard
  shows live progress without polling.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

import httpx
from bson import ObjectId
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mistralai import ChatMistralAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import projects_collection

logger = get_logger(__name__)

MAX_HEAL_ATTEMPTS = 3


# ── State ────────────────────────────────────────────────────────

class AgentState(TypedDict):
    project_id:     str
    user_id:        str
    prompt:         str
    project_name:   str
    preferred_stack: dict[str, Any]

    # Filled in as the graph runs
    spec:           dict[str, Any]          # structured requirements
    file_plan:      list[str]               # filenames to generate
    file_tree:      dict[str, str]          # path → content
    current_file:   str                     # file being generated/healed
    heal_attempts:  int
    error:          str                     # last validation error
    status:         str                     # mirrors ProjectStatus

    messages:       Annotated[list, add_messages]


# ── Codestral client ─────────────────────────────────────────────

def _get_llm(temperature: float = 0.15) -> ChatMistralAI:
    """Return a ChatMistralAI client pointed at the Codestral endpoint."""
    return ChatMistralAI(
        model=settings.codestral_model,
        api_key=settings.codestral_api_key,
        base_url=settings.codestral_base_url,
        temperature=temperature,
        max_tokens=8192,
    )


# ── MongoDB helpers ──────────────────────────────────────────────

async def _update_project(project_id: str, fields: dict) -> None:
    fields["updated_at"] = datetime.now(tz=timezone.utc)
    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {"$set": fields},
    )


async def _append_log(project_id: str, message: str) -> None:
    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {
            "$push": {"generation_log": message},
            "$set":  {"updated_at": datetime.now(tz=timezone.utc)},
        },
    )


# ── WebSocket broadcast (imported lazily to avoid circular import) ──

async def _ws(project_id: str, msg_type: str, **kwargs) -> None:
    """Send a WebSocket message to all clients watching this project."""
    try:
        from app.api.v1.projects import ws_broadcast
        await ws_broadcast(project_id, {"type": msg_type, **kwargs})
    except Exception as exc:
        logger.warning("ws_broadcast failed", error=str(exc))


# ── Node: analyze ────────────────────────────────────────────────

async def node_analyze(state: AgentState) -> dict:
    """
    Parse the natural language prompt into a structured spec (JSON).
    Uses Codestral with a strict JSON-only system prompt.
    """
    pid = state["project_id"]
    await _update_project(pid, {"status": "analyzing"})
    await _append_log(pid, "🔍 Analyzing requirements...")
    await _ws(pid, "status", status="analyzing", message="Analyzing your requirements...")

    llm = _get_llm(temperature=0.1)

    system = SystemMessage(content="""You are a senior software architect.
Analyze the user's app idea and return ONLY a valid JSON object — no markdown, no explanation.

JSON shape:
{
  "app_type": "web_app | api | cli | mobile_web",
  "app_name": "snake_case_name",
  "description": "one sentence",
  "features": ["feature1", "feature2"],
  "auth_required": true | false,
  "database_needed": true | false,
  "api_needed": true | false,
  "mobile_responsive": true | false,
  "complexity": "simple | moderate | complex",
  "stack": {
    "frontend": "e.g. HTML/CSS/JS or React",
    "backend": "e.g. FastAPI or Express",
    "database": "e.g. MongoDB or SQLite"
  },
  "file_plan": ["list", "of", "filenames", "to", "generate"]
}

file_plan rules:
- Include ALL files needed for a working app
- simple: 3-6 files, moderate: 7-14 files, complex: 15-25 files
- Always include README.md
- Use realistic relative paths e.g. "src/main.py", "frontend/index.html"
""")

    human = HumanMessage(content=f"App idea: {state['prompt']}")

    try:
        response = await llm.ainvoke([system, human])
        raw = response.content.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        spec = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Spec analysis failed", error=str(exc))
        spec = {
            "app_type": "web_app",
            "app_name": state["project_name"].replace(" ", "_").lower(),
            "description": state["prompt"][:200],
            "features": [],
            "auth_required": False,
            "database_needed": False,
            "api_needed": False,
            "mobile_responsive": True,
            "complexity": "simple",
            "stack": {"frontend": "HTML/CSS/JS", "backend": "", "database": ""},
            "file_plan": ["index.html", "style.css", "app.js", "README.md"],
        }

    file_plan = spec.pop("file_plan", ["index.html", "README.md"])
    stack     = spec.pop("stack", {})

    await _update_project(pid, {"spec": spec, "stack": stack, "status": "analyzing"})
    await _append_log(pid, f"✅ Spec ready — {len(file_plan)} files to generate")
    await _ws(pid, "log", message=f"Spec ready: {spec.get('app_name')} ({spec.get('complexity')} complexity)")

    return {
        "spec":       spec,
        "file_plan":  file_plan,
        "file_tree":  {},
        "heal_attempts": 0,
        "status":     "analyzing",
        "messages":   [system, human, response],
    }


# ── Node: generate ───────────────────────────────────────────────

async def node_generate(state: AgentState) -> dict:
    """
    Generate each file in file_plan sequentially using Codestral.
    Streams a log line per file as it's created.
    """
    pid       = state["project_id"]
    file_plan = state["file_plan"]
    spec      = state["spec"]
    file_tree = dict(state.get("file_tree", {}))

    await _update_project(pid, {"status": "generating"})
    await _ws(pid, "status", status="generating", message="Generating code...")

    llm = _get_llm(temperature=0.2)

    stack_desc = ""
    if state.get("preferred_stack"):
        s = state["preferred_stack"]
        stack_desc = f"Use this stack: frontend={s.get('frontend','')}, backend={s.get('backend','')}, database={s.get('database','')}."

    system_prompt = f"""You are an expert full-stack developer.
Generate production-quality code for the file specified.
App: {spec.get('app_name')} — {spec.get('description')}
Features: {', '.join(spec.get('features', []))}
{stack_desc}

Rules:
- Output ONLY the raw file content — no markdown fences, no explanation
- Write complete, working code — no placeholders or TODOs
- Use modern best practices for the language/framework
- Include helpful comments
- If generating HTML, make it responsive and styled
"""

    files_done = list(file_tree.keys())

    for filename in file_plan:
        if filename in file_tree:
            continue  # already generated (e.g. after self-heal partial completion)

        await _append_log(pid, f"📝 Generating {filename}...")
        await _ws(pid, "log", message=f"Generating {filename}...")

        context = ""
        if files_done:
            # Show the last 2 generated files as context
            for prev in files_done[-2:]:
                snippet = file_tree[prev][:300]
                context += f"\n\n# Already generated: {prev}\n{snippet}..."

        human_msg = HumanMessage(
            content=f"Generate the file: {filename}\n\nAlready created:{context if context else ' (this is the first file)'}"
        )

        try:
            response = await llm.ainvoke([
                SystemMessage(content=system_prompt),
                human_msg,
            ])
            content = response.content.strip()
            # Strip any accidental markdown fences
            content = re.sub(r"^```\w*\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
            file_tree[filename] = content
            files_done.append(filename)

            # Persist partial progress after each file
            await _update_project(pid, {"file_tree": file_tree})
            await _append_log(pid, f"✅ {filename} ({len(content)} chars)")
            await _ws(pid, "file_done", filename=filename, size=len(content))

        except Exception as exc:
            logger.error("File generation failed", filename=filename, error=str(exc))
            await _append_log(pid, f"❌ Failed to generate {filename}: {exc}")
            # Continue with other files — self-heal will catch missing ones

    await _ws(pid, "status", status="quality_check", message="Running quality checks...")
    return {"file_tree": file_tree, "status": "quality_check"}


# ── Node: validate ───────────────────────────────────────────────

async def node_validate(state: AgentState) -> dict:
    """
    Run structural validation on generated files.
    Checks Python syntax, JS basic structure, HTML completeness.
    Returns error + current_file if something needs healing.
    """
    pid       = state["project_id"]
    file_tree = state["file_tree"]
    file_plan = state["file_plan"]

    await _update_project(pid, {"status": "quality_check"})
    await _ws(pid, "status", status="quality_check", message="Validating code quality...")

    errors: list[str] = []

    # 1. Check all planned files were generated
    missing = [f for f in file_plan if f not in file_tree]
    if missing:
        errors.append(f"Missing files: {', '.join(missing[:3])}")

    # 2. Python syntax check
    for path, content in file_tree.items():
        if path.endswith(".py") and content:
            try:
                ast.parse(content)
            except SyntaxError as e:
                errors.append(f"Python syntax error in {path} line {e.lineno}: {e.msg}")
                await _append_log(pid, f"⚠️ Syntax error in {path}: {e.msg}")

    # 3. HTML completeness check
    for path, content in file_tree.items():
        if path.endswith(".html") and content:
            if "<html" not in content.lower() and "<!doctype" not in content.lower():
                errors.append(f"Incomplete HTML in {path}: missing html tag")

    # 4. Empty files check
    empty = [p for p, c in file_tree.items() if not c or len(c.strip()) < 10]
    if empty:
        errors.append(f"Empty/minimal files: {', '.join(empty[:3])}")

    if errors:
        # Pick the first problematic file to heal
        first_error = errors[0]
        problem_file = next(
            (p for p in file_tree if any(p in e for e in errors)),
            list(file_tree.keys())[0] if file_tree else "",
        )
        await _append_log(pid, f"⚠️ Validation found {len(errors)} issue(s)")
        return {
            "status":       "self_healing",
            "error":        first_error,
            "current_file": problem_file,
        }

    await _append_log(pid, "✅ All files validated successfully")
    await _ws(pid, "log", message="All files validated ✓")
    return {"status": "complete", "error": "", "current_file": ""}


# ── Node: self_heal ───────────────────────────────────────────────

async def node_self_heal(state: AgentState) -> dict:
    """
    Fix a specific file that failed validation.
    Sends the original content + error back to Codestral for correction.
    """
    pid          = state["project_id"]
    error        = state["error"]
    target_file  = state["current_file"]
    file_tree    = dict(state["file_tree"])
    attempts     = state["heal_attempts"] + 1

    await _update_project(pid, {"status": "self_healing", "self_heal_attempts": attempts})
    await _append_log(pid, f"🔧 Self-healing {target_file} (attempt {attempts}/{MAX_HEAL_ATTEMPTS})...")
    await _ws(pid, "status", status="self_healing",
              message=f"Self-healing {target_file} (attempt {attempts})...")

    llm = _get_llm(temperature=0.1)

    broken_content = file_tree.get(target_file, "# File was empty or missing")

    response = await llm.ainvoke([
        SystemMessage(content="""You are a code debugger. Fix the provided file.
Output ONLY the corrected file content — no markdown, no explanation."""),
        HumanMessage(content=f"""File: {target_file}
Error: {error}

Current content:
{broken_content[:3000]}

Return the fully corrected file."""),
    ])

    fixed = response.content.strip()
    fixed = re.sub(r"^```\w*\s*|\s*```$", "", fixed, flags=re.MULTILINE).strip()

    if fixed:
        file_tree[target_file] = fixed
        await _update_project(pid, {"file_tree": file_tree})
        await _append_log(pid, f"✅ {target_file} healed")
        await _ws(pid, "log", message=f"{target_file} fixed ✓")

    return {
        "file_tree":    file_tree,
        "heal_attempts": attempts,
        "status":       "quality_check",
    }


# ── Node: complete ───────────────────────────────────────────────

async def node_complete(state: AgentState) -> dict:
    """Mark the project as complete and notify the frontend."""
    pid = state["project_id"]
    await _update_project(pid, {
        "status":    "awaiting_confirmation",
        "file_tree": state["file_tree"],
    })
    await _append_log(pid, f"🚀 Generation complete — {len(state['file_tree'])} files ready")
    await _ws(pid, "complete",
              status="awaiting_confirmation",
              message="Generation complete! Review your project.",
              file_count=len(state["file_tree"]))
    return {"status": "awaiting_confirmation"}


# ── Node: fail ───────────────────────────────────────────────────

async def node_fail(state: AgentState) -> dict:
    """Mark the project as failed after exhausting heal attempts."""
    pid = state["project_id"]
    msg = f"Failed after {state['heal_attempts']} self-heal attempts: {state['error']}"
    await _update_project(pid, {"status": "failed", "error_message": msg})
    await _append_log(pid, f"❌ {msg}")
    await _ws(pid, "error", status="failed", message=msg)
    return {"status": "failed"}


# ── Routing ──────────────────────────────────────────────────────

def route_after_validate(state: AgentState) -> str:
    if state["status"] == "complete":
        return "complete"
    if state["heal_attempts"] >= MAX_HEAL_ATTEMPTS:
        return "fail"
    return "self_heal"


def route_after_heal(state: AgentState) -> str:
    # Always re-validate after a heal attempt
    return "validate"


# ── Graph assembly ───────────────────────────────────────────────

def build_generation_graph() -> Any:
    graph = StateGraph(AgentState)

    graph.add_node("analyze",   node_analyze)
    graph.add_node("generate",  node_generate)
    graph.add_node("validate",  node_validate)
    graph.add_node("self_heal", node_self_heal)
    graph.add_node("complete",  node_complete)
    graph.add_node("fail",      node_fail)

    graph.set_entry_point("analyze")

    graph.add_edge("analyze",  "generate")
    graph.add_edge("generate", "validate")

    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {"complete": "complete", "self_heal": "self_heal", "fail": "fail"},
    )

    graph.add_conditional_edges(
        "self_heal",
        route_after_heal,
        {"validate": "validate"},
    )

    graph.add_edge("complete", END)
    graph.add_edge("fail",     END)

    return graph.compile()


# ── Public entry point ───────────────────────────────────────────

_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_generation_graph()
    return _compiled_graph


async def run_generation_agent(
    project_id:     str,
    user_id:        str,
    prompt:         str,
    project_name:   str,
    preferred_stack: dict | None = None,
) -> None:
    """
    Entry point called by the FastAPI background task.
    Runs the full LangGraph pipeline for a project.
    """
    logger.info("Generation agent starting", project_id=project_id)

    initial_state: AgentState = {
        "project_id":     project_id,
        "user_id":        user_id,
        "prompt":         prompt,
        "project_name":   project_name,
        "preferred_stack": preferred_stack or {},
        "spec":           {},
        "file_plan":      [],
        "file_tree":      {},
        "current_file":   "",
        "heal_attempts":  0,
        "error":          "",
        "status":         "pending",
        "messages":       [],
    }

    try:
        graph = get_graph()
        await graph.ainvoke(initial_state)
        logger.info("Generation agent complete", project_id=project_id)
    except Exception as exc:
        logger.error("Generation agent crashed", project_id=project_id, error=str(exc))
        await _update_project(project_id, {
            "status":        "failed",
            "error_message": f"Agent crashed: {exc}",
        })
        await _ws(project_id, "error", status="failed", message=f"Agent error: {exc}")
        