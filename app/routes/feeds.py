"""Pull feeds — permanent per-account URLs serving always-up-to-date exports.

Consumers (Google Merchant Center, e-commerce sites, PIM sync jobs) fetch a
capability URL on their own schedule; no headers or API client needed:

    GET /api/feed/{token}/{channel}
    channels: google-merchant · shopify · woocommerce · prestashop · akeneo · pim

The token is minted per account in the app (API & Intégrations → Flux
automatiques) and can be regenerated at any time, which invalidates the old
URL. Content is the latest version of every listing (same source as
GET /api/v1/listings), rendered by the multichannel export builders.

The /api/feed/ prefix is exempted from the session AuthMiddleware — the token
in the path IS the authentication (read-only catalog content, revocable).
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.db import resolve_feed_token
from app.models import AmazonListing
from app.utils.export_channels import CHANNELS

router = APIRouter(prefix="/api/feed")


@router.get("/{token}/{channel}")
async def serve_feed(token: str, channel: str, marketplace: str = ""):
    spec = CHANNELS.get(channel)
    if not spec:
        raise HTTPException(404, f"Canal inconnu : {channel} — disponibles : {', '.join(CHANNELS)}")

    email = resolve_feed_token(token, channel)
    if not email:
        raise HTTPException(404, "Flux introuvable")  # 404 (not 401): don't confirm token shape

    # Light per-token rate limit — feed consumers poll a few times a day
    from app.main import _rate_limit
    if not _rate_limit(f"{email}:feed", limit=120, window=3600):
        raise HTTPException(429, "Limite de 120 requêtes/heure atteinte pour ce flux")

    from app.routes.public_api import _latest_listings
    raw = _latest_listings(email)
    if marketplace:
        raw = [l for l in raw if l.get("marketplace") == marketplace]

    listings = []
    for l in raw:
        try:
            listings.append(AmazonListing.model_validate(l))
        except Exception:
            continue  # skip malformed legacy rows rather than break the feed

    from app.main import _get_image_urls_for_skus
    image_urls = _get_image_urls_for_skus([l.sku for l in listings])

    body = spec["builder"](listings, image_urls=image_urls)
    return Response(
        content=body,
        media_type=spec["media_type"],
        headers={
            "Content-Disposition": f'inline; filename="{spec["filename"]}"',
            "Cache-Control": "no-cache",
        },
    )
