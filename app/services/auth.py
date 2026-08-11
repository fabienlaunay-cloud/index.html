import os
import hashlib
import hmac
import base64
import json
import time
import datetime
from collections import defaultdict
from app.db import get_db

_JWT_SECRET_DEFAULT = "change-me-use-a-long-random-string-in-production"
SECRET_KEY = os.getenv("JWT_SECRET", _JWT_SECRET_DEFAULT)
if SECRET_KEY == _JWT_SECRET_DEFAULT:
    import warnings
    warnings.warn(
        "JWT_SECRET is not set — using insecure default. "
        "Set JWT_SECRET env var to a 32+ character random string.",
        stacklevel=1,
    )
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", "24"))

# ── Rate limiting ────────────────────────────────────────────────────────────
_RATE_LIMIT_WINDOW = 300   # 5 minutes
_RATE_LIMIT_MAX = 5        # 5 tentatives max

_login_attempts: dict = defaultdict(list)

# ── Token invalidation after password change ─────────────────────────────────
# Maps email → timestamp of last password change. Tokens issued before that
# timestamp are rejected. Resets on server restart (24h token expiry is fallback).
_pw_changed_at: dict[str, float] = {}


def invalidate_tokens_for(email: str):
    """Invalidate all existing tokens for this email (call after password change)."""
    _pw_changed_at[email.lower()] = time.time()


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _RATE_LIMIT_WINDOW]
    return len(_login_attempts[ip]) < _RATE_LIMIT_MAX


def record_failed_attempt(ip: str):
    _login_attempts[ip].append(time.time())


def clear_attempts(ip: str):
    _login_attempts.pop(ip, None)


def get_retry_after(ip: str) -> int:
    if not _login_attempts.get(ip):
        return 0
    oldest = min(_login_attempts[ip])
    return max(0, int(_RATE_LIMIT_WINDOW - (time.time() - oldest)))


def hash_password(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return base64.b64encode(salt + key).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        decoded = base64.b64decode(stored_hash.encode("utf-8"))
        salt, stored_key = decoded[:32], decoded[32:]
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return key == stored_key
    except Exception:
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def create_token(email: str, is_admin: bool = False) -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(datetime.datetime.utcnow().timestamp())
    exp = now + TOKEN_EXPIRE_HOURS * 3600
    claims: dict = {"sub": email, "exp": exp, "iat": now}
    if is_admin:
        claims["adm"] = 1
    payload = _b64url_encode(json.dumps(claims).encode())
    sig = _b64url_encode(
        hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{sig}"


def _decode_token_data(token: str) -> dict | None:
    """Return the verified payload dict, or None if invalid/expired."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload_b64, sig = parts
        expected = _b64url_encode(
            hmac.new(SECRET_KEY.encode(), f"{header}.{payload_b64}".encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, sig):
            return None
        data = json.loads(_b64url_decode(payload_b64))
        if data.get("exp", 0) < datetime.datetime.utcnow().timestamp():
            return None
        return data
    except Exception:
        return None


def verify_token(token: str):
    data = _decode_token_data(token)
    if not data:
        return None
    email = data.get("sub", "").lower()
    iat = data.get("iat", 0)
    changed_at = _pw_changed_at.get(email, 0)
    if changed_at and iat < changed_at:
        return None
    return email or None


def authenticate_user(email: str, password: str):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND is_active = 1",
        (email.lower().strip(),),
    ).fetchone()
    conn.close()
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return dict(user)


def create_user(email: str, password: str, name: str = "", is_admin: bool = False) -> dict:
    import sqlite3
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, name, is_admin) VALUES (?, ?, ?, ?)",
            (email.lower().strip(), hash_password(password), name, 1 if is_admin else 0),
        )
        conn.commit()
        return {"email": email.lower().strip(), "name": name, "is_admin": is_admin}
    except sqlite3.IntegrityError:
        raise ValueError(f"L'email {email} existe déjà")
    except Exception as e:
        # psycopg2 UniqueViolation or other DB error
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise ValueError(f"L'email {email} existe déjà")
        raise
    finally:
        conn.close()


def is_admin(email: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT is_admin FROM users WHERE email = ? AND is_active = 1", (email.lower(),)
    ).fetchone()
    conn.close()
    return bool(row and row["is_admin"])


def list_users() -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT u.email, u.name, u.is_active, u.is_admin, u.plan, u.agency_enabled, u.created_at,
                  u.company, u.account_type, u.signup_status,
                  CASE WHEN ac.user_email IS NOT NULL THEN 1 ELSE 0 END AS has_amazon
           FROM users u
           LEFT JOIN amazon_credentials ac ON ac.user_email = u.email
           ORDER BY u.created_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_user(email: str):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE email = ?", (email.lower(),))
    conn.commit()
    conn.close()


def toggle_user(email: str, active: bool):
    conn = get_db()
    if active:
        # Activation manuelle par l'admin : le compte n'attend plus son paiement
        conn.execute(
            "UPDATE users SET is_active = 1, signup_status = 'active' WHERE email = ?",
            (email.lower(),),
        )
    else:
        conn.execute("UPDATE users SET is_active = 0 WHERE email = ?", (email.lower(),))
    conn.commit()
    conn.close()
