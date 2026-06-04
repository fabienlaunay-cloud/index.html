import os
import secrets
import datetime
from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
from app.services.auth import (
    authenticate_user, create_token, verify_token, _decode_token_data,
    create_user, list_users, delete_user, toggle_user, is_admin,
    check_rate_limit, record_failed_attempt, clear_attempts, get_retry_after,
    hash_password,
)
from app.db import get_db, get_config, set_config


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

import re as _re

_PW_RE = _re.compile(r'^(?=.*[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]).{12,}$')


def _validate_password(password: str):
    if not password or not _PW_RE.match(password):
        raise HTTPException(
            400,
            "Le mot de passe doit contenir au moins 12 caractères et un caractère spécial (!@#$%^&*…)"
        )


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
    # Welcome email — non-blocking (fails silently if SMTP not configured)
    try:
        from app.services.email import send_welcome
        import asyncio
        asyncio.get_event_loop().run_in_executor(None, send_welcome, email)
    except Exception:
        pass
    return {"token": jwt_token, "email": email, "is_admin": False}


@router.get("/debug-admin")
async def debug_admin():
    """Diagnostic public — vérifie si des admins existent et si la DB répond."""
    try:
        conn = get_db()
        row = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_admin = 1 AND is_active = 1").fetchone()
        admin_count = row["cnt"] if row else 0
        row2 = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
        total = row2["cnt"] if row2 else 0
        conn.close()
        return {
            "db_ok": True,
            "total_users": total,
            "active_admins": admin_count,
            "admin_email_configured": bool(os.getenv("ADMIN_EMAIL")),
        }
    except Exception as e:
        return {"db_ok": False, "error": str(e)}


class ResetAdminRequest(BaseModel):
    secret: str
    email: str
    new_password: str


@router.post("/reset-admin")
async def reset_admin_password(req: ResetAdminRequest):
    """Reset admin password via ADMIN_RESET_SECRET env var. Disabled when env var is unset."""
    reset_secret = os.getenv("ADMIN_RESET_SECRET", "")
    if not reset_secret:
        raise HTTPException(403, "Reset désactivé — définissez ADMIN_RESET_SECRET dans Railway")
    if req.secret != reset_secret:
        raise HTTPException(403, "Secret invalide")
    _validate_password(req.new_password)
    new_hash = hash_password(req.new_password)
    conn = get_db()
    result = conn.execute(
        "UPDATE users SET password_hash = ?, is_active = 1 WHERE email = ?",
        (new_hash, req.email.lower().strip()),
    )
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        raise HTTPException(404, f"Aucun utilisateur trouvé pour {req.email}")
    return {"status": "ok", "message": f"Mot de passe réinitialisé pour {req.email}"}


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
    return {"email": email, "is_admin": admin}


# ── Admin endpoints (JWT admin requis, pas de secret séparé) ─────────────────

@admin_router.get("/users")
async def get_users(authorization: str = Header(None)):
    _require_admin(authorization)
    return list_users()


@admin_router.post("/users")
async def add_user(req: CreateUserRequest, authorization: str = Header(None)):
    _require_admin(authorization)
    try:
        # Mot de passe temporaire aléatoire — le client définira le sien via le lien
        temp_pw = req.password if req.password else secrets.token_hex(16)
        user = create_user(req.email, temp_pw, req.name)
        invite_token = _make_invite_token(req.email)
        return {"status": "created", "invite_token": invite_token, **user}
    except ValueError as e:
        raise HTTPException(400, str(e))


@admin_router.post("/users/{email}/invite")
async def resend_invite(email: str, authorization: str = Header(None)):
    _require_admin(authorization)
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email.lower(),)).fetchone()
    conn.close()
    if not exists:
        raise HTTPException(404, "Utilisateur introuvable")
    invite_token = _make_invite_token(email)
    return {"invite_token": invite_token}


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


@admin_router.get("/config/debug")
async def debug_app_config(authorization: str = Header(None)):
    """Diagnostic : montre le contenu brut de app_config et le chemin de la DB."""
    _require_admin(authorization)
    from app.db import DB_PATH
    import os as _os
    conn = get_db()
    rows = conn.execute("SELECT key, length(value) as len, updated_at FROM app_config").fetchall()
    conn.close()
    return {
        "db_path": DB_PATH,
        "db_exists": _os.path.isfile(DB_PATH),
        "db_size_bytes": _os.path.getsize(DB_PATH) if _os.path.isfile(DB_PATH) else 0,
        "app_config_rows": [{"key": r["key"], "value_length": r["len"], "updated_at": r["updated_at"]} for r in rows],
    }

@admin_router.post("/config")
async def set_app_config(req: ConfigRequest, authorization: str = Header(None)):
    _require_admin(authorization)
    if req.key not in ALLOWED_CONFIG_KEYS:
        raise HTTPException(400, f"Clé non autorisée : {req.key}")
    set_config(req.key, req.value.strip())
    return {"status": "ok", "key": req.key}
