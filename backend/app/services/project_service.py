"""
ChisCode — Project Service
Business logic for project management, generation, and version control.
Production-ready with comprehensive error handling and validation.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple
from bson import ObjectId
import asyncio
import json
import uuid

from app.core.logging import get_logger
from app.core.config import settings
from app.db.mongodb import projects_collection, project_versions_collection
from app.schemas.project import (
    ProjectInDB,
    ProjectPublic,
    ProjectDetail,
    ProjectStatus,
    ProjectUpdate,
    ProjectVersion,
    ProjectVersionPublic,
    TechStack,
    ProjectSpec,
    GenerateProjectRequest
)
from app.services import user_service
from app.websocket.manager import ws_manager
from app.db import redis_client

logger = get_logger(__name__)


# ── Custom Exceptions ────────────────────────────────────────

class ProjectNotFoundError(Exception):
    """Raised when project is not found."""
    pass


class ProjectAccessDeniedError(Exception):
    """Raised when user doesn't have access to project."""
    pass


class ProjectLimitExceededError(Exception):
    """Raised when user exceeds project limits."""
    pass


class GenerationFailedError(Exception):
    """Raised when code generation fails."""
    pass


class InvalidProjectDataError(Exception):
    """Raised when project data is invalid."""
    pass


# ── CRUD Operations ──────────────────────────────────────────

async def get_user_projects(
    user_id: str,
    skip: int = 0,
    limit: int = 20,
    status_filter: Optional[str] = None
) -> List[ProjectPublic]:
    """Get all projects for a user with optional filtering."""
    try:
        coll = projects_collection()
        
        # Validate inputs
        skip = max(0, skip)
        limit = min(100, max(1, limit))  # Cap at 100
        
        # Build query
        query = {"user_id": user_id}
        if status_filter:
            query["status"] = status_filter
        
        # Execute query with projection to reduce data transfer
        cursor = coll.find(
            query,
            {
                "_id": 1,
                "user_id": 1,
                "name": 1,
                "description": 1,
                "status": 1,
                "file_tree": 1,
                "created_at": 1,
                "updated_at": 1,
                "current_version": 1
            }
        ).sort("created_at", -1).skip(skip).limit(limit)
        
        projects = []
        async for doc in cursor:
            try:
                project = ProjectInDB(**doc)
                projects.append(ProjectPublic(
                    **project.model_dump(by_alias=True),
                    file_count=len(project.file_tree) if project.file_tree else 0
                ))
            except Exception as e:
                logger.warning(f"Failed to parse project {doc.get('_id')}: {e}")
                continue
        
        return projects
        
    except Exception as e:
        logger.error(f"Failed to get user projects: {e}")
        return []


async def get_project(project_id: str, user_id: str) -> Optional[ProjectInDB]:
    """Get a project by ID, ensuring user ownership."""
    try:
        # Validate project_id
        if not ObjectId.is_valid(project_id):
            return None
        
        coll = projects_collection()
        doc = await coll.find_one({
            "_id": ObjectId(project_id),
            "user_id": user_id
        })
        
        if doc is None:
            return None
        
        return ProjectInDB(**doc)
        
    except Exception as e:
        logger.error(f"Failed to get project {project_id}: {e}")
        return None


async def create_project(
    user_id: str,
    name: str,
    description: Optional[str] = None,
    original_prompt: Optional[str] = None
) -> ProjectInDB:
    """Create a new empty project."""
    try:
        # Validate inputs
        if not name or len(name) < 1:
            raise InvalidProjectDataError("Project name is required")
        if len(name) > 100:
            raise InvalidProjectDataError("Project name too long (max 100 characters)")
        
        coll = projects_collection()
        
        now = datetime.now(timezone.utc)
        project_doc = {
            "user_id": user_id,
            "name": name.strip(),
            "description": (description or "").strip()[:500],  # Limit description length
            "original_prompt": (original_prompt or "").strip()[:1000],
            "status": "pending",
            "file_tree": {},
            "generation_log": [],
            "self_heal_attempts": 0,
            "current_version": 0,
            "views": 0,
            "created_at": now,
            "updated_at": now
        }
        
        result = await coll.insert_one(project_doc)
        project_doc["_id"] = result.inserted_id
        
        logger.info(f"Project created - project_id: {str(result.inserted_id)}, user_id: {user_id}")
        
        return ProjectInDB(**project_doc)
        
    except InvalidProjectDataError:
        raise
    except Exception as e:
        logger.error(f"Failed to create project: {e}")
        raise


async def update_project(
    project_id: str,
    user_id: str,
    update_data: ProjectUpdate
) -> Optional[ProjectInDB]:
    """Update project metadata."""
    try:
        # Validate project_id
        if not ObjectId.is_valid(project_id):
            return None
        
        coll = projects_collection()
        
        # Prepare update data
        update_dict = update_data.model_dump(exclude_unset=True)
        if not update_dict:
            return await get_project(project_id, user_id)
        
        # Validate and clean data
        if "name" in update_dict:
            name = update_dict["name"].strip()
            if len(name) < 1 or len(name) > 100:
                raise InvalidProjectDataError("Invalid project name")
            update_dict["name"] = name
        
        if "description" in update_dict:
            update_dict["description"] = update_dict["description"].strip()[:500]
        
        update_dict["updated_at"] = datetime.now(timezone.utc)
        
        # Execute update
        result = await coll.find_one_and_update(
            {"_id": ObjectId(project_id), "user_id": user_id},
            {"$set": update_dict},
            return_document=True
        )
        
        if result is None:
            return None
        
        logger.info(f"Project updated - project_id: {project_id}, user_id: {user_id}")
        
        return ProjectInDB(**result)
        
    except InvalidProjectDataError:
        raise
    except Exception as e:
        logger.error(f"Failed to update project {project_id}: {e}")
        return None


async def delete_project(project_id: str, user_id: str) -> bool:
    """Delete a project and all its versions."""
    try:
        # Validate project_id
        if not ObjectId.is_valid(project_id):
            return False
        
        coll = projects_collection()
        versions_coll = project_versions_collection()
        
        # Delete project
        result = await coll.delete_one({
            "_id": ObjectId(project_id),
            "user_id": user_id
        })
        
        if result.deleted_count > 0:
            # Also delete all versions
            try:
                await versions_coll.delete_many({"project_id": project_id})
            except Exception as e:
                logger.warning(f"Failed to delete versions for project {project_id}: {e}")
            
            logger.info(f"Project deleted - project_id: {project_id}, user_id: {user_id}")
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Failed to delete project {project_id}: {e}")
        return False


# ── Generation Operations ────────────────────────────────────

async def check_project_limit(user_id: str) -> bool:
    """Check if user has reached their project limit."""
    try:
        # Get user's plan
        user = await user_service.get_user_by_id(user_id)
        if user is None:
            raise ProjectAccessDeniedError("User not found")
        
        # Define limits per plan
        limits = {
            "free": 5,
            "basic": 20,
            "pro": 100,
            "yearly": 1000
        }
        
        limit = limits.get(user.plan, 5)
        
        # Count current projects
        coll = projects_collection()
        count = await coll.count_documents({"user_id": user_id})
        
        if count >= limit:
            raise ProjectLimitExceededError(
                f"You've reached the limit of {limit} projects on your {user.plan} plan. "
                "Upgrade or delete existing projects to create more."
            )
        
        return True
        
    except ProjectLimitExceededError:
        raise
    except Exception as e:
        logger.error(f"Failed to check project limit: {e}")
        return True  # Allow on error


async def check_rate_limit(user_id: str) -> bool:
    """Check if user has reached their daily generation limit."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        
        # Get user's plan
        user = await user_service.get_user_by_id(user_id)
        if user is None:
            return True  # Allow on error
        
        # Define rate limits per plan
        limits = {
            "free": 10,
            "basic": 50,
            "pro": 200,
            "yearly": 1000
        }
        
        limit = limits.get(user.plan, 10)
        
        # Get current usage from Redis
        key = f"gen_rate_limit:{user_id}:{today}"
        
        if redis_client.is_connected():
            current = await redis_client.get_current_usage(user_id, today)
            
            if current >= limit:
                raise ProjectLimitExceededError(
                    f"You've reached your daily generation limit of {limit}. "
                    "Please try again tomorrow or upgrade your plan."
                )
            
            # Increment counter
            await redis_client.cache_set(key, str(current + 1), ttl=86400)
        
        return True
        
    except ProjectLimitExceededError:
        raise
    except Exception as e:
        logger.warning(f"Failed to check rate limit (allowing): {e}")
        return True


async def start_generation(
    user_id: str,
    prompt: str,
    project_name: Optional[str] = None,
    preferred_stack: Optional[TechStack] = None
) -> str:
    """Start generating a new project from a prompt."""
    try:
        # Validate prompt
        if not prompt or len(prompt.strip()) < 10:
            raise InvalidProjectDataError("Prompt must be at least 10 characters")
        if len(prompt) > 5000:
            raise InvalidProjectDataError("Prompt too long (max 5000 characters)")
        
        # Create project entry
        project = await create_project(
            user_id=user_id,
            name=project_name or f"Project-{uuid.uuid4().hex[:8]}",
            description=prompt[:200],
            original_prompt=prompt
        )
        
        # Store stack preference if provided
        if preferred_stack:
            coll = projects_collection()
            await coll.update_one(
                {"_id": ObjectId(project.id)},
                {"$set": {"stack": preferred_stack.model_dump()}}
            )
        
        # Send initial WebSocket update
        try:
            await ws_manager.send_status(
                project.id,
                "pending",
                "Project created. Initializing generation..."
            )
        except Exception as e:
            logger.warning(f"Failed to send WebSocket update: {e}")
        
        return project.id
        
    except (InvalidProjectDataError, ProjectLimitExceededError):
        raise
    except Exception as e:
        logger.error(f"Failed to start generation: {e}")
        raise GenerationFailedError(f"Failed to start generation: {str(e)}")


async def run_generation_agent(
    project_id: str,
    user_id: str,
    prompt: str,
    preferred_stack: Optional[TechStack] = None
):
    """
    Background task for project generation.
    This simulates the AI generation process with real-time WebSocket updates.
    
    TODO: Integrate with actual LangGraph agent for code generation
    """
    coll = projects_collection()
    log = []
    
    try:
        # Validate inputs
        if not ObjectId.is_valid(project_id):
            raise GenerationFailedError("Invalid project ID")
        
        # Update status to analyzing
        await _update_project_status(project_id, "analyzing", "Analyzing requirements...")
        log.append("Analyzing requirements...")
        
        # Simulate analysis phase
        await asyncio.sleep(2)
        
        # Generate project specification
        spec = await _generate_project_spec(prompt)
        await coll.update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {"spec": spec.model_dump()}}
        )
        
        try:
            await ws_manager.send_log(project_id, "✅ Requirements analyzed", "success")
        except:
            pass
        
        # Update status to generating
        await _update_project_status(project_id, "generating", "Generating code...")
        
        # Simulate file generation
        files = await _generate_project_files(spec)
        total_files = len(files)
        
        if total_files == 0:
            raise GenerationFailedError("No files generated")
        
        # Limit file count to prevent abuse
        if total_files > 100:
            logger.warning(f"Generated too many files ({total_files}), truncating")
            files = dict(list(files.items())[:100])
            total_files = 100
        
        for i, (path, content) in enumerate(files.items()):
            # Validate file path
            if len(path) > 500:
                logger.warning(f"File path too long, skipping: {path[:50]}...")
                continue
            
            # Limit file size
            if len(content) > 1_000_000:  # 1MB limit
                logger.warning(f"File too large, truncating: {path}")
                content = content[:1_000_000] + "\n... (truncated)"
            
            # Update file tree incrementally
            await coll.update_one(
                {"_id": ObjectId(project_id)},
                {"$set": {f"file_tree.{path}": content}}
            )
            
            # Send progress update
            percentage = int(((i + 1) / total_files) * 100)
            
            try:
                await ws_manager.send_progress(project_id, percentage, f"Generating {path}")
                await ws_manager.send_log(project_id, f"📄 Generated {path}", "info")
            except:
                pass
            
            # Small delay to simulate work
            await asyncio.sleep(0.5)
        
        # Quality check phase
        await _update_project_status(project_id, "quality_check", "Running quality checks...")
        await asyncio.sleep(1)
        
        # Self-healing phase (if needed)
        issues = await _run_quality_checks(files)
        if issues:
            await _update_project_status(project_id, "self_healing", "Fixing issues...")
            try:
                await ws_manager.send_log(project_id, f"🔧 Found {len(issues)} issues, fixing...", "warn")
            except:
                pass
            await asyncio.sleep(1)
            files = await _fix_issues(files, issues)
        
        # Awaiting confirmation
        await _update_project_status(
            project_id,
            "awaiting_confirmation",
            "Generation complete! Please review and confirm."
        )
        
        # Create initial version
        await _create_project_version(project_id, user_id, files, "Initial generation")
        
        # Send completion notification
        try:
            await ws_manager.send_complete(project_id, "✅ Project generated successfully!")
        except:
            pass
        
        logger.info(f"Generation completed - project_id: {project_id}, user_id: {user_id}, files: {len(files)}")
        
    except GenerationFailedError:
        raise
    except Exception as e:
        logger.error(f"Generation failed - project_id: {project_id}, error: {str(e)}")
        await _update_project_status(project_id, "failed", f"Generation failed: {str(e)}")
        try:
            await ws_manager.send_error(project_id, str(e))
        except:
            pass
        raise GenerationFailedError(str(e))


async def _update_project_status(project_id: str, status: str, message: str):
    """Update project status and send WebSocket update."""
    try:
        coll = projects_collection()
        await coll.update_one(
            {"_id": ObjectId(project_id)},
            {
                "$set": {
                    "status": status,
                    "updated_at": datetime.now(timezone.utc)
                },
                "$push": {
                    "generation_log": {
                        "$each": [message],
                        "$slice": -100  # Keep only last 100 log entries
                    }
                }
            }
        )
        
        try:
            await ws_manager.send_status(project_id, status, message)
            await ws_manager.send_log(project_id, message)
        except Exception as e:
            logger.debug(f"WebSocket update failed: {e}")
            
    except Exception as e:
        logger.error(f"Failed to update project status: {e}")


async def _generate_project_spec(prompt: str) -> ProjectSpec:
    """
    Generate project specification from prompt.
    
    TODO: Integrate with LLM for actual spec generation
    Currently returns a placeholder implementation.
    """
    # Placeholder implementation
    app_name = prompt[:30].replace(" ", "_")
    
    return ProjectSpec(
        app_type="web",
        app_name=app_name,
        description=prompt[:100],
        features=["user authentication", "database", "api"],
        auth_required=True,
        database_needed=True,
        api_needed=True,
        mobile_responsive=True,
        complexity="moderate"
    )


async def _generate_project_files(spec: ProjectSpec) -> Dict[str, str]:
    """
    Generate project files based on specification.
    
    TODO: Integrate with code generation model (LangGraph agent)
    Currently returns placeholder files.
    """
    # Placeholder implementation
    files = {
        "README.md": f"# {spec.app_name}\n\n{spec.description}\n\n## Features\n" + 
                     "\n".join(f"- {f}" for f in spec.features),
        "requirements.txt": "fastapi>=0.104.0\nuvicorn>=0.24.0\npydantic>=2.0.0",
        "main.py": """from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Hello World"}
""",
        "config.py": """# Configuration
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-here")
"""
    }
    
    return files


async def _run_quality_checks(files: Dict[str, str]) -> List[str]:
    """
    Run quality checks on generated files.
    
    TODO: Implement actual quality checks (syntax validation, linting, etc.)
    """
    issues = []
    
    # Basic checks
    if not files:
        issues.append("No files generated")
    
    # Check for required files
    if "README.md" not in files:
        issues.append("Missing README.md")
    
    return issues


async def _fix_issues(files: Dict[str, str], issues: List[str]) -> Dict[str, str]:
    """
    Fix issues in generated files.
    
    TODO: Implement auto-fixing using LLM
    """
    # For now, just return files as-is
    # In product