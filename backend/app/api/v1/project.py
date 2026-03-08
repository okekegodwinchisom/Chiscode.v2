"""
ChisCode — Project Routes (Phase 2 — Agent wired)
===================================================
Changes from Phase 1:
  - start_generation: kicks off run_generation_agent as a FastAPI BackgroundTask
  - BackgroundTasks injected into the endpoint signature
  - ws_url uses wss:// in production, ws:// in dev
  - iterate_project: stubbed for Phase 4 (agent call placeholder added)
"""
import json
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

from app.agents.generation_agent import run_generation_agent
from app.api.deps import check_rate_limit, get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import project_versions_collection, projects_collection
from app.schemas.project import (
    ConfirmProjectRequest,
    GenerateProjectRequest,
    GenerationStarted,
    IterateProjectRequest,
    ProjectDetail,
    ProjectInDB,
    ProjectPublic,
    ProjectVersionPublic,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])

# In-memory WebSocket manager — Phase 8 will move to Redis pub/sub
_active_connections: dict[str, list[WebSocket]] = {}


# ── WebSocket ─────────────────────────────────────────────────────

async def ws_broadcast(project_id: str, message: dict) -> None:
    """Broadcast a JSON message to all WebSocket clients watching a project."""
    connections = _active_connections.get(project_id, [])
    dead = []
    for ws in connections:
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections.remove(ws)


@router.websocket("/ws/{project_id}")
async def project_ws(websocket: WebSocket, project_id: str):
    """
    WebSocket endpoint for real-time generation progress.
    The frontend connects here immediately after POST /generate.
    Message types: log | status | file_done | complete | error
    """
    await websocket.accept()
    _active_connections.setdefault(project_id, []).append(websocket)
    logger.info("WebSocket connected", project_id=project_id)

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        conns = _active_connections.get(project_id, [])
        if websocket in conns:
            conns.remove(websocket)
        logger.info("WebSocket disconnected", project_id=project_id)


# ── Project CRUD ──────────────────────────────────────────────────

@router.get("/", response_model=list[ProjectPublic])
async def list_projects(
    current_user=Depends(get_current_user),
    skip: int = 0,
    limit: int = 20,
):
    """List all projects for the authenticated user, newest first."""
    coll   = projects_collection()
    cursor = coll.find(
        {"user_id": str(current_user.id)},
        sort=[("created_at", -1)],
        skip=skip,
        limit=limit,
    )
    projects = []
    async for doc in cursor:
        p   = ProjectInDB(**doc)
        pub = ProjectPublic(**p.model_dump(by_alias=True), file_count=len(p.file_tree))
        projects.append(pub)
    return projects


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: str, current_user=Depends(get_current_user)):
    """Get full project details including file tree and generation log."""
    doc = await projects_collection().find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    p = ProjectInDB(**doc)
    return ProjectDetail(**p.model_dump(by_alias=True), file_count=len(p.file_tree))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, current_user=Depends(get_current_user)):
    """Permanently delete a project (owner only)."""
    result = await projects_collection().delete_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")


# ── Generation ────────────────────────────────────────────────────

@router.post("/generate", response_model=GenerationStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_generation(
    req:              GenerateProjectRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(check_rate_limit),
):
    """
    Create a project record then launch the AI agent as a background task.

    Flow:
      1. Insert project doc with status=pending
      2. Return {project_id, ws_url} immediately (202)
      3. Agent runs in background: analyze → generate → validate → heal? → complete
      4. Frontend streams progress via WebSocket at ws_url
    """
    coll         = projects_collection()
    project_name = req.project_name or f"project-{ObjectId()}"

    doc = {
        "user_id":         str(current_user.id),
        "name":            project_name,
        "description":     req.prompt[:200],
        "original_prompt": req.prompt,
        "status":          "pending",
        "file_tree":       {},
        "generation_log":  ["Project created. Agent starting..."],
        "self_heal_attempts": 0,
        "current_version": 0,
        "created_at":      datetime.now(tz=timezone.utc),
        "updated_at":      datetime.now(tz=timezone.utc),
    }
    if req.preferred_stack:
        doc["stack"] = req.preferred_stack.model_dump()

    result     = await coll.insert_one(doc)
    project_id = str(result.inserted_id)

    # Kick off the LangGraph agent asynchronously
    background_tasks.add_task(
        run_generation_agent,
        project_id=project_id,
        user_id=str(current_user.id),
        prompt=req.prompt,
        project_name=project_name,
        preferred_stack=req.preferred_stack.model_dump() if req.preferred_stack else None,
    )

    logger.info("Generation queued", project_id=project_id, user_id=str(current_user.id))

    # wss:// in production (HF Spaces is HTTPS), ws:// in dev
    base = settings.frontend_base_url.split("://")[-1]
    scheme = "wss" if settings.is_production else "ws"
    ws_url = f"{scheme}://{base}/api/v1/project/ws/{project_id}"

    return GenerationStarted(
        project_id=project_id,
        ws_url=ws_url,
        message="Generation started. Connect to ws_url for live progress.",
    )


# ── Confirm / Cancel ──────────────────────────────────────────────

@router.post("/{project_id}/confirm")
async def confirm_project(
    project_id: str,
    req: ConfirmProjectRequest,
    current_user=Depends(get_current_user),
):
    """
    User approves the generated project.
    Phase 3 will trigger the GitHub commit here.
    """
    coll = projects_collection()
    doc  = await coll.find_one({"_id": ObjectId(project_id), "user_id": str(current_user.id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    if doc.get("status") != "awaiting_confirmation":
        raise HTTPException(status_code=400, detail="Project is not awaiting confirmation.")

    await coll.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"status": "committing", "updated_at": datetime.now(tz=timezone.utc)}},
    )
    await ws_broadcast(project_id, {
        "type": "status", "status": "committing", "message": "Confirmed! (GitHub integration coming in Phase 3)"
    })
    return {"message": "Confirmed. GitHub integration coming in Phase 3."}


@router.post("/{project_id}/cancel")
async def cancel_project(project_id: str, current_user=Depends(get_current_user)):
    """Cancel a pending or in-progress project."""
    result = await projects_collection().update_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)},
        {"$set": {"status": "cancelled", "updated_at": datetime.now(tz=timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"message": "Project cancelled."}


# ── Iteration ─────────────────────────────────────────────────────

@router.post("/{project_id}/iterate", status_code=status.HTTP_202_ACCEPTED)
async def iterate_project(
    project_id:       str,
    req:              IterateProjectRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(check_rate_limit),
):
    """
    Refine an existing project with a follow-up prompt.
    Phase 4: will run the iteration agent (diff-aware generation).
    """
    coll = projects_collection()
    doc  = await coll.find_one({"_id": ObjectId(project_id), "user_id": str(current_user.id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    if doc.get("status") not in ("complete", "awaiting_confirmation"):
        raise HTTPException(status_code=400, detail="Project must be complete before iterating.")

    await coll.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"status": "analyzing", "updated_at": datetime.now(tz=timezone.utc)}},
    )

    # TODO Phase 4: background_tasks.add_task(run_iteration_agent, project_id, req.prompt)
    await ws_broadcast(project_id, {
        "type": "status", "status": "analyzing",
        "message": "Iteration agent coming in Phase 4.",
    })

    return {"message": "Iteration queued.", "project_id": project_id}


# ── Version Control ───────────────────────────────────────────────

@router.get("/{project_id}/versions", response_model=list[ProjectVersionPublic])
async def list_versions(project_id: str, current_user=Depends(get_current_user)):
    """List all saved versions of a project."""
    proj = await projects_collection().find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    cursor   = project_versions_collection().find({"project_id": project_id}, sort=[("version", -1)])
    versions = []
    async for doc in cursor:
        versions.append(ProjectVersionPublic(**doc))
    return versions


@router.post("/{project_id}/rollback/{version}")
async def rollback_to_version(
    project_id: str,
    version:    int,
    current_user=Depends(get_current_user),
):
    """Restore a project to a previous version's file snapshot."""
    proj_coll = projects_collection()
    proj      = await proj_coll.find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    ver_doc = await project_versions_collection().find_one(
        {"project_id": project_id, "version": version}
    )
    if not ver_doc:
        raise HTTPException(status_code=404, detail=f"Version {version} not found.")

    await proj_coll.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "file_tree":       ver_doc["file_snapshot"],
            "current_version": version,
            "status":          "complete",
            "updated_at":      datetime.now(tz=timezone.utc),
        }},
    )
    return {"message": f"Rolled back to version {version}."}
    