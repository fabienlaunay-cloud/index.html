"""
Object storage — Cloudflare R2 when R2_* env vars present, local disk otherwise.

Stored URLs are always /api/photos/{key} so the DB stays portable.
The serve_photo endpoint redirects to R2_PUBLIC_URL when set (bucket public access
enabled) — files are served directly from Cloudflare CDN, zero server bandwidth.
"""
import os
from typing import Optional

_R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
_R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID")
_R2_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
_R2_BUCKET     = os.getenv("R2_BUCKET_NAME")
# e.g. https://pub-abc123.r2.dev  (no trailing slash)
# Found in R2 bucket → Settings → Public Access → Bucket URL
R2_PUBLIC_URL  = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

USE_R2 = all([_R2_ACCOUNT_ID, _R2_ACCESS_KEY, _R2_SECRET_KEY, _R2_BUCKET])

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_DIR = os.getenv("PHOTOS_DIR", os.path.join(_PROJECT_ROOT, "data", "photos"))

_s3 = None  # lazily initialized


def _safe_key(key: str) -> str:
    """Reject path-traversal attempts. Keys are flat filenames, never paths."""
    if not key or key != os.path.basename(key) or key in (".", ".."):
        raise ValueError(f"Clé de fichier invalide : {key!r}")
    return key


def _get_s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{_R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=_R2_ACCESS_KEY,
            aws_secret_access_key=_R2_SECRET_KEY,
            region_name="auto",
        )
    return _s3


def public_url(key: str) -> Optional[str]:
    """Return the direct CDN URL for a key when public access is enabled, else None."""
    if USE_R2 and R2_PUBLIC_URL:
        return f"{R2_PUBLIC_URL}/{key}"
    return None


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    """Upload a file to R2 or save to local disk."""
    key = _safe_key(key)
    if USE_R2:
        _get_s3().put_object(Bucket=_R2_BUCKET, Key=key, Body=data, ContentType=content_type)
    else:
        os.makedirs(LOCAL_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_DIR, key), "wb") as f:
            f.write(data)


def get(key: str) -> Optional[bytes]:
    """Retrieve file bytes from R2 or local disk."""
    try:
        key = _safe_key(key)
    except ValueError:
        return None
    if USE_R2:
        try:
            resp = _get_s3().get_object(Bucket=_R2_BUCKET, Key=key)
            return resp["Body"].read()
        except Exception:
            return None
    else:
        path = os.path.join(LOCAL_DIR, key)
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None


def exists(key: str) -> bool:
    """Return True if the file exists."""
    try:
        key = _safe_key(key)
    except ValueError:
        return False
    if USE_R2:
        try:
            _get_s3().head_object(Bucket=_R2_BUCKET, Key=key)
            return True
        except Exception:
            return False
    return os.path.exists(os.path.join(LOCAL_DIR, key))


def delete(key: str) -> bool:
    """Delete a file from R2 or local disk. Returns True if it existed."""
    try:
        key = _safe_key(key)
    except ValueError:
        return False
    if USE_R2:
        try:
            _get_s3().delete_object(Bucket=_R2_BUCKET, Key=key)
            return True
        except Exception:
            return False
    path = os.path.join(LOCAL_DIR, key)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def list_keys(prefix: str = "") -> list[str]:
    """List stored file keys (names)."""
    if USE_R2:
        try:
            paginator = _get_s3().get_paginator("list_objects_v2")
            keys = []
            for page in paginator.paginate(Bucket=_R2_BUCKET, Prefix=prefix):
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
            return keys
        except Exception:
            return []
    else:
        try:
            return [f for f in os.listdir(LOCAL_DIR) if not prefix or f.startswith(prefix)]
        except FileNotFoundError:
            return []
