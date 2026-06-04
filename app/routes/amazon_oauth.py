import os
import secrets
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional

from app.db import get_db, get_config, set_config, save_catalog_items, get_catalog, get_catalog_summary
from app.models import Marketplace

router = APIRouter(prefix="/api/amazon")

REDIRECT_URI = os.getenv("AMAZON_REDIRECT_URI", "https://synqio.io/api/amazon/callback")




@router.get("/connect")
async def amazon_connect(request: Request):
    """Retourne l'URL d'autorisation Amazon (le JS gère la redirection)."""
    app_id = get_config("AMAZON_APP_ID", "").strip()
    if not app_id:
        raise HTTPException(503, "AMAZON_APP_ID manquant — configurez-le dans l'espace admin SynqIO")
    email = request.state.user_email
    state = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT INTO amazon_oauth_states (state, user_email) VALUES (?, ?) "
        "ON CONFLICT(state) DO UPDATE SET user_email=EXCLUDED.user_email",
        (state, email),
    )
    conn.commit()
    conn.close()
    auth_url = (
        f"https://sellercentral.amazon.fr/apps/authorize/consent"
        f"?application_id={app_id}"
        f"&state={state}"
        f"&version=beta"
    )
    return {"url": auth_url}


@router.get("/callback")
async def amazon_callback(
    spapi_oauth_code: str = None,
    state: str = None,
    selling_partner_id: str = None,
):
    """Reçoit le code OAuth Amazon, échange contre un refresh_token, stocke en DB."""
    if not spapi_oauth_code or not state:
        raise HTTPException(400, "Paramètres OAuth manquants")

    conn = get_db()
    row = conn.execute(
        "SELECT user_email FROM amazon_oauth_states WHERE state = ?", (state,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(400, "State OAuth invalide ou expiré")

    email = row["user_email"]
    conn.execute("DELETE FROM amazon_oauth_states WHERE state = ?", (state,))
    conn.commit()
    conn.close()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "authorization_code",
                "code": spapi_oauth_code,
                "redirect_uri": REDIRECT_URI,
                "client_id": LWA_CLIENT_ID,
                "client_secret": LWA_CLIENT_SECRET,
            },
        )
        if not resp.is_success:
            raise HTTPException(502, f"Erreur LWA: {resp.text}")
        tokens = resp.json()

    conn = get_db()
    conn.execute(
        """
        INSERT INTO amazon_credentials (user_email, seller_id, refresh_token)
        VALUES (?, ?, ?)
        ON CONFLICT(user_email) DO UPDATE SET
            seller_id = excluded.seller_id,
            refresh_token = excluded.refresh_token,
            updated_at = CURRENT_TIMESTAMP
        """,
        (email, selling_partner_id or "", tokens["refresh_token"]),
    )
    conn.commit()
    conn.close()
    # Si ouvert en popup : envoyer postMessage au parent et fermer
    # Si ouvert en onglet principal (fallback) : rediriger vers l'app
    return HTMLResponse("""<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:60px 20px;color:#1f2937">
<div style="font-size:3rem;margin-bottom:16px">✅</div>
<h2 style="margin:0 0 8px;font-size:1.2rem">Seller Central connecté !</h2>
<p style="color:#6b7280;font-size:.9rem">Fermeture automatique...</p>
<script>
  try { window.opener && window.opener.postMessage('amazon_connected','*'); } catch(e){}
  setTimeout(function(){ window.close(); if(!window.closed){ window.location.href='/'; } }, 1200);
</script>
</body></html>""")


@router.get("/status")
async def amazon_status(request: Request):
    """Vérifie si l'utilisateur a connecté son Seller Central."""
    email = request.state.user_email
    conn = get_db()
    row = conn.execute(
        "SELECT seller_id, updated_at FROM amazon_credentials WHERE user_email = ?",
        (email,),
    ).fetchone()
    conn.close()
    if row:
        return {"connected": True, "seller_id": row["seller_id"], "connected_at": row["updated_at"]}
    return {"connected": False}


@router.delete("/disconnect")
async def amazon_disconnect(request: Request):
    """Supprime les credentials Amazon de l'utilisateur."""
    email = request.state.user_email
    conn = get_db()
    conn.execute("DELETE FROM amazon_credentials WHERE user_email = ?", (email,))
    conn.commit()
    conn.close()
    return {"disconnected": True}


# ── Catalog sync & SKU resolution ─────────────────────────────────────────────

class CatalogSyncBody(BaseModel):
    marketplace: str = "amazon_fr"


class ResolveSkusBody(BaseModel):
    eans: List[str]
    marketplace: str = "amazon_fr"


def _parse_marketplace(value: str) -> Marketplace:
    """Convert marketplace string like 'amazon_fr' to Marketplace enum."""
    try:
        return Marketplace(value)
    except ValueError:
        return Marketplace.AMAZON_FR


@router.post("/catalog/sync")
async def catalog_sync(request: Request, body: CatalogSyncBody):
    """Fetch seller catalog from SP-API and persist to product_catalog table."""
    from app.services.amazon_sp import fetch_seller_catalog
    email = request.state.user_email
    marketplace = _parse_marketplace(body.marketplace)
    items = await fetch_seller_catalog(email, marketplace)
    count = save_catalog_items(email, body.marketplace, items)
    return {"synced": count, "marketplace": body.marketplace}


@router.get("/catalog")
async def catalog_get(request: Request, marketplace: Optional[str] = None):
    """Return catalog summary or full catalog for one marketplace."""
    email = request.state.user_email
    summary = get_catalog_summary(email)
    total = sum(summary.values())
    if marketplace:
        items = get_catalog(email, marketplace)
        return {"summary": summary, "total": total, "marketplace": marketplace, "items": items}
    return {"summary": summary, "total": total}


@router.post("/resolve-skus")
async def resolve_skus(request: Request, body: ResolveSkusBody):
    """
    For each EAN, resolve to SKU/ASIN:
      1. Check product_catalog for this user+marketplace → source: "catalog"
      2. Lookup via SP-API EAN search → source: "amazon"
      3. Not found → source: "new"
    """
    from app.services.amazon_sp import lookup_asins_by_eans
    email = request.state.user_email
    marketplace_str = body.marketplace

    # Step 1: check local catalog
    catalog_rows = get_catalog(email, marketplace_str)
    catalog_by_ean = {row["ean"]: row for row in catalog_rows if row.get("ean")}

    results = []
    eans_for_api = []

    for ean in body.eans:
        if ean in catalog_by_ean:
            row = catalog_by_ean[ean]
            results.append({
                "ean": ean,
                "sku": row["sku"],
                "asin": row.get("asin", ""),
                "source": "catalog",
            })
        else:
            eans_for_api.append(ean)

    # Step 2: lookup remaining EANs via SP-API
    if eans_for_api:
        marketplace = _parse_marketplace(marketplace_str)
        try:
            asin_map = await lookup_asins_by_eans(eans_for_api, marketplace, email)
        except Exception:
            asin_map = {}

        for ean in eans_for_api:
            if ean in asin_map:
                results.append({
                    "ean": ean,
                    "sku": None,
                    "asin": asin_map[ean],
                    "source": "amazon",
                })
            else:
                results.append({
                    "ean": ean,
                    "sku": None,
                    "asin": None,
                    "source": "new",
                })

    return results
