"""
ChisCode — User Routes
Profile, usage, and API key management endpoints.
"""
from datetime import date, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user, require_plan
from app.core.config import settings
from app.db import redis_client
from app.schemas.user import ApiKeyResponse, UsageResponse, UserPublic
from app.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserPublic)
async def get_profile(current_user=Depends(get_current_user)):
    """Return the currently authenticated user's profile."""
    return UserPublic.model_validate(current_user.model_dump(by_alias=True))


@router.get("/me/usage", response_model=UsageResponse)
async def get_usage(current_user=Depends(get_current_user)):
    """Return today's request usage and plan limits."""
    today = date.today()
    used = await redis_client.get_current_usage(str(current_user.id), today.isoformat())
    daily_limit = settings.get_rate_limit(current_user.plan)

    from datetime import datetime, timedelta
    tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time())

    return UsageResponse(
        plan=current_user.plan,
        daily_limit=daily_limit,
        used_today=used,
        remaining=max(0, daily_limit - used),
        resets_at=tomorrow.isoformat() + "Z",
    )


@router.post("/me/api-key", response_model=ApiKeyResponse)
async def generate_api_key(
    current_user=Depends(require_plan("pro", "yearly")),
):
    """
    Generate a new API key (Pro/Yearly plans only).
    The raw key is returned once — store it securely.
    """
    raw_key = await user_service.generate_user_api_key(str(current_user.id))
    return ApiKeyResponse(api_key=raw_key)


@router.delete("/me/api-key", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    current_user=Depends(require_plan("pro", "yearly")),
):
    """Revoke the current API key."""
    await user_service.revoke_user_api_key(str(current_user.id))