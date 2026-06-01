import csv
import json
import io
from typing import List
from app.models import AmazonListing


def to_csv_bytes(listings: List[AmazonListing]) -> bytes:
    output = io.StringIO()
    if not listings:
        return b""
    fieldnames = [
        "sku", "marketplace", "title", "bullet_point_1", "bullet_point_2",
        "bullet_point_3", "bullet_point_4", "bullet_point_5",
        "description", "backend_keywords", "brand", "category",
        "price", "ean", "seo_score",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for listing in listings:
        bullets = listing.bullet_points + [""] * 5
        writer.writerow({
            "sku": listing.sku,
            "marketplace": listing.marketplace.value,
            "title": listing.title,
            "bullet_point_1": bullets[0],
            "bullet_point_2": bullets[1],
            "bullet_point_3": bullets[2],
            "bullet_point_4": bullets[3],
            "bullet_point_5": bullets[4],
            "description": listing.description,
            "backend_keywords": listing.backend_keywords,
            "brand": listing.brand,
            "category": listing.category,
            "price": listing.price or "",
            "ean": listing.ean or "",
            "seo_score": listing.seo_score or "",
        })
    return output.getvalue().encode("utf-8-sig")


def to_json_bytes(listings: List[AmazonListing]) -> bytes:
    return json.dumps(
        [l.model_dump() for l in listings],
        ensure_ascii=False,
        indent=2,
        default=str,
    ).encode("utf-8")


import re as _re

def _strip_html(text: str) -> str:
    return _re.sub(r'<[^>]+>', '', text or '').strip()


def to_amazon_flat_file_bytes(listings: List[AmazonListing]) -> bytes:
    """Amazon Listing Loader flat file (tab-separated, UTF-8-BOM).

    Compatible with Seller Central > Inventory > Add Products via Spreadsheet.
    Uses the standard Listing Loader template columns accepted across categories.
    """
    output = io.StringIO()
    headers = [
        "item-sku",
        "update-delete",
        "item-name",
        "brand-name",
        "manufacturer",
        "item-type",
        "product-description",
        "bullet-point1",
        "bullet-point2",
        "bullet-point3",
        "bullet-point4",
        "bullet-point5",
        "generic-keywords",
        "standard-price",
        "quantity",
        "external-product-id",
        "external-product-id-type",
        "condition-type",
    ]
    # Row 1 : column names
    output.write("\t".join(headers) + "\n")
    # Row 2 : template metadata (required by Amazon parser)
    output.write("\t".join(["TemplateType=fptcustom", "Version=2021.1201"] + [""] * (len(headers) - 2)) + "\n")

    for listing in listings:
        bullets = listing.bullet_points + [""] * 5
        row = [
            listing.sku,
            "a",                                        # add / partial-update
            listing.title,
            listing.brand,
            listing.brand,
            listing.category.lower().replace(" ", "_") or "home",
            _strip_html(listing.description),           # no HTML in flat files
            bullets[0], bullets[1], bullets[2], bullets[3], bullets[4],
            listing.backend_keywords,
            str(listing.price) if listing.price else "",
            "1",                                        # quantity placeholder
            listing.ean or "",
            "EAN" if listing.ean else "",
            "New",
        ]
        output.write("\t".join(row) + "\n")

    return output.getvalue().encode("utf-8-sig")
