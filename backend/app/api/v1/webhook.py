"""
ChisCode — Webhook Routes
RevenueCat subscription webhook handler.
"""
import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import users_collection
from app.services import user_service

logger = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# RevenueCat event type → ChisCode plan mapping
_PLAN_MAP = {
    "basic_monthly": "basic",
    "basic": "basic",
    "pro_monthly": "pro",
    "pro": "pro",
    "yearly": "yearly",
    "chiscode_yearly": "yearly",
}


def _verify_revenuecat_signature(body: bytes, signature: str | None) -> bool:
    """Verify the RevenueCat webhook signature (HMAC-SHA256)."""
    if not settings.revenuecat_webhook_secret or not signature:
        # Skip verification in development if secret not configured
        return not settings.is_production

    mac = hmac.new(
        key=settings.revenuecat_webhook_secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    )
    return hmac.compare_digest(mac.hexdigest(), signature)


@router.post("/revenuecat", status_code=status.HTTP_200_OK)
async def revenuecat_webhook(
    request: Request,
    x_revenuecat_signature: str | None = Header(default=None),
):
    """
    Handle RevenueCat subscription lifecycle events.
    
    Supported events:
    - INITIAL_PURCHASE → upgrade plan
    - RENEWAL → confirm plan
    - CANCELLATION → schedule downgrade (handled at EXPIRATION)
    - EXPIRATION → downgrade to free
    - BILLING_ISSUE → flag account
    """
    body = await request.body()

    if not _verify_revenuecat_signature(body, x_revenuecat_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON.")

    event = payload.get("event", {})
    event_type = event.get("type", "")
    app_user_id = event.get("app_user_id", "")   # This should be our MongoDB user ID
    product_id = event.get("product_id", "")

    logger.info(
        "RevenueCat webhook received",
        event_type=event_type,
        user_id=app_user_id,
        product_id=product_id,
    )

    try:
        if event_type in ("INITIAL_PURCHASE", "RENEWAL"):
            plan = _PLAN_MAP.get(product_id, "basic")
            await user_service.update_user_plan(app_user_id, plan)
            logger.info("Plan upgraded", user_id=app_user_id, plan=plan)

        elif event_type == "EXPIRATION":
            await user_service.update_user_plan(app_user_id, "free")
            logger.info("Plan expired → downgraded to free", user_id=app_user_id)

        elif event_type == "CANCELLATION":
            # Don't downgrade immediately — wait for EXPIRATION at period end
            logger.info("Subscription cancelled (active until period end)", user_id=app_user_id)

        elif event_type == "BILLING_ISSUE":
            # TODO: Send email notification via email service
            logger.warning("Billing issue detected", user_id=app_user_id)

        else:
            logger.debug("Unhandled RevenueCat event type", event_type=event_type)

    except user_service.UserNotFoundError:
        logger.error("RevenueCat webhook: user not found", user_id=app_user_id)
        # Return 200 to prevent RevenueCat from retrying indefinitely
        return {"status": "ok", "note": "user not found"}

    return {"status": "ok"}