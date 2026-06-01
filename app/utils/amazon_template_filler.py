"""
Fills an Amazon category-specific .xlsm/.xlsx inventory template with listing data.

Amazon templates have a fixed structure:
  Row 1: metadata / section labels
  Row 2: human-readable field labels (FR/EN)
  Row 3: machine-readable attribute names  ← column index built from this row
  Row 4+: data rows (we write here)
"""

import zipfile
import io
import re
from xml.etree import ElementTree as ET
from typing import List

import openpyxl
from openpyxl.styles import PatternFill

from app.models import AmazonListing

# Map our model fields → Amazon attribute name patterns
_FIELD_MAP = {
    "item_sku":                  lambda l: l.sku,
    "brand_name":                lambda l: l.brand,
    "manufacturer":              lambda l: l.brand,
    "part_number":               lambda l: l.sku,
    "item_name":                 lambda l: l.title,
    "product_description":       lambda l: _strip_html(l.description),
    "bullet_point1":             lambda l: l.bullet_points[0] if len(l.bullet_points) > 0 else "",
    "bullet_point2":             lambda l: l.bullet_points[1] if len(l.bullet_points) > 1 else "",
    "bullet_point3":             lambda l: l.bullet_points[2] if len(l.bullet_points) > 2 else "",
    "bullet_point4":             lambda l: l.bullet_points[3] if len(l.bullet_points) > 3 else "",
    "bullet_point5":             lambda l: l.bullet_points[4] if len(l.bullet_points) > 4 else "",
    "generic_keywords":          lambda l: l.backend_keywords,
    "external_product_id":       lambda l: l.ean or "",
    "external_product_id_type":  lambda l: "EAN" if l.ean else "",
    "update_delete":             lambda l: "PartialUpdate",
    "standard_price":            lambda l: str(l.price) if l.price else "",
    "list_price":                lambda l: str(l.price) if l.price else "",
    "quantity":                  lambda l: "1",
    "condition_type":            lambda l: "New",
    "condition_note":            lambda l: "",
}

_HTML_RE = re.compile(r'<[^>]+>')


def _strip_html(text: str) -> str:
    return _HTML_RE.sub('', text or '').strip()


def _get_shared_strings(zf: zipfile.ZipFile) -> list:
    with zf.open('xl/sharedStrings.xml') as f:
        tree = ET.parse(f)
    ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    return [
        ''.join(t.text or '' for t in si.findall('.//ns:t', ns))
        for si in tree.findall('.//ns:si', ns)
    ]


def _find_template_sheet(zf: zipfile.ZipFile) -> str:
    """Return the sheet name that contains the actual data template (row 3 has field names)."""
    sheets = sorted([n for n in zf.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')])
    shared = _get_shared_strings(zf)
    for sheet in sheets:
        rows = _read_sheet_rows(zf, sheet.split('/')[-1], shared, max_rows=4)
        if len(rows) >= 3:
            # Row 3 should have many non-empty cells and contain 'item_sku'
            row3 = rows[2] if len(rows) > 2 else []
            if any('item_sku' in (c or '') for c in row3):
                return sheet.split('/')[-1]
    return 'sheet7.xml'  # fallback for standard Amazon templates


def _read_sheet_rows(zf: zipfile.ZipFile, sheet_name: str, shared: list, max_rows: int = 5) -> list:
    with zf.open(f'xl/worksheets/{sheet_name}') as f:
        tree = ET.parse(f)
    ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    rows = []
    for row_el in tree.findall('.//ns:row', ns):
        cells = []
        for cell in row_el.findall('ns:c', ns):
            t = cell.get('t', '')
            v_el = cell.find('ns:v', ns)
            val = ''
            if v_el is not None and v_el.text is not None:
                val = shared[int(v_el.text)] if t == 's' else v_el.text
            cells.append(val)
        rows.append(cells)
        if len(rows) >= max_rows:
            break
    return rows


def fill_amazon_template(template_bytes: bytes, listings: List[AmazonListing]) -> bytes:
    """
    Read an Amazon category template (.xlsm/.xlsx), fill it with listing data,
    and return the filled workbook as .xlsx bytes.
    """
    # Open with openpyxl (keep_vba=True preserves macros structure but we save as xlsx)
    wb = openpyxl.load_workbook(io.BytesIO(template_bytes), keep_vba=True, data_only=True)

    # Find the template sheet (the one with field names in row 3)
    template_ws = None
    for ws in wb.worksheets:
        row3 = [str(ws.cell(3, c).value or '') for c in range(1, min(20, ws.max_column + 1))]
        if any('item_sku' in v for v in row3):
            template_ws = ws
            break

    if template_ws is None:
        raise ValueError("Impossible de trouver la feuille template dans ce fichier Amazon.")

    # Build column index: attribute_name → column number (from row 3)
    col_index: dict[str, int] = {}
    for col in range(1, template_ws.max_column + 1):
        val = str(template_ws.cell(3, col).value or '').strip()
        if val:
            col_index[val] = col

    # Get the feed_product_type from the template (col 1, row 1 metadata or existing data)
    feed_product_type = ""
    fpt_col = col_index.get("feed_product_type", 1)
    # Try to read from an existing example row or row 3 context
    for r in range(4, 8):
        v = template_ws.cell(r, fpt_col).value
        if v:
            feed_product_type = str(v)
            break

    # Write data starting at row 4 (clear any existing example rows first)
    start_row = 4
    for i, listing in enumerate(listings):
        row_num = start_row + i
        # Write feed_product_type if we found one
        if feed_product_type and fpt_col:
            template_ws.cell(row_num, fpt_col).value = feed_product_type

        # Fill mapped fields
        for attr, getter in _FIELD_MAP.items():
            if attr in col_index:
                value = getter(listing)
                if value:
                    template_ws.cell(row_num, col_index[attr]).value = value

    # Save to bytes as xlsx
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
