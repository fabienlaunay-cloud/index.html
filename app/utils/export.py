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


def to_amazon_flat_file_bytes(listings: List[AmazonListing]) -> bytes:
    """Amazon flat file format (tab-separated, UTF-8)."""
    output = io.StringIO()
    headers = [
        "item-sku", "item-name", "brand-name", "manufacturer",
        "product-description", "bullet-point1", "bullet-point2",
        "bullet-point3", "bullet-point4", "bullet-point5",
        "generic-keywords", "standard-price", "external-product-id",
        "external-product-id-type",
    ]
    output.write("\t".join(headers) + "\n")
    output.write("\t".join(["TemplateType=fptcustom"] + [""] * (len(headers) - 1)) + "\n")
    output.write("\t".join(["Version=2014.0901"] + [""] * (len(headers) - 1)) + "\n")

    for listing in listings:
        bullets = listing.bullet_points + [""] * 5
        row = [
            listing.sku,
            listing.title,
            listing.brand,
            listing.brand,
            listing.description,
            bullets[0], bullets[1], bullets[2], bullets[3], bullets[4],
            listing.backend_keywords,
            str(listing.price) if listing.price else "",
            listing.ean or "",
            "EAN" if listing.ean else "",
        ]
        output.write("\t".join(row) + "\n")

    return output.getvalue().encode("utf-8-sig")
