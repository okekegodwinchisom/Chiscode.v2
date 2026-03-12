"""
ChisCode — Billing Service (Phase 7)
======================================
All payment-related business logic:
  - RevenueCat REST API calls (fetch customer, manage subscriptions)
  - Plan enforcement helpers
  - Entitlement checks
  - Billing issue tracking

The webhook handler (webhooks.py) writes plan changes to MongoDB.
This service handles everything else billing-related.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from bson import ObjectId
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import users_collection
from app.db import redis_client

logger = get_logger(__name__)

# ── RevenueCat REST API base ───────────────────────────────────
_RC_BASE = "https://api.revenuecat.com/v1"

# ── Plan → RevenueCat product ID map (must match RC dashboard) ─
PLAN_PRODUCT_MAP: dict[str, str] = {
    "basic":  "chiscode_basic_monthly",
    "pro":    "chiscode_pro_monthly",
    "yearly": "chiscode_yearly",
}

# ── Plan display metadata ──────────────────────────────────────
PLAN_META = {
    "free": {
        "name":        "Free",
        "price":       "$0",
        "daily_limit": 5,
        "features": [
            "5 generations/day",
            "Export as ZIP",
            "GitHub push",
            "Community support",
        ],
        "missing": [
            "API key access",
            "One-click deployment",
            "Priority generation",
        ],
    },
    "basic": {
        "name":        "Basic",
        "price":       "$25/mo",
        "daily_limit": 100,
        "features": [
            "100 generations/day",
            "All deployment platforms",
            "Export as ZIP",
            "GitHub push",
            "Email support",
        ],
        "missing": [
            "API key access",
            "Priority generation",
        ],
    },
    "pro": {
        "name":        "Pro",
        "price":       "$120/mo",
        "daily_limit": 1000,
        "features": [
            "1,000 generations/day",
            "API key access",
            "Priority generation queue",
            "All deployment platforms",
            "GitHub push",
            "Priority support",
        ],
        "missing": [],
    },
    "yearly": {
        "name":        "Yearly",
        "price":       "$1,000/yr",
        "daily_limit": 1000,
        "features": [
            "1,000 generations/day",
            "API key access",
            "Priority generation queue",
            "All deployment platforms",
            "GitHub push",
            "Priority support",
            "2 months free vs monthly Pro",
        ],
        "missing": [],
    },
}


# ── Schemas ────────────────────────────────────────────────────

class CustomerInfo(BaseModel):
    user_id:            str
    plan:               str
    revenuecat_id:      Optional[str]
    is_active:          bool
    billing_flagged:    bool
    entitlements:       list[str]
    management_url:     Optional[str]   # RevenueCat subscriber portal URL


class UsageSummary(BaseModel):
    plan:            str
    plan_name:       str
    plan_price:      str
    daily_limit:     int
    used_today:      int
    remaining:       int
    resets_at:       str
    features:        list[str]
    missing:         list[str]
    upgrade_url:     Optional[str]


# ── RevenueCat API helpers ─────────────────────────────────────

def _rc_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.revenuecat_api_key}",
        "Content-Type":  "application/json",
        "X-Platform":    "web",
    }


async def fetch_rc_customer(rc_id: str) -> dict:
    """Fetch subscriber details from RevenueCat REST API."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{_RC_BASE}/subscribers/{rc_id}",
            headers=_rc_headers(),
        )
        r.raise_for_status()
        return r.json().get("subscriber", {})


async def get_rc_management_url(rc_id: str) -> str | None:
    """Get the self-service subscription management URL for a customer."""
    if not settings.revenuecat_api_key or not rc_id:
        return None
    try:
        subscriber = await fetch_rc_customer(rc_id)
        return subscriber.get("management_url")
    except Exception as exc:
        logger.warning("Could not fetch RC management URL", error=str(exc))
        return None


async def register_rc_customer(user_id: str, email: str) -> str | None:
    """
    Create or retrieve a RevenueCat customer for a new user.
    We use the MongoDB user_id as the RC app_user_id for easy correlation.
    Returns the RC customer ID (same as user_id in this setup).
    """
    if not settings.revenuecat_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{_RC_BASE}/subscribers/{user_id}",
                headers=_rc_headers(),
                json={"email": email},
            )
            # 200 = existing, 201 = created — both fine
            if r.status_code in (200, 201):
                await users_collection().update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {"revenuecat_customer_id": user_id,
                              "updated_at": datetime.now(tz=timezone.utc)}},
                )
                logger.info("RC customer registered", user_id=user_id)
                return user_id
    except Exception as exc:
        logger.warning("RC customer registration failed", error=str(exc))
    return None


# ── Plan enforcement ───────────────────────────────────────────

async def check_generation_allowed(user_id: str, plan: str) -> tuple[bool, str]:
    """
    Check if the user is allowed to run a generation.
    Returns (allowed: bool, reason: str).
    Reads today's usage from Redis (same key as rate limiter).
    """
    from datetime import date
    today    = date.today().isoformat()
    used     = await redis_client.get_current_usage(user_id, today)
    limit    = settings.get_rate_limit(plan)

    if used >= limit:
        meta    = PLAN_META.get(plan, {})
        next_pl = _next_plan(plan)
        return False, (
            f"Daily limit reached ({used}/{limit}). "
            f"Resets at midnight UTC. "
            + (f"Upgrade to {next_pl} for more generations." if next_pl else "")
        )

    # Check billing flag
    doc = await users_collection().find_one(
        {"_id": ObjectId(user_id)}, {"billing_flagged": 1}
    )
    if doc and doc.get("billing_flagged"):
        return False, "Your account has a billing issue. Please update your payment method."

    return True, ""


def check_feature_allowed(plan: str, feature: str) -> bool:
    """
    Check if a plan includes a given feature.
    Features: 'api_key', 'deploy', 'priority'
    """
    feature_gates: dict[str, list[str]] = {
        "api_key":  ["pro", "yearly"],
        "deploy":   ["basic", "pro", "yearly"],
        "priority": ["pro", "yearly"],
    }
    return plan in feature_gates.get(feature, [])


def _next_plan(current: str) -> str | None:
    order = ["free", "basic", "pro", "yearly"]
    idx   = order.index(current) if current in order else -1
    return order[idx + 1] if idx < len(order) - 2 else None


# ── Usage summary ──────────────────────────────────────────────

async def get_usage_summary(user_id: str, plan: str) -> UsageSummary:
    """Full usage summary for the billing/account page."""
    from datetime import date, timedelta

    today      = date.today()
    used       = await redis_client.get_current_usage(user_id, today.isoformat())
    limit      = settings.get_rate_limit(plan)
    resets_at  = datetime.combine(
        today + timedelta(days=1), datetime.min.time()
    ).isoformat() + "Z"

    meta       = PLAN_META.get(plan, PLAN_META["free"])
    next_pl    = _next_plan(plan)
    upgrade_url = _checkout_url(next_pl) if next_pl else None

    return UsageSummary(
        plan=plan,
        plan_name=meta["name"],
        plan_price=meta["price"],
        daily_limit=limit,
        used_today=used,
        remaining=max(0, limit - used),
        resets_at=resets_at,
        features=meta["features"],
        missing=meta["missing"],
        upgrade_url=upgrade_url,
    )


# ── Checkout URLs ──────────────────────────────────────────────
# RevenueCat web checkout URLs — configure in RC dashboard,
# then store as HF Secrets: RC_CHECKOUT_BASIC, RC_CHECKOUT_PRO, RC_CHECKOUT_YEARLY

def _checkout_url(plan: str | None) -> str | None:
    if not plan:
        return None
    env_map = {
        "basic":  getattr(settings, "rc_checkout_basic",  None),
        "pro":    getattr(settings, "rc_checkout_pro",    None),
        "yearly": getattr(settings, "rc_checkout_yearly", None),
    }
    return env_map.get(plan)


def get_checkout_url(plan: str) -> str | None:
    return _checkout_url(plan)


# ── Billing flag management ────────────────────────────────────

async def flag_billing_issue(user_id: str) -> None:
    """Mark a user's account as having a billing issue (from webhook)."""
    await users_collection().update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {
            "billing_flagged":    True,
            "billing_flagged_at": datetime.now(tz=timezone.utc),
            "updated_at":         datetime.now(tz=timezone.utc),
        }},
    )
    logger.warning("Billing issue flagged", user_id=user_id)


async def clear_billing_flag(user_id: str) -> None:
    """Clear a billing flag after successful payment (called on RENEWAL)."""
    await users_collection().update_one(
        {"_id": ObjectId(user_id)},
        {"$unset": {"billing_flagged": "", "billing_flagged_at": ""},
         "$set":   {"updated_at": datetime.now(tz=timezone.utc)}},
    )


# ── Customer info (full) ───────────────────────────────────────

async def get_customer_info(user_id: str) -> CustomerInfo:
    doc = await users_collection().find_one({"_id": ObjectId(user_id)})
    if not doc:
        raise ValueError(f"User {user_id} not found")

    plan       = doc.get("plan", "free")
    rc_id      = doc.get("revenuecat_customer_id")
    mgmt_url   = await get_rc_management_url(rc_id) if rc_id else None

    # Build entitlements list
    entitlements: list[str] = []
    if check_feature_allowed(plan, "api_key"):   entitlements.append("api_key")
    if check_feature_allowed(plan, "deploy"):    entitlements.append("deploy")
    if check_feature_allowed(plan, "priority"):  entitlements.append("priority")

    return CustomerInfo(
        user_id=user_id,
        plan=plan,
        revenuecat_id=rc_id,
        is_active=doc.get("is_active", True),
        billing_flagged=doc.get("billing_flagged", False),
        entitlements=entitlements,
        management_url=mgmt_url,
    )
    