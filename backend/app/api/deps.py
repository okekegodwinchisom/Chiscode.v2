"""
ChisCode — FastAPI Dependencies
Reusable dependency functions for auth, rate limiting, and database access.
"""
from datetime import date

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from jose import JWTError

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decode_token
from app.db import redis_client
from app.schemas.user import UserInDB
from app.services import user_service

logger = get_logger(__name__)


# ── Auth Dependencies ─────────────────────────────────────────

async def get_current_user_from_jwt(
    access_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> UserInDB:
    """
    Resolve the current user from a JWT cookie or Authorization header.
    Checks the token blacklist (logged-out tokens).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Prefer cookie, fall back to Authorization header
    token = access_token
    if not token and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise credentials_exception

    if not token:
        raise credentials_exception

    try:
        payload = decode_token(token)
        user_id: str = payload.get("sub", "")
        jti: str = payload.get("jti", "")
        if not user_id or not jti:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Check token blacklist
    if await redis_client.is_token_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please log in again.",
        )

    try:
        user = await user_service.get_user_by_id(user_id)
    except user_service.UserNotFoundError:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated.",
        )

    return user


async def get_current_user_from_api_key(
    x_chiscode_api_key: str | None = Header(default=None, alias="X-ChisCode-API-Key"),
) -> UserInDB | None:
    """Resolve user from an API key header (Pro/Yearly plans only)."""
    if not x_chiscode_api_key:
        return None
    user = await user_service.get_user_by_api_key(x_chiscode_api_key)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )
    if user.plan not in ("pro", "yearly"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key access requires Pro or Yearly plan.",
        )
    return user


async def get_current_user(
    jwt_user: UserInDB | None = Depends(get_current_user_from_jwt),
    api_key_user: UserInDB | None = Depends(get_current_user_from_api_key),
) -> UserInDB:
    """
    Master auth dependency — accepts either JWT cookie/header OR API key.
    Use this on all protected endpoints.
    """
    user = api_key_user or jwt_user
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return user


# ── Rate Limiting ─────────────────────────────────────────────

async def check_rate_limit(
    current_user: UserInDB = Depends(get_current_user),
) -> UserInDB:
    """
    Check and increment the user's daily request counter.
    Raises 429 if the limit is exceeded.
    """
    today = date.today().isoformat()
    daily_limit = settings.get_rate_limit(current_user.plan)

    allowed, count, limit = await redis_client.check_and_increment_rate_limit(
        user_id=str(current_user.id),
        daily_limit=daily_limit,
        date_str=today,
    )

    if not allowed:
        plan_display = current_user.plan.capitalize()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily limit of {limit} requests reached for {plan_display} plan. "
                "Resets at midnight UTC."
            ),
            headers={"Retry-After": "86400", "X-RateLimit-Limit": str(limit)},
        )

    logger.debug(
        "Rate limit check passed",
        user_id=str(current_user.id),
        plan=current_user.plan,
        count=count,
        limit=limit,
    )
    return current_user


# ── Plan Guards ───────────────────────────────────────────────

def require_plan(*plans: str):
    """Dependency factory — require the user to be on one of the given plans."""
    async def _check(current_user: UserInDB = Depends(get_current_user)) -> UserInDB:
        if current_user.plan not in plans:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires one of these plans: {', '.join(plans)}.",
            )
        return current_user
    return _check