"""
ChisCode — Plan Guard (Phase 7)
=================================
FastAPI dependency functions for plan + entitlement enforcement.
Drop these into any route that needs gating.

Usage examples:

    # Require paid plan to deploy
    @router.post("/projects/{id}/deploy")
    async def deploy(
        ...,
        _: None = Depends(require_feature("deploy")),
    ):

    # Require Pro/Yearly for API key
    @router.post("/users/me/api-key")
    async def gen_key(
        current_user = Depends(require_plan("pro", "yearly")),
    ):

    # Check generation quota (increments counter on success)
    @router.post("/projects/generate")
    async def generate(
        current_user = Depends(require_generation_quota),
    ):
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.db import redis_client
from app.schemas.user import UserInDB
from app.services.billing_service import (
    check_feature_allowed,
    check_generation_allowed,
    get_checkout_url,
)

logger = get_logger(__name__)


# ── Plan gate ──────────────────────────────────────────────────

def require_plan(*plans: str):
    """
    Dependency factory: require the user to be on one of the listed plans.

        current_user = Depends(require_plan("pro", "yearly"))
    """
    async def _check(
        current_user: UserInDB = Depends(get_current_user),
    ) -> UserInDB:
        if current_user.plan not in plans:
            plan_list   = " or ".join(p.capitalize() for p in plans)
            checkout_url = get_checkout_url(plans[0]) if plans else None
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error":        "plan_required",
                    "message":      f"This feature requires {plan_list} plan.",
                    "current_plan": current_user.plan,
                    "upgrade_url":  checkout_url,
                },
            )
        return current_user
    return _check


# ── Feature gate ───────────────────────────────────────────────

def require_feature(feature: str):
    """
    Dependency factory: gate on a named feature entitlement.
    Features: 'api_key', 'deploy', 'priority'

        _: None = Depends(require_feature("deploy"))
    """
    _feature_plan_map = {
        "api_key":  ("pro", "yearly"),
        "deploy":   ("basic", "pro", "yearly"),
        "priority": ("pro", "yearly"),
    }

    async def _check(
        current_user: UserInDB = Depends(get_current_user),
    ) -> UserInDB:
        if not check_feature_allowed(current_user.plan, feature):
            required_plans = _feature_plan_map.get(feature, ())
            upgrade_to     = required_plans[0] if required_plans else None
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error":        "feature_not_available",
                    "feature":      feature,
                    "message":      f"'{feature}' is not available on your current plan.",
                    "current_plan": current_user.plan,
                    "upgrade_url":  get_checkout_url(upgrade_to) if upgrade_to else None,
                },
            )
        return current_user
    return _check


# ── Generation quota ───────────────────────────────────────────

async def require_generation_quota(
    current_user: UserInDB = Depends(get_current_user),
) -> UserInDB:
    """
    Dependency: verify the user has remaining generation quota.
    Does NOT increment the counter — the generation endpoint does that
    after confirming the job was actually kicked off.

    To increment after a successful kick-off:
        await redis_client.increment_usage(str(user.id), date.today().isoformat())
    """
    allowed, reason = await check_generation_allowed(
        str(current_user.id), current_user.plan
    )
    if not allowed:
        limit       = settings.get_rate_limit(current_user.plan)
        upgrade_url = get_checkout_url(
            {"free": "basic", "basic": "pro", "pro": "yearly"}.get(current_user.plan)
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error":        "quota_exceeded",
                "message":      reason,
                "daily_limit":  limit,
                "current_plan": current_user.plan,
                "upgrade_url":  upgrade_url,
            },
            headers={"Retry-After": "86400"},
        )
    return current_user


# ── Billing flag check ─────────────────────────────────────────

async def require_no_billing_issue(
    current_user: UserInDB = Depends(get_current_user),
) -> UserInDB:
    """
    Block access for accounts with unresolved billing issues.
    Use on any revenue-generating endpoint.
    """
    from bson import ObjectId
    from app.db.mongodb import users_collection

    doc = await users_collection().find_one(
        {"_id": ObjectId(current_user.id)}, {"billing_flagged": 1}
    )
    if doc and doc.get("billing_flagged"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error":   "billing_issue",
                "message": "Your account has a billing issue. Please update your payment method.",
                "portal_url": "/billing/portal",
            },
        )
    return current_user
    