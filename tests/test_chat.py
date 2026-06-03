import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def _mock_chat_result(text: str = "Bonjour! Quel produit souhaitez-vous référencer?"):
    """Build a return value matching app.services.chat_agent.chat() output."""
    return {
        "reply": text,
        "ready_products": None,
        "tokens": {"input": 50, "output": 30},
    }


def test_create_and_list_sessions(client, auth):
    headers, email = auth
    # Create a session
    resp = client.post("/api/chat/sessions", headers=headers)
    assert resp.status_code == 200
    session = resp.json()
    assert "id" in session
    assert session["title"] == "Nouvelle conversation"
    session_id = session["id"]
    # List sessions
    resp = client.get("/api/chat/sessions", headers=headers)
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()]
    assert session_id in ids


def test_get_session(client, auth):
    headers, _ = auth
    resp = client.post("/api/chat/sessions", headers=headers)
    session_id = resp.json()["id"]
    resp = client.get(f"/api/chat/sessions/{session_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert data["messages"] == []


def test_send_message(client, auth):
    headers, _ = auth
    resp = client.post("/api/chat/sessions", headers=headers)
    session_id = resp.json()["id"]
    # Patch the high-level chat() coroutine used by the route handler.
    # The route does a local import so we patch at the source module.
    with patch("app.services.chat_agent.chat", new_callable=AsyncMock,
               return_value=_mock_chat_result()) as mock_chat:
        resp = client.post(
            f"/api/chat/sessions/{session_id}/message",
            headers=headers,
            data={"message": "J'ai un casque audio Bluetooth"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "reply" in body
    assert len(body["reply"]) > 0


def test_delete_session(client, auth):
    headers, _ = auth
    resp = client.post("/api/chat/sessions", headers=headers)
    session_id = resp.json()["id"]
    resp = client.delete(f"/api/chat/sessions/{session_id}", headers=headers)
    assert resp.status_code == 200
    # Confirm gone
    resp = client.get(f"/api/chat/sessions/{session_id}", headers=headers)
    assert resp.status_code == 404


def test_session_isolation_between_users(client):
    from app.services.auth import create_user, create_token
    import secrets
    e1 = f"u1_{secrets.token_hex(4)}@test.com"
    e2 = f"u2_{secrets.token_hex(4)}@test.com"
    create_user(e1, "Pass1234!", "User1")
    create_user(e2, "Pass1234!", "User2")
    h1 = {"Authorization": f"Bearer {create_token(e1)}"}
    h2 = {"Authorization": f"Bearer {create_token(e2)}"}
    # User 1 creates session
    sid = client.post("/api/chat/sessions", headers=h1).json()["id"]
    # User 2 cannot access it
    resp = client.get(f"/api/chat/sessions/{sid}", headers=h2)
    assert resp.status_code == 404
