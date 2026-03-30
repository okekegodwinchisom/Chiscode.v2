"""
ChisCode — Deploy & Preview API Router (Phase 6)
=================================================
POST /api/v1/projects/{id}/deploy          — start deployment (SSE stream)
POST /api/v1/projects/{id}/preview         — generate/refresh preview
GET  /api/v1/preview/{id}                  — serve live preview HTML
GET  /api/v1/projects/{id}/preview/card    — get preview card data
GET  /api/v1/projects/{id}/deploy/configs  — get all generated config files
GET  /api/v1/projects/{id}/preview/live    — get live preview URL
"""
from __future__ import annotations

from typing import Optional
import json
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.logging import get_logger
from app.db.mongodb import get_db, projects_collection
from app.schemas.user import UserInDB
from app.services.deployment_service import DeployConfig, deploy_project
from app.services.preview_service import (
    generate_preview,
    get_preview_card,
    get_preview_html,
)

logger = get_logger(__name__)
router = APIRouter(tags=["deploy"])


# ── Deploy request ─────────────────────────────────────────────

class DeployRequest(BaseModel):
    platform:      str
    vercel_token:  Optional[str] = None
    netlify_token: Optional[str] = None
    render_token:  Optional[str] = None
    cf_api_token:  Optional[str] = None
    cf_account_id: Optional[str] = None


# ── Endpoints ──────────────────────────────────────────────────

@router.post("/projects/{project_id}/deploy")
async def deploy_endpoint(
    project_id:   str,
    req:          DeployRequest,
    current_user: UserInDB = Depends(get_current_user),
):
    """Stream deployment progress as SSE."""
    from bson import ObjectId

    doc = await projects_collection().find_one({
        "_id":     ObjectId(project_id),
        "user_id": current_user.id,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    if doc.get("status") != "complete":
        raise HTTPException(
            status_code=400,
            detail="Project must be complete before deploying.",
        )

    cfg = DeployConfig(
        platform=req.platform,
        project_name=doc.get("name", "chiscode-project"),
        project_id=project_id,
        user_id=str(current_user.id),
        stack=doc.get("stack", {}),
        file_tree=doc.get("file_tree", {}),
        vercel_token=req.vercel_token,
        netlify_token=req.netlify_token,
        render_token=req.render_token,
        cf_api_token=req.cf_api_token,
        cf_account_id=req.cf_account_id,
        github_token=doc.get("github_token_encrypted"),
        github_username=current_user.github_username,
    )

    async def stream():
        await projects_collection().update_one(
            {"_id": ObjectId(project_id)},
            {"$push": {"deploy_log": {
                "platform":   req.platform,
                "started_at": __import__("datetime").datetime.utcnow().isoformat(),
            }}},
        )

        config_files: dict[str, str] = {}

        async for event in deploy_project(cfg):
            if event.get("event") == "config_ready":
                config_files.update(event.get("config_files", {}))
                if config_files:
                    merged_tree = {**doc.get("file_tree", {}), **config_files}
                    await projects_collection().update_one(
                        {"_id": ObjectId(project_id)},
                        {"$set": {"file_tree": merged_tree}},
                    )

            if event.get("event") == "deploy_done" and event.get("url"):
                await projects_collection().update_one(
                    {"_id": ObjectId(project_id)},
                    {"$set": {f"deploy_urls.{req.platform}": event["url"]}},
                )

            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control":     "no-cache",
        },
    )


@router.post("/projects/{project_id}/preview")
async def create_preview(
    project_id:   str,
    current_user: UserInDB = Depends(get_current_user),
):
    """
    Generate a live preview for the project using Modal sandbox.
    Falls back to static preview if sandbox creation fails.
    """
    from bson import ObjectId
    from app.core.config import settings
    from app.services.modal_service import ModalService
    modal_svc = ModalService()

    doc = await projects_collection().find_one({
        "_id":     ObjectId(project_id),
        "user_id": current_user.id,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")

    file_tree    = doc.get("file_tree", {})
    stack        = doc.get("stack", {})
    project_name = doc.get("name", "app")

    # ── Try Modal sandbox first ─────────────────────────────────────
    try:
        sandbox   = await modal_svc.create_sandbox(...)
        )
        
        # Save live URL and sandbox ID to project
        await projects_collection().update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {
                "preview_url":          sandbox["preview_url"],
                "modal_sandbox_id":     sandbox["sandbox_id"],
                "preview_type":         "live",
                "preview_updated_at":   __import__("datetime").datetime.utcnow().isoformat(),
            }},
        )
        
        logger.info(
            "Modal sandbox preview created",
            project_id=project_id,
            sandbox_id=sandbox["sandbox_id"],
            preview_url=sandbox["preview_url"]
        )
        
        return {
            "type":         "live",
            "preview_url":  sandbox["preview_url"],
            "sandbox_id":   sandbox["sandbox_id"],
            "port":         sandbox["port"],
        }

    except Exception as exc:
        logger.warning(
            "Modal sandbox failed — falling back to static preview",
            project_id=project_id,
            error=str(exc)
        )

    # ── Fallback to static preview ────────────────────────────
    info = await generate_preview(
        project_id=project_id,
        file_tree=file_tree,
        stack=stack,
        project_name=project_name,
        base_url=settings.frontend_base_url,
    )
    
    # Store that we're using static preview
    await projects_collection().update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "preview_type": "static",
            "preview_updated_at": __import__("datetime").datetime.utcnow().isoformat(),
        }},
    )
    
    return info.model_dump()


@router.get("/preview/{project_id}", response_class=HTMLResponse)
async def serve_preview(project_id: str):
    """
    Serve the live preview HTML.
    No auth required — iframe needs direct access.
    Auto-generates preview if not yet stored.
    """
    from bson import ObjectId
    
    html = await get_preview_html(project_id)

    # Auto-generate if not found in MongoDB
    if not html:
        try:
            doc = await projects_collection().find_one(
                {"_id": ObjectId(project_id)}
            )
            if doc:
                logger.info("Auto-generating preview", project_id=project_id)
                await generate_preview(
                    project_id=project_id,
                    file_tree=doc.get("file_tree", {}),
                    stack=doc.get("stack", {}),
                    project_name=doc.get("name", ""),
                )
                html = await get_preview_html(project_id)
        except Exception as exc:
            logger.warning("Auto-preview generation failed", error=str(exc))

    if not html:
        return HTMLResponse(
            content="""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="background:#07090f;color:#6b7f95;font-family:monospace;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
  <div style="text-align:center">
    <div style="font-size:2rem;margin-bottom:1rem">📭</div>
    <p>Preview not available.</p>
    <p style="font-size:.8rem">Click "Refresh Preview" to generate one.</p>
  </div>
</body>
</html>""",
            status_code=404,
        )

    return HTMLResponse(
        content=html,
        headers={
            # Permissive CSP — allows inline scripts/styles and CDN resources
            # frame-ancestors * allows embedding in any iframe (needed for HF Spaces)
            "Content-Security-Policy": (
                "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
                "frame-ancestors *;"
            ),
        },
    )


@router.get("/projects/{project_id}/preview/card")
async def get_card(
    project_id:   str,
    current_user: UserInDB = Depends(get_current_user),
):
    """Get preview card data for non-HTML projects."""
    from bson import ObjectId
    
    card = await get_preview_card(project_id)
    if not card:
        doc = await projects_collection().find_one(
            {"_id": ObjectId(project_id), "user_id": current_user.id}
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Project not found.")
        info = await generate_preview(
            project_id=project_id,
            file_tree=doc.get("file_tree", {}),
            stack=doc.get("stack", {}),
            project_name=doc.get("name", ""),
        )
        card = info.card_data if hasattr(info, 'card_data') else None

    return card or {}


@router.get("/projects/{project_id}/deploy/configs")
async def get_deploy_configs(
    project_id:   str,
    current_user: UserInDB = Depends(get_current_user),
):
    """Return all platform config files generated for this project."""
    from bson import ObjectId

    doc = await projects_collection().find_one({
        "_id":     ObjectId(project_id),
        "user_id": current_user.id,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")

    file_tree    = doc.get("file_tree", {})
    config_names = (
        "vercel.json", "netlify.toml", "render.yaml",
        "fly.toml", ".cloudflare", "Dockerfile",
    )
    return {
        "configs": {
            k: v for k, v in file_tree.items()
            if any(c in k for c in config_names)
        },
        "deploy_urls": doc.get("deploy_urls", {}),
    }


@router.get("/projects/{project_id}/preview/live")
async def get_live_preview_url(
    project_id:   str,
    current_user: UserInDB = Depends(get_current_user),
):
    """
    Return the live Modal preview URL if sandbox is still running,
    otherwise fall back to the static HTML preview.
    """
    from bson import ObjectId
    from app.services.modal_service import ModalService
    modal_svc = ModalService()

    doc = await projects_collection().find_one({
        "_id":     ObjectId(project_id),
        "user_id": current_user.id,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")

    # Check if Modal sandbox is still alive
    modal_url          = doc.get("preview_url", "")
    modal_sandbox_id   = doc.get("modal_sandbox_id", "")

    if modal_url and modal_sandbox_id:
        try:
            status = await modal_svc.get_sandbox_status(modal_sandbox_id)
            if status.get("status") in ("running", "started"):
                return {
                    "url":  modal_url,
                    "type": "live",
                    "sandbox_id": modal_sandbox_id
                }
            else:
                logger.info(
                    "Modal sandbox no longer running",
                    project_id=project_id,
                    sandbox_id=modal_sandbox_id,
                    status=status.get("status")
                )
        except Exception as exc:
            logger.warning(
                "Failed to check Modal sandbox status",
                project_id=project_id,
                error=str(exc)
            )

    # Fall back to static preview
    return {
        "url":   f"/api/v1/preview/{project_id}",
        "type":  "static",
    }


@router.post("/projects/{project_id}/preview/refresh")
async def refresh_preview(
    project_id:   str,
    current_user: UserInDB = Depends(get_current_user),
):
    """
    Force refresh the preview by destroying the existing sandbox
    and creating a new one.
    """
    from bson import ObjectId
    
    doc = await projects_collection().find_one({
        "_id":     ObjectId(project_id),
        "user_id": current_user.id,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    # Clean up existing sandbox if present
    existing_sandbox_id = doc.get("modal_sandbox_id")
    if existing_sandbox_id:
        try:
            await sandbox_service.destroy_sandbox(existing_sandbox_id)
            logger.info("Destroyed existing sandbox for refresh", sandbox_id=existing_sandbox_id)
        except Exception as exc:
            logger.warning("Failed to destroy existing sandbox", error=str(exc))
    
    # Create new preview
    return await create_preview(project_id, current_user)


@router.delete("/projects/{project_id}/preview/sandbox")
async def destroy_preview_sandbox(
    project_id:   str,
    current_user: UserInDB = Depends(get_current_user),
):
    """
    Manually destroy the Modal sandbox for a project.
    Useful for cleanup or when preview is no longer needed.
    """
    from bson import ObjectId
    
    doc = await projects_collection().find_one({
        "_id":     ObjectId(project_id),
        "user_id": current_user.id,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    sandbox_id = doc.get("modal_sandbox_id")
    if not sandbox_id:
        return {"message": "No sandbox found for this project"}
    
    try:
        await sandbox_service.destroy_sandbox(sandbox_id)
        
        # Clear sandbox ID from project
        await projects_collection().update_one(
            {"_id": ObjectId(project_id)},
            {"$unset": {"modal_sandbox_id": "", "preview_url": ""}}
        )
        
        return {"message": "Sandbox destroyed successfully", "sandbox_id": sandbox_id}
        
    except Exception as exc:
        logger.error("Failed to destroy sandbox", sandbox_id=sandbox_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to destroy sandbox: {exc}")