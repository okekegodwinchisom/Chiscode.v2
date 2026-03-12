"""
ChisCode — Billing API Router (Phase 7)
========================================
GET  /billing/usage          — current usage + plan summary
GET  /billing/plans          — all plan metadata + checkout URLs
GET  /billing/customer       — RC customer info + management URL
GET  /billing/checkout/{plan}— redirect to RevenueCat checkout
POST /billing/cancel         — open RC portal for cancellation
GET  /billing/entitlements   — list of enabled feature flags for current user
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app.api.deps import get_current_user
from app.core.logging import get_logger
from app.schemas.user import UserInDB
from app.services.billing_service import (
    PLAN_META,
    CustomerInfo,
    UsageSummary,
    check_feature_allowed,
    get_checkout_url,
    get_customer_info,
    get_usage_summary,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


# ── Usage ──────────────────────────────────────────────────────

@router.get("/usage", response_model=UsageSummary)
async def get_usage(current_user: UserInDB = Depends(get_current_user)):
    """Current generation usage vs. plan limit, with plan metadata."""
    return await get_usage_summary(str(current_user.id), current_user.plan)


# ── Plans ──────────────────────────────────────────────────────

@router.get("/plans")
async def get_plans(current_user: UserInDB = Depends(get_current_user)):
    """
    All plan tiers with features, pricing, and checkout URLs.
    Used to render the pricing/upgrade page.
    """
    plans = []
    for plan_key, meta in PLAN_META.items():
        plans.append({
            "id":           plan_key,
            "name":         meta["name"],
            "price":        meta["price"],
            "daily_limit":  meta["daily_limit"],
            "features":     meta["features"],
            "missing":      meta["missing"],
            "checkout_url": get_checkout_url(plan_key),
            "is_current":   current_user.plan == plan_key,
            "is_upgrade":   _is_upgrade(current_user.plan, plan_key),
        })
    return {"plans": plans, "current_plan": current_user.plan}


# ── Customer portal ────────────────────────────────────────────

@router.get("/customer", response_model=CustomerInfo)
async def get_customer(current_user: UserInDB = Depends(get_current_user)):
    """
    Full billing customer info: RC ID, management URL, entitlements.
    Frontend uses management_url to link to RevenueCat self-service portal.
    """
    return await get_customer_info(str(current_user.id))


# ── Checkout redirect ──────────────────────────────────────────

@router.get("/checkout/{plan}")
async def checkout_redirect(
    plan:         str,
    current_user: UserInDB = Depends(get_current_user),
):
    """
    Redirect to RevenueCat checkout for the chosen plan.
    RC checkout URL should include ?app_user_id={user_id} for attribution.
    """
    valid_plans = ("basic", "pro", "yearly")
    if plan not in valid_plans:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Choose from: {valid_plans}")

    # Prevent downgrade via checkout
    if not _is_upgrade(current_user.plan, plan):
        raise HTTPException(
            status_code=400,
            detail="Use the customer portal to manage or cancel your current subscription.",
        )

    base_url = get_checkout_url(plan)
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail="Checkout not configured. Contact support.",
        )

    # Append user ID for RevenueCat attribution
    sep      = "&" if "?" in base_url else "?"
    full_url = f"{base_url}{sep}app_user_id={current_user.id}"

    return RedirectResponse(url=full_url, status_code=302)


# ── Cancel / portal ────────────────────────────────────────────

@router.post("/portal")
async def open_portal(current_user: UserInDB = Depends(get_current_user)):
    """
    Return the RevenueCat self-service management URL for the current user.
    Frontend opens this in a new tab for subscription management / cancellation.
    """
    info = await get_customer_info(str(current_user.id))
    if not info.management_url:
        return {
            "url":     None,
            "message": "No active subscription found.",
        }
    return {
        "url":     info.management_url,
        "message": "Redirecting to subscription management portal.",
    }


# ── Entitlements ───────────────────────────────────────────────

@router.get("/entitlements")
async def get_entitlements(current_user: UserInDB = Depends(get_current_user)):
    """
    Feature flags for the current user's plan.
    Frontend uses this to show/hide gated UI elements.
    """
    plan = current_user.plan
    return {
        "plan": plan,
        "entitlements": {
            "api_key":    check_feature_allowed(plan, "api_key"),
            "deploy":     check_feature_allowed(plan, "deploy"),
            "priority":   check_feature_allowed(plan, "priority"),
            "rag":        True,   # all plans get RAG
            "templates":  True,   # all plans get templates
            "github":     True,   # all plans get GitHub push
        },
    }


# ── Helper ─────────────────────────────────────────────────────

def _is_upgrade(current: str, target: str) -> bool:
    order = ["free", "basic", "pro", "yearly"]
    try:
        return order.index(target) > order.index(current)
    except ValueError:
        return False
