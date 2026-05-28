import os
import secrets
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse

from app.db import get_db

router = APIRouter(prefix="/api/amazon")

AMAZON_APP_ID = os.getenv("AMAZON_APP_ID", "")
LWA_CLIENT_ID = os.getenv("LWA_CLIENT_ID", "")
LWA_CLIENT_SECRET = os.getenv("LWA_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("AMAZON_REDIRECT_URI", "https://synqio.io/api/amazon/callback")


@router.get("/connect")
async def amazon_connect(request: Request):
    """Retourne l'URL d'autorisation Amazon (le JS gère la redirection)."""
    email = request.state.user_email
    state = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO amazon_oauth_states (state, user_email) VALUES (?, ?)",
        (state, email),
    )
    conn.commit()
    conn.close()
    auth_url = (
        f"https://sellercentral.amazon.fr/apps/authorize/consent"
        f"?application_id={AMAZON_APP_ID}"
        f"&state={state}"
        f"&version=beta"
    )
    return {"url": auth_url}


@router.get("/callback")
async def amazon_callback(
    spapi_oauth_code: str = None,
    state: str = None,
    selling_partner_id: str = None,
):
    """Reçoit le code OAuth Amazon, échange contre un refresh_token, stocke en DB."""
    if not spapi_oauth_code or not state:
        raise HTTPException(400, "Paramètres OAuth manquants")

    conn = get_db()
    row = conn.execute(
        "SELECT user_email FROM amazon_oauth_states WHERE state = ?", (state,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(400, "State OAuth invalide ou expiré")

    email = row["user_email"]
    conn.execute("DELETE FROM amazon_oauth_states WHERE state = ?", (state,))
    conn.commit()
    conn.close()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "authorization_code",
                "code": spapi_oauth_code,
                "redirect_uri": REDIRECT_URI,
                "client_id": LWA_CLIENT_ID,
                "client_secret": LWA_CLIENT_SECRET,
            },
        )
        if not resp.is_success:
            raise HTTPException(502, f"Erreur LWA: {resp.text}")
        tokens = resp.json()

    conn = get_db()
    conn.execute(
        """
        INSERT INTO amazon_credentials (user_email, seller_id, refresh_token)
        VALUES (?, ?, ?)
        ON CONFLICT(user_email) DO UPDATE SET
            seller_id = excluded.seller_id,
            refresh_token = excluded.refresh_token,
            updated_at = CURRENT_TIMESTAMP
        """,
        (email, selling_partner_id or "", tokens["refresh_token"]),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/?amazon=connected")


@router.get("/status")
async def amazon_status(request: Request):
    """Vérifie si l'utilisateur a connecté son Seller Central."""
    email = request.state.user_email
    conn = get_db()
    row = conn.execute(
        "SELECT seller_id, updated_at FROM amazon_credentials WHERE user_email = ?",
        (email,),
    ).fetchone()
    conn.close()
    if row:
        return {"connected": True, "seller_id": row["seller_id"], "connected_at": row["updated_at"]}
    return {"connected": False}


@router.delete("/disconnect")
async def amazon_disconnect(request: Request):
    """Supprime les credentials Amazon de l'utilisateur."""
    email = request.state.user_email
    conn = get_db()
    conn.execute("DELETE FROM amazon_credentials WHERE user_email = ?", (email,))
    conn.commit()
    conn.close()
    return {"disconnected": True}
