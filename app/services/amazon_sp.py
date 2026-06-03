"""
Amazon Selling Partner API integration with AWS Signature V4.

Modes:
  demo   → aucun appel réel (défaut)
  sandbox → appels vers l'endpoint sandbox Amazon
  live    → appels vers l'endpoint production Amazon
"""

import os
import json
import asyncio
import httpx
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from typing import List, Optional
from datetime import datetime

from app.models import AmazonListing, PublishResult, Marketplace
from app.db import get_db, get_config

def _sp_mode() -> str:
    return get_config("AMAZON_SP_MODE", "demo")

def _is_demo() -> bool:
    return _sp_mode() == "demo"

def _sp_endpoint() -> str:
    mode = _sp_mode()
    if mode == "sandbox":
        return "https://sandbox.sellingpartnerapi-eu.amazon.com"
    return "https://sellingpartnerapi-eu.amazon.com"

MARKETPLACE_IDS = {
    Marketplace.AMAZON_FR: "A13V1IB3VIYZZH",
    Marketplace.AMAZON_DE: "A1PA6795UKMFR9",
    Marketplace.AMAZON_IT: "APJ6JRA9NG5V4",
    Marketplace.AMAZON_ES: "A1RKKUPIHCS9HS",
    Marketplace.AMAZON_UK: "A1F83G8C2ARO7P",
    Marketplace.AMAZON_NL: "A1805IZSGTT6HS",
    Marketplace.AMAZON_SE: "A2NODRKZP88ZB9",
    Marketplace.AMAZON_PL: "A1C3SOZRARQ6R3",
    Marketplace.AMAZON_BE: "ANBVA00213BO8Q",
}

MARKETPLACE_LOCALES = {
    Marketplace.AMAZON_FR: "fr_FR",
    Marketplace.AMAZON_DE: "de_DE",
    Marketplace.AMAZON_IT: "it_IT",
    Marketplace.AMAZON_ES: "es_ES",
    Marketplace.AMAZON_UK: "en_GB",
    Marketplace.AMAZON_NL: "nl_NL",
    Marketplace.AMAZON_SE: "sv_SE",
    Marketplace.AMAZON_PL: "pl_PL",
    Marketplace.AMAZON_BE: "fr_BE",
}


def _get_sp_credentials(user_email: str = None) -> dict:
    """Credentials depuis la DB app_config, puis env vars en fallback."""
    base = {
        "lwa_client_id": get_config("LWA_CLIENT_ID"),
        "lwa_client_secret": get_config("LWA_CLIENT_SECRET"),
        "aws_access_key": get_config("AWS_ACCESS_KEY_ID"),
        "aws_secret_key": get_config("AWS_SECRET_ACCESS_KEY"),
        "role_arn": get_config("AWS_ROLE_ARN"),
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
    return {
        **base,
        "refresh_token": get_config("AMAZON_REFRESH_TOKEN"),
        "seller_id": get_config("AMAZON_SELLER_ID"),
    }


async def _get_lwa_token(creds: dict) -> str:
    """Échange le refresh_token contre un access_token LWA."""
    async with httpx.AsyncClient(timeout=30) as client:
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


def _assume_role(creds: dict) -> dict:
    """Assume le rôle IAM et retourne des credentials temporaires."""
    sts = boto3.client(
        "sts",
        aws_access_key_id=creds["aws_access_key"],
        aws_secret_access_key=creds["aws_secret_key"],
        region_name="eu-west-1",
    )
    response = sts.assume_role(
        RoleArn=creds["role_arn"],
        RoleSessionName="SynqIO-SP-API",
    )
    return response["Credentials"]


def _sign_request(
    method: str,
    url: str,
    body_bytes: bytes,
    temp_creds: dict,
    lwa_token: str,
) -> dict:
    """Signe une requête HTTP avec AWS Signature V4."""
    credentials = Credentials(
        access_key=temp_creds["AccessKeyId"],
        secret_key=temp_creds["SecretAccessKey"],
        token=temp_creds["SessionToken"],
    )
    aws_request = AWSRequest(
        method=method,
        url=url,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "x-amz-access-token": lwa_token,
        },
    )
    SigV4Auth(credentials, "execute-api", "eu-west-1").add_auth(aws_request)
    return dict(aws_request.headers)


def _build_aplus_document(listing: AmazonListing, locale: str) -> dict:
    """Build an A+ Content document payload from listing.a_plus_content."""
    a_plus = listing.a_plus_content or {}
    headline = (a_plus.get("headline") or listing.title)[:150]
    modules_raw = a_plus.get("modules") or []

    content_modules = []

    # Intro block: headline as bold paragraph
    content_modules.append({
        "contentModuleType": "STANDARD_PRODUCT_DESCRIPTION",
        "standardProductDescription": {
            "body": {
                "value": f"<p><b>{headline}</b></p>",
                "decoratorSet": [],
            }
        },
    })

    # One STANDARD_PRODUCT_DESCRIPTION per module
    for mod in modules_raw:
        title = mod.get("title") or ""
        body = mod.get("body") or ""
        html = (f"<p><b>{title}</b></p><p>{body}</p>" if title else f"<p>{body}</p>")
        content_modules.append({
            "contentModuleType": "STANDARD_PRODUCT_DESCRIPTION",
            "standardProductDescription": {
                "body": {
                    "value": html[:5000],
                    "decoratorSet": [],
                }
            },
        })

    return {
        "contentType": "EMC",
        "name": f"{listing.brand or listing.sku} — {listing.sku}",
        "locale": locale,
        "contentModuleList": content_modules,
    }


async def _get_asin_for_sku(
    sku: str,
    seller_id: str,
    marketplace_id: str,
    lwa_token: str,
    temp_creds: dict,
) -> Optional[str]:
    """Return the ASIN for a SKU via the Listings Items API, or None."""
    url = (
        f"{_sp_endpoint()}/listings/2021-08-01/items/{seller_id}/{sku}"
        f"?marketplaceIds={marketplace_id}&includedData=summaries"
    )
    headers = _sign_request("GET", url, b"", temp_creds, lwa_token)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        for summary in data.get("summaries", []):
            if summary.get("asin"):
                return summary["asin"]
    return None


async def _push_aplus(
    listing: AmazonListing,
    asin: str,
    lwa_token: str,
    marketplace_id: str,
    locale: str,
    temp_creds: dict,
) -> dict:
    """Create an A+ content document and publish it to the given ASIN."""
    # 1. Create content document
    doc = _build_aplus_document(listing, locale)
    body_bytes = json.dumps(doc).encode("utf-8")
    create_url = f"{_sp_endpoint()}/aplus/2020-11-01/contentDocuments?marketplaceId={marketplace_id}"
    headers = _sign_request("POST", create_url, body_bytes, temp_creds, lwa_token)

    async with httpx.AsyncClient(timeout=30) as client:
        create_resp = await client.post(create_url, headers=headers, content=body_bytes)
        if create_resp.status_code not in (200, 201):
            return {
                "aplus_status": "failed",
                "aplus_error": f"create {create_resp.status_code}: {create_resp.text[:200]}",
            }
        content_ref_key = create_resp.json().get("contentReferenceKey")

    if not content_ref_key:
        return {"aplus_status": "failed", "aplus_error": "missing contentReferenceKey"}

    # 2. Publish document to the ASIN
    pub_body = json.dumps({"contentReferenceKey": content_ref_key, "asin": asin}).encode("utf-8")
    pub_url = f"{_sp_endpoint()}/aplus/2020-11-01/contentPublishRecords?marketplaceId={marketplace_id}"
    pub_headers = _sign_request("POST", pub_url, pub_body, temp_creds, lwa_token)

    async with httpx.AsyncClient(timeout=30) as client:
        pub_resp = await client.post(pub_url, headers=pub_headers, content=pub_body)

    if pub_resp.status_code in (200, 201):
        return {
            "aplus_status": "submitted",
            "aplus_content_ref": content_ref_key,
            "aplus_asin": asin,
        }
    return {
        "aplus_status": "failed",
        "aplus_content_ref": content_ref_key,
        "aplus_error": f"publish {pub_resp.status_code}: {pub_resp.text[:200]}",
    }


def _listing_to_sp_payload(listing: AmazonListing, seller_id: str, marketplace_id: str) -> dict:
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
    lwa_token: str,
    seller_id: str,
    marketplace_id: str,
    marketplace: Marketplace,
    temp_creds: dict,
) -> dict:
    payload = _listing_to_sp_payload(listing, seller_id, marketplace_id)
    body_bytes = json.dumps(payload).encode("utf-8")
    url = f"{_sp_endpoint()}/listings/2021-08-01/items/{seller_id}/{listing.sku}?marketplaceIds={marketplace_id}"

    headers = _sign_request("PUT", url, body_bytes, temp_creds, lwa_token)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(url, headers=headers, content=body_bytes)

    result = {
        "sku": listing.sku,
        "status": resp.status_code,
        "response": resp.json() if resp.content else {},
    }

    # Push A+ content when listing is accepted and a_plus_content is present
    if listing.a_plus_content and resp.status_code in (200, 201):
        locale = MARKETPLACE_LOCALES.get(marketplace, "fr_FR")
        try:
            asin = await _get_asin_for_sku(listing.sku, seller_id, marketplace_id, lwa_token, temp_creds)
            if asin:
                aplus = await _push_aplus(listing, asin, lwa_token, marketplace_id, locale, temp_creds)
                result.update(aplus)
            else:
                result["aplus_status"] = "pending_asin"
        except Exception as e:
            result["aplus_status"] = "error"
            result["aplus_error"] = str(e)

    return result


async def publish_listings(
    listings: List[AmazonListing],
    marketplace: Marketplace,
    dry_run: bool = True,
    user_email: str = None,
) -> PublishResult:
    if _is_demo() or dry_run:
        return _demo_publish(listings, marketplace, dry_run)

    creds = _get_sp_credentials(user_email)
    marketplace_id = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS[Marketplace.AMAZON_FR])

    try:
        lwa_token = await _get_lwa_token(creds)
    except Exception as e:
        return PublishResult(
            published=0, failed=len(listings),
            errors=[{"error": f"Auth LWA échouée: {e}"}], report=[],
        )

    try:
        loop = asyncio.get_event_loop()
        temp_creds = await loop.run_in_executor(None, lambda: _assume_role(creds))
    except Exception as e:
        return PublishResult(
            published=0, failed=len(listings),
            errors=[{"error": f"AWS AssumeRole échoué: {e}"}], report=[],
        )

    semaphore = asyncio.Semaphore(5)
    report = []
    errors = []

    async def _do(listing: AmazonListing):
        async with semaphore:
            try:
                result = await _publish_one(listing, lwa_token, creds["seller_id"], marketplace_id, marketplace, temp_creds)
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
        entry = {
            "sku": listing.sku,
            "title": listing.title,
            "status": "dry_run" if dry_run else "demo_published",
            "marketplace": marketplace.value,
            "seo_score": listing.seo_score,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if listing.a_plus_content:
            entry["aplus_status"] = "demo_submitted"
        report.append(entry)
    return PublishResult(published=len(listings), failed=0, errors=[], report=report)
