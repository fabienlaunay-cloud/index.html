import os
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from app.services.auth import (
    authenticate_user, create_token, verify_token,
    create_user, list_users, delete_user, toggle_user, is_admin,
)
from app.db import get_db

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


class ToggleUserRequest(BaseModel):
    active: bool


# ── Helper : vérifie que le token JWT appartient à un admin ──────────────────

def _require_admin(authorization: str = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token manquant")
    email = verify_token(authorization.split(" ", 1)[1])
    if not email:
        raise HTTPException(401, "Session expirée")
    if not is_admin(email):
        raise HTTPException(403, "Accès réservé aux administrateurs")
    return email


# ── Auth endpoints ────────────────────────────────────────────────────────────

@router.get("/needs-setup")
async def needs_setup():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return {"needs_setup": count == 0}


@router.post("/setup")
async def setup_first_user(req: CreateUserRequest):
    """Crée le premier compte admin — uniquement si la base est vide."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    if count > 0:
        raise HTTPException(403, "Setup déjà effectué")
    user = create_user(req.email, req.password, req.name, is_admin=True)
    token = create_token(req.email)
    return {"token": token, "email": user["email"], "name": user["name"], "is_admin": True}


@router.post("/login")
async def login(req: LoginRequest):
    user = authenticate_user(req.email, req.password)
    if not user:
        raise HTTPException(401, "Email ou mot de passe incorrect")
    token = create_token(req.email)
    return {
        "token": token,
        "email": user["email"],
        "name": user.get("name", ""),
        "is_admin": bool(user.get("is_admin", 0)),
    }


@router.get("/me")
async def me(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token manquant")
    email = verify_token(authorization.split(" ", 1)[1])
    if not email:
        raise HTTPException(401, "Token invalide ou expiré")
    return {"email": email, "is_admin": is_admin(email)}


# ── Admin endpoints (JWT admin requis, pas de secret séparé) ─────────────────

@admin_router.get("/users")
async def get_users(authorization: str = Header(None)):
    _require_admin(authorization)
    return list_users()


@admin_router.post("/users")
async def add_user(req: CreateUserRequest, authorization: str = Header(None)):
    _require_admin(authorization)
    try:
        user = create_user(req.email, req.password, req.name)
        return {"status": "created", **user}
    except ValueError as e:
        raise HTTPException(400, str(e))


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
