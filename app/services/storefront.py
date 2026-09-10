"""Catalogue public d'une boutique e-commerce, et écart avec Amazon.

L'argument de vente le plus efficace tient en une phrase : « vous avez 47
références sur votre site, 6 sur Amazon ». Encore faut-il la calculer, et
surtout pouvoir dire **lesquelles** manquent — un compteur seul ne se vend pas.

Ce module ne fait aucun appel réseau : il analyse ce qu'on lui donne. La
récupération, protégée contre les SSRF, reste dans `main.py`.
"""
import json
import re
import unicodedata as _ud

SHOPIFY, WOO, PRESTA, WIX, UNKNOWN = (
    "shopify", "woocommerce", "prestashop", "wix", "")

# Chemin public du catalogue Shopify. Ouvert par défaut sur toutes les
# boutiques, sauf désactivation explicite par le marchand.
SHOPIFY_CATALOG = "/products.json"
SHOPIFY_PAGE_SIZE = 250


def _fold(text: str) -> str:
    n = _ud.normalize("NFD", (text or "").lower())
    return "".join(c for c in n if _ud.category(c) != "Mn")


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _fold(text)).strip()


# ── Plateforme ───────────────────────────────────────────────────────────────
# Les cookies trahissent la plateforme bien plus sûrement que l'en-tête
# `Server`, qui n'annonce souvent que le CDN placé devant (Cloudflare, Fastly).
_SIGNS = (
    (SHOPIFY, ("_shopify_y", "_shopify_s", "cdn.shopify.com", "shopify-features",
               "myshopify.com")),
    (WOO,     ("woocommerce-cart-hash", "wp-content/plugins/woocommerce",
               "wc-ajax")),
    (PRESTA,  ("prestashop-", "/modules/ps_", "prestashop=")),
    (WIX,     ("x-wix-request-id", "static.parastorage.com", "wixstores")),
)


def detect_platform(html_text: str = "", headers: dict | None = None) -> str:
    hay = (html_text or "")[:400_000].lower()
    for k, v in (headers or {}).items():
        hay += f"\n{k}: {v}".lower()
    for name, needles in _SIGNS:
        if any(n in hay for n in needles):
            return name
    return UNKNOWN


# ── Catalogue Shopify ────────────────────────────────────────────────────────
def parse_shopify(raw: bytes | str) -> list[dict]:
    """Produits d'une page de `/products.json`. Renvoie [] si le format change."""
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for p in (data.get("products") or []) if isinstance(data, dict) else []:
        if not isinstance(p, dict):
            continue
        variants = [v for v in (p.get("variants") or []) if isinstance(v, dict)]
        prices = []
        for v in variants:
            try:
                prices.append(float(v.get("price")))
            except (TypeError, ValueError):
                continue
        images = [i.get("src") for i in (p.get("images") or [])
                  if isinstance(i, dict) and i.get("src")]
        out.append({
            "title": (p.get("title") or "").strip(),
            "handle": p.get("handle") or "",
            "vendor": (p.get("vendor") or "").strip(),
            "type": (p.get("product_type") or "").strip(),
            "variants": len(variants),
            "sku": next((v.get("sku") for v in variants if v.get("sku")), ""),
            "price": min(prices) if prices else None,
            "image": images[0] if images else "",
            "published": p.get("published_at") or "",
        })
    return out


def brand_from_catalog(products: list[dict]) -> str:
    """Marque déduite du champ `vendor`, que Shopify remplit presque toujours.

    Bien plus fiable que de deviner depuis le nom de domaine, et c'est cette
    valeur qui sert ensuite à chercher la marque sur Amazon."""
    counts: dict[str, int] = {}
    for p in products:
        v = (p.get("vendor") or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ""
    best, n = max(counts.items(), key=lambda kv: kv[1])
    # Un catalogue multimarque (revendeur) n'a pas de marque dominante : mieux
    # vaut ne rien affirmer que d'en désigner une au hasard.
    return best if n >= max(2, len(products) * 0.4) else ""


# ── Rapprochement site ↔ Amazon ──────────────────────────────────────────────
# Mots trop courants pour distinguer deux produits : les garder ferait matcher
# « Huile d'olive 50 cl » avec « Huile d'olive 1 L ».
_STOP = {
    "de", "des", "du", "la", "le", "les", "un", "une", "et", "aux", "au", "pour",
    "avec", "sans", "en", "sur", "par", "lot", "pack", "set", "kit", "ml", "cl",
    "gr", "kg", "cm", "mm", "taille", "size", "the", "and", "for", "with",
}


def _tokens(title: str, brand: str = "") -> set[str]:
    words = _key(title).split()
    drop = set(_key(brand).split())
    return {w for w in words
            if len(w) > 2 and w not in _STOP and w not in drop}


def match_gap(site_products: list[dict], amazon_titles: list[str],
              brand: str = "", threshold: float = 0.5) -> dict:
    """Quelles références du site n'ont pas d'équivalent sur Amazon.

    Le rapprochement se fait sur les jetons distinctifs du titre, marque
    retirée. `threshold` est la part des jetons du produit qu'il faut retrouver
    côté Amazon — 0,5 tolère une reformulation, 1,0 exigerait le titre exact,
    ce qu'aucun vendeur ne fait."""
    amz = [(t, _tokens(t, brand)) for t in amazon_titles if (t or "").strip()]
    present, missing = [], []
    for p in site_products:
        toks = _tokens(p.get("title") or "", brand)
        if not toks:
            continue
        best, best_score = "", 0.0
        for title, atoks in amz:
            if not atoks:
                continue
            score = len(toks & atoks) / len(toks)
            if score > best_score:
                best, best_score = title, score
        row = {"title": p.get("title") or "", "price": p.get("price"),
               "sku": p.get("sku") or "", "score": round(best_score, 2)}
        if best_score >= threshold:
            present.append({**row, "amazon": best})
        else:
            missing.append(row)

    # Les manquantes d'abord les plus chères : c'est le manque à gagner, et
    # c'est l'ordre dans lequel un vendeur veut les traiter.
    missing.sort(key=lambda r: (r["price"] is None, -(r["price"] or 0)))
    counted = len(present) + len(missing)
    return {
        "site_total": len(site_products),
        "compared": counted,
        "on_amazon": len(present),
        "missing": len(missing),
        "coverage": round(len(present) / counted * 100) if counted else None,
        "missing_items": missing[:40],
        "present_items": present[:20],
    }
