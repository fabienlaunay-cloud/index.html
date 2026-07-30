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
            from app.services.crypto import decrypt_token
            return {**base, "refresh_token": decrypt_token(row["refresh_token"]), "seller_id": row["seller_id"]}
    from app.services.crypto import decrypt_token
    return {
        **base,
        "refresh_token": decrypt_token(get_config("AMAZON_REFRESH_TOKEN")),
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
    "collier": "ANIMAL_COLLAR",
    "laisse":  "ANIMAL_COLLAR",
    "harnais": "ANIMAL_COLLAR",
    "muselière": "ANIMAL_MUZZLE",
    "litière": "ANIMAL_LITTER",
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
    "C-COL": "ANIMAL_COLLAR",
    "COL":   "ANIMAL_COLLAR",
    "LAI":   "ANIMAL_COLLAR",
    "HAR":   "ANIMAL_COLLAR",
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
        "item_name":   _txt(listing.title[:200]),
        "brand":       _txt(listing.brand),   # brand is localizable text → needs language_tag
        "product_description": _txt(clean_desc[:2000]),
        "bullet_point": [
            {"value": bp[:500], "marketplace_id": marketplace_id, "language_tag": language_tag}
            for bp in clean_bullets[:5] if bp
        ],
        "country_of_origin": [{"value": getattr(listing, "country_of_origin", None) or "FR", "marketplace_id": marketplace_id}],
        "supplier_declared_dg_hz_regulation": [{"value": "not_applicable", "marketplace_id": marketplace_id}],
        "condition_type": [{"value": "new_new", "marketplace_id": marketplace_id}],
    }

    # EAN is required by Amazon for new ASIN creation (GTIN validation)
    if listing.ean:
        attributes["externally_assigned_product_identifier"] = [
            {"value": listing.ean, "type": "EAN", "marketplace_id": marketplace_id}
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
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, headers=headers)
            data = resp.json()
            if "errors" in data:
                _log.warning(f"[SP-API] SCHEMA NOT_FOUND for {product_type}: {data['errors']}")
                await _search_product_types(["collar", "pet", "animal"], marketplace_id, lwa_token, temp_creds)
                return
            # Log requirements supported
            _log.warning(f"[SP-API] SCHEMA requirements={data.get('requirements')} enforced={data.get('requirementsEnforced')}")
            schema_url = data.get("schema", {}).get("link", {}).get("resource", "")
            _log.warning(f"[SP-API] SCHEMA url={schema_url[:120] if schema_url else 'none'}")
            if schema_url:
                schema_resp = await client.get(schema_url)
                schema = schema_resp.json()
                required_array = schema.get("required", [])
                all_props = schema.get("properties", {})
                _log.warning(f"[SP-API] SCHEMA required: {required_array}")
                # Log full definition of interesting attributes to see valid values
                for attr_name in ["country_of_origin", "supplier_declared_dg_hz_regulation",
                                   "condition_type", "fulfillment_availability", "purchasable_offer"]:
                    defn = all_props.get(attr_name)
                    if defn:
                        raw = json.dumps(defn, ensure_ascii=False)
                        # Log in chunks of 800 to avoid truncation
                        for i in range(0, min(len(raw), 2400), 800):
                            _log.warning(f"[SP-API] ATTR {attr_name}[{i}]: {raw[i:i+800]}")
                    else:
                        _log.warning(f"[SP-API] ATTR {attr_name}: not found in properties")
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
    from urllib.parse import quote
    language_tag = MARKETPLACE_LOCALES.get(marketplace, "fr_FR")
    payload = _listing_to_sp_payload(listing, seller_id, marketplace_id, language_tag)
    body_bytes = json.dumps(payload).encode("utf-8")

    encoded_sku = quote(listing.sku, safe="")
    base_url = f"{_sp_endpoint()}/listings/2021-08-01/items/{seller_id}/{encoded_sku}"

    _log.warning(f"[SP-API] seller_id={seller_id} encoded_sku={encoded_sku} marketplace_id={marketplace_id}")
    _log.warning(f"[SP-API] productType={payload['productType']} lang={language_tag}")
    _log.warning(f"[SP-API] payload: {json.dumps(payload)[:3000]}")

    # ── DIAGNOSTIC 1: seller marketplace participations ──────────────────────
    part_url = f"{_sp_endpoint()}/sellers/v1/marketplaceParticipations"
    part_headers = _sign_request("GET", part_url, b"", temp_creds, lwa_token)
    async with httpx.AsyncClient(timeout=30) as client:
        part_resp = await client.get(part_url, headers=part_headers)
    _log.warning(f"[SP-API] participations: {part_resp.status_code} | {part_resp.text[:800]}")

    # ── DIAGNOSTIC 2: catalog list (no SKU) — verifies seller_id is correct ──
    # If this returns 400, the seller_id stored in DB does not match the Merchant Token.
    catalog_url = (
        f"{_sp_endpoint()}/listings/2021-08-01/items/{seller_id}"
        f"?marketplaceIds={marketplace_id}&pageSize=1"
    )
    catalog_headers = _sign_request("GET", catalog_url, b"", temp_creds, lwa_token)
    async with httpx.AsyncClient(timeout=30) as client:
        catalog_resp = await client.get(catalog_url, headers=catalog_headers)
    _log.warning(f"[SP-API] catalog list (seller_id test): {catalog_resp.status_code} | {catalog_resp.text[:400]}")

    # ── DIAGNOSTIC 3: GET existing listing (specific SKU) ────────────────────
    get_url = f"{base_url}?marketplaceIds={marketplace_id}"
    get_headers = _sign_request("GET", get_url, b"", temp_creds, lwa_token)
    async with httpx.AsyncClient(timeout=30) as client:
        get_resp = await client.get(get_url, headers=get_headers)
    _log.warning(f"[SP-API] GET listing: {get_resp.status_code} | {get_resp.text[:400]}")

    # ── PUT with issueLocale for localised error detail ───────────────────────
    put_url = f"{base_url}?marketplaceIds={marketplace_id}&issueLocale={language_tag.replace('_', '-')}"
    put_headers = _sign_request("PUT", put_url, body_bytes, temp_creds, lwa_token)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(put_url, headers=put_headers, content=body_bytes)

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


def _first_attr(attrs: dict, key: str) -> str:
    """SP-API attributes are lists of {value, marketplace_id, …}. Return the first value."""
    vals = attrs.get(key)
    if isinstance(vals, list) and vals and isinstance(vals[0], dict):
        return str(vals[0].get("value") or "").strip()
    return ""


def _extract_catalog_images(summary: dict, attrs: dict) -> list:
    """Main image from the summary + additional images from attribute locators."""
    images = []
    main = (summary.get("mainImage") or {}).get("link", "")
    if main:
        images.append(main)
    for i in range(1, 9):
        loc = attrs.get(f"other_product_image_locator_{i}")
        if isinstance(loc, list) and loc and isinstance(loc[0], dict):
            url = loc[0].get("media_location") or loc[0].get("value") or ""
            if url and url not in images:
                images.append(url)
    return images


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
            {"sku": "DEMO-SKU-001", "asin": "B08XYZ1234", "ean": "3760123456789",
             "title": "Produit Démo 1", "brand": "SynqIO", "description": "Description démo.",
             "bullet_points": ["Avantage 1", "Avantage 2"],
             "images": ["https://m.media-amazon.com/images/I/demo1.jpg"]},
            {"sku": "YOGA-MAT-007", "asin": "B07DEF9012", "ean": "3760111222333",
             "title": "Tapis Yoga Démo", "brand": "SynqIO", "description": "",
             "bullet_points": [], "images": []},
        ]

    # searchListingsItems rejects a catalog-wide listing ("InvalidInput"). The
    # reliable way to dump a seller's whole catalog is the Reports API report
    # GET_MERCHANT_LISTINGS_ALL_DATA — a TSV with sku, item-name, asin, price and
    # image-url. Fetch it via the same request→poll→document→download flow.
    return await _fetch_catalog_via_report(user_email, marketplace)


def _parse_merchant_listings_tsv(text: str) -> list:
    """Parse GET_MERCHANT_LISTINGS_ALL_DATA (tab-separated) into catalog items."""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return []
    headers = [h.strip().lower() for h in lines[0].split("\t")]
    def idx(*names):
        for n in names:
            if n in headers:
                return headers.index(n)
        return -1
    i_sku = idx("seller-sku", "sku")
    i_name = idx("item-name", "title")
    i_asin = idx("asin1", "asin", "product-id")
    i_img = idx("image-url", "main-image-url")
    i_price = idx("price")
    i_status = idx("status", "listing-status")
    i_qty = idx("quantity", "afn-fulfillable-quantity")
    items = []
    for line in lines[1:]:
        cols = line.split("\t")
        def get(i):
            return cols[i].strip() if 0 <= i < len(cols) else ""
        sku = get(i_sku)
        if not sku:
            continue
        price = None
        raw_price = get(i_price)
        if raw_price:
            try:
                price = round(float(raw_price.replace(",", ".")), 2)
            except ValueError:
                price = None
        qty = None
        raw_qty = get(i_qty)
        if raw_qty:
            try:
                qty = int(float(raw_qty))
            except ValueError:
                qty = None
        status = (get(i_status) or "").strip().lower()  # active | inactive | incomplete
        img = get(i_img)
        items.append({
            "sku": sku, "asin": get(i_asin), "ean": "", "title": get(i_name),
            "brand": "", "description": "", "bullet_points": [],
            "images": [img] if img.startswith("http") else [],
            "price": price, "status": status, "quantity": qty,
        })
    return items


async def _fetch_catalog_via_report(user_email: str, marketplace: Marketplace,
                                     max_wait: float = 90.0) -> list:
    creds = _get_sp_credentials(user_email)
    if not creds.get("refresh_token"):
        raise RuntimeError("Compte Amazon non connecté")
    lwa = await _get_lwa_token(creds)
    loop = asyncio.get_event_loop()
    temp = await loop.run_in_executor(None, lambda: _assume_role(creds))
    base = f"{_sp_endpoint()}/reports/2021-06-30"
    mkid = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS[Marketplace.AMAZON_FR])

    r = await _sp_signed("POST", f"{base}/reports", {
        "reportType": "GET_MERCHANT_LISTINGS_ALL_DATA",
        "marketplaceIds": [mkid],
    }, lwa, temp)
    if not r.is_success:
        raise RuntimeError(f"Amazon {r.status_code} : {r.text[:280]}")
    report_id = r.json().get("reportId")
    if not report_id:
        raise RuntimeError("reportId absent de la réponse Amazon")

    waited, delay, doc_id = 0.0, 3.0, None
    while waited < max_wait:
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.4, 8.0)
        rs = await _sp_signed("GET", f"{base}/reports/{report_id}", None, lwa, temp)
        if not rs.is_success:
            continue
        st = rs.json()
        status = st.get("processingStatus")
        if status == "DONE":
            doc_id = st.get("reportDocumentId")
            break
        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"Rapport Amazon {status.lower()}")
    if not doc_id:
        raise RuntimeError("Le rapport catalogue met trop de temps — réessayez dans une minute.")

    rd = await _sp_signed("GET", f"{base}/documents/{doc_id}", None, lwa, temp)
    if not rd.is_success:
        raise RuntimeError("Impossible de récupérer le document du rapport catalogue")
    doc = rd.json()
    url = doc.get("url")
    if not url:
        raise RuntimeError("URL du rapport absente")
    async with httpx.AsyncClient(timeout=60) as client:
        dl = await client.get(url)
    raw = dl.content
    if doc.get("compressionAlgorithm") == "GZIP":
        import gzip as _gz
        raw = _gz.decompress(raw)
    # Merchant reports are often latin-1/cp1252 encoded
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return _parse_merchant_listings_tsv(text)


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


# ── Sales & Traffic report (auto-feed the Business Watchdog) ──────────────────

def parse_sales_traffic(data: dict) -> dict:
    """Parse a GET_SALES_AND_TRAFFIC_REPORT JSON document into per-ASIN metrics.
    Pure/deterministic — unit-testable without any network call."""
    out: dict = {}
    for row in (data.get("salesAndTrafficByAsin") or []):
        asin = row.get("childAsin") or row.get("parentAsin") or row.get("asin")
        if not asin:
            continue
        sales = row.get("sales") or {}
        traffic = row.get("traffic") or {}
        ops = sales.get("orderedProductSales") or {}
        try:
            revenue = float(ops.get("amount") or 0)
        except (TypeError, ValueError):
            revenue = 0.0
        out[str(asin).upper()] = {
            "units_ordered": int(sales.get("unitsOrdered") or 0),
            "revenue": revenue,
            "sessions": int(traffic.get("sessions") or 0),
            "page_views": int(traffic.get("pageViews") or 0),
            "conversion_rate": float(traffic.get("unitSessionPercentage") or 0),
        }
    return out


async def _sp_signed(method: str, url: str, body: dict, lwa: str, temp: dict):
    body_bytes = json.dumps(body).encode() if body is not None else b""
    headers = _sign_request(method, url, body_bytes, temp, lwa)
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.request(method, url, headers=headers, content=body_bytes or None)


async def fetch_sales_and_traffic(user_email: str, marketplace: Marketplace,
                                  start_iso: str, end_iso: str, max_wait: float = 50.0) -> dict:
    """Full Reports API flow → per-ASIN metrics. Returns {} in demo mode.
    Raises RuntimeError('report_pending') if generation didn't finish in time
    (the caller retries later — the weekly cron has more patience)."""
    if _is_demo():
        return {}
    creds = _get_sp_credentials(user_email)
    if not creds.get("refresh_token"):
        raise RuntimeError("Compte Amazon non connecté")
    lwa = await _get_lwa_token(creds)
    temp = _assume_role(creds)
    base = f"{_sp_endpoint()}/reports/2021-06-30"
    mkid = MARKETPLACE_IDS.get(marketplace, MARKETPLACE_IDS[Marketplace.AMAZON_FR])

    # 1) request the report
    r = await _sp_signed("POST", f"{base}/reports", {
        "reportType": "GET_SALES_AND_TRAFFIC_REPORT",
        "marketplaceIds": [mkid],
        "dataStartTime": start_iso,
        "dataEndTime": end_iso,
        "reportOptions": {"asinGranularity": "CHILD", "dateGranularity": "WEEK"},
    }, lwa, temp)
    if r.status_code == 403:
        raise RuntimeError("Rôle SP-API 'Brand Analytics' requis pour ce rapport")
    if not r.is_success:
        raise RuntimeError(f"Amazon a répondu {r.status_code} à la demande de rapport")
    report_id = r.json().get("reportId")
    if not report_id:
        raise RuntimeError("reportId absent de la réponse Amazon")

    # 2) poll until DONE (bounded)
    waited, delay, doc_id = 0.0, 3.0, None
    while waited < max_wait:
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 10.0)
        rs = await _sp_signed("GET", f"{base}/reports/{report_id}", None, lwa, temp)
        if not rs.is_success:
            continue
        st = rs.json()
        status = st.get("processingStatus")
        if status == "DONE":
            doc_id = st.get("reportDocumentId")
            break
        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"Rapport Amazon {status.lower()}")
    if not doc_id:
        raise RuntimeError("report_pending")

    # 3) fetch the document URL
    rd = await _sp_signed("GET", f"{base}/documents/{doc_id}", None, lwa, temp)
    if not rd.is_success:
        raise RuntimeError("Impossible de récupérer le document du rapport")
    doc = rd.json()
    url = doc.get("url")
    if not url:
        raise RuntimeError("URL du rapport absente")

    # 4) download + (gzip?) + parse
    async with httpx.AsyncClient(timeout=60) as client:
        dl = await client.get(url)
    raw = dl.content
    if doc.get("compressionAlgorithm") == "GZIP":
        import gzip as _gz
        raw = _gz.decompress(raw)
    return parse_sales_traffic(json.loads(raw.decode("utf-8", errors="replace")))
