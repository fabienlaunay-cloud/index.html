import os
import secrets
import hashlib
import datetime
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.services.auth import (
    authenticate_user, create_token, verify_token, _decode_token_data,
    create_user, list_users, delete_user, toggle_user, is_admin,
    check_rate_limit, record_failed_attempt, clear_attempts, get_retry_after,
    hash_password, invalidate_tokens_for,
)
from app.db import get_db, get_config, set_config


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

import re as _re

_PW_RE    = _re.compile(r'^(?=.*[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]).{12,}$')
_EMAIL_RE = _re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _validate_password(password: str):
    if not password or not _PW_RE.match(password):
        raise HTTPException(
            400,
            "Le mot de passe doit contenir au moins 12 caractères et un caractère spécial (!@#$%^&*…)"
        )


def _validate_email(email: str):
    if not email or not _EMAIL_RE.match(email):
        raise HTTPException(400, "Adresse email invalide")


router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])



# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class CreateUserRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class SetupRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    setup_secret: str = ""


class ToggleUserRequest(BaseModel):
    active: bool


class AcceptInviteRequest(BaseModel):
    password: str


# ── Helper : vérifie que le token JWT appartient à un admin ──────────────────

def _require_admin(authorization: str = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token manquant")
    raw = authorization.split(" ", 1)[1]
    from app.services.auth import _decode_token_data
    data = _decode_token_data(raw)
    if not data:
        raise HTTPException(401, "Session expirée")
    email = data.get("sub")
    if not email:
        raise HTTPException(401, "Session expirée")
    # Accept either the JWT adm claim (survives DB wipes) or DB flag
    if not (bool(data.get("adm")) or is_admin(email)):
        raise HTTPException(403, "Accès réservé aux administrateurs")
    return email


# ── Auth endpoints ────────────────────────────────────────────────────────────

@router.get("/needs-setup")
async def needs_setup():
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
    conn.close()
    count = row["cnt"] if row else 0
    return {"needs_setup": count == 0}


@router.post("/setup")
async def setup_first_user(req: SetupRequest):
    """Crée le premier compte admin — uniquement si la base est vide.
    Si SETUP_SECRET est défini dans les variables d'environnement, il est requis."""
    required_secret = os.getenv("SETUP_SECRET", "")
    if required_secret and req.setup_secret != required_secret:
        raise HTTPException(403, "Clé d'installation invalide — définissez SETUP_SECRET dans Railway")
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
    conn.close()
    count = row["cnt"] if row else 0
    if count > 0:
        raise HTTPException(403, "Setup déjà effectué")
    _validate_email(req.email)
    _validate_password(req.password)
    user = create_user(req.email, req.password, req.name, is_admin=True)
    token = create_token(req.email, is_admin=True)
    return {"token": token, "email": user["email"], "name": user["name"], "is_admin": True}


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    ip = _get_ip(request)
    if not check_rate_limit(ip):
        retry = get_retry_after(ip)
        raise HTTPException(
            429,
            f"Trop de tentatives. Réessayez dans {retry // 60}m{retry % 60:02d}s.",
            headers={"Retry-After": str(retry)},
        )
    _validate_email(req.email)
    user = authenticate_user(req.email, req.password)
    if not user:
        record_failed_attempt(ip)
        raise HTTPException(401, "Email ou mot de passe incorrect")
    clear_attempts(ip)
    admin = bool(user.get("is_admin", 0))
    token = create_token(req.email, is_admin=admin)
    return {
        "token": token,
        "email": user["email"],
        "name": user.get("name", ""),
        "is_admin": admin,
    }


@router.post("/register")
async def register(req: LoginRequest, request: Request):
    """Self-service signup — required for sellers arriving from the Amazon
    Appstore listing. Creates a starter account and logs it in."""
    ip = _get_ip(request)
    if not check_rate_limit(ip):
        retry = get_retry_after(ip)
        raise HTTPException(429, f"Trop de tentatives. Réessayez dans {retry // 60}m{retry % 60:02d}s.",
                            headers={"Retry-After": str(retry)})
    _validate_email(req.email)
    if len(req.password or "") < 8:
        raise HTTPException(400, "Mot de passe : 8 caractères minimum")
    email = req.email.strip().lower()
    conn = get_db()
    row = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        conn.close()
        record_failed_attempt(ip)  # slow down account enumeration
        raise HTTPException(409, "Un compte existe déjà avec cet email — connectez-vous")
    from app.services.auth import hash_password
    conn.execute(
        "INSERT INTO users (email, password_hash, name, is_active, is_admin, plan) "
        "VALUES (?, ?, ?, 1, 0, 'starter')",
        (email, hash_password(req.password), ""))
    conn.commit()
    conn.close()
    token = create_token(email, is_admin=False)
    return {"token": token, "email": email, "name": "", "is_admin": False}


def _make_invite_token(email: str) -> str:
    """Génère et stocke un token d'invitation 72h, retourne le token."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() + datetime.timedelta(hours=72)).isoformat()
    conn = get_db()
    # Invalider les anciens tokens non utilisés pour cet email
    conn.execute("UPDATE invitation_tokens SET used = 1 WHERE email = ? AND used = 0", (email.lower(),))
    conn.execute(
        "INSERT INTO invitation_tokens (token, email, expires_at) VALUES (?, ?, ?)",
        (token, email.lower(), expires),
    )
    conn.commit()
    conn.close()
    return token


@router.get("/invite/{token}")
async def get_invite(token: str):
    conn = get_db()
    row = conn.execute(
        "SELECT email, expires_at, used FROM invitation_tokens WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    if not row or int(row["used"] or 0):
        raise HTTPException(404, "Lien invalide ou déjà utilisé")
    try:
        expires = datetime.datetime.fromisoformat(str(row["expires_at"]))
    except Exception:
        raise HTTPException(404, "Lien invalide ou déjà utilisé")
    if datetime.datetime.utcnow() > expires:
        raise HTTPException(410, "Lien expiré (72h max) — demandez un nouvel accès")
    return {"email": row["email"], "valid": True}


@router.post("/invite/{token}/accept")
async def accept_invite(token: str, req: AcceptInviteRequest):
    _validate_password(req.password)
    conn = get_db()
    row = conn.execute(
        "SELECT email, expires_at, used FROM invitation_tokens WHERE token = ?", (token,)
    ).fetchone()
    if not row or int(row["used"] or 0):
        conn.close()
        raise HTTPException(404, "Lien invalide ou déjà utilisé")
    try:
        expires = datetime.datetime.fromisoformat(str(row["expires_at"]))
    except Exception:
        conn.close()
        raise HTTPException(404, "Lien invalide ou déjà utilisé")
    if datetime.datetime.utcnow() > expires:
        conn.close()
        raise HTTPException(410, "Lien expiré")
    email = row["email"]
    new_hash = hash_password(req.password)
    conn.execute(
        "UPDATE users SET password_hash = ?, is_active = 1 WHERE email = ?",
        (new_hash, email),
    )
    conn.execute("UPDATE invitation_tokens SET used = 1 WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    jwt_token = create_token(email)
    # Welcome email — sent 5 min after activation so it lands after the invite email
    try:
        from app.services.email import send_welcome
        import asyncio
        async def _delayed_welcome(addr):
            await asyncio.sleep(300)
            await asyncio.get_event_loop().run_in_executor(None, send_welcome, addr)
        asyncio.ensure_future(_delayed_welcome(email))
    except Exception:
        pass
    return {"token": jwt_token, "email": email, "is_admin": False}




class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Génère un token de reset et envoie l'email — silencieux si l'email n'existe pas."""
    ip = _get_ip(request)
    if not check_rate_limit(ip):
        retry = get_retry_after(ip)
        raise HTTPException(429, f"Trop de tentatives. Réessayez dans {retry // 60}m{retry % 60:02d}s.", headers={"Retry-After": str(retry)})
    _validate_email(req.email)
    record_failed_attempt(ip)  # always count to prevent email enumeration via timing
    email = req.email.lower().strip()
    conn = get_db()
    user = conn.execute("SELECT 1 FROM users WHERE email = ? AND is_active = 1", (email,)).fetchone()
    if user:
        token = secrets.token_urlsafe(32)
        expires = (datetime.datetime.utcnow() + datetime.timedelta(hours=1)).isoformat()
        conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE email = ? AND used = 0", (email,))
        conn.execute(
            "INSERT INTO password_reset_tokens (token, email, expires_at) VALUES (?, ?, ?)",
            (token, email, expires),
        )
        conn.commit()
        conn.close()
        try:
            from app.services.email import send_password_reset
            import asyncio
            app_url = os.getenv("APP_URL", "https://synqio.io").rstrip("/")
            reset_url = f"{app_url}/?reset={token}"
            asyncio.get_event_loop().run_in_executor(None, send_password_reset, email, reset_url)
        except Exception:
            pass
    else:
        conn.close()
    return {"status": "ok", "message": "Si cet email existe, un lien de réinitialisation a été envoyé."}


@router.get("/reset-password/{token}")
async def validate_reset_token(token: str):
    conn = get_db()
    row = conn.execute(
        "SELECT email, expires_at, used FROM password_reset_tokens WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    if not row or int(row["used"] or 0):
        raise HTTPException(404, "Lien invalide ou déjà utilisé")
    try:
        expires = datetime.datetime.fromisoformat(str(row["expires_at"]))
    except Exception:
        raise HTTPException(404, "Lien invalide")
    if datetime.datetime.utcnow() > expires:
        raise HTTPException(410, "Lien expiré (1h max)")
    return {"email": row["email"], "valid": True}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest):
    _validate_password(req.new_password)
    conn = get_db()
    row = conn.execute(
        "SELECT email, expires_at, used FROM password_reset_tokens WHERE token = ?", (req.token,)
    ).fetchone()
    if not row or int(row["used"] or 0):
        conn.close()
        raise HTTPException(404, "Lien invalide ou déjà utilisé")
    try:
        expires = datetime.datetime.fromisoformat(str(row["expires_at"]))
    except Exception:
        conn.close()
        raise HTTPException(404, "Lien invalide")
    if datetime.datetime.utcnow() > expires:
        conn.close()
        raise HTTPException(410, "Lien expiré")
    email = row["email"]
    new_hash = hash_password(req.new_password)
    conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (new_hash, email))
    conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE token = ?", (req.token,))
    conn.commit()
    conn.close()
    token = create_token(email)
    return {"token": token, "email": email}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request, authorization: str = Header(None)):
    ip = _get_ip(request)
    if not check_rate_limit(ip):
        retry = get_retry_after(ip)
        raise HTTPException(429, f"Trop de tentatives. Réessayez dans {retry // 60}m{retry % 60:02d}s.", headers={"Retry-After": str(retry)})
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token manquant")
    token_str = authorization.split(" ", 1)[1]
    email = verify_token(token_str)
    if not email:
        raise HTTPException(401, "Token invalide ou expiré")
    from app.services.auth import verify_password
    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE email = ? AND is_active = 1", (email,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Utilisateur introuvable")
    if not verify_password(req.current_password, row["password_hash"]):
        record_failed_attempt(ip)
        raise HTTPException(400, "Mot de passe actuel incorrect")
    clear_attempts(ip)
    _validate_password(req.new_password)
    new_hash = hash_password(req.new_password)
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (new_hash, email))
    conn.commit()
    conn.close()
    invalidate_tokens_for(email)
    try:
        import asyncio
        from app.services.email import send_password_changed
        asyncio.get_event_loop().run_in_executor(None, send_password_changed, email)
    except Exception:
        pass
    return {"status": "ok"}


@router.get("/me")
async def me(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token manquant")
    token_str = authorization.split(" ", 1)[1]
    email = verify_token(token_str)
    if not email:
        raise HTTPException(401, "Token invalide ou expiré")
    # Read is_admin from JWT claim first (survives DB wipes/redeploys),
    # then confirm against DB so demotions take effect on next login.
    jwt_data = _decode_token_data(token_str) or {}
    admin = bool(jwt_data.get("adm")) or is_admin(email)
    # Admins always have the agency feature (for testing); others per DB flag.
    conn = get_db()
    row = conn.execute("SELECT agency_enabled FROM users WHERE email = ?", (email.lower(),)).fetchone()
    conn.close()
    agency = admin or bool(row and row["agency_enabled"])
    return {"email": email, "is_admin": admin, "agency_enabled": agency}


# ── Admin endpoints (JWT admin requis, pas de secret séparé) ─────────────────

@admin_router.get("/users")
async def get_users(authorization: str = Header(None)):
    _require_admin(authorization)
    return list_users()


def _send_invite_email(email: str, invite_token: str, name: str = ""):
    try:
        from app.services.email import send_invite
        import asyncio
        app_url = os.getenv("APP_URL", "https://synqio.io").rstrip("/")
        invite_url = f"{app_url}/?invite={invite_token}"
        asyncio.get_event_loop().run_in_executor(None, send_invite, email, invite_url, name)
    except Exception:
        pass


@admin_router.post("/users")
async def add_user(req: CreateUserRequest, authorization: str = Header(None)):
    _require_admin(authorization)
    _validate_email(req.email)
    try:
        temp_pw = req.password if req.password else secrets.token_hex(16)
        user = create_user(req.email, temp_pw, req.name)
        invite_token = _make_invite_token(req.email)
        _send_invite_email(req.email, invite_token, req.name)
        return {"status": "created", "invite_token": invite_token, **user}
    except ValueError as e:
        raise HTTPException(400, str(e))


@admin_router.post("/users/{email}/invite")
async def resend_invite(email: str, authorization: str = Header(None)):
    _require_admin(authorization)
    conn = get_db()
    row = conn.execute("SELECT name FROM users WHERE email = ?", (email.lower(),)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Utilisateur introuvable")
    invite_token = _make_invite_token(email)
    _send_invite_email(email, invite_token, row["name"] if row["name"] else "")
    return {"invite_token": invite_token, "email_sent": True}


@admin_router.delete("/users/{email}")
async def remove_user(email: str, authorization: str = Header(None)):
    _require_admin(authorization)
    delete_user(email)
    return {"status": "deleted", "email": email}


@admin_router.patch("/users/{email}")
async def toggle(email: str, req: ToggleUserRequest, authorization: str = Header(None)):
    _require_admin(authorization)
    toggle_user(email, req.active)
    return {"status": "updated", "email": email, "active": req.active}


class UpdatePlanRequest(BaseModel):
    plan: str


class UpdateAdminRequest(BaseModel):
    is_admin: bool


@admin_router.patch("/users/{email}/admin")
async def update_admin_role(email: str, req: UpdateAdminRequest, authorization: str = Header(None)):
    caller = _require_admin(authorization)
    if email.lower() == caller.lower():
        raise HTTPException(400, "Vous ne pouvez pas modifier votre propre rôle admin")
    conn = get_db()
    conn.execute("UPDATE users SET is_admin = ? WHERE email = ?", (1 if req.is_admin else 0, email.lower()))
    conn.commit()
    conn.close()
    return {"status": "updated", "email": email, "is_admin": req.is_admin}


@admin_router.patch("/users/{email}/plan")
async def update_plan(email: str, req: UpdatePlanRequest, authorization: str = Header(None)):
    _require_admin(authorization)
    if req.plan not in ("starter", "business", "scale", "maintenance"):
        raise HTTPException(400, "Plan invalide")
    conn = get_db()
    conn.execute("UPDATE users SET plan = ? WHERE email = ?", (req.plan, email.lower()))
    conn.commit()
    conn.close()
    return {"status": "updated", "email": email, "plan": req.plan}


class UpdateAgencyRequest(BaseModel):
    agency_enabled: bool


@admin_router.patch("/users/{email}/agency")
async def update_agency(email: str, req: UpdateAgencyRequest, authorization: str = Header(None)):
    """Enable/disable the multi-client (agency) workspace feature for a user."""
    _require_admin(authorization)
    conn = get_db()
    conn.execute("UPDATE users SET agency_enabled = ? WHERE email = ?",
                 (1 if req.agency_enabled else 0, email.lower()))
    conn.commit()
    conn.close()
    return {"status": "updated", "email": email, "agency_enabled": req.agency_enabled}



ALLOWED_CONFIG_KEYS = {"AMAZON_APP_ID", "LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "AMAZON_SP_MODE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ROLE_ARN", "AMAZON_REFRESH_TOKEN", "AMAZON_SELLER_ID"}

class ConfigRequest(BaseModel):
    key: str
    value: str


class DirectConnectRequest(BaseModel):
    refresh_token: str
    seller_id: str
    user_email: str


@admin_router.post("/amazon-connect-direct")
async def amazon_connect_direct(req: DirectConnectRequest, authorization: str = Header(None)):
    """Connecte un utilisateur directement avec un refresh token (bypass OAuth)."""
    _require_admin(authorization)
    conn = get_db()
    conn.execute(
        """INSERT INTO amazon_credentials (user_email, seller_id, refresh_token)
           VALUES (?, ?, ?)
           ON CONFLICT(user_email) DO UPDATE SET
               seller_id=excluded.seller_id,
               refresh_token=excluded.refresh_token,
               updated_at=CURRENT_TIMESTAMP""",
        (req.user_email.lower().strip(), req.seller_id.strip(), req.refresh_token.strip()),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Compte Amazon connecté pour {req.user_email}"}

@admin_router.get("/config")
async def get_app_config(authorization: str = Header(None)):
    _require_admin(authorization)
    result = {}
    for k in ALLOWED_CONFIG_KEYS:
        conn = get_db()
        row = conn.execute("SELECT value FROM app_config WHERE key = ?", (k,)).fetchone()
        conn.close()
        db_val = row["value"] if row and row["value"] else None
        env_val = os.getenv(k, "")
        if db_val:
            result[k] = "✅ présent (DB)"
        elif env_val:
            result[k] = "✅ présent (env)"
        else:
            result[k] = "❌ vide"
    return result


@admin_router.post("/config")
async def set_app_config(req: ConfigRequest, authorization: str = Header(None)):
    _require_admin(authorization)
    if req.key not in ALLOWED_CONFIG_KEYS:
        raise HTTPException(400, f"Clé non autorisée : {req.key}")
    set_config(req.key, req.value.strip())
    return {"status": "ok", "key": req.key}


# ── GDPR Unsubscribe ──────────────────────────────────────────────────────────

def _unsubscribe_token(email: str) -> str:
    """Generate the 16-char verification token for unsubscribe links."""
    secret = os.getenv("SMTP_PASS", os.getenv("JWT_SECRET", ""))
    raw = hashlib.sha256(f"{email}{secret}".encode()).hexdigest()
    return raw[:16]


class UnsubscribeRequest(BaseModel):
    email: str
    token: str


@router.post("/unsubscribe")
async def unsubscribe(req: UnsubscribeRequest):
    email = req.email.lower().strip()
    expected = _unsubscribe_token(email)
    if req.token != expected:
        raise HTTPException(400, "Token invalide")
    conn = get_db()
    conn.execute("UPDATE users SET email_unsubscribed = 1 WHERE email = ?", (email,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_page(email: str = "", token: str = ""):
    app_url = os.getenv("APP_URL", "https://synqio.io").rstrip("/")
    if email and token:
        # Process the unsubscribe automatically if params are provided
        email_lower = email.lower().strip()
        expected = _unsubscribe_token(email_lower)
        if token == expected:
            conn = get_db()
            conn.execute("UPDATE users SET email_unsubscribed = 1 WHERE email = ?", (email_lower,))
            conn.commit()
            conn.close()
            message = "Vous avez bien été désinscrit(e) de nos emails."
            success = True
        else:
            message = "Lien invalide. Veuillez réessayer."
            success = False
    else:
        message = "Paramètres manquants."
        success = False

    color = "#16a34a" if success else "#dc2626"
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Désinscription SynqIO</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:60px auto;padding:24px;color:#1f2937;text-align:center">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:28px;margin-bottom:28px">
    <h1 style="color:white;margin:0;font-size:24px;font-weight:800">SynqIO</h1>
  </div>
  <p style="font-size:16px;color:{color};font-weight:600">{message}</p>
  <p style="font-size:14px;color:#6b7280;margin-top:16px">
    Vous pouvez vous réinscrire à tout moment depuis votre espace.
  </p>
  <a href="{app_url}" style="display:inline-block;margin-top:24px;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:12px 28px;border-radius:10px;font-weight:700;text-decoration:none">
    Retour à SynqIO
  </a>
</body></html>""")
