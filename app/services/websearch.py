"""Recherche web — pour trouver des boutiques à partir d'un secteur.

Deux fournisseurs interchangeables, choisis par la clé présente dans
l'environnement. Aucune clé : la fonctionnalité se désactive proprement au
lieu d'échouer à l'exécution.

    BRAVE_SEARCH_KEY   → api.search.brave.com   (le moins cher)
    SERPAPI_KEY        → serpapi.com            (résultats Google)

Le moteur ne sert qu'à produire des *candidats*. La qualification — est-ce
bien une boutique Shopify, combien de références — se fait ensuite en lisant
le site, jamais en faisant confiance au moteur.
"""
import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("synqio.websearch")

BRAVE = "https://api.search.brave.com/res/v1/web/search"
SERPAPI = "https://serpapi.com/search.json"
_TIMEOUT = 12.0

OK, UNAVAILABLE, DISABLED = "ok", "unavailable", "disabled"

_FAIL_MAX = 3
_COOLDOWN = 120.0
_breaker = {"fails": 0, "until": 0.0}


def provider() -> str:
    if os.getenv("BRAVE_SEARCH_KEY"):
        return "brave"
    if os.getenv("SERPAPI_KEY"):
        return "serpapi"
    return ""


def _breaker_open() -> bool:
    return time.time() < _breaker["until"]


def _note_failure():
    _breaker["fails"] += 1
    if _breaker["fails"] >= _FAIL_MAX:
        _breaker["until"] = time.time() + _COOLDOWN
        _breaker["fails"] = 0
        log.warning("[websearch] injoignable — appels suspendus %.0fs", _COOLDOWN)


# ── Domaines à écarter ───────────────────────────────────────────────────────
# Une recherche « cosmétique bio boutique » ramène surtout des places de
# marché, des annuaires et des articles de blog. Aucun n'est un prospect : ce
# sont des marques qui vendent en direct qu'on cherche.
_SKIP = {
    "amazon.fr", "amazon.com", "cdiscount.com", "fnac.com", "darty.com",
    "ebay.fr", "etsy.com", "aliexpress.com", "temu.com", "shein.com",
    "rakuten.com", "leboncoin.fr", "veepee.fr", "zalando.fr", "asos.com",
    "carrefour.fr", "leclerc.fr", "auchan.fr", "monoprix.fr", "intermarche.com",
    "wikipedia.org", "facebook.com", "instagram.com", "youtube.com",
    "linkedin.com", "pinterest.fr", "pinterest.com", "tiktok.com", "x.com",
    "twitter.com", "reddit.com", "quora.com", "medium.com",
    "societe.com", "pappers.fr", "pagesjaunes.fr", "verif.com", "infogreffe.fr",
    "shopify.com", "wix.com", "squarespace.com", "wordpress.com", "prestashop.com",
    "google.com", "bing.com", "yahoo.com", "doctissimo.fr", "marmiton.org",
}
# Sous-domaines et chemins qui trahissent un article plutôt qu'une boutique.
_SKIP_HINT = ("blog.", "news.", "/blog/", "/actualites/", "/article", "/wiki/",
              "/forum", "/annuaire", "/comparatif", "/avis-")


def _domain(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _keep(url: str) -> bool:
    d = _domain(url)
    if not d or "." not in d:
        return False
    low = url.lower()
    if any(h in low for h in _SKIP_HINT):
        return False
    # On compare aussi le domaine parent : « shop.amazon.fr » doit sauter.
    parts = d.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in _SKIP:
            return False
    return True


def queries_for(sector: str, country: str = "fr") -> list[str]:
    """Requêtes complémentaires pour un secteur donné.

    Les empreintes Shopify (« Propulsé par Shopify ») sont mal indexées : on
    les tente, mais on ratisse surtout large et on qualifie ensuite en lisant
    les sites."""
    s = re.sub(r"\s+", " ", (sector or "").strip())
    if not s:
        return []
    return [
        f"{s} marque française boutique en ligne",
        f"{s} acheter en ligne site officiel",
        f'{s} "propulsé par Shopify"',
    ]


def _parse_brave(payload: dict) -> list[dict]:
    rows = ((payload.get("web") or {}).get("results") or [])
    return [{"title": r.get("title") or "", "url": r.get("url") or "",
             "snippet": r.get("description") or ""}
            for r in rows if isinstance(r, dict) and r.get("url")]


def _parse_serpapi(payload: dict) -> list[dict]:
    rows = payload.get("organic_results") or []
    return [{"title": r.get("title") or "", "url": r.get("link") or "",
             "snippet": r.get("snippet") or ""}
            for r in rows if isinstance(r, dict) and r.get("link")]


async def search(query: str, count: int = 20, country: str = "fr") -> dict:
    """Une requête chez le fournisseur configuré. Ne lève jamais."""
    prov = provider()
    if not prov:
        return {"status": DISABLED, "results": [],
                "error": "aucune clé de recherche configurée "
                         "(BRAVE_SEARCH_KEY ou SERPAPI_KEY)"}
    if _breaker_open():
        return {"status": UNAVAILABLE, "results": [],
                "error": "moteur de recherche injoignable"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as c:
            if prov == "brave":
                r = await c.get(BRAVE,
                                params={"q": query, "count": min(count, 20),
                                        "country": country, "search_lang": country},
                                headers={"Accept": "application/json",
                                         "X-Subscription-Token": os.environ["BRAVE_SEARCH_KEY"]})
            else:
                r = await c.get(SERPAPI,
                                params={"q": query, "engine": "google",
                                        "google_domain": f"google.{country}",
                                        "gl": country, "hl": country,
                                        "num": min(count, 20),
                                        "api_key": os.environ["SERPAPI_KEY"]})
        if r.status_code != 200:
            log.info("[websearch] %s -> HTTP %s", prov, r.status_code)
            _note_failure()
            return {"status": UNAVAILABLE, "results": [],
                    "error": f"{prov} a répondu HTTP {r.status_code}"}
        payload = r.json() or {}
    except Exception as exc:
        log.info("[websearch] %s -> %s", prov, type(exc).__name__)
        _note_failure()
        return {"status": UNAVAILABLE, "results": [],
                "error": "moteur de recherche injoignable"}

    _breaker.update(fails=0, until=0.0)
    rows = _parse_brave(payload) if prov == "brave" else _parse_serpapi(payload)
    return {"status": OK, "provider": prov, "results": rows, "error": ""}


def candidates(batches: list[list[dict]], limit: int = 30) -> list[dict]:
    """Domaines uniques et plausibles, issus de plusieurs recherches.

    On garde le premier titre rencontré pour chaque domaine : c'est en général
    la page d'accueil, donc le nom de la marque."""
    seen: dict[str, dict] = {}
    for rows in batches:
        for r in rows:
            url = r.get("url") or ""
            if not _keep(url):
                continue
            d = _domain(url)
            if d and d not in seen:
                seen[d] = {"domain": d, "title": (r.get("title") or "").strip(),
                           "url": f"https://{d}"}
            if len(seen) >= limit:
                return list(seen.values())
    return list(seen.values())
