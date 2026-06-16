"""
Extracts product data from an e-commerce URL.
Priority: JSON-LD Schema.org Product > Open Graph meta tags > HTML fallback.
"""

import re
import json
import html as html_lib
from typing import Optional
from urllib.parse import urljoin, urlparse

_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _clean(text: str) -> str:
    return _HTML_TAG_RE.sub('', html_lib.unescape(text or '')).strip()


def _normalize_images(images: list, base_url: str) -> list:
    """Make image URLs absolute and drop junk (data:, svg, placeholders, dupes).
    A relative or broken URL can't be fetched as a generation reference, which is
    why listings end up with 100% synthetic visuals instead of the real product."""
    out, seen = [], set()
    for raw in images or []:
        u = (raw or '').strip()
        if not u or u.startswith('data:'):
            continue
        # protocol-relative or relative → absolutize against the page URL
        if u.startswith('//'):
            u = 'https:' + u
        elif not u.startswith(('http://', 'https://')):
            u = urljoin(base_url, u)
        if not u.startswith(('http://', 'https://')):
            continue
        low = u.lower()
        if low.endswith('.svg') or 'sprite' in low or 'placeholder' in low or 'blank.' in low:
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_json_ld(html_text: str) -> Optional[dict]:
    """Return the first JSON-LD object whose @type is Product."""
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.DOTALL | re.IGNORECASE,
    )
    for raw in scripts:
        try:
            data = json.loads(raw.strip())
            items = (
                data.get('@graph', [data]) if isinstance(data, dict)
                else data if isinstance(data, list)
                else [data]
            )
            for item in items:
                if isinstance(item, dict) and str(item.get('@type', '')).lower() == 'product':
                    return item
        except Exception:
            continue
    return None


def _meta(html_text: str, *props: str) -> Optional[str]:
    """Extract the first matching meta/OG tag value."""
    for prop in props:
        for pattern in [
            rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']',
            rf'<meta[^>]+name=["\']{prop}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{prop}["\']',
        ]:
            m = re.search(pattern, html_text, re.IGNORECASE)
            if m:
                return html_lib.unescape(m.group(1).strip())
    return None


def scrape_product(html_text: str, url: str) -> dict:
    """
    Extract product fields from HTML text.
    Returns a dict with keys compatible with RawProduct:
      name, brand, description, price, ean, sku, images, color, material
    """
    result: dict = {}

    # ── 1. JSON-LD (most reliable) ─────────────────────────────────────────────
    ld = _extract_json_ld(html_text)
    if ld:
        if ld.get('name'):
            result['name'] = _clean(ld['name'])
        if ld.get('description'):
            result['description'] = _clean(ld['description'])

        brand = ld.get('brand')
        if brand:
            result['brand'] = _clean(brand.get('name', '') if isinstance(brand, dict) else str(brand))

        offers = ld.get('offers') or ld.get('offer')
        if offers:
            offer = (offers[0] if isinstance(offers, list) else offers)
            if isinstance(offer, dict):
                price_val = offer.get('price') or offer.get('lowPrice')
                if price_val:
                    try:
                        result['price'] = float(str(price_val).replace(',', '.'))
                    except Exception:
                        pass

        for gtin_key in ('gtin13', 'gtin14', 'gtin', 'gtin8'):
            val = ld.get(gtin_key)
            if val and len(str(val)) in (8, 12, 13, 14):
                result['ean'] = str(val)
                break

        if ld.get('sku'):
            result['sku'] = str(ld['sku'])[:30]

        img = ld.get('image')
        if img:
            if isinstance(img, str):
                result['images'] = [img]
            elif isinstance(img, list):
                result['images'] = [
                    i if isinstance(i, str) else i.get('url', i.get('contentUrl', ''))
                    for i in img[:5] if i
                ]
            elif isinstance(img, dict):
                result['images'] = [img.get('url', img.get('contentUrl', ''))]

        for prop in ld.get('additionalProperty', []):
            if not isinstance(prop, dict):
                continue
            prop_name = str(prop.get('name', '')).lower()
            prop_val = _clean(str(prop.get('value', '')))
            if not result.get('color') and any(w in prop_name for w in ('color', 'couleur')):
                result['color'] = prop_val
            if not result.get('material') and any(w in prop_name for w in ('material', 'matière', 'matiere')):
                result['material'] = prop_val

    # ── 2. Open Graph / meta fallback ─────────────────────────────────────────
    if not result.get('name'):
        v = _meta(html_text, 'title')
        if v:
            result['name'] = v

    if not result.get('description'):
        v = _meta(html_text, 'description', 'description')
        if v:
            result['description'] = v

    if not result.get('images'):
        v = _meta(html_text, 'image')
        if v:
            result['images'] = [v]

    if not result.get('price'):
        v = _meta(html_text, 'price:amount')
        if v:
            try:
                result['price'] = float(v.replace(',', '.'))
            except Exception:
                pass

    # ── 3. HTML fallback (h1 for title) ───────────────────────────────────────
    if not result.get('name'):
        m = re.search(r'<h1[^>]*>([^<]{3,200})</h1>', html_text, re.IGNORECASE)
        if m:
            result['name'] = _clean(m.group(1))

    # ── 4. SKU from URL slug if not found ─────────────────────────────────────
    if not result.get('sku'):
        slug = url.rstrip('/').split('/')[-1].split('?')[0]
        result['sku'] = re.sub(r'[^a-zA-Z0-9\-_]', '-', slug)[:20].strip('-') or 'URL-PRODUCT'

    if not result.get('name'):
        raise ValueError(
            "Impossible d'extraire le nom du produit depuis cette URL. "
            "Le site bloque peut-être les requêtes automatiques (Cloudflare, etc.)."
        )

    # Absolutize + clean image URLs so they can be used as a generation reference
    result['images'] = _normalize_images(result.get('images'), url)

    return result
