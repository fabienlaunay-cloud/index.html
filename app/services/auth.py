import os
import hashlib
import hmac
import base64
import json
import datetime
from app.db import get_db

SECRET_KEY = os.getenv("JWT_SECRET", "change-me-use-a-long-random-string-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", "24"))


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


def create_token(email: str) -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    exp = int((datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_EXPIRE_HOURS)).timestamp())
    payload = _b64url_encode(json.dumps({"sub": email, "exp": exp}).encode())
    sig = _b64url_encode(
        hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{sig}"


def verify_token(token: str):
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
        return data.get("sub")
    except Exception:
        return None


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


def create_user(email: str, password: str, name: str = "") -> dict:
    import sqlite3
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)",
            (email.lower().strip(), hash_password(password), name),
        )
        conn.commit()
        return {"email": email.lower().strip(), "name": name}
    except sqlite3.IntegrityError:
        raise ValueError(f"L'email {email} existe déjà")
    finally:
        conn.close()


def list_users() -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT email, name, is_active, created_at FROM users ORDER BY created_at DESC"
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
    conn.execute(
        "UPDATE users SET is_active = ? WHERE email = ?",
        (1 if active else 0, email.lower()),
    )
    conn.commit()
    conn.close()
