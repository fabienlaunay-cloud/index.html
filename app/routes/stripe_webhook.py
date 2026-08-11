import os
import stripe
from fastapi import APIRouter, Request, HTTPException
from app.db import get_db
from app.logger import log

router = APIRouter(prefix="/api/stripe", tags=["stripe"])

# Amount in cents → plan slug
# Covers both monthly-only links and setup+first-month links
_AMOUNT_TO_PLAN: dict[int, str] = {
    # Monthly recurring links (mensualité seule)
    9900:   "maintenance",
    39000:  "starter",
    79000:  "business",
    149000: "scale",
    # Setup + first month links (premier paiement)
    88000:  "starter",    # 490 setup + 390
    178000: "business",   # 990 setup + 790
    348000: "scale",      # 1990 setup + 1490
    118800: "maintenance", # 12 × 99 annuel
}


def _set_user_plan(email: str, plan: str) -> bool:
    """Update plan in DB; also reset quota_alert_sent for the new period.

    Activates the account at the same time: a self-service signup lands in DB
    with is_active = 0 / signup_status = 'pending_payment', and this is the
    only place that opens the door.
    """
    conn = None
    try:
        conn = get_db()
        result = conn.execute(
            "UPDATE users SET plan = ?, quota_alert_sent = 0, quota_full_period = '', "
            "is_active = 1, signup_status = 'active' WHERE email = ?",
            (plan, email.lower().strip()),
        )
        rows = result.rowcount
        conn.commit()  # sans commit, close() annule la transaction
        return rows > 0
    except Exception as e:
        log.error(f"[stripe] DB update failed for {email}: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _was_pending(email: str) -> bool:
    """True si le compte attendait son paiement — sert à n'envoyer le mail de
    bienvenue qu'à la première activation, pas à chaque renouvellement."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT signup_status FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
        conn.close()
        return bool(row) and (dict(row).get("signup_status") or "active") == "pending_payment"
    except Exception:
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
        meta = session.get("metadata") or {}

        email = (
            meta.get("synqio_email")
            or session.get("client_reference_id")
            or session.get("customer_email")
            or (session.get("customer_details") or {}).get("email")
            or ""
        ).lower().strip()

        if not email:
            log.warning("[stripe] checkout.session.completed — no customer email")
            return {"status": "ignored", "reason": "no email"}

        # Tunnel d'inscription : le plan voyage dans les métadonnées de la
        # session. Les anciens liens de paiement Stripe n'en ont pas — on
        # retombe alors sur la correspondance par montant.
        amount = session.get("amount_total", 0)
        plan = meta.get("synqio_plan") or _AMOUNT_TO_PLAN.get(amount)

        if not plan:
            log.warning(f"[stripe] unknown amount {amount} for {email}")
            return {"status": "ignored", "reason": f"unknown amount {amount}"}

        first_activation = _was_pending(email)
        updated = _set_user_plan(email, plan)
        if updated:
            log.info(f"[stripe] plan updated → {email} = {plan}"
                     f"{' (activation)' if first_activation else ''}")
        else:
            log.warning(f"[stripe] user not found in DB: {email}")

        try:
            import asyncio
            from app.services.email import send_admin_new_payment, send_welcome
            amount_eur = (session.get("amount_total", 0) or 0) // 100
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, send_admin_new_payment, email, plan, amount_eur, updated)
            if updated and first_activation:
                # Confirmation de création de compte — l'accès vient de s'ouvrir
                loop.run_in_executor(None, send_welcome, email)
        except Exception:
            pass

        return {"status": "ok", "email": email, "plan": plan,
                "activated": bool(updated and first_activation)}

    return {"status": "ignored", "event": event["type"]}
