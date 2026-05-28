"""
Amazon Selling Partner API integration.

In demo/dry-run mode (default), no real API call is made.
Set AMAZON_SP_MODE=live + credentials to publish for real.
"""

import os
import json
import asyncio
import httpx
from typing import List, Optional
from datetime import datetime

from app.models import AmazonListing, PublishResult, Marketplace
from app.db import get_db

DEMO_MODE = os.getenv("AMAZON_SP_MODE", "demo") == "demo"

MARKETPLACE_IDS = {
    Marketplace.AMAZON_FR: "A13V1IB3VIYZZH",
    Marketplace.AMAZON_DE: "A1PA6795UKMFR9",
    Marketplace.AMAZON_IT: "APJ6JRA9NG5V4",
    Marketplace.AMAZON_ES: "A1RKKUPIHCS9HS",
    Marketplace.AMAZON_UK: "A1F83G8C2ARO7P",
}

SP_API_ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"


def _get_sp_credentials(user_email: str = None) -> dict:
    """Récupère les credentials SP-API : depuis la DB si user_email fourni, sinon depuis les env vars."""
    base = {
        "lwa_client_id": os.getenv("LWA_CLIENT_ID", ""),
        "lwa_client_secret": os.getenv("LWA_CLIENT_SECRET", ""),
        "aws_access_key": os.getenv("AWS_ACCESS_KEY_ID", ""),
        "aws_secret_key": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        "role_arn": os.getenv("AWS_ROLE_ARN", ""),
    }
    if user_email:
        conn = get_db()
        row = conn.execute(
            "SELECT refresh_token, seller_id FROM amazon_credentials WHERE user_email = ?",
            (user_email,),
        ).fetchone()
        conn.close()
        if row:
            return {**base, "refresh_token": row["refresh_token"], "seller_id": row["seller_id"]}
    return {**base, "refresh_token": os.getenv("AMAZON_REFRESH_TOKEN", ""), "seller_id": os.getenv("AMAZON_SELLER_ID", "")}


async def _get_lwa_token(creds: dict) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
                "client_id": creds["lwa_client_id"],
                "client_secret": creds["lwa_client_secret"],
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def _listing_to_sp_payload(listing: AmazonListing, seller_id: str, marketplace_id: str) -> dict:
    """Convert internal listing model to Amazon SP-API Listings Items payload."""
    attributes = {
        "item_name": [{"value": listing.title, "marketplace_id": marketplace_id}],
        "brand": [{"value": listing.brand}],
        "product_description": [{"value": listing.description, "marketplace_id": marketplace_id}],
        "generic_keyword": [{"value": listing.backend_keywords, "marketplace_id": marketplace_id}],
        "bullet_point": [
            {"value": bp, "marketplace_id": marketplace_id}
            for bp in listing.bullet_points
        ],
    }
    if listing.price:
        attributes["purchasable_offer"] = [{
            "marketplace_id": marketplace_id,
            "currency": "EUR",
            "our_price": [{"schedule": [{"value_with_tax": listing.price}]}],
        }]
    if listing.ean:
        attributes["externally_assigned_product_identifier"] = [
            {"type": "EAN", "value": listing.ean}
        ]

    return {
        "productType": listing.category.upper().replace(" ", "_") or "PRODUCT",
        "requirements": "LISTING",
        "attributes": attributes,
    }


async def _publish_one(
    listing: AmazonListing,
    token: str,
    seller_id: str,
    marketplace_id: str,
) -> dict:
    payload = _listing_to_sp_payload(listing, seller_id, marketplace_id)
    url = f"{SP_API_ENDPOINT}/listings/2021-08-01/items/{seller_id}/{listing.sku}"
    params = {"marketplaceIds": marketplace_id}
    headers = {
        "x-amz-access-token": token,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(url, headers=headers, params=params, json=payload)
        return {
            "sku": listing.sku,
            "status": resp.status_code,
            "response": resp.json() if resp.content else {},
        }


async def publish_listings(
    listings: List[AmazonListing],
    marketplace: Marketplace,
    dry_run: bool = True,
    user_email: str = None,
) -> PublishResult:
    if DEMO_MODE or dry_run:
        return _demo_publish(listings, marketplace, dry_run)

    creds = _get_sp_credentials(user_email)
    marketplace_id = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS[Marketplace.AMAZON_FR])

    try:
        token = await _get_lwa_token(creds)
    except Exception as e:
        return PublishResult(
            published=0,
            failed=len(listings),
            errors=[{"error": f"Auth LWA échouée: {e}"}],
            report=[],
        )

    semaphore = asyncio.Semaphore(5)
    report = []
    errors = []

    async def _do(listing: AmazonListing):
        async with semaphore:
            try:
                result = await _publish_one(listing, token, creds["seller_id"], marketplace_id)
                if result["status"] in (200, 201):
                    report.append({"sku": listing.sku, "status": "published", **result})
                else:
                    errors.append({"sku": listing.sku, "status": result["status"], "detail": result})
            except Exception as e:
                errors.append({"sku": listing.sku, "error": str(e)})

    await asyncio.gather(*[_do(l) for l in listings])

    return PublishResult(
        published=len(report),
        failed=len(errors),
        errors=errors,
        report=report,
    )


def _demo_publish(listings: List[AmazonListing], marketplace: Marketplace, dry_run: bool) -> PublishResult:
    report = []
    for listing in listings:
        report.append({
            "sku": listing.sku,
            "title": listing.title,
            "status": "dry_run" if dry_run else "demo_published",
            "marketplace": marketplace.value,
            "seo_score": listing.seo_score,
            "timestamp": datetime.utcnow().isoformat(),
        })
    return PublishResult(
        published=len(listings),
        failed=0,
        errors=[],
        report=report,
    )
