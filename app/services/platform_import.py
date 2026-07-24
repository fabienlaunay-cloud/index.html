"""Reverse import — recognize e-commerce/PIM platform exports automatically.

A client migrating to (or optimizing with) SynqIO usually starts from an
existing catalog export. Instead of forcing them through the manual column-
mapping modal, we detect the platform from the header signature and convert
rows straight to RawProduct:

- Shopify   product export CSV  (Handle / Title / Body (HTML) / Variant SKU…)
- WooCommerce product export CSV (Type / SKU / Name / Regular price…)
- PrestaShop product export CSV  (";" separated — Reference / EAN13 / Price…)
- Akeneo    PIM export CSV       (";" separated, locale-scoped: name-fr_FR…)

`detect_platform(headers)` returns the platform id or None; `parse_platform`
converts the file. Generic files keep going through the existing COLUMN_MAP
flow untouched.
"""
import csv
import io
import re
from typing import List, Optional

from app.models import RawProduct

PLATFORM_LABELS = {
    "shopify": "Shopify",
    "woocommerce": "WooCommerce",
    "prestashop": "PrestaShop",
    "akeneo": "Akeneo PIM",
}

_LOCALE_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _norm(h: str) -> str:
    return (h or "").strip().lower()


def detect_platform(headers: List[str]) -> Optional[str]:
    """Identify the source platform from its header signature."""
    hs = {_norm(h) for h in headers if h}
    if "handle" in hs and ("variant sku" in hs or "body (html)" in hs):
        return "shopify"
    if "regular price" in hs and ("short description" in hs or "is featured?" in hs):
        return "woocommerce"
    if ("ean13" in hs or "reference #" in hs) and any(
            h.startswith("price tax") or h == "active (0/1)" for h in hs):
        return "prestashop"
    if "sku" in hs and any(re.match(r"^(name|label|description)-[a-z]{2}_[a-z]{2}$", h, re.I) for h in hs):
        return "akeneo"
    return None


def _split_urls(cell: str) -> List[str]:
    return [u.strip() for u in (cell or "").split(",") if u.strip().startswith("http")]


def _to_price(raw) -> Optional[float]:
    try:
        v = float(str(raw).replace(",", ".").replace("€", "").strip())
        return v if v > 0 else None
    except Exception:
        return None


def _rows(content: bytes, delimiter: str) -> List[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


# ── Shopify ──────────────────────────────────────────────────────────────────

def _parse_shopify(content: bytes) -> List[RawProduct]:
    """Group rows by Handle: the first titled row is the product; extra rows
    contribute images and variants."""
    by_handle: dict = {}
    order: list = []
    for row in _rows(content, ","):
        g = lambda k: (row.get(k) or "").strip()
        handle = g("Handle")
        if not handle:
            continue
        slot = by_handle.setdefault(handle, {"main": None, "images": [], "variants": []})
        if handle not in order:
            order.append(handle)
        if g("Title") and slot["main"] is None:
            slot["main"] = row
        img = g("Image Src")
        if img and img not in slot["images"]:
            slot["images"].append(img)
        if g("Variant SKU"):
            slot["variants"].append(row)

    products: List[RawProduct] = []
    for handle in order:
        slot = by_handle[handle]
        main = slot["main"]
        if main is None:
            continue
        g = lambda row, k: (row.get(k) or "").strip()
        variants = slot["variants"] or [main]
        multi = len({g(v, "Variant SKU") for v in variants if g(v, "Variant SKU")}) > 1
        for v in variants:
            sku = g(v, "Variant SKU") or handle
            opt = g(v, "Option1 Value")
            products.append(RawProduct(
                sku=sku,
                name=g(main, "Title") + (f" — {opt}" if multi and opt and opt != "Default Title" else ""),
                brand=g(main, "Vendor") or None,
                category=g(main, "Type") or None,
                description=g(main, "Body (HTML)") or None,
                price=_to_price(g(v, "Variant Price")),
                ean=g(v, "Variant Barcode") or None,
                weight_kg=(float(g(v, "Variant Grams")) / 1000) if g(v, "Variant Grams").isdigit() else None,
                images=slot["images"],
                focus_keywords=[t.strip() for t in g(main, "Tags").split(",") if t.strip()][:8],
                parent_sku=handle if multi else None,
                variation_value=opt if (multi and opt and opt != "Default Title") else None,
            ))
    return products


# ── WooCommerce ──────────────────────────────────────────────────────────────

def _parse_woocommerce(content: bytes) -> List[RawProduct]:
    products = []
    for row in _rows(content, ","):
        g = lambda k: (row.get(k) or "").strip()
        sku, name = g("SKU"), g("Name")
        if not name or (g("Type") and g("Type") not in ("simple", "variable", "variation")):
            continue
        brand = g("Meta: brand")
        if not brand and "marque" in _norm(g("Attribute 1 name")):
            brand = g("Attribute 1 value(s)")
        products.append(RawProduct(
            sku=sku or name[:40],
            name=name,
            brand=brand or None,
            category=(g("Categories").split(">")[-1].split(",")[0].strip() or None),
            description=g("Description") or g("Short description") or None,
            price=_to_price(g("Regular price")),
            ean=g("GTIN, UPC, EAN, or ISBN") or None,
            weight_kg=_to_price(g("Weight (kg)")),
            images=_split_urls(g("Images")),
            focus_keywords=[t.strip() for t in g("Tags").split(",") if t.strip()][:8],
        ))
    return products


# ── PrestaShop ───────────────────────────────────────────────────────────────

def _parse_prestashop(content: bytes) -> List[RawProduct]:
    products = []
    for row in _rows(content, ";"):
        # PrestaShop headers vary slightly by version — match loosely
        def find(*needles):
            for k, v in row.items():
                nk = _norm(k)
                if any(n in nk for n in needles):
                    return (v or "").strip()
            return ""
        name = find("name")
        if not name:
            continue
        products.append(RawProduct(
            sku=find("reference") or name[:40],
            name=name,
            brand=find("brand", "manufacturer") or None,
            category=(find("categories").split(",")[-1].strip() or None),
            description=find("description") or find("summary") or None,
            price=_to_price(find("price tax included", "price tax excluded", "price")),
            ean=find("ean13", "ean") or None,
            weight_kg=_to_price(find("weight")),
            images=_split_urls(find("image url")),
            focus_keywords=[t.strip() for t in find("tags").split(",") if t.strip()][:8],
        ))
    return products


# ── Akeneo ───────────────────────────────────────────────────────────────────

def _strip_akeneo_suffixes(header: str) -> str:
    """name-fr_FR → name · price-EUR → price · description-fr_FR-ecommerce → description"""
    parts = (header or "").split("-")
    while len(parts) > 1 and (_LOCALE_RE.match(parts[-1]) or _CURRENCY_RE.match(parts[-1])
                              or parts[-1] in ("ecommerce", "mobile", "print")):
        parts.pop()
    return "-".join(parts)


def _parse_akeneo(content: bytes) -> List[RawProduct]:
    products = []
    for row in _rows(content, ";"):
        flat: dict = {}
        for k, v in row.items():
            base = _norm(_strip_akeneo_suffixes(k))
            if base not in flat or (v or "").strip():
                flat[base] = (v or "").strip()
        g = lambda k: flat.get(k, "")
        sku = g("sku") or g("identifier")
        name = g("name") or g("label") or g("title")
        if not sku or not name:
            continue
        bullets = [g(f"bullet_{i}") for i in range(1, 6)]
        products.append(RawProduct(
            sku=sku,
            name=name,
            brand=g("brand") or g("marque") or None,
            category=(g("categories").split(",")[0].replace("_", " ").strip() or None),
            description=g("description") or g("short_description") or None,
            price=_to_price(g("price")),
            ean=g("ean") or g("gtin") or None,
            color=g("color") or g("couleur") or None,
            size=g("size") or None,
            material=g("material") or None,
            weight_kg=_to_price(g("weight")),
            images=_split_urls(g("image_urls") or g("images") or g("image")),
            features=[b for b in bullets if b],
            focus_keywords=[t.strip() for t in g("keywords").replace(";", ",").split(",") if t.strip()][:8],
        ))
    return products


_PARSERS = {
    "shopify": _parse_shopify,
    "woocommerce": _parse_woocommerce,
    "prestashop": _parse_prestashop,
    "akeneo": _parse_akeneo,
}


def parse_platform(content: bytes, platform: str) -> List[RawProduct]:
    parser = _PARSERS.get(platform)
    if not parser:
        raise ValueError(f"Plateforme inconnue : {platform}")
    return parser(content)
