"""
ChisCode — Project Routes
Generation, iteration, version control, and WebSocket progress streaming.
"""
import json
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import (
    APIRouter, 
    Depends, 
    HTTPException, 
    WebSocket, 
    WebSocketDisconnect, 
    status, 
    Query, 
    BackgroundTasks,
    Header
)

from app.api.deps import check_rate_limit, get_current_user, get_optional_user
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
    ProjectUpdate,
    ProjectStatus,
    ProjectStats
)
from app.services import project_service
from app.websocket.manager import ws_manager

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])


# ── WebSocket Endpoint ─────────────────────────────────────────

@router.websocket("/ws/{project_id}")
async def project_websocket(websocket: WebSocket, project_id: str):
    """
    WebSocket endpoint for real-time generation progress.
    Clients connect here to receive live updates during code generation.
    """
    await websocket.accept()
    
    # Try to authenticate user from query params (token)
    token = websocket.query_params.get("token")
    user = None
    user_id = "anonymous"
    
    if token:
        try:
            # Authenticate token - implement this based on your auth system
            user = await get_optional_user(token)
            if user is not None:
                user_id = str(user.id)
        except Exception as e:
            logger.warning(f"WebSocket auth failed: {e}")
    
    await ws_manager.connect(websocket, project_id, user_id)
    logger.info(f"WebSocket connected - project_id: {project_id}, user_id: {user_id}")

    try:
        # Send initial connection confirmation
        await websocket.send_json({
            "type": "connected",
            "message": f"Connected to project {project_id}",
            "project_id": project_id,
            "user_id": user_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        while True:
            # Handle ping/pong for keepalive
            data = await websocket.receive_text()
            
            if data == "ping":
                await websocket.send_text("pong")
                
            elif data == "status":
                # Client requesting status update
                try:
                    project_status = await project_service.get_project_status(
                        project_id, 
                        user_id if user_id != "anonymous" else None
                    )
                    
                    if project_status is not None:
                        await websocket.send_json({
                            "type": "status",
                            "status": project_status.status,
                            "progress": getattr(project_status, 'progress', None),
                            "message": getattr(project_status, 'message', None),
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                    else:
                        await websocket.send_json({
                            "type": "status",
                            "status": "unknown",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                except Exception as e:
                    logger.error(f"Error getting status: {e}")
                    await websocket.send_json({
                        "type": "error",
                        "message": "Failed to get status",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected - project_id: {project_id}, user_id: {user_id}")
    except Exception as e:
        logger.error(f"WebSocket error - project_id: {project_id}, error: {str(e)}")
    finally:
        await ws_manager.disconnect(project_id, user_id)


# ── Project CRUD ──────────────────────────────────────────────

@router.get("/", response_model=List[ProjectPublic])
async def list_projects(
    current_user=Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, description="Filter by project status")
):
    """List all projects for the authenticated user."""
    try:
        projects = await project_service.get_user_projects(
            user_id=str(current_user.id),
            skip=skip,
            limit=limit,
            status_filter=status_filter
        )
        return projects
    except Exception as e:
        logger.error(f"Error listing projects: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve projects"
        )


@router.post("/", response_model=ProjectPublic, status_code=status.HTTP_201_CREATED)
async def create_empty_project(
    name: str = Query(..., min_length=1, max_length=100),
    description: Optional[str] = Query(None, max_length=500),
    current_user=Depends(get_current_user)
):
    """Create a new empty project."""
    try:
        project = await project_service.create_project(
            user_id=str(current_user.id),
            name=name,
            description=description
        )
        
        logger.info(f"Empty project created - project_id: {str(project.id)}, user_id: {str(current_user.id)}")
        
        # Convert to dict safely
        project_dict = project.dict(by_alias=True) if hasattr(project, 'dict') else project.model_dump(by_alias=True)
        return ProjectPublic.model_validate(project_dict)
        
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create project: {str(e)}"
        )


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(
    project_id: str,
    current_user=Depends(get_current_user)
):
    """Get full details of a single project."""
    project = await project_service.get_project(project_id, str(current_user.id))
    
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Project not found."
        )
    
    # Increment view count (optional, run in background)
    try:
        await project_service.increment_project_views(project_id)
    except Exception as e:
        logger.warning(f"Failed to increment views: {e}")
    
    # Convert to dict safely
    project_dict = project.dict(by_alias=True) if hasattr(project, 'dict') else project.model_dump(by_alias=True)
    return ProjectDetail.model_validate(project_dict)


@router.patch("/{project_id}", response_model=ProjectPublic)
async def update_project(
    project_id: str,
    update_data: ProjectUpdate,
    current_user=Depends(get_current_user)
):
    """Update project metadata (name, description, etc.)."""
    project = await project_service.update_project(
        project_id=project_id,
        user_id=str(current_user.id),
        update_data=update_data
    )
    
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Project not found."
        )
    
    logger.info(f"Project updated - project_id: {project_id}, user_id: {str(current_user.id)}")
    
    project_dict = project.dict(by_alias=True) if hasattr(project, 'dict') else project.model_dump(by_alias=True)
    return ProjectPublic.model_validate(project_dict)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    current_user=Depends(get_current_user)
):
    """Delete a project (owner only)."""
    deleted = await project_service.delete_project(project_id, str(current_user.id))
    
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Project not found."
        )
    
    logger.info(f"Project deleted - project_id: {project_id}, user_id: {str(current_user.id)}")
    return None


# ── Generation ────────────────────────────────────────────────

@router.post("/generate", response_model=GenerationStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_generation(
    req: GenerateProjectRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(check_rate_limit),
):
    """
    Start AI code generation for a new project.

    Creates a project record immediately, then runs the LangGraph agent
    as a background task. Connect to the WebSocket URL for live progress.
    """
    try:
        # Check if user has reached project limit
        await project_service.check_project_limit(str(current_user.id))
        
        # Start generation
        project_id = await project_service.start_generation(
            user_id=str(current_user.id),
            prompt=req.prompt,
            project_name=req.project_name,
            preferred_stack=req.preferred_stack
        )
        
        # Add background task for actual generation with error handling
        async def safe_generation():
            try:
                await project_service.run_generation_agent(
                    project_id=project_id,
                    user_id=str(current_user.id),
                    prompt=req.prompt,
                    preferred_stack=req.preferred_stack
                )
            except Exception as e:
                logger.error(f"Generation task failed - project_id: {project_id}, error: {str(e)}")
                # Update project status to failed
                await project_service.mark_project_failed(project_id, str(e))
                # Notify via WebSocket
                await ws_manager.send_error(project_id, f"Generation failed: {str(e)}")
        
        background_tasks.add_task(safe_generation)
        
        # Construct WebSocket URL
        base_url = settings.frontend_base_url.split('://')[-1]
        protocol = "wss" if "https" in settings.frontend_base_url else "ws"
        ws_url = f"{protocol}://{base_url}/api/v1/projects/ws/{project_id}"
        
        logger.info(f"Generation started - project_id: {project_id}, user_id: {str(current_user.id)}")
        
        return GenerationStarted(
            project_id=project_id,
            ws_url=ws_url,
            message="Generation started. Connect to WebSocket for live progress."
        )
        
    except Exception as e:
        logger.error(f"Failed to start generation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start generation: {str(e)}"
        )


@router.get("/{project_id}/status", response_model=ProjectStatus)
async def get_project_status(
    project_id: str,
    current_user=Depends(get_current_user)
):
    """Get the current generation status of a project."""
    project_status = await project_service.get_project_status(project_id, str(current_user.id))
    
    if project_status is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    return project_status


@router.post("/{project_id}/confirm")
async def confirm_project(
    project_id: str,
    req: ConfirmProjectRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
):
    """
    User approves the generated project.
    This will trigger GitHub commit and deployment.
    """
    # Send WebSocket update
    await ws_manager.send_status(project_id, "committing", "Processing confirmation...")
    
    try:
        result = await project_service.confirm_project(
            project_id=project_id,
            user_id=str(current_user.id),
            commit_message=req.commit_message,
            push_to_github=req.push_to_github
        )
        
        # Add background task for GitHub operations
        if req.push_to_github:
            async def safe_github_push():
                try:
                    await project_service.push_to_github(
                        project_id=project_id,
                        user_id=str(current_user.id)
                    )
                except Exception as e:
                    logger.error(f"GitHub push failed - project_id: {project_id}, error: {str(e)}")
                    await ws_manager.send_error(project_id, f"GitHub push failed: {str(e)}")
            
            background_tasks.add_task(safe_github_push)
        
        logger.info(f"Project confirmed - project_id: {project_id}, user_id: {str(current_user.id)}")
        
        return {
            "message": "Project confirmed successfully.",
            "repository_url": result.get("repository_url") if result else None
        }
        
    except Exception as e:
        logger.error(f"Project confirmation failed - project_id: {project_id}, error: {str(e)}")
        await ws_manager.send_error(project_id, f"Confirmation failed: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Confirmation failed: {str(e)}"
        )


@router.post("/{project_id}/cancel")
async def cancel_project(
    project_id: str,
    current_user=Depends(get_current_user)
):
    """Cancel a pending or awaiting-confirmation project."""
    cancelled = await project_service.cancel_project(project_id, str(current_user.id))
    
    if not cancelled:
        raise HTTPException(
            status_code=404, 
            detail="Project not found or cannot be cancelled."
        )
    
    await ws_manager.send_status(project_id, "cancelled", "Project cancelled by user")
    logger.info(f"Project cancelled - project_id: {project_id}, user_id: {str(current_user.id)}")
    
    return {"message": "Project cancelled successfully."}


@router.post("/{project_id}/iterate", status_code=status.HTTP_202_ACCEPTED)
async def iterate_project(
    project_id: str,
    req: IterateProjectRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(check_rate_limit),
):
    """
    Submit a refinement request for an existing project.
    Runs the iteration agent in the background.
    """
    # Verify project exists and is complete
    project = await project_service.get_project(project_id, str(current_user.id))
    
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    if project.status not in ("complete", "failed"):
        raise HTTPException(
            status_code=400, 
            detail="Project must be complete or failed before iterating."
        )
    
    # Start iteration
    iteration_id = await project_service.start_iteration(
        project_id=project_id,
        user_id=str(current_user.id),
        prompt=req.prompt
    )
    
    # Add background task for iteration with error handling
    async def safe_iteration():
        try:
            await project_service.run_iteration_agent(
                iteration_id=iteration_id,
                project_id=project_id,
                user_id=str(current_user.id),
                prompt=req.prompt
            )
        except Exception as e:
            logger.error(f"Iteration failed - iteration_id: {iteration_id}, error: {str(e)}")
            await ws_manager.send_error(project_id, f"Iteration failed: {str(e)}")
    
    background_tasks.add_task(safe_iteration)
    
    logger.info(f"Iteration started - project_id: {project_id}, user_id: {str(current_user.id)}")
    
    return {
        "message": "Iteration started.",
        "project_id": project_id,
        "iteration_id": iteration_id
    }


# ── Version Control ────────────────────────────────────────────

@router.get("/{project_id}/versions", response_model=List[ProjectVersionPublic])
async def list_versions(
    project_id: str,
    current_user=Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100)
):
    """List all committed versions of a project."""
    versions = await project_service.get_project_versions(
        project_id=project_id,
        user_id=str(current_user.id),
        skip=skip,
        limit=limit
    )
    return versions


@router.get("/{project_id}/versions/{version}", response_model=ProjectVersionPublic)
async def get_version(
    project_id: str,
    version: int,
    current_user=Depends(get_current_user)
):
    """Get a specific version of a project."""
    version_data = await project_service.get_project_version(
        project_id=project_id,
        user_id=str(current_user.id),
        version=version
    )
    
    if version_data is None:
        raise HTTPException(status_code=404, detail="Version not found.")
    
    return version_data


@router.post("/{project_id}/rollback/{version}")
async def rollback_to_version(
    project_id: str,
    version: int,
    current_user=Depends(get_current_user),
    background_tasks: BackgroundTasks
):
    """
    Roll back a project to a previous version.
    Restores from file_snapshot and optionally reverts GitHub.
    """
    try:
        result = await project_service.rollback_to_version(
            project_id=project_id,
            user_id=str(current_user.id),
            version=version
        )
        
        # Optionally revert GitHub repo
        if result.get("github_repo_url"):
            async def safe_github_revert():
                try:
                    await project_service.revert_github_repo(
                        project_id=project_id,
                        user_id=str(current_user.id),
                        version=version
                    )
                except Exception as e:
                    logger.error(f"GitHub revert failed: {e}")
            
            background_tasks.add_task(safe_github_revert)
        
        logger.info(f"Project rolled back - project_id: {project_id}, version: {version}, user_id: {str(current_user.id)}")
        
        return {
            "message": f"Rolled back to version {version}.",
            "version": version,
            "file_count": len(result.get("file_tree", {}))
        }
        
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Rollback failed - project_id: {project_id}, error: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Rollback failed: {str(e)}"
        )


# ── Project Statistics ─────────────────────────────────────────

@router.get("/stats/summary", response_model=ProjectStats)
async def get_project_stats(current_user=Depends(get_current_user)):
    """Get summary statistics for user's projects."""
    stats = await project_service.get_user_project_stats(str(current_user.id))
    return stats


@router.get("/{project_id}/stats")
async def get_single_project_stats(
    project_id: str,
    current_user=Depends(get_current_user)
):
    """Get detailed statistics for a single project."""
    stats = await project_service.get_project_detailed_stats(project_id, str(current_user.id))
    
    if stats is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    return stats


# ── Project Files ─────────────────────────────────────────────

@router.get("/{project_id}/files")
async def list_project_files(
    project_id: str,
    current_user=Depends(get_current_user),
    path: Optional[str] = Query(None, description="Filter by directory path")
):
    """List files in a project, optionally filtered by path."""
    files = await project_service.get_project_files(project_id, str(current_user.id), path)
    
    if files is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    return {"files": files}


@router.get("/{project_id}/files/{file_path:path}")
async def get_file_content(
    project_id: str,
    file_path: str,
    current_user=Depends(get_current_user),
    version: Optional[int] = Query(None, description="Version to retrieve")
):
    """Get content of a specific file in the project."""
    content = await project_service.get_file_content(
        project_id=project_id,
        user_id=str(current_user.id),
        file_path=file_path,
        version=version
    )
    
    if content is None:
        raise HTTPException(status_code=404, detail="File not found.")
    
    return {
        "content": content, 
        "path": file_path, 
        "version": version or "latest"
    }