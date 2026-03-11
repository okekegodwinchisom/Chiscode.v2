"""
ChisCode — API Router
Aggregates all v1 route modules.
"""
from fastapi import APIRouter

from app.api.v1 import auth, projects, users, webhooks
from app.api.mcp_server import router as mcp_router

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(webhooks.router)
api_router.include_router(mcp_router)