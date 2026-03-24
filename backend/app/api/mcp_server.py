"""
ChisCode — MCP Tool Server
===========================
Exposes all agent capabilities as MCP-compatible JSON-RPC tools.
Mounted at /api/mcp on the main FastAPI app.

Every tool follows the same contract:
  POST /api/mcp/tools/{tool_name}
  Body:  { "params": { ... } }
  Reply: { "result": { ... }, "error": null }
         { "result": null,   "error": "message" }

The orchestrator and all agents call these endpoints — they never
import each other directly. This means:
  - Tools are independently testable
  - New tools can be added without touching agents
  - Agents can run on separate workers in future
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/mcp", tags=["mcp"])


# ── Tool envelope ─────────────────────────────────────────────────

class ToolRequest(BaseModel):
    params: dict[str, Any] = {}


class ToolResponse(BaseModel):
    result: Any = None
    error:  str | None = None


def ok(data: Any) -> ToolResponse:
    return ToolResponse(result=data)


def err(msg: str) -> ToolResponse:
    logger.warning("MCP tool error", error=msg)
    return ToolResponse(error=msg)


# ── Tool registry ─────────────────────────────────────────────────

TOOL_REGISTRY = {
    "code_generator": {
        "description": "Generate a single source file using Codestral",
        "params": ["filename", "system_prompt", "user_prompt"],
    },
    "stack_advisor": {
        "description": "Suggest 3 ranked tech stacks for an app description",
        "params": ["prompt", "app_type", "complexity", "features"],
    },
    "quality_checker": {
        "description": "Lint and validate a file_tree dict",
        "params": ["file_tree", "file_plan"],
    },
    "file_scaffold": {
        "description": "Generate a complete file plan list from a stack + spec using LLM",
        "params": ["spec", "stack"],
    },
    "github_push": {
        "description": "Create GitHub repo and push all files",
        "params": ["github_token", "repo_name", "description", "file_tree",
                   "commit_message", "private"],
    },
    "github_pr": {
        "description": "Push changed files to a branch and open a PR",
        "params": ["github_token", "owner", "repo", "branch_name", "file_tree",
                   "commit_message", "pr_title", "pr_body"],
    },
    "project_read": {
        "description": "Read a project document from MongoDB",
        "params": ["project_id"],
    },
    "project_write": {
        "description": "Update fields on a project document",
        "params": ["project_id", "fields"],
    },
    "project_log": {
        "description": "Append a log line to a project document",
        "params": ["project_id", "message"],
    },
    "search_web": {
        "description": "DuckDuckGo search — returns top snippets",
        "params": ["query", "max_results"],
    },
    "analyze_prompt": {
        "description": "Parse a natural language app description into a structured spec",
        "params": ["prompt", "project_name"],
    },
    "daytona_sandbox": {
        "description": "Create a Daytona sandbox and return a live preview URL",
        "params": ["project_id", "project_name", "file_tree", "stack"],
    },
}


@router.get("/tools")
async def list_tools():
    """Discover all registered MCP tools."""
    return {"tools": TOOL_REGISTRY}


# ═══════════════════════════════════════════════════════════════
# TOOL: code_generator
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/code_generator", response_model=ToolResponse)
async def tool_code_generator(req: ToolRequest):
    """
    Generate a single file via Codestral.
    Returns { content: str, filename: str, chars: int }
    """
    p             = req.params
    filename      = p.get("filename", "")
    system_prompt = p.get("system_prompt", "You are an expert developer.")
    user_prompt   = p.get("user_prompt", f"Generate file: {filename}")

    if not filename:
        return err("filename is required")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_mistralai import ChatMistralAI

        llm = ChatMistralAI(
            model=settings.codestral_model,
            api_key=settings.codestral_api_key,
            base_url=settings.codestral_base_url,
            temperature=p.get("temperature", 0.2),
            max_tokens=16384,  # ← bumped from 8192
        )
        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
        content = _strip_fences(response.content)
        return ok({"content": content, "filename": filename, "chars": len(content)})

    except Exception as exc:
        return err(f"code_generator failed for {filename}: {exc}")


# ═══════════════════════════════════════════════════════════════
# TOOL: stack_advisor
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/stack_advisor", response_model=ToolResponse)
async def tool_stack_advisor(req: ToolRequest):
    """Suggest 3 tech stacks. Returns { options: [...] }"""
    p = req.params
    try:
        from app.agents.stack_advisor import suggest_stacks
        options = await suggest_stacks(
            prompt     = p.get("prompt", ""),
            app_type   = p.get("app_type", "web_app"),
            complexity = p.get("complexity", "moderate"),
            features   = p.get("features", []),
        )
        return ok({"options": options})
    except Exception as exc:
        return err(f"stack_advisor failed: {exc}")


# ═══════════════════════════════════════════════════════════════
# TOOL: quality_checker
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/quality_checker", response_model=ToolResponse)
async def tool_quality_checker(req: ToolRequest):
    """
    Lint and validate generated files.
    Returns { issues: [...], passed: bool }
    """
    p         = req.params
    file_tree = p.get("file_tree", {})
    file_plan = p.get("file_plan", list(file_tree.keys()))
    issues    = []

    # Missing files
    missing = [f for f in file_plan if f not in file_tree]
    if missing:
        issues.append(f"Missing files: {', '.join(missing)}")

    for path, content in file_tree.items():
        # Empty file
        if not content or len(content.strip()) < 20:
            issues.append(f"{path}: file is empty")
            continue

        # Python syntax
        if path.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as e:
                issues.append(f"{path}: SyntaxError line {e.lineno} — {e.msg}")

        # HTML structure
        if path.endswith(".html"):
            low = content.lower()
            if "<!doctype" not in low and "<html" not in low:
                issues.append(f"{path}: missing DOCTYPE or <html> root")

        # JSON validity
        if path.endswith(".json"):
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                issues.append(f"{path}: invalid JSON — {e.msg}")

        # package.json sanity
        if path == "package.json":
            try:
                pkg = json.loads(content)
                if "name" not in pkg:
                    issues.append("package.json: missing 'name' field")
            except Exception:
                pass

    if not any(f.lower() == "readme.md" for f in file_tree):
        issues.append("README.md is missing")

    return ok({
        "issues":     issues,
        "passed":     len(issues) == 0,
        "file_count": len(file_tree),
    })

# ═══════════════════════════════════════════════════════════════
# TOOL: file_scaffold  (LLM-based — no hardcoded templates)
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/file_scaffold", response_model=ToolResponse)
async def tool_file_scaffold(req: ToolRequest):
    """
    Generate the file plan (list of paths) for a given stack + spec.
    Uses LLM to produce a complete, correct file list for any stack.
    Returns { files: [...], count: int }
    """
    p    = req.params
    spec = p.get("spec", {})
    stack = p.get("stack", {})

    app_name    = spec.get("app_name", "my-app")
    description = spec.get("description", "")
    features    = spec.get("features", [])
    auth        = spec.get("auth_required", False)
    frontend    = stack.get("frontend", "")
    backend     = stack.get("backend", "")
    database    = stack.get("database", "")
    extras      = stack.get("extras", [])

    stack_desc = " + ".join(filter(None, [frontend, backend, database] + extras[:3]))

    try:
        from langchain_core.messages import HumanMessage
        from langchain_mistralai import ChatMistralAI

        llm = ChatMistralAI(
            model=settings.codestral_model,
            api_key=settings.codestral_api_key,
            base_url=settings.codestral_base_url,
            temperature=0.1,
            max_tokens=4096,
        )

        response = await llm.ainvoke([
            HumanMessage(content=(
                f"List every file path needed for this project as a JSON array.\n\n"
                f"App: {app_name}\n"
                f"Description: {description}\n"
                f"Stack: {stack_desc}\n"
                f"Features: {', '.join(features) if features else 'standard CRUD'}\n"
                f"Auth required: {auth}\n"
                f"Extra packages: {', '.join(extras) if extras else 'none'}\n\n"
                f"The project must be production-ready and immediately runnable. "
                f"Include all source files, configs, and root files the {stack_desc} "
                f"stack requires. Return ONLY the JSON array. No explanation, no markdown."
            )),
        ])

        raw   = _strip_fences(response.content).strip()
        files = json.loads(raw)

        if not isinstance(files, list):
            raise ValueError("Response is not a JSON array")

        # Sanitize
        seen, unique = set(), []
        for f in files:
            if (
                isinstance(f, str) and f.strip()
                and not f.startswith("/")
                and ".." not in f
                and "node_modules" not in f
                and f not in seen
            ):
                seen.add(f)
                unique.append(f.strip())

        logger.info("file_scaffold complete",
                    app=app_name, stack=stack_desc, file_count=len(unique))

        return ok({"files": unique, "count": len(unique)})

    except json.JSONDecodeError as exc:
        logger.warning("file_scaffold JSON parse failed — using fallback",
                       error=str(exc))
        return ok(_fallback_scaffold(frontend, backend, spec))

    except Exception as exc:
        return err(f"file_scaffold failed: {exc}")


def _fallback_scaffold(frontend: str, backend: str, spec: dict) -> dict:
    """Minimal hardcoded fallback if LLM scaffold fails."""
    f   = frontend.lower()
    b   = backend.lower()
    files = ["README.md", ".env.example"]

    if "svelte" in f:
        files += ["package.json", "svelte.config.js", "vite.config.js",
                  "src/routes/+page.svelte", "src/routes/+layout.svelte",
                  "src/lib/stores/auth.js", "src/lib/utils/api.js", "src/app.html"]
    elif "react" in f or "next" in f:
        files += ["package.json", "tsconfig.json",
                  "src/app/page.tsx", "src/app/layout.tsx",
                  "src/components/Layout.tsx", "src/lib/api.ts"]
    elif "vue" in f or "nuxt" in f:
        files += ["package.json", "nuxt.config.ts",
                  "pages/index.vue", "layouts/default.vue"]
    else:
        files += ["index.html", "css/style.css", "js/app.js"]

    if "express" in b or "node" in b:
        files += ["server.js", "middleware/auth.js",
                  "routes/index.js", "models/User.js"]
    elif "fastapi" in b or "python" in b:
        files += ["main.py", "requirements.txt",
                  "app/routes/__init__.py", "app/models.py",
                  "app/database.py", "app/config.py"]

    seen, unique = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)

    return {"files": unique, "count": len(unique)}


# ═══════════════════════════════════════════════════════════════
# TOOL: github_push
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/github_push", response_model=ToolResponse)
async def tool_github_push(req: ToolRequest):
    """Create GitHub repo + push all files. Returns { repo_url, commit_sha, owner }"""
    p              = req.params
    token          = p.get("github_token", "")
    repo_name      = p.get("repo_name", "")
    description    = p.get("description", "")
    file_tree      = p.get("file_tree", {})
    commit_message = p.get("commit_message", "Initial commit — generated by ChisCode")
    private        = p.get("private", False)

    if not token:
        return err("github_token is required")
    if not repo_name:
        return err("repo_name is required")
    if not file_tree:
        return err("file_tree is empty — nothing to push")

    try:
        from app.services.github_service import GitHubService
        gh     = GitHubService(token)
        result = await gh.create_repo_and_push(
            repo_name=repo_name,
            description=description[:255],
            file_tree=file_tree,
            commit_message=commit_message,
            private=private,
        )
        return ok(result)
    except Exception as exc:
        return err(f"github_push failed: {exc}")


# ═══════════════════════════════════════════════════════════════
# TOOL: github_pr
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/github_pr", response_model=ToolResponse)
async def tool_github_pr(req: ToolRequest):
    """Push to feature branch + open PR. Returns { pr_url, commit_sha, branch }"""
    p              = req.params
    token          = p.get("github_token", "")
    owner          = p.get("owner", "")
    repo           = p.get("repo", "")
    branch_name    = p.get("branch_name", "chiscode/update")
    file_tree      = p.get("file_tree", {})
    commit_message = p.get("commit_message", "feat: ChisCode iteration")
    pr_title       = p.get("pr_title", "ChisCode Update")
    pr_body        = p.get("pr_body", "_Generated by ChisCode_")

    if not all([token, owner, repo]):
        return err("github_token, owner, repo are all required")

    try:
        from app.services.github_service import GitHubService
        gh     = GitHubService(token)
        result = await gh.push_iteration_pr(
            owner=owner, repo=repo, branch_name=branch_name,
            file_tree=file_tree, commit_message=commit_message,
            pr_title=pr_title, pr_body=pr_body,
        )
        return ok(result)
    except Exception as exc:
        return err(f"github_pr failed: {exc}")


# ═══════════════════════════════════════════════════════════════
# TOOL: project_read / project_write / project_log
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/project_read", response_model=ToolResponse)
async def tool_project_read(req: ToolRequest):
    """Read a project from MongoDB. Returns the full doc (no _id)."""
    project_id = req.params.get("project_id", "")
    if not project_id:
        return err("project_id is required")
    try:
        from bson import ObjectId
        from app.db.mongodb import projects_collection
        doc = await projects_collection().find_one({"_id": ObjectId(project_id)})
        if not doc:
            return err(f"Project {project_id} not found")
        doc["_id"]     = str(doc["_id"])
        doc["user_id"] = str(doc.get("user_id", ""))
        return ok(doc)
    except Exception as exc:
        return err(f"project_read failed: {exc}")


@router.post("/tools/project_write", response_model=ToolResponse)
async def tool_project_write(req: ToolRequest):
    """Update fields on a project. Returns { updated: bool }"""
    p          = req.params
    project_id = p.get("project_id", "")
    fields     = p.get("fields", {})
    if not project_id:
        return err("project_id is required")
    if not fields:
        return err("fields dict is empty")
    try:
        from datetime import datetime, timezone
        from bson import ObjectId
        from app.db.mongodb import projects_collection
        fields["updated_at"] = datetime.now(tz=timezone.utc)
        r = await projects_collection().update_one(
            {"_id": ObjectId(project_id)}, {"$set": fields}
        )
        return ok({"updated": r.modified_count > 0})
    except Exception as exc:
        return err(f"project_write failed: {exc}")


@router.post("/tools/project_log", response_model=ToolResponse)
async def tool_project_log(req: ToolRequest):
    """Append a log line. Returns { appended: bool }"""
    p          = req.params
    project_id = p.get("project_id", "")
    message    = p.get("message", "")
    if not project_id or not message:
        return err("project_id and message are required")
    try:
        from datetime import datetime, timezone
        from bson import ObjectId
        from app.db.mongodb import projects_collection
        await projects_collection().update_one(
            {"_id": ObjectId(project_id)},
            {"$push": {"generation_log": message},
             "$set":  {"updated_at": datetime.now(tz=timezone.utc)}},
        )
        return ok({"appended": True})
    except Exception as exc:
        return err(f"project_log failed: {exc}")


# ═══════════════════════════════════════════════════════════════
# TOOL: search_web
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/search_web", response_model=ToolResponse)
async def tool_search_web(req: ToolRequest):
    """
    DuckDuckGo search — returns top snippets.
    Returns { results: [{title, url, snippet}] }
    """
    p           = req.params
    query       = p.get("query", "")
    max_results = int(p.get("max_results", 5))

    if not query:
        return err("query is required")

    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title", ""),
                    "url":     r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
        return ok({"results": results, "count": len(results)})
    except Exception as exc:
        return err(f"search_web failed: {exc}")


# ═══════════════════════════════════════════════════════════════
# TOOL: analyze_prompt
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/analyze_prompt", response_model=ToolResponse)
async def tool_analyze_prompt(req: ToolRequest):
    """
    Parse a natural language app description into a structured spec dict.
    Returns { spec: {...} }
    """
    p            = req.params
    prompt       = p.get("prompt", "")
    project_name = p.get("project_name", "my-app")

    if not prompt:
        return err("prompt is required")

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_mistralai import ChatMistralAI

        llm = ChatMistralAI(
            model=settings.codestral_model,
            api_key=settings.codestral_api_key,
            base_url=settings.codestral_base_url,
            temperature=0.1,
            max_tokens=2048,
        )
        response = await llm.ainvoke([
            SystemMessage(content="""Analyze the app idea. Return ONLY valid JSON:
{
  "app_type": "web_app|api|cli|mobile_web|dashboard|landing_page|e_commerce|real_time_app|data_app|game|ai_app",
  "app_name": "snake_case_name",
  "description": "one clear sentence",
  "features": ["feature1", "feature2"],
  "auth_required": false,
  "database_needed": false,
  "api_needed": false,
  "mobile_responsive": true,
  "complexity": "simple|moderate|complex"
}"""),
            HumanMessage(content=(
                f"App idea: {prompt}\n"
                f"Project name hint: {project_name}"
            )),
        ])
        raw  = _strip_fences(response.content)
        spec = json.loads(raw)
        return ok({"spec": spec})
    except Exception as exc:
        return err(f"analyze_prompt failed: {exc}")


# ═══════════════════════════════════════════════════════════════
# TOOL: daytona_sandbox
# ═══════════════════════════════════════════════════════════════

@router.post("/tools/daytona_sandbox", response_model=ToolResponse)
async def tool_daytona_sandbox(req: ToolRequest):
    """
    Create a Daytona sandbox, upload files, start the app.
    Returns { workspace_id, preview_url, port }
    """
    p            = req.params
    project_id   = p.get("project_id", "")
    project_name = p.get("project_name", "chiscode-app")
    file_tree    = p.get("file_tree", {})
    stack        = p.get("stack", {})

    if not project_id:
        return err("project_id is required")
    if not file_tree:
        return err("file_tree is empty — nothing to sandbox")

    try:
        from app.services.daytona_service import DaytonaService
        daytona = DaytonaService()
        result  = await daytona.create_sandbox(
            project_id=project_id,
            project_name=project_name,
            file_tree=file_tree,
            stack=stack,
        )
        return ok(result)
    except Exception as exc:
        return err(f"daytona_sandbox failed: {exc}")


# ── Helper ────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()   