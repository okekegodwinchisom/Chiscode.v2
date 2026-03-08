"""
ChisCode — Project Routes
Generation, iteration, version control, and WebSocket progress streaming.
"""
import json
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

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
router = APIRouter(prefix="/projects", tags=["projects"], redirect_slashes=False)

# In-memory WebSocket connection manager (Phase 8 will move to Redis pub/sub)
_active_connections: dict[str, list[WebSocket]] = {}


# ── WebSocket Manager ─────────────────────────────────────────

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
    Clients connect here to receive live updates during code generation.
    """
    await websocket.accept()
    _active_connections.setdefault(project_id, []).append(websocket)
    logger.info("WebSocket connected", project_id=project_id)

    try:
        while True:
            # Keep-alive: echo ping messages
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        conns = _active_connections.get(project_id, [])
        if websocket in conns:
            conns.remove(websocket)
        logger.info("WebSocket disconnected", project_id=project_id)


# ── Project CRUD ──────────────────────────────────────────────

@router.get("/", response_model=list[ProjectPublic], response_model_by_alias=True)
async def list_projects(
    current_user=Depends(get_current_user),
    skip: int = 0,
    limit: int = 20,
):
    """List all projects for the authenticated user."""
    coll = projects_collection()
    cursor = coll.find(
        {"user_id": str(current_user.id)},
        sort=[("created_at", -1)],
        skip=skip,
        limit=limit,
    )
    projects = []
    async for doc in cursor:
        p = ProjectInDB(**doc)
        pub = ProjectPublic(
            **p.model_dump(by_alias=True),
            file_count=len(p.file_tree),
        )
        projects.append(pub)
    return projects


@router.get("/{project_id}", response_model=ProjectDetail, response_model_by_alias=True)
async def get_project(project_id: str, current_user=Depends(get_current_user)):
    """Get full details of a single project."""
    coll = projects_collection()
    doc = await coll.find_one({"_id": ObjectId(project_id), "user_id": str(current_user.id)})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    p = ProjectInDB(**doc)
    return ProjectDetail(**p.model_dump(by_alias=True), file_count=len(p.file_tree))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, current_user=Depends(get_current_user)):
    """Delete a project (owner only)."""
    coll = projects_collection()
    result = await coll.delete_one({"_id": ObjectId(project_id), "user_id": str(current_user.id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")


# ── Generation ────────────────────────────────────────────────

@router.post("/generate", response_model=GenerationStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_generation(
    req: GenerateProjectRequest,
    current_user=Depends(check_rate_limit),
):
    """
    Start AI code generation for a new project.

    Creates a project record immediately, then runs the LangGraph agent
    as a background task. Connect to the WebSocket URL for live progress.
    """
    coll = projects_collection()

    project_name = req.project_name or f"project-{ObjectId()}"
    doc = {
        "user_id": str(current_user.id),
        "name": project_name,
        "description": req.prompt[:200],
        "original_prompt": req.prompt,
        "status": "pending",
        "file_tree": {},
        "generation_log": ["Project created. Waiting for agent..."],
        "self_heal_attempts": 0,
        "current_version": 0,
        "created_at": datetime.now(tz=timezone.utc),
        "updated_at": datetime.now(tz=timezone.utc),
    }
    if req.preferred_stack:
        doc["stack"] = req.preferred_stack.model_dump()

    result = await coll.insert_one(doc)
    project_id = str(result.inserted_id)

    # TODO Phase 2: Kick off LangGraph agent as a background task
    # background_tasks.add_task(run_generation_agent, project_id, req, current_user)

    logger.info("Generation started", project_id=project_id, user_id=str(current_user.id))

    ws_url = f"ws://{settings.frontend_base_url.split('://')[-1]}/projects/ws/{project_id}"
    return GenerationStarted(project_id=project_id, ws_url=ws_url)


@router.post("/{project_id}/confirm")
async def confirm_project(
    project_id: str,
    req: ConfirmProjectRequest,
    current_user=Depends(get_current_user),
):
    """
    User approves the generated project.
    Phase 3: This will trigger the GitHub commit.
    """
    coll = projects_collection()
    doc = await coll.find_one({"_id": ObjectId(project_id), "user_id": str(current_user.id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    if doc.get("status") != "awaiting_confirmation":
        raise HTTPException(status_code=400, detail="Project is not awaiting confirmation.")

    await coll.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"status": "committing", "updated_at": datetime.now(tz=timezone.utc)}},
    )

    # TODO Phase 3: Trigger GitHub commit
    await ws_broadcast(project_id, {"type": "status", "status": "committing", "message": "Committing to GitHub..."})
    return {"message": "Confirmation received. Committing..."}


@router.post("/{project_id}/cancel")
async def cancel_project(project_id: str, current_user=Depends(get_current_user)):
    """Cancel a pending or awaiting-confirmation project."""
    coll = projects_collection()
    result = await coll.update_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)},
        {"$set": {"status": "cancelled", "updated_at": datetime.now(tz=timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"message": "Project cancelled."}


@router.post("/{project_id}/iterate", status_code=status.HTTP_202_ACCEPTED)
async def iterate_project(
    project_id: str,
    req: IterateProjectRequest,
    current_user=Depends(check_rate_limit),
):
    """
    Submit a refinement request for an existing project.
    Phase 4: Runs the iteration agent.
    """
    coll = projects_collection()
    doc = await coll.find_one({"_id": ObjectId(project_id), "user_id": str(current_user.id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    if doc.get("status") not in ("complete",):
        raise HTTPException(status_code=400, detail="Project must be complete before iterating.")

    # TODO Phase 4: Kick off iteration agent
    await coll.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"status": "analyzing", "updated_at": datetime.now(tz=timezone.utc)}},
    )
    return {"message": "Iteration started.", "project_id": project_id}


# ── Version Control ────────────────────────────────────────────

@router.get("/{project_id}/versions", response_model=list[ProjectVersionPublic], response_model_by_alias=True)
async def list_versions(project_id: str, current_user=Depends(get_current_user)):
    """List all committed versions of a project."""
    # Verify ownership
    proj = await projects_collection().find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    coll = project_versions_collection()
    cursor = coll.find({"project_id": project_id}, sort=[("version", -1)])
    versions = []
    async for doc in cursor:
        versions.append(ProjectVersionPublic(**doc))
    return versions


@router.post("/{project_id}/rollback/{version}")
async def rollback_to_version(
    project_id: str,
    version: int,
    current_user=Depends(get_current_user),
):
    """
    Roll back a project to a previous version.
    Phase 3: Restores from file_snapshot and optionally reverts GitHub.
    """
    # Verify ownership
    proj_coll = projects_collection()
    proj = await proj_coll.find_one(
        {"_id": ObjectId(project_id), "user_id": str(current_user.id)}
    )
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    ver_doc = await project_versions_collection().find_one(
        {"project_id": project_id, "version": version}
    )
    if not ver_doc:
        raise HTTPException(status_code=404, detail=f"Version {version} not found.")

    # Restore file tree from snapshot
    await proj_coll.update_one(
        {"_id": ObjectId(project_id)},
        {
            "$set": {
                "file_tree": ver_doc["file_snapshot"],
                "current_version": version,
                "status": "complete",
                "updated_at": datetime.now(tz=timezone.utc),
            }
        },
    )

    # TODO Phase 3: Also revert GitHub repo to commit_sha

    return {"message": f"Rolled back to version {version}."}
