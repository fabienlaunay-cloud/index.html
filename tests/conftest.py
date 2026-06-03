import os
import pytest
import tempfile
from fastapi.testclient import TestClient

# Use isolated SQLite for tests — override before importing the app
@pytest.fixture(scope="session", autouse=True)
def _isolated_db(tmp_path_factory):
    db_file = str(tmp_path_factory.mktemp("db") / "test.db")
    os.environ["DB_PATH"] = db_file
    os.environ.pop("DATABASE_URL", None)
    # Provide stub API keys so endpoints don't return 503
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-chars-long!")
    yield db_file


@pytest.fixture(scope="session")
def client(_isolated_db):
    from app.db import init_db
    init_db()
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def _make_user(email: str, password: str = "TestPass123!", name: str = "Test User") -> str:
    """Create a user and return their JWT token."""
    from app.services.auth import create_user, create_token
    create_user(email, password, name)
    return create_token(email)


@pytest.fixture
def user_token(client):
    """Returns (token, email) for a fresh test user."""
    import secrets
    email = f"user_{secrets.token_hex(4)}@test.com"
    token = _make_user(email)
    return token, email


@pytest.fixture
def auth(user_token):
    token, email = user_token
    return {"Authorization": f"Bearer {token}"}, email
