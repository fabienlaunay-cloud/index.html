"""Native push connectors — publish SynqIO listings straight into the client's
e-commerce platform via its official API. No file, no manual import.

Supported platforms:
- ``shopify``     — Admin REST API. Config: {"shop": "boutique.myshopify.com",
                    "access_token": "shpat_…"} (custom-app Admin token).
- ``woocommerce`` — WP REST API v3. Config: {"site_url": "https://site.com",
                    "consumer_key": "ck_…", "consumer_secret": "cs_…"}.

Push is idempotent: products are matched (Shopify by handle, WooCommerce by
SKU) and updated when they exist, created otherwise. Each listing returns its
own {sku, action, detail} so one failure never aborts the batch.

Credentials are stored encrypted at rest (services/crypto.py) — this module
receives the already-decrypted config dict.
"""
import asyncio
import json
import re
from typing import List, Optional

import httpx

from app.models import AmazonListing
from app.utils.export import _IMAGE_IDS_ORDERED, _strip_html
from app.utils.export_channels import _slug, _full_html_description, _short_description

_TIMEOUT = 20.0
_SHOPIFY_API_VERSION = "2024-01"
MAX_PUSH = 100  # per request — keeps runs bounded and under platform rate limits


def _images_for(sku: str, image_urls: Optional[dict]) -> list:
    imgs = (image_urls or {}).get(sku) or {}
    return [imgs[i] for i in _IMAGE_IDS_ORDERED if i in imgs]


# ── Shopify ──────────────────────────────────────────────────────────────────

def _shopify_base(cfg: dict) -> str:
    shop = (cfg.get("shop") or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{shop}/admin/api/{_SHOPIFY_API_VERSION}"


def _shopify_headers(cfg: dict) -> dict:
    return {"X-Shopify-Access-Token": (cfg.get("access_token") or "").strip(),
            "Content-Type": "application/json"}


async def shopify_test(cfg: dict) -> dict:
    """Validate credentials — returns the shop name or raises ValueError."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(f"{_shopify_base(cfg)}/shop.json", headers=_shopify_headers(cfg))
    if r.status_code == 401:
        raise ValueError("Jeton d'accès Shopify invalide")
    if r.status_code == 404:
        raise ValueError("Boutique introuvable — vérifiez le domaine .myshopify.com")
    if not r.is_success:
        raise ValueError(f"Shopify a répondu {r.status_code}")
    shop = r.json().get("shop", {})
    return {"name": shop.get("name", ""), "domain": shop.get("myshopify_domain", "")}


def _shopify_payload(l: AmazonListing, images: list) -> dict:
    return {"product": {
        "title": l.title,
        "body_html": _full_html_description(l),
        "vendor": l.brand,
        "product_type": l.category,
        "tags": ", ".join((l.backend_keywords or "").split())[:255],
        "status": "draft",  # draft so the client reviews before going live
        "variants": [{
            "sku": l.sku,
            "price": str(l.price) if l.price is not None else "0.00",
            "barcode": l.ean or "",
            "grams": int(l.weight_kg * 1000) if l.weight_kg else 0,
        }],
        "images": [{"src": u, "alt": l.title} for u in images],
    }}


async def shopify_push(cfg: dict, listings: List[AmazonListing],
                       image_urls: Optional[dict] = None) -> List[dict]:
    base, headers = _shopify_base(cfg), _shopify_headers(cfg)
    results = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for l in listings[:MAX_PUSH]:
            if l.is_parent:
                continue
            handle = _slug(f"{l.title} {l.sku}") or _slug(l.sku)
            payload = _shopify_payload(l, _images_for(l.sku, image_urls))
            payload["product"]["handle"] = handle
            try:
                # Idempotence: same handle → update instead of duplicate
                r = await client.get(f"{base}/products.json",
                                     params={"handle": handle, "fields": "id"}, headers=headers)
                existing = (r.json().get("products") or []) if r.is_success else []
                if existing:
                    pid = existing[0]["id"]
                    r = await client.put(f"{base}/products/{pid}.json", json=payload, headers=headers)
                    action = "updated"
                else:
                    r = await client.post(f"{base}/products.json", json=payload, headers=headers)
                    action = "created"
                if r.is_success:
                    results.append({"sku": l.sku, "action": action,
                                    "detail": str(r.json().get("product", {}).get("id", ""))})
                else:
                    err = str(r.json().get("errors", r.text))[:200] if r.text else str(r.status_code)
                    results.append({"sku": l.sku, "action": "error", "detail": err})
            except Exception as e:
                results.append({"sku": l.sku, "action": "error", "detail": type(e).__name__})
            await asyncio.sleep(0.55)  # Shopify: 2 req/s bucket
    return results


# ── WooCommerce ──────────────────────────────────────────────────────────────

def _woo_base(cfg: dict) -> str:
    return (cfg.get("site_url") or "").strip().rstrip("/") + "/wp-json/wc/v3"


def _woo_auth(cfg: dict) -> tuple:
    return ((cfg.get("consumer_key") or "").strip(), (cfg.get("consumer_secret") or "").strip())


async def woo_test(cfg: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(f"{_woo_base(cfg)}/products", params={"per_page": 1}, auth=_woo_auth(cfg))
    if r.status_code == 401:
        raise ValueError("Clés WooCommerce invalides (consumer key/secret)")
    if r.status_code == 404:
        raise ValueError("API WooCommerce introuvable — vérifiez l'URL du site et que l'API REST est activée")
    if not r.is_success:
        raise ValueError(f"WooCommerce a répondu {r.status_code}")
    return {"products_visible": r.headers.get("X-WP-Total", "?")}


def _woo_payload(l: AmazonListing, images: list) -> dict:
    return {
        "name": l.title,
        "type": "simple",
        "status": "draft",
        "sku": l.sku,
        "regular_price": str(l.price) if l.price is not None else "",
        "description": _full_html_description(l),
        "short_description": _short_description(l),
        "weight": str(l.weight_kg) if l.weight_kg else "",
        "images": [{"src": u, "alt": l.title} for u in images],
        "attributes": ([{"name": "Marque", "options": [l.brand], "visible": True}] if l.brand else []),
        "meta_data": ([{"key": "_gtin", "value": l.ean}] if l.ean else []),
    }


async def woo_push(cfg: dict, listings: List[AmazonListing],
                   image_urls: Optional[dict] = None) -> List[dict]:
    base, auth = _woo_base(cfg), _woo_auth(cfg)
    results = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for l in listings[:MAX_PUSH]:
            if l.is_parent:
                continue
            payload = _woo_payload(l, _images_for(l.sku, image_urls))
            try:
                r = await client.get(f"{base}/products", params={"sku": l.sku}, auth=auth)
                existing = r.json() if r.is_success and isinstance(r.json(), list) else []
                if existing:
                    pid = existing[0]["id"]
                    r = await client.put(f"{base}/products/{pid}", json=payload, auth=auth)
                    action = "updated"
                else:
                    r = await client.post(f"{base}/products", json=payload, auth=auth)
                    action = "created"
                if r.is_success:
                    results.append({"sku": l.sku, "action": action, "detail": str(r.json().get("id", ""))})
                else:
                    try:
                        err = str(r.json().get("message", r.status_code))[:200]
                    except Exception:
                        err = str(r.status_code)
                    results.append({"sku": l.sku, "action": "error", "detail": err})
            except Exception as e:
                results.append({"sku": l.sku, "action": "error", "detail": type(e).__name__})
            await asyncio.sleep(0.3)
    return results


# ── Registry ─────────────────────────────────────────────────────────────────

PLATFORMS = {
    "shopify": {
        "label": "Shopify",
        "test": shopify_test,
        "push": shopify_push,
        "fields": ["shop", "access_token"],
    },
    "woocommerce": {
        "label": "WooCommerce",
        "test": woo_test,
        "push": woo_push,
        "fields": ["site_url", "consumer_key", "consumer_secret"],
    },
}
