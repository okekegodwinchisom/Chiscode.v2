# app/api/v1/preview.py

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

from app.api.deps import get_current_user
from app.schemas.user import UserInDB
from app.services.e2b_service import E2BSandboxService
from app.services.fragments_templates import detect_template, generate_fragments_code
from app.services.preview_service import generate_static_preview
from app.db.mongodb import projects_collection
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["preview"])

# Initialize E2B service
e2b_service = E2BSandboxService()


class PreviewResponse(BaseModel):
    type: str  # "live" or "static"
    preview_url: Optional[str] = None
    sandbox_id: Optional[str] = None
    screenshot: Optional[str] = None
    expires_at: Optional[str] = None
    message: Optional[str] = None


@router.post("/projects/{project_id}/preview", response_model=PreviewResponse)
async def create_preview(
    project_id: str,
    current_user: UserInDB = Depends(get_current_user)
):
    """
    Generate a live preview for a project using E2B sandboxes.
    Falls back to static preview if sandbox creation fails.
    """
    from bson import ObjectId
    
    # Get project
    doc = await projects_collection().find_one({
        "_id": ObjectId(project_id),
        "user_id": current_user.id
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")
    
    file_tree = doc.get("file_tree", {})
    stack = doc.get("stack", {})
    project_name = doc.get("name", "app")
    
    # Detect template (Fragments-inspired)
    template = detect_template(file_tree, stack)
    logger.info(
        "Preview template detected",
        project_id=project_id,
        template=template.name,
        port=template.port
    )
    
    # Generate Fragments-compatible structure
    fragments_data = generate_fragments_code(file_tree, template)
    
    # Try E2B sandbox first
    try:
        sandbox = await e2b_service.create_sandbox(
            project_id=project_id,
            project_name=project_name,
            file_tree=file_tree,
            stack=stack
        )
        
        # Capture screenshot (optional)
        screenshot = await e2b_service.capture_screenshot(sandbox["preview_url"])
        
        # Save to database
        await projects_collection().update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {
                "preview_url": sandbox["preview_url"],
                "e2b_sandbox_id": sandbox["sandbox_id"],
                "preview_type": "live",
                "preview_expires_at": sandbox["expires_at"],
                "preview_screenshot": screenshot,
                "preview_template": template.name
            }}
        )
        
        return PreviewResponse(
            type="live",
            preview_url=sandbox["preview_url"],
            sandbox_id=sandbox["sandbox_id"],
            screenshot=screenshot,
            expires_at=sandbox["expires_at"]
        )
        
    except Exception as e:
        logger.warning(
            "E2B sandbox failed, falling back to static preview",
            project_id=project_id,
            error=str(e)
        )
        
        # Fallback to static preview
        preview_info = await generate_static_preview(
            project_id=project_id,
            file_tree=file_tree,
            stack=stack,
            project_name=project_name
        )
        
        await projects_collection().update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {
                "preview_type": "static",
                "preview_data": preview_info
            }}
        )
        
        return PreviewResponse(
            type="static",
            message="Live preview unavailable. Showing static preview instead.",
            preview_url=preview_info.get("url")
        )


@router.get("/projects/{project_id}/preview/status")
async def get_preview_status(
    project_id: str,
    current_user: UserInDB = Depends(get_current_user)
):
    """Check if a live preview sandbox is still running."""
    from bson import ObjectId
    
    doc = await projects_collection().find_one({
        "_id": ObjectId(project_id),
        "user_id": current_user.id
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")
    
    sandbox_id = doc.get("e2b_sandbox_id")
    if not sandbox_id:
        return {"status": "not_available", "type": doc.get("preview_type", "static")}
    
    status = await e2b_service.get_sandbox_status(sandbox_id)
    
    return {
        "status": status["status"],
        "sandbox_id": sandbox_id,
        "preview_url": doc.get("preview_url"),
        "expires_at": doc.get("preview_expires_at")
    }


@router.delete("/projects/{project_id}/preview")
async def destroy_preview(
    project_id: str,
    current_user: UserInDB = Depends(get_current_user)
):
    """Destroy the live preview sandbox."""
    from bson import ObjectId
    
    doc = await projects_collection().find_one({
        "_id": ObjectId(project_id),
        "user_id": current_user.id
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")
    
    sandbox_id = doc.get("e2b_sandbox_id")
    if sandbox_id:
        await e2b_service.destroy_sandbox(sandbox_id)
        
        await projects_collection().update_one(
            {"_id": ObjectId(project_id)},
            {"$unset": {"e2b_sandbox_id": "", "preview_url": "", "preview_expires_at": ""}}
        )
        
        return {"message": "Preview sandbox destroyed", "sandbox_id": sandbox_id}
    
    return {"message": "No active preview sandbox found"}


@router.get("/preview/templates")
async def list_preview_templates():
    """List all available preview templates (Fragments-inspired)."""
    from app.services.fragments_templates import PREVIEW_TEMPLATES
    
    return {
        "templates": [
            {
                "name": t.name,
                "language": t.language,
                "port": t.port,
                "file_requirements": t.file_requirements
            }
            for t in PREVIEW_TEMPLATES
        ]
    }