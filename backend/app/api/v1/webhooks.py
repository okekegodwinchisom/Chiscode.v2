"""
ChisCode — Webhooks Router (Phase 7 upgrade)
=============================================
Drop-in replacement for backend/app/api/v1/webhooks.py

Changes from Phase 1 version:
  - BILLING_ISSUE now calls billing_service.flag_billing_issue()
    instead of just logging
  - RENEWAL now also calls billing_service.clear_billing_flag()
    to unblock accounts recovered from billing issues
  - INITIAL_PURCHASE registers user with RevenueCat and resets
    daily request counter in Redis
  - UNCANCELLATION event handled (user re-subscribes before period end)
"""
import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import users_collection
from app.services import user_service
from app.services.billing_service import clear_billing_flag, flag_billing_issue

logger = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# ── RevenueCat product_id → ChisCode plan ─────────────────────
_PLAN_MAP = {
    "chiscode_basic_monthly": "basic",
    "basic_monthly":          "basic",
    "basic":                  "basic",
    "chiscode_pro_monthly":   "pro",
    "pro_monthly":            "pro",
    "pro":                    "pro",
    "chiscode_yearly":        "yearly",
    "yearly":                 "yearly",
}


def _verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 verification of RevenueCat webhook payload."""
    if not settings.revenuecat_webhook_secret or not signature:
        return not settings.is_production    # allow in dev without secret

    expected = hmac.new(
        settings.revenuecat_webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/revenuecat", status_code=status.HTTP_200_OK)
async def revenuecat_webhook(
    request: Request,
    x_revenuecat_signature: str | None = Header(default=None),
):
    """
    Handle RevenueCat subscription lifecycle events.

    Event → Action:
      INITIAL_PURCHASE  → upgrade plan, clear billing flag, register RC customer
      RENEWAL           → confirm plan active, clear billing flag
      UNCANCELLATION    → re-activate plan (user cancelled then re-subscribed)
      CANCELLATION      → log only (plan stays active until EXPIRATION)
      EXPIRATION        → downgrade to free
      BILLING_ISSUE     → flag account, block future generations
      SUBSCRIBER_ALIAS  → update RC customer ID in MongoDB
    """
    body = await request.body()

    if not _verify_signature(body, x_revenuecat_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        )

    event      = payload.get("event", {})
    event_type = event.get("type", "")
    user_id    = event.get("app_user_id", "")    # MongoDB _id as string
    product_id = event.get("product_id", "")
    period_end = event.get("expiration_at_ms")   # epoch ms

    logger.info(
        "RevenueCat webhook received",
        event_type=event_type,
        user_id=user_id,
        product_id=product_id,
    )

    try:
        # ── INITIAL_PURCHASE ──────────────────────────────────
        if event_type == "INITIAL_PURCHASE":
            plan = _PLAN_MAP.get(product_id, "basic")
            await user_service.update_user_plan(user_id, plan)
            await clear_billing_flag(user_id)
            # Register with RC (idempotent — safe to call again)
            doc = await users_collection().find_one({"_id": __import__("bson").ObjectId(user_id)}, {"email": 1})
            if doc:
                from app.services.billing_service import register_rc_customer
                await register_rc_customer(user_id, doc.get("email", ""))
            logger.info("Plan purchased", user_id=user_id, plan=plan)

        # ── RENEWAL ───────────────────────────────────────────
        elif event_type == "RENEWAL":
            plan = _PLAN_MAP.get(product_id, "basic")
            await user_service.update_user_plan(user_id, plan)
            await clear_billing_flag(user_id)   # recover from any billing issue
            logger.info("Plan renewed", user_id=user_id, plan=plan)

        # ── UNCANCELLATION (re-subscribe before period end) ───
        elif event_type == "UNCANCELLATION":
            plan = _PLAN_MAP.get(product_id, "basic")
            await user_service.update_user_plan(user_id, plan)
            logger.info("Subscription reinstated", user_id=user_id, plan=plan)

        # ── CANCELLATION (stays active until period end) ──────
        elif event_type == "CANCELLATION":
            logger.info(
                "Subscription cancelled — active until period end",
                user_id=user_id,
                period_end_ms=period_end,
            )

        # ── EXPIRATION (period ended → downgrade) ─────────────
        elif event_type == "EXPIRATION":
            await user_service.update_user_plan(user_id, "free")
            logger.info("Plan expired — downgraded to free", user_id=user_id)

        # ── BILLING_ISSUE ─────────────────────────────────────
        elif event_type == "BILLING_ISSUE":
            await flag_billing_issue(user_id)
            logger.warning("Billing issue — account flagged", user_id=user_id)

        # ── SUBSCRIBER_ALIAS ──────────────────────────────────
        elif event_type == "SUBSCRIBER_ALIAS":
            new_id = event.get("new_app_user_id", "")
            if new_id:
                await users_collection().update_one(
                    {"_id": __import__("bson").ObjectId(user_id)},
                    {"$set": {"revenuecat_customer_id": new_id}},
                )
                logger.info("RC subscriber alias updated", user_id=user_id, new_rc_id=new_id)

        else:
            logger.debug("Unhandled RevenueCat event", event_type=event_type)

    except user_service.UserNotFoundError:
        # Return 200 to stop RevenueCat retrying for unknown users
        logger.error("Webhook: user not found", user_id=user_id)
        return {"status": "ok", "note": "user not found"}
    except Exception as exc:
        logger.error("Webhook processing error", error=str(exc), event_type=event_type)
        # Return 500 so RevenueCat retries for transient failures
        raise HTTPException(status_code=500, detail="Webhook processing failed.")

    return {"status": "ok"}
