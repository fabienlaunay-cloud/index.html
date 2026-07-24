import os
import json as _json
import secrets
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional


def _js_str(s: Optional[str]) -> str:
    """Safely embed a Python string as a JS string literal (XSS-safe)."""
    return _json.dumps(s or "")

from app.db import get_db, get_config, set_config, save_catalog_items, get_catalog, get_catalog_summary
from app.models import Marketplace

router = APIRouter(prefix="/api/amazon")

REDIRECT_URI = os.getenv("AMAZON_REDIRECT_URI", "https://synqio.io/api/amazon/callback")


def _lwa_credentials() -> tuple[str, str]:
    """Read LWA credentials from env or config DB at call time."""
    client_id = os.getenv("LWA_CLIENT_ID", "").strip() or get_config("LWA_CLIENT_ID", "").strip()
    client_secret = os.getenv("LWA_CLIENT_SECRET", "").strip() or get_config("LWA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(503, "LWA_CLIENT_ID / LWA_CLIENT_SECRET non configurés dans l'admin SynqIO")
    return client_id, client_secret


# Seller Central consent-page host per marketplace. The LWA token-exchange
# endpoint (api.amazon.com) is global, but the consent page must be served from
# the seller's regional domain. All currently supported marketplaces are EU.
_CONSENT_HOSTS = {
    "amazon_fr": "sellercentral.amazon.fr",
    "amazon_de": "sellercentral.amazon.de",
    "amazon_it": "sellercentral.amazon.it",
    "amazon_es": "sellercentral.amazon.es",
    "amazon_uk": "sellercentral.amazon.co.uk",
    "amazon_nl": "sellercentral.amazon.nl",
    "amazon_se": "sellercentral.amazon.se",
    "amazon_pl": "sellercentral.amazon.pl",
    "amazon_be": "sellercentral.amazon.com.be",
}
_DEFAULT_CONSENT_HOST = "sellercentral-europe.amazon.com"  # EU-wide fallback


def _consent_host(marketplace: Optional[str]) -> str:
    return _CONSENT_HOSTS.get((marketplace or "").strip().lower(), _DEFAULT_CONSENT_HOST)


def _oauth_beta() -> bool:
    """version=beta runs the consent page in DRAFT mode (developer's own account
    only). Off by default now the app is PUBLIC; togglable for testing a draft."""
    return (os.getenv("AMAZON_OAUTH_BETA", "").strip()
            or get_config("AMAZON_OAUTH_BETA", "").strip()).lower() in ("1", "true", "yes")


def _new_state(email: str) -> str:
    """Mint and persist an anti-CSRF state bound to a SynqIO user."""
    state = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT INTO amazon_oauth_states (state, user_email) VALUES (?, ?) "
        "ON CONFLICT(state) DO UPDATE SET user_email=EXCLUDED.user_email",
        (state, email),
    )
    conn.commit()
    conn.close()
    return state


@router.get("/connect")
async def amazon_connect(request: Request, marketplace: Optional[str] = None):
    """Seller-initiated flow: return the Amazon consent URL (JS handles redirect)."""
    app_id = get_config("AMAZON_APP_ID", "").strip()
    if not app_id:
        raise HTTPException(503, "AMAZON_APP_ID manquant — configurez-le dans l'espace admin SynqIO")
    state = _new_state(request.state.user_email)
    auth_url = (
        f"https://{_consent_host(marketplace)}/apps/authorize/consent"
        f"?application_id={app_id}"
        f"&state={state}"
    )
    if _oauth_beta():
        auth_url += "&version=beta"
    return {"url": auth_url}


class AppstoreAuthorizeBody(BaseModel):
    amazon_callback_uri: str
    amazon_state: str


@router.post("/appstore-authorize")
async def amazon_appstore_authorize(request: Request, body: AppstoreAuthorizeBody):
    """Amazon-initiated flow (from the Appstore listing): the /login page calls
    this once the SynqIO user is authenticated. We mint our own state and return
    the consent URL to redirect the seller back to Amazon's consent page."""
    callback = (body.amazon_callback_uri or "").strip()
    amazon_state = (body.amazon_state or "").strip()
    # Guard against open-redirect: only ever redirect to an Amazon Seller Central host
    if not (callback.startswith("https://") and ".amazon." in callback
            and "sellercentral" in callback and "?" not in callback and "#" not in callback):
        raise HTTPException(400, "amazon_callback_uri invalide")
    if not amazon_state:
        raise HTTPException(400, "amazon_state manquant")
    state = _new_state(request.state.user_email)
    url = (
        f"{callback}"
        f"?redirect_uri={REDIRECT_URI}"
        f"&amazon_state={amazon_state}"
        f"&state={state}"
    )
    if _oauth_beta():
        url += "&version=beta"
    return {"url": url}


@router.get("/login")
async def amazon_login(amazon_callback_uri: str = "", amazon_state: str = "", selling_partner_id: str = ""):
    """Login URI registered with Amazon. When a seller clicks "Authorize" on the
    Appstore listing, Amazon opens this page with amazon_callback_uri +
    amazon_state. We ensure the seller is logged into SynqIO (client-side token),
    then hand off to /appstore-authorize which builds the consent redirect.

    This route is PUBLIC (the seller arrives via top-level navigation with no
    Authorization header) — see PUBLIC_PATHS in main.py."""
    return HTMLResponse(f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Connexion Amazon — SynqIO</title></head>
<body style="font-family:sans-serif;text-align:center;padding:60px 20px;color:#1f2937">
<div style="font-size:2.4rem;margin-bottom:12px">🔗</div>
<h2 style="margin:0 0 8px;font-size:1.15rem">Autorisation Amazon en cours…</h2>
<p id="msg" style="color:#6b7280;font-size:.9rem">Vérification de votre session SynqIO…</p>
<script>
(function() {{
  var cb = {_js_str(amazon_callback_uri)};
  var ast = {_js_str(amazon_state)};
  var msg = document.getElementById('msg');
  if (!cb || !ast) {{ msg.textContent = "Lien d'autorisation Amazon incomplet."; return; }}
  var token = localStorage.getItem('synqio_token');
  if (!token) {{
    // Not logged in — stash the Amazon params and send the seller to login.
    sessionStorage.setItem('synqio_amazon_pending', JSON.stringify({{cb: cb, ast: ast}}));
    location.href = '/?amazon_authorize=1';
    return;
  }}
  fetch('/api/amazon/appstore-authorize', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}},
    body: JSON.stringify({{amazon_callback_uri: cb, amazon_state: ast}})
  }}).then(function(r) {{ return r.json().then(function(d) {{ return {{ok: r.ok, d: d}}; }}); }})
    .then(function(res) {{
      if (res.ok && res.d.url) {{ location.href = res.d.url; }}
      else {{ msg.textContent = 'Erreur : ' + ((res.d && res.d.detail) || 'autorisation impossible'); }}
    }})
    .catch(function() {{ msg.textContent = 'Erreur réseau — réessayez.'; }});
}})();
</script>
</body></html>""")


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

    lwa_client_id, lwa_client_secret = _lwa_credentials()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "authorization_code",
                "code": spapi_oauth_code,
                "redirect_uri": REDIRECT_URI,
                "client_id": lwa_client_id,
                "client_secret": lwa_client_secret,
            },
        )
        if not resp.is_success:
            raise HTTPException(502, f"Erreur LWA: {resp.text}")
        tokens = resp.json()

    from app.services.crypto import encrypt_token
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
        (email, selling_partner_id or "", encrypt_token(tokens["refresh_token"])),
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


class ManualTokenBody(BaseModel):
    refresh_token: str
    seller_id: str = ""
    target_email: str = ""  # admin only: store for another user


@router.post("/manual-token")
async def amazon_manual_token(body: ManualTokenBody, request: Request):
    """Admin endpoint — stores a manually generated SP-API refresh token."""
    requester = request.state.user_email
    conn = get_db()
    row = conn.execute("SELECT is_admin FROM users WHERE email = ?", (requester,)).fetchone()
    is_admin = row and row["is_admin"]
    conn.close()

    email = (body.target_email.strip() or requester) if is_admin else requester
    if body.target_email and body.target_email != requester and not is_admin:
        raise HTTPException(403, "Réservé aux admins")

    token = body.refresh_token.strip()
    if not token.startswith("Atzr|"):
        raise HTTPException(400, "Le refresh token doit commencer par 'Atzr|'")

    from app.services.crypto import encrypt_token
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
        (email, body.seller_id.strip(), encrypt_token(token)),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "email": email}


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
