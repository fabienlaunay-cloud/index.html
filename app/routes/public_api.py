"""SynqIO Public API v1 — programmatic access to generated product content.

Authentication: an API key created in the app (menu → API & Intégrations),
sent as ``Authorization: Bearer sk_synqio_…`` or ``X-Api-Key``.
Keys are stored hashed (SHA-256); the plaintext is shown once at creation.

The path prefix /api/v1/ is exempted from the session AuthMiddleware
(PUBLIC_PREFIX_PATHS) — every endpoint here authenticates via the
``_require_key`` dependency instead.

Endpoints:
- GET /api/v1                       → API index (public)
- GET /api/v1/listings              → latest version of every listing (paginated)
- GET /api/v1/listings/{sku}        → one listing by SKU
- GET /api/v1/batches               → generation batches (metadata)
- GET /api/v1/batches/{batch_id}    → one batch with its listings
- GET/POST/DELETE /api/v1/webhooks  → manage webhook subscriptions
"""
import hashlib
import secrets
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.db import (get_generation, list_generations, resolve_api_key,
                    create_webhook, list_webhooks, delete_webhook)

router = APIRouter(prefix="/api/v1")

API_VERSION = "1.0"
_EVENTS = {"listing.generated"}


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def _require_key(
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
) -> str:
    """Resolve the API key from headers → owning user email, else 401."""
    key = ""
    if authorization.startswith("Bearer "):
        key = authorization.split(" ", 1)[1].strip()
    if not key and x_api_key:
        key = x_api_key.strip()
    if not key.startswith("sk_synqio_"):
        raise HTTPException(401, "Clé API manquante — en-tête 'Authorization: Bearer sk_synqio_…' ou 'X-Api-Key'")
    email = resolve_api_key(_hash_key(key))
    if not email:
        raise HTTPException(401, "Clé API invalide ou révoquée")
    # Basic per-key rate limit (shared limiter from main)
    from app.main import _rate_limit
    if not _rate_limit(f"{email}:apiv1", limit=600, window=3600):
        raise HTTPException(429, "Limite de 600 requêtes/heure atteinte — réessayez plus tard")
    return email


def _attach_images(listings: list, skus: list) -> None:
    """Add generated image URLs to each listing dict (in canonical slot order)."""
    from app.main import _get_image_urls_for_skus
    from app.utils.export import _IMAGE_IDS_ORDERED
    image_urls = _get_image_urls_for_skus(skus)
    for l in listings:
        imgs = image_urls.get(l.get("sku"), {})
        l["images"] = [{"slot": i, "url": imgs[i]} for i in _IMAGE_IDS_ORDERED if i in imgs]


def _latest_listings(email: str) -> list:
    """Flatten every batch (newest first) and keep the newest version per SKU.

    Dedicated query (single round-trip) with a strict recency order: SQLite's
    CURRENT_TIMESTAMP is second-precision, so ``created_at DESC`` alone can tie
    for quick consecutive batches — rowid breaks the tie by true insertion
    order. PostgreSQL has microsecond timestamps (ties are practically
    impossible); ``id DESC`` is only a deterministic fallback there.
    """
    import json as _json
    import os as _os
    from app.db import get_db, _ensure_history_table
    conn = get_db()
    _ensure_history_table(conn)
    tiebreak = "id DESC" if _os.getenv("DATABASE_URL") else "rowid DESC"
    rows = conn.execute(
        f"SELECT id, created_at, listings_json FROM generation_history "
        f"WHERE user_email = ? ORDER BY created_at DESC, {tiebreak}",
        (email,),
    ).fetchall()
    conn.close()
    seen: dict = {}
    for r in rows:  # newest first — first seen per SKU wins
        try:
            listings = _json.loads(r["listings_json"] or "[]")
        except Exception:
            continue
        for l in listings:
            sku = l.get("sku")
            if sku and sku not in seen:
                l["batch_id"] = r["id"]
                l["generated_at"] = str(r["created_at"] or "")
                seen[sku] = l
    return list(seen.values())


@router.get("")
@router.get("/")
async def api_index():
    return {
        "name": "SynqIO Public API",
        "version": API_VERSION,
        "documentation": "https://synqio.io/api-docs.html",
        "authentication": "Authorization: Bearer sk_synqio_… (or X-Api-Key header)",
        "endpoints": [
            "GET /api/v1/listings",
            "GET /api/v1/listings/{sku}",
            "GET /api/v1/batches",
            "GET /api/v1/batches/{batch_id}",
            "GET|POST|DELETE /api/v1/webhooks",
        ],
    }


@router.get("/listings")
async def api_listings(
    email: str = Depends(_require_key),
    marketplace: Optional[str] = None,
    sku: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_images: bool = True,
):
    items = _latest_listings(email)
    if marketplace:
        items = [l for l in items if l.get("marketplace") == marketplace]
    if sku:
        wanted = {s.strip() for s in sku.split(",") if s.strip()}
        items = [l for l in items if l.get("sku") in wanted]
    total = len(items)
    page = items[offset:offset + limit]
    if include_images and page:
        _attach_images(page, [l["sku"] for l in page])
    return {"total": total, "count": len(page), "offset": offset, "listings": page}


@router.get("/listings/{sku}")
async def api_listing_one(sku: str, email: str = Depends(_require_key)):
    for l in _latest_listings(email):
        if l.get("sku") == sku:
            _attach_images([l], [sku])
            return l
    raise HTTPException(404, f"SKU introuvable : {sku}")


@router.get("/batches")
async def api_batches(email: str = Depends(_require_key)):
    return {"batches": [
        {"id": b["id"], "created_at": str(b.get("created_at") or ""),
         "marketplace": b.get("marketplace"), "product_count": b.get("product_count"),
         "avg_seo_score": b.get("avg_seo_score"), "label": b.get("label") or ""}
        for b in list_generations(email)
    ]}


@router.get("/batches/{batch_id}")
async def api_batch_one(batch_id: str, email: str = Depends(_require_key),
                        include_images: bool = True):
    batch = get_generation(batch_id, email)
    if not batch:
        raise HTTPException(404, "Batch introuvable")
    listings = batch.get("listings", [])
    if include_images and listings:
        _attach_images(listings, [l.get("sku") for l in listings if l.get("sku")])
    return {"id": batch["id"], "created_at": str(batch.get("created_at") or ""),
            "marketplace": batch.get("marketplace"), "label": batch.get("label") or "",
            "listings": listings}


# ── Webhook management (also possible from the app UI) ───────────────────────

class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["listing.generated"]


@router.get("/webhooks")
async def api_webhooks_list(email: str = Depends(_require_key)):
    return {"webhooks": list_webhooks(email)}


@router.post("/webhooks")
async def api_webhooks_create(body: WebhookCreate, email: str = Depends(_require_key)):
    url = (body.url or "").strip()
    if not url.startswith("https://"):
        raise HTTPException(400, "L'URL du webhook doit être en https://")
    bad = [e for e in body.events if e not in _EVENTS]
    if bad:
        raise HTTPException(400, f"Événements inconnus : {bad} — disponibles : {sorted(_EVENTS)}")
    if len(list_webhooks(email)) >= 10:
        raise HTTPException(400, "Maximum 10 webhooks par compte")
    hook_id = f"wh_{uuid.uuid4().hex[:12]}"
    secret = f"whsec_{secrets.token_urlsafe(24)}"
    create_webhook(hook_id, email, url, secret, ",".join(body.events))
    # The secret is returned ONCE — store it to verify X-Synqio-Signature
    return {"id": hook_id, "url": url, "events": body.events, "secret": secret}


@router.delete("/webhooks/{hook_id}")
async def api_webhooks_delete(hook_id: str, email: str = Depends(_require_key)):
    if not delete_webhook(hook_id, email):
        raise HTTPException(404, "Webhook introuvable")
    return {"deleted": True}
