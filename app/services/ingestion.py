import io
import csv
import json
from typing import List, BinaryIO
from app.models import RawProduct


COLUMN_MAP = {
    # SKU / référence
    "sku": "sku", "référence": "sku", "ref": "sku", "reference": "sku", "id": "sku",
    # Nom
    "nom": "name", "name": "name", "titre": "name", "title": "name", "libellé": "name",
    # Marque
    "marque": "brand", "brand": "brand",
    # Catégorie
    "catégorie": "category", "category": "category", "cat": "category",
    # Description
    "description": "description", "desc": "description",
    # Prix
    "prix": "price", "price": "price", "tarif": "price",
    # EAN
    "ean": "ean", "gtin": "ean", "barcode": "ean", "code barre": "ean",
    # Poids
    "poids": "weight_kg", "weight": "weight_kg", "weight_kg": "weight_kg",
    # Dimensions
    "dimensions": "dimensions_cm", "dim": "dimensions_cm",
    # Couleur / Matière
    "couleur": "color", "color": "color", "colour": "color",
    "matière": "material", "material": "material", "matériau": "material",
    # Images
    "images": "images", "image": "images", "photo": "images", "photos": "images",
    "image url": "images", "image_url": "images", "photo url": "images", "photo_url": "images",
    "url image": "images", "url photo": "images",
    # Caractéristiques
    "caractéristiques": "features", "features": "features", "points clés": "features",
    # Mots-clés produit
    "mots clés": "focus_keywords", "mots_clés": "focus_keywords", "mots-clés": "focus_keywords",
    "mots cles": "focus_keywords", "mots_cles": "focus_keywords",
    "keywords": "focus_keywords", "search terms": "focus_keywords",
    # Segment (alias catégorie)
    "segment": "category",
}


def _normalise_header(h: str) -> str:
    return h.strip().lower().replace("_", " ")


def _map_row(row: dict) -> dict:
    mapped: dict = {}
    extra: dict = {}
    for key, value in row.items():
        norm = _normalise_header(key)
        target = COLUMN_MAP.get(norm)
        if target:
            mapped[target] = value
        else:
            extra[key] = value
    mapped.setdefault("extra", extra)
    return mapped


def _coerce(mapped: dict) -> RawProduct:
    if "price" in mapped and mapped["price"] not in (None, ""):
        try:
            mapped["price"] = float(str(mapped["price"]).replace(",", "."))
        except ValueError:
            mapped.pop("price", None)

    if "weight_kg" in mapped and mapped["weight_kg"] not in (None, ""):
        try:
            mapped["weight_kg"] = float(str(mapped["weight_kg"]).replace(",", "."))
        except ValueError:
            mapped.pop("weight_kg", None)

    for list_field in ("images", "features"):
        if list_field in mapped and isinstance(mapped[list_field], str):
            mapped[list_field] = [
                v.strip() for v in mapped[list_field].split("|") if v.strip()
            ]

    if "focus_keywords" in mapped and isinstance(mapped["focus_keywords"], str):
        mapped["focus_keywords"] = [
            k.strip() for k in mapped["focus_keywords"].replace(";", ",").split(",") if k.strip()
        ]

    if not mapped.get("sku"):
        mapped["sku"] = mapped.get("name", "UNKNOWN")[:20]

    return RawProduct(**{k: v for k, v in mapped.items() if v not in (None, "", [])})


def parse_csv(content: bytes, delimiter: str = None) -> List[RawProduct]:
    text = content.decode("utf-8-sig", errors="replace")
    if delimiter is None:
        sample = text[:2000]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    products = []
    for row in reader:
        try:
            products.append(_coerce(_map_row(dict(row))))
        except Exception:
            pass
    return products


def parse_excel(content: bytes) -> List[RawProduct]:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl manquant — pip install openpyxl")

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active
    headers = [str(cell.value or "").strip() for cell in next(ws.iter_rows(max_row=1))]
    products = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        row_dict = {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
        if all(v is None for v in row_dict.values()):
            continue
        try:
            products.append(_coerce(_map_row(row_dict)))
        except Exception:
            pass
    return products


def parse_json(content: bytes) -> List[RawProduct]:
    data = json.loads(content)
    if isinstance(data, dict):
        data = data.get("products", data.get("items", [data]))
    products = []
    for item in data:
        try:
            products.append(_coerce(_map_row(item)))
        except Exception:
            pass
    return products


def parse_file(filename: str, content: bytes) -> List[RawProduct]:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "csv":
        return parse_csv(content)
    if ext in ("xlsx", "xls"):
        return parse_excel(content)
    if ext == "json":
        return parse_json(content)
    raise ValueError(f"Format non supporté: .{ext}  (csv, xlsx, json acceptés)")
