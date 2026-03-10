"""
ChisCode — Project Routes
Generation (SSE streaming), CRUD, version control.
"""
import json
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse

from app.agents.generation_agent import generate_project_stream
from app.api.deps import check_rate_limit, get_current_user
from app.core.logging import get_logger
from app.db.mongodb import project_versions_collection, projects_collection
from app.schemas.project import (
    ConfirmProjectRequest,
    GenerateProjectRequest,
    IterateProjectRequest,
    ProjectDetail,
    ProjectInDB,
    ProjectPublic,
    ProjectVersionPublic,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"], redirect_slashes=False)

# In-memory WebSocket manager — kept for future use, no-ops safely if unused
_active_connections: dict[str, list[WebSocket]] = {}


# ── WebSocket (kept for compatibility) ───────────────────────────

async def ws_broadcast(project_id: str, message: dict) -> None:
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
    await websocket.accept()
    _active_connections.setdefault(project_id, []).append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        conns = _active_connections.get(project_id, [])
        if websocket in conns:
            conns.remove(websocket)


# ── Project CRUD ──────────────────────────────────────────────────

@router.get("/", response_model=list[ProjectPublic], response_model_by_alias=True)
async def list_projects(
    current_user=Depends(get_current_user),
    skip: int = 0,
    limit: int = 20,
):
    """List all projects for the authenticated user, newest first."""
    cursor = projects_collection().find(
        {"user_id": str(current_user.id)},
        sort=[("created_at", -1)],
        skip=skip,
        limit=limit,
    )
    projects = []
    async for doc in cursor:
        p = ProjectInDB(**doc)
        projects.append(ProjectPublic(**p.model_dump(by_alias=True), file_count=len(p.file_tree)))
    return projects


@router.get("/{project_id}", response_model=ProjectDetail, response_model_by_alias=True)
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


# ── Generation (SSE) ──────────────────────────────────────────────

@router.post("/generate")
async def start_generation(
    req: GenerateProjectRequest,
    current_user=Depends(check_rate_limit),
):
    """
    Create a project record then stream generation progress as SSE.

    The client reads the response body as a stream — no polling, no WebSocket.
    Each chunk is a `data: {...}\\n\\n` SSE line with an `event` field:
      log      — progress message
      status   — status change  {status, message}
      file     — file generated {filename, size}
      issues   — quality notes  {issues: [...]}
      complete — finished       {file_count}
      error    — fatal          {message}

    The X-Project-Id response header carries the new project's MongoDB _id
    so the client can navigate to /projects/{id} on completion.
    """
    project_name = req.project_name or f"project-{ObjectId()}"

    doc = {
        "user_id":            str(current_user.id),
        "name":               project_name,
        "description":        req.prompt[:200],
        "original_prompt":    req.prompt,
        "status":             "pending",
        "file_tree":          {},
        "generation_log":     ["Agent starting..."],
        "self_heal_attempts": 0,
        "current_version":    0,
        "created_at":         datetime.now(tz=timezone.utc),
        "updated_at":         datetime.now(tz=timezone.utc),
    }
    if req.preferred_stack:
        doc["stack"] = req.preferred_stack.model_dump()

    result     = await projects_collection().insert_one(doc)
    project_id = str(result.inserted_id)

    logger.info("Generation started", project_id=project_id, user_id=str(current_user.id))

    generator = generate_project_stream(
        project_id=project_id,
        user_id=str(current_user.id),
        prompt=req.prompt,
        project_name=project_name,
        preferred_stack=req.preferred_stack.model_dump() if req.preferred_stack else None,
    )

    response = StreamingResponse(
        generator,
        media_type="text/event-stream",
    )
    response.headers["X-Project-Id"]      = project_id
    response.headers["Cache-Control"]     = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Transfer-Encoding"] = "chunked"
    return response

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
    doc = await projects_collection().find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    if doc.get("status") != "awaiting_confirmation":
        raise HTTPException(status_code=400, detail="Project is not awaiting confirmation.")

    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"status": "committing", "updated_at": datetime.now(tz=timezone.utc)}},
    )
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
    project_id: str,
    req: IterateProjectRequest,
    current_user=Depends(check_rate_limit),
):
    """
    Refine an existing project with a follow-up prompt.
    Phase 4: will stream the iteration agent as SSE.
    """
    doc = await projects_collection().find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    if doc.get("status") not in ("complete", "awaiting_confirmation"):
        raise HTTPException(status_code=400, detail="Project must be complete before iterating.")

    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"status": "analyzing", "updated_at": datetime.now(tz=timezone.utc)}},
    )
    return {"message": "Iteration queued (Phase 4).", "project_id": project_id}


# ── Version Control ───────────────────────────────────────────────

@router.get("/{project_id}/versions", response_model=list[ProjectVersionPublic], response_model_by_alias=True)
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
    proj = await projects_collection().find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    ver_doc = await project_versions_collection().find_one(
        {"project_id": project_id, "version": version}
    )
    if not ver_doc:
        raise HTTPException(status_code=404, detail=f"Version {version} not found.")

    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "file_tree":       ver_doc["file_snapshot"],
            "current_version": version,
            "status":          "complete",
            "updated_at":      datetime.now(tz=timezone.utc),
        }},
    )
    return {"message": f"Rolled back to version {version}."}
    