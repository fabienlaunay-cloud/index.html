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


_CATEGORY_TO_PRODUCT_TYPE = {
    # Animaux
    "collier": "PET_COLLAR_LEAD_HARNESS",
    "laisse": "PET_COLLAR_LEAD_HARNESS",
    "harnais": "PET_COLLAR_LEAD_HARNESS",
    "pet": "PET_SUPPLIES",
    "chien": "PET_SUPPLIES",
    "chat": "PET_SUPPLIES",
    "animal": "PET_SUPPLIES",
    # Maison
    "maison": "HOME",
    "cuisine": "KITCHEN",
    "jardin": "LAWN_AND_GARDEN",
    # Mode
    "vêtement": "CLOTHING",
    "chaussure": "SHOE",
    "sac": "BAG",
    "bijou": "JEWELRY",
    # Sport
    "sport": "SPORTING_GOODS",
    "fitness": "SPORTING_GOODS",
    "yoga": "SPORTING_GOODS",
    # Beauté
    "beauté": "BEAUTY",
    "cosmétique": "BEAUTY",
    "soin": "BEAUTY",
    # Électronique
    "électronique": "CONSUMER_ELECTRONICS",
    "informatique": "CONSUMER_ELECTRONICS",
}


import re as _re

def _strip_html(text: str) -> str:
    return _re.sub(r'<[^>]+>', '', text or '').strip()


_SKU_PREFIX_TO_PRODUCT_TYPE = {
    "C-COL": "PET_COLLAR_LEAD_HARNESS",
    "COL":   "PET_COLLAR_LEAD_HARNESS",
    "LAI":   "PET_COLLAR_LEAD_HARNESS",
    "HAR":   "PET_COLLAR_LEAD_HARNESS",
}


def _resolve_product_type(listing) -> str:
    """Map listing to a valid Amazon product type. Falls back to PET_SUPPLIES."""
    import logging as _log
    explicit = getattr(listing, "product_type", None) or ""
    if explicit:
        return explicit.upper().replace(" ", "_")

    # 1. Try category keywords
    cat = (listing.category or "").lower()
    _log.warning(f"[SP-API] category='{cat}' sku='{listing.sku}' → resolving product type")
    for keyword, ptype in _CATEGORY_TO_PRODUCT_TYPE.items():
        if keyword in cat:
            _log.warning(f"[SP-API] category match '{keyword}' → {ptype}")
            return ptype

    # 2. Try SKU prefix
    sku_upper = listing.sku.upper()
    for prefix, ptype in _SKU_PREFIX_TO_PRODUCT_TYPE.items():
        if sku_upper.startswith(prefix):
            _log.warning(f"[SP-API] SKU prefix match '{prefix}' → {ptype}")
            return ptype

    _log.warning(f"[SP-API] no match → PET_SUPPLIES (fallback)")
    return "PET_SUPPLIES"


def _listing_to_sp_payload(
    listing: AmazonListing,
    seller_id: str,
    marketplace_id: str,
    language_tag: str = "fr_FR",
) -> dict:
    product_type = _resolve_product_type(listing)
    clean_desc = _strip_html(listing.description)
    clean_bullets = [_strip_html(bp) for bp in listing.bullet_points]

    def _txt(value: str) -> list:
        return [{"value": value, "marketplace_id": marketplace_id, "language_tag": language_tag}]

    attributes = {
        "item_name":          _txt(listing.title),
        "brand":              [{"value": listing.brand, "marketplace_id": marketplace_id}],
        "product_description": _txt(clean_desc),
        "generic_keyword":    _txt(listing.backend_keywords),
        "bullet_point":       [
            {"value": bp, "marketplace_id": marketplace_id, "language_tag": language_tag}
            for bp in clean_bullets if bp
        ],
        "condition_type": [{"value": "new_new", "marketplace_id": marketplace_id}],
        "fulfillment_availability": [{"fulfillment_channel_code": "DEFAULT", "quantity": 1}],
    }
    if listing.price:
        attributes["purchasable_offer"] = [{
            "marketplace_id": marketplace_id,
            "currency": "EUR",
            "our_price": [{"schedule": [{"value_with_tax": float(listing.price)}]}],
        }]
    if listing.ean:
        attributes["externally_assigned_product_identifier"] = [
            {"type": "EAN", "value": listing.ean}
        ]
    return {
        "productType": product_type,
        "requirements": "LISTING",
        "attributes": attributes,
    }


async def _log_product_type_schema(
    product_type: str, marketplace_id: str, lwa_token: str, temp_creds: dict
):
    """Query Amazon's Product Type Definitions API and log required attributes."""
    import logging as _log
    try:
        url = (
            f"{_sp_endpoint()}/definitions/2020-09-01/productTypes/{product_type}"
            f"?marketplaceIds={marketplace_id}&requirements=LISTING"
        )
        headers = _sign_request("GET", url, b"", temp_creds, lwa_token)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            data = resp.json()
            if "errors" in data:
                _log.warning(f"[SP-API] SCHEMA NOT_FOUND for {product_type}: {data['errors']}")
                # Search for valid product types with keyword
                await _search_product_types(["collar", "pet", "animal"], marketplace_id, lwa_token, temp_creds)
                return
            schema_url = data.get("schema", {}).get("link", {}).get("resource", "")
            if schema_url:
                schema_resp = await client.get(schema_url)
                schema = schema_resp.json()
                attrs = (
                    schema.get("properties", {})
                          .get("attributes", {})
                          .get("properties", {})
                )
                required = [k for k, v in attrs.items() if isinstance(v, dict) and v.get("minItems", 0) >= 1]
                _log.warning(f"[SP-API] SCHEMA required for {product_type}: {required}")
            else:
                _log.warning(f"[SP-API] SCHEMA (no schema URL): {str(data)[:500]}")
    except Exception as exc:
        _log.warning(f"[SP-API] SCHEMA query error: {exc}")


async def _search_product_types(keywords: list, marketplace_id: str, lwa_token: str, temp_creds: dict):
    """Search valid product types for this marketplace and log results."""
    import logging as _log
    async with httpx.AsyncClient(timeout=30) as client:
        for kw in keywords:
            try:
                url = f"{_sp_endpoint()}/definitions/2020-09-01/productTypes?marketplaceIds={marketplace_id}&keywords={kw}"
                headers = _sign_request("GET", url, b"", temp_creds, lwa_token)
                resp = await client.get(url, headers=headers)
                data = resp.json()
                types = [pt.get("name") for pt in data.get("productTypes", [])]
                _log.warning(f"[SP-API] product types for keyword '{kw}': {types}")
            except Exception as exc:
                _log.warning(f"[SP-API] search '{kw}' error: {exc}")


async def search_product_types_for_marketplace(
    keywords: list,
    marketplace: "Marketplace",
    user_email: str,
) -> dict:
    """Public helper for the admin API to list valid Amazon product types."""
    creds = _get_sp_credentials(user_email)
    marketplace_id = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS[Marketplace.AMAZON_FR])
    lwa_token = await _get_lwa_token(creds)
    temp_creds = await _assume_role(creds)
    results = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for kw in keywords:
            url = f"{_sp_endpoint()}/definitions/2020-09-01/productTypes?marketplaceIds={marketplace_id}&keywords={kw}"
            headers = _sign_request("GET", url, b"", temp_creds, lwa_token)
            resp = await client.get(url, headers=headers)
            results[kw] = resp.json()
    return results


async def _publish_one(
    listing: AmazonListing,
    lwa_token: str,
    seller_id: str,
    marketplace_id: str,
    marketplace: Marketplace,
    temp_creds: dict,
) -> dict:
    import logging as _log
    language_tag = MARKETPLACE_LOCALES.get(marketplace, "fr_FR")
    payload = _listing_to_sp_payload(listing, seller_id, marketplace_id, language_tag)
    body_bytes = json.dumps(payload).encode("utf-8")
    url = f"{_sp_endpoint()}/listings/2021-08-01/items/{seller_id}/{listing.sku}?marketplaceIds={marketplace_id}"

    headers = _sign_request("PUT", url, body_bytes, temp_creds, lwa_token)

    _log.warning(f"[SP-API] PUT {url}")
    _log.warning(f"[SP-API] productType={payload['productType']} lang={language_tag}")
    _log.warning(f"[SP-API] payload: {json.dumps(payload)[:3000]}")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(url, headers=headers, content=body_bytes)

    _log.warning(f"[SP-API] status: {resp.status_code} | response: {resp.text[:1000]}")

    if resp.status_code >= 400:
        await _log_product_type_schema(payload["productType"], marketplace_id, lwa_token, temp_creds)

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
                    amz_errors = result.get("response", {}).get("errors", [])
                    amz_msg = "; ".join(
                        f"{e.get('code','?')}: {e.get('details') or e.get('message','')}"
                        for e in amz_errors
                    ) if amz_errors else str(result.get("response", ""))
                    errors.append({"sku": listing.sku, "status": result["status"], "error": amz_msg})
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


async def fetch_seller_catalog(
    user_email: str,
    marketplace: Marketplace,
) -> list:
    """
    Fetches all seller's listings from SP-API with pagination.
    Returns list of {sku, asin, ean, title}.
    In demo mode, returns sample data.
    """
    if _is_demo():
        return [
            {"sku": "DEMO-SKU-001", "asin": "B08XYZ1234", "ean": "3760123456789", "title": "Produit Démo 1"},
            {"sku": "DEMO-SKU-042", "asin": "B09ABC5678", "ean": "3760987654321", "title": "Produit Démo 2"},
            {"sku": "YOGA-MAT-007", "asin": "B07DEF9012", "ean": "3760111222333", "title": "Tapis Yoga Démo"},
        ]

    creds = _get_sp_credentials(user_email)
    marketplace_id = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS[Marketplace.AMAZON_FR])

    lwa_token = await _get_lwa_token(creds)
    loop = asyncio.get_event_loop()
    temp_creds = await loop.run_in_executor(None, lambda: _assume_role(creds))

    seller_id = creds["seller_id"]
    semaphore = asyncio.Semaphore(1)
    items = []
    page_token = None
    page_count = 0
    max_pages = 50

    while page_count < max_pages:
        async with semaphore:
            url = (
                f"{_sp_endpoint()}/listings/2021-08-01/items/{seller_id}"
                f"?marketplaceIds={marketplace_id}&includedData=summaries,attributes"
            )
            if page_token:
                url += f"&pageToken={page_token}"

            headers = _sign_request("GET", url, b"", temp_creds, lwa_token)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            for item in data.get("items", []):
                sku = item.get("sku", "")
                if not sku:
                    continue
                summaries = item.get("summaries", [])
                asin = summaries[0].get("asin", "") if summaries else ""
                title = summaries[0].get("itemName", "") if summaries else ""

                # Extract EAN from attributes
                ean = ""
                attrs = item.get("attributes", {})
                ext_ids = attrs.get("externally_assigned_product_identifier", [])
                for ext_id in ext_ids:
                    if ext_id.get("type", "").upper() == "EAN":
                        ean = ext_id.get("value", "")
                        break

                items.append({"sku": sku, "asin": asin, "ean": ean, "title": title})

            page_token = data.get("pagination", {}).get("nextPageToken")
            page_count += 1

            if not page_token:
                break

            await asyncio.sleep(0.5)

    return items


async def lookup_asins_by_eans(
    eans: list,
    marketplace: Marketplace,
    user_email: str = None,
) -> dict:
    """
    Lookup ASINs for a list of EANs via SP-API Catalog Items API.
    Returns {ean: asin} dict (only for EANs that have an ASIN on Amazon).
    In demo mode, returns a sample mapping for known demo EANs.
    """
    if _is_demo():
        demo_map = {
            "3760123456789": "B08XYZ1234",
            "3760987654321": "B09ABC5678",
            "3760111222333": "B07DEF9012",
        }
        return {ean: demo_map[ean] for ean in eans if ean in demo_map}

    creds = _get_sp_credentials(user_email)
    marketplace_id = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS[Marketplace.AMAZON_FR])

    lwa_token = await _get_lwa_token(creds)
    loop = asyncio.get_event_loop()
    temp_creds = await loop.run_in_executor(None, lambda: _assume_role(creds))

    result = {}
    batch_size = 10

    for i in range(0, len(eans), batch_size):
        batch = eans[i:i + batch_size]
        identifiers = ",".join(batch)
        url = (
            f"{_sp_endpoint()}/catalog/2022-04-01/items"
            f"?identifiers={identifiers}&identifiersType=EAN"
            f"&marketplaceIds={marketplace_id}&includedData=identifiers"
        )
        headers = _sign_request("GET", url, b"", temp_creds, lwa_token)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                continue
            data = resp.json()

        for item in data.get("items", []):
            asin = item.get("asin", "")
            if not asin:
                continue
            # Find which EAN this item corresponds to
            for id_entry in item.get("identifiers", []):
                for id_item in id_entry.get("identifiers", []):
                    if id_item.get("identifierType", "").upper() == "EAN":
                        ean_val = id_item.get("identifier", "")
                        if ean_val in batch:
                            result[ean_val] = asin

    return result
