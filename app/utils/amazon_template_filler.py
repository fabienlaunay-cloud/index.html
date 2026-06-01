"""
Fills an Amazon inventory template (.xlsm/.xlsx) with listing data.

Supports both Amazon template types:
  - Category templates (new products): attributeRow=3, dataRow=4
    e.g. ANIMAL_COLLAR.xlsm — has item_sku, item_name, bullet_point1…
  - Listing Loader (existing products): attributeRow=5, dataRow=7
    e.g. ListingLoader.xlsm — has contribution_sku#1.value, purchasable_offer[...]…

Row 1 metadata contains `attributeRow=N&dataRow=M` which we parse to locate
the correct rows automatically.
"""

import io
import re
import urllib.parse
from typing import List, Optional

import openpyxl

from app.models import AmazonListing

_HTML_RE = re.compile(r'<[^>]+>')


def _strip_html(text: str) -> str:
    return _HTML_RE.sub('', text or '').strip()


def _parse_template_settings(wb: openpyxl.Workbook) -> dict:
    """
    Read row 1 of the first sheet that contains 'settings=' metadata.
    Returns dict with attributeRow, dataRow (1-indexed).
    """
    defaults = {"attributeRow": 3, "dataRow": 4}
    for ws in wb.worksheets:
        cell_val = str(ws.cell(1, 1).value or '')
        # settings is sometimes in col 1 or further right
        for col in range(1, min(10, ws.max_column + 1)):
            val = str(ws.cell(1, col).value or '')
            if 'settings=' in val or 'attributeRow=' in val:
                # Parse as URL query string
                qs = val.split('settings=', 1)[-1] if 'settings=' in val else val
                params = dict(urllib.parse.parse_qsl(qs))
                result = {**defaults}
                if 'attributeRow' in params:
                    result['attributeRow'] = int(params['attributeRow'])
                if 'dataRow' in params:
                    result['dataRow'] = int(params['dataRow'])
                return result
    return defaults


def _find_template_worksheet(wb: openpyxl.Workbook, attr_row: int) -> Optional[openpyxl.worksheet.worksheet.Worksheet]:
    """Return the worksheet whose attributeRow contains recognisable column names."""
    markers = {'item_sku', 'contribution_sku', 'external_product_id', 'externally_assigned', 'record_action'}
    for ws in wb.worksheets:
        for col in range(1, min(ws.max_column + 1, 20)):
            val = str(ws.cell(attr_row, col).value or '').lower()
            if any(m in val for m in markers):
                return ws
    # fallback: return first worksheet with content at that row
    for ws in wb.worksheets:
        if ws.cell(attr_row, 1).value:
            return ws
    return wb.active


def _build_col_index(ws, attr_row: int) -> dict[str, int]:
    """Map attribute_name → column number from the given row."""
    index = {}
    for col in range(1, ws.max_column + 1):
        val = str(ws.cell(attr_row, col).value or '').strip()
        if val:
            index[val] = col
    return index


# ── Mapping helpers ────────────────────────────────────────────────────────────

def _get_value(col_index: dict, listing: AmazonListing, *keys) -> tuple[int, str]:
    """Return (col, value) for the first matching key, or (None, None)."""
    for key in keys:
        if key in col_index:
            return col_index[key], key
    # Pattern match for complex keys (e.g. purchasable_offer[...])
    for pattern_fn, value_fn, match_keys in _PATTERN_RULES:
        for key in match_keys:
            for attr, col in col_index.items():
                if key in attr:
                    return col, attr
    return None, None


# Simple exact-match rules: attribute_name → getter(listing)
_EXACT_MAP = {
    # Category template style
    "item_sku":                               lambda l: l.sku,
    "brand_name":                             lambda l: l.brand,
    "manufacturer":                           lambda l: l.brand,
    "part_number":                            lambda l: l.sku,
    "item_name":                              lambda l: l.title,
    "product_description":                    lambda l: _strip_html(l.description),
    "bullet_point1":                          lambda l: l.bullet_points[0] if len(l.bullet_points) > 0 else "",
    "bullet_point2":                          lambda l: l.bullet_points[1] if len(l.bullet_points) > 1 else "",
    "bullet_point3":                          lambda l: l.bullet_points[2] if len(l.bullet_points) > 2 else "",
    "bullet_point4":                          lambda l: l.bullet_points[3] if len(l.bullet_points) > 3 else "",
    "bullet_point5":                          lambda l: l.bullet_points[4] if len(l.bullet_points) > 4 else "",
    "generic_keywords":                       lambda l: l.backend_keywords,
    "external_product_id":                    lambda l: l.ean or "",
    "external_product_id_type":               lambda l: "EAN" if l.ean else "",
    "update_delete":                          lambda l: "PartialUpdate",
    "standard_price":                         lambda l: str(l.price) if l.price else "",
    "quantity":                               lambda l: "1",
    "condition_type":                         lambda l: "New",
    # Listing Loader style (new format)
    "contribution_sku#1.value":               lambda l: l.sku,
    "::record_action":                        lambda l: "partial_update",
    "externally_assigned_product_identifier#1.type":  lambda l: "EAN" if l.ean else "",
    "externally_assigned_product_identifier#1.value": lambda l: l.ean or "",
    "condition_type#1.value":                 lambda l: "new",
    "fulfillment_availability#1.quantity":    lambda l: "1",
}

# Pattern-based rules for columns whose names vary by marketplace ID
_PATTERN_RULES = [
    # Price: any column containing 'our_price' and 'value_with_tax'
    (lambda attr: 'our_price' in attr and 'value_with_tax' in attr and 'B2B' not in attr,
     lambda l: str(l.price) if l.price else ""),
]


def fill_amazon_template(template_bytes: bytes, listings: List[AmazonListing]) -> bytes:
    """
    Read an Amazon category template (.xlsm/.xlsx), detect its structure,
    fill it with listing data, and return the result as .xlsx bytes.
    """
    wb = openpyxl.load_workbook(io.BytesIO(template_bytes), keep_vba=True, data_only=True)

    # 1. Parse settings to find attributeRow and dataRow
    settings = _parse_template_settings(wb)
    attr_row = settings['attributeRow']
    data_row = settings['dataRow']

    # 2. Find the worksheet that has the template
    ws = _find_template_worksheet(wb, attr_row)
    if ws is None:
        raise ValueError("Impossible de trouver la feuille template dans ce fichier Amazon.")

    # 3. Build column index from attribute row
    col_index = _build_col_index(ws, attr_row)

    # 4. For category templates: get feed_product_type from existing content
    feed_product_type = ""
    fpt_col = col_index.get("feed_product_type")
    if fpt_col:
        for r in range(attr_row + 1, attr_row + 5):
            v = ws.cell(r, fpt_col).value
            if v:
                feed_product_type = str(v)
                break

    # 5. Write listing data starting at data_row
    for i, listing in enumerate(listings):
        row_num = data_row + i

        # Feed product type (category templates only)
        if feed_product_type and fpt_col:
            ws.cell(row_num, fpt_col).value = feed_product_type

        # Exact-match attributes
        for attr, getter in _EXACT_MAP.items():
            if attr in col_index:
                value = getter(listing)
                if value:
                    ws.cell(row_num, col_index[attr]).value = value

        # Pattern-match attributes (e.g. price with marketplace ID in name)
        for (pattern_fn, value_fn) in _PATTERN_RULES:
            for attr, col in col_index.items():
                if pattern_fn(attr):
                    value = value_fn(listing)
                    if value and not ws.cell(row_num, col).value:
                        ws.cell(row_num, col).value = value
                    break  # fill only first matching price column

    # 6. Save as xlsx
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
