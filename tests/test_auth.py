import pytest
import secrets


def test_login_success(client):
    email = f"login_{secrets.token_hex(4)}@test.com"
    from app.services.auth import create_user
    create_user(email, "Pass1234!", "Login User")
    resp = client.post("/api/auth/login", json={"email": email, "password": "Pass1234!"})
    assert resp.status_code == 200
    data = resp.json()
    # The login endpoint returns {"token": "...", "email": "...", "name": "...", "is_admin": ...}
    assert "token" in data
    assert data["email"] == email


def test_login_wrong_password(client):
    email = f"badpw_{secrets.token_hex(4)}@test.com"
    from app.services.auth import create_user
    create_user(email, "RealPass1!", "User")
    resp = client.post("/api/auth/login", json={"email": email, "password": "WrongPass!"})
    assert resp.status_code in (400, 401)


def test_login_unknown_user(client):
    resp = client.post("/api/auth/login", json={"email": "nobody@nowhere.com", "password": "x"})
    assert resp.status_code in (400, 401)


def test_protected_endpoint_rejects_unauthenticated(client):
    resp = client.get("/api/chat/sessions")
    assert resp.status_code == 401


def test_protected_endpoint_accepts_valid_token(client, auth):
    headers, email = auth
    resp = client.get("/api/chat/sessions", headers=headers)
    assert resp.status_code == 200


def test_invalid_token_rejected(client):
    resp = client.get("/api/chat/sessions",
                      headers={"Authorization": "Bearer not.a.real.token"})
    assert resp.status_code == 401
