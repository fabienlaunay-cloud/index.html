import os
import pytest
import tempfile


@pytest.fixture
def disk_storage(tmp_path, monkeypatch):
    """Storage fixture using temp directory, R2 disabled."""
    monkeypatch.setenv("PHOTOS_DIR", str(tmp_path))
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("R2_BUCKET_NAME", raising=False)
    # Re-import to pick up new env
    import importlib
    import app.services.storage as stor
    importlib.reload(stor)
    return stor


def test_put_and_get(disk_storage):
    disk_storage.put("test.txt", b"hello world", "text/plain")
    data = disk_storage.get("test.txt")
    assert data == b"hello world"


def test_get_missing_returns_none(disk_storage):
    assert disk_storage.get("nonexistent.png") is None


def test_exists(disk_storage):
    assert not disk_storage.exists("file.png")
    disk_storage.put("file.png", b"data", "image/png")
    assert disk_storage.exists("file.png")


def test_list_keys(disk_storage):
    disk_storage.put("img1.png", b"a", "image/png")
    disk_storage.put("img2.jpg", b"b", "image/jpeg")
    keys = disk_storage.list_keys()
    assert "img1.png" in keys
    assert "img2.jpg" in keys


def test_public_url_none_without_r2_public_url(disk_storage):
    assert disk_storage.public_url("img.png") is None
