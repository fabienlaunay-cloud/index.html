import os
import stripe
from fastapi import APIRouter, Request, HTTPException
from app.db import get_db
from app.logger import log

router = APIRouter(prefix="/api/stripe", tags=["stripe"])

# Monthly price (cents) → plan slug
_AMOUNT_TO_PLAN: dict[int, str] = {
    9900:   "maintenance",
    39000:  "starter",
    79000:  "business",
    149000: "scale",
}


def _set_user_plan(email: str, plan: str) -> bool:
    """Update plan in DB; also reset quota_alert_sent for the new period."""
    try:
        conn = get_db()
        result = conn.execute(
            "UPDATE users SET plan = ?, quota_alert_sent = 0 WHERE email = ?",
            (plan, email.lower().strip()),
        )
        conn.close()
        return result.rowcount > 0
    except Exception as e:
        log.error(f"[stripe] DB update failed for {email}: {e}")
        return False


@router.post("/webhook")
async def stripe_webhook(request: Request):
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid Stripe signature")
    except Exception as e:
        raise HTTPException(400, f"Webhook error: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        # Only handle subscription checkouts
        if session.get("mode") != "subscription":
            return {"status": "ignored", "reason": "not a subscription"}

        email = (
            session.get("customer_email")
            or (session.get("customer_details") or {}).get("email")
            or ""
        ).lower().strip()

        if not email:
            log.warning("[stripe] checkout.session.completed — no customer email")
            return {"status": "ignored", "reason": "no email"}

        amount = session.get("amount_total", 0)
        plan = _AMOUNT_TO_PLAN.get(amount)

        if not plan:
            log.warning(f"[stripe] unknown amount {amount} for {email}")
            return {"status": "ignored", "reason": f"unknown amount {amount}"}

        updated = _set_user_plan(email, plan)
        if updated:
            log.info(f"[stripe] plan updated → {email} = {plan}")
        else:
            log.warning(f"[stripe] user not found in DB: {email}")

        return {"status": "ok", "email": email, "plan": plan}

    return {"status": "ignored", "event": event["type"]}
