"""
ChisCode — API Router
Aggregates all v1 route modules.
"""
from fastapi import APIRouter

from app.api.v1 import auth, project, users, webhook

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(project.router)
api_router.include_router(webhook.router)