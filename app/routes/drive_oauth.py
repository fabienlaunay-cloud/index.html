"""
Google Drive OAuth 2.0 flow — per-user token management.
Env vars required:
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  APP_URL  (e.g. https://synqio.up.railway.app)
  GOOGLE_API_KEY  (optional, for Drive Picker in frontend)
"""
import os
import secrets
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse

from app.db import get_db, get_drive_token, set_drive_token, delete_drive_token
from app.logger import log

router = APIRouter(prefix="/api/drive/oauth", tags=["drive-oauth"])

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL     = "https://www.googleapis.com/oauth2/v2/userinfo"
SCOPES           = "https://www.googleapis.com/auth/drive.readonly"


def _client_id()     -> str: return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
def _client_secret() -> str: return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
def _redirect_uri()  -> str:
    base = os.environ.get("APP_URL", "").rstrip("/")
    return f"{base}/api/drive/oauth/callback"


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/status")
async def drive_oauth_status(request: Request):
    email = request.state.user_email
    token = get_drive_token(email)
    return {
        "connected": token is not None,
        "drive_email": token["drive_email"] if token else "",
        "client_id": _client_id(),
        "api_key": os.environ.get("GOOGLE_API_KEY", ""),
        "oauth_configured": bool(_client_id() and _client_secret() and os.environ.get("APP_URL")),
    }


# ── Connect ────────────────────────────────────────────────────────────────────

@router.get("/connect")
async def drive_oauth_connect(request: Request):
    if not _client_id() or not _client_secret():
        raise HTTPException(503, "GOOGLE_OAUTH_CLIENT_ID / SECRET non configurés")
    if not os.environ.get("APP_URL"):
        raise HTTPException(503, "APP_URL non configuré")

    email = request.state.user_email
    state = secrets.token_urlsafe(32)

    conn = get_db()
    conn.execute(
        "INSERT INTO google_oauth_states (state, user_email) VALUES (?, ?) "
        "ON CONFLICT(state) DO UPDATE SET user_email=EXCLUDED.user_email",
        (state, email),
    )
    conn.commit()
    conn.close()

    auth_url = (
        f"{GOOGLE_AUTH_URL}"
        f"?client_id={_client_id()}"
        f"&redirect_uri={_redirect_uri()}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&state={state}"
    )
    return {"url": auth_url}


# ── Callback ───────────────────────────────────────────────────────────────────

@router.get("/callback")
async def drive_oauth_callback(code: str = None, state: str = None, error: str = None):
    app_url = os.environ.get("APP_URL", "").rstrip("/")

    if error or not code or not state:
        return RedirectResponse(f"{app_url}/?drive_oauth=error")

    conn = get_db()
    row = conn.execute(
        "SELECT user_email FROM google_oauth_states WHERE state = ?", (state,)
    ).fetchone()
    conn.execute("DELETE FROM google_oauth_states WHERE state = ?", (state,))
    conn.commit()
    conn.close()

    if not row:
        return RedirectResponse(f"{app_url}/?drive_oauth=error")

    user_email = row["user_email"] if hasattr(row, "keys") else row[0]

    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        })
        tokens = resp.json()

    if "refresh_token" not in tokens:
        log.error(f"[drive-oauth] no refresh_token in response for {user_email}: {tokens}")
        return RedirectResponse(f"{app_url}/?drive_oauth=error")

    # Get user's Google email
    drive_email = ""
    try:
        async with httpx.AsyncClient() as client:
            ui = await client.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            drive_email = ui.json().get("email", "")
    except Exception:
        pass

    set_drive_token(user_email, tokens["refresh_token"], drive_email)
    log.info(f"[drive-oauth] {user_email} connected Google Drive ({drive_email})")
    return RedirectResponse(f"{app_url}/?drive_oauth=success")


# ── Disconnect ─────────────────────────────────────────────────────────────────

@router.delete("/disconnect")
async def drive_oauth_disconnect(request: Request):
    email = request.state.user_email
    delete_drive_token(email)
    log.info(f"[drive-oauth] {email} disconnected Google Drive")
    return {"ok": True}


# ── Fresh access token (for Drive Picker) ─────────────────────────────────────

@router.get("/token")
async def drive_oauth_get_token(request: Request):
    email = request.state.user_email
    token_data = get_drive_token(email)
    if not token_data:
        raise HTTPException(401, "Google Drive non connecté")

    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": token_data["refresh_token"],
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
        })
        data = resp.json()

    if "access_token" not in data:
        raise HTTPException(502, "Impossible de rafraîchir le token Google")

    return {"access_token": data["access_token"]}
