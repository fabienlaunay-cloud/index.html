import pytest
from app.models import AmazonListing, Marketplace
from app.utils.export import to_csv_bytes, to_json_bytes, to_amazon_flat_file_bytes


def make_listing(**kwargs) -> AmazonListing:
    defaults = dict(
        sku="TEST-001",
        title="Titre produit test",
        bullet_points=["Point 1", "Point 2", "Point 3"],
        description="Description test",
        backend_keywords="keyword1 keyword2 keyword3",
        brand="TestBrand",
        category="Electronics",
        price=49.99,
        ean="1234567890123",
        seo_score=75,
        marketplace=Marketplace.AMAZON_FR,
    )
    defaults.update(kwargs)
    return AmazonListing(**defaults)


def test_csv_export():
    listings = [make_listing(), make_listing(sku="TEST-002")]
    data = to_csv_bytes(listings)
    text = data.decode("utf-8-sig")
    assert "TEST-001" in text
    assert "TEST-002" in text
    assert "bullet_point_1" in text
    assert "Point 1" in text


def test_json_export():
    import json
    listings = [make_listing()]
    data = to_json_bytes(listings)
    result = json.loads(data)
    assert isinstance(result, list)
    assert result[0]["sku"] == "TEST-001"
    assert result[0]["seo_score"] == 75


def test_flat_file_export():
    listings = [make_listing()]
    data = to_amazon_flat_file_bytes(listings)
    text = data.decode("utf-8-sig")
    # Amazon fptcustom requires the SKU column to be named exactly "sku"
    # and the highlights column "item_highlights" (underscore), not hyphenated.
    assert "\tsku\t" in ("\t" + text.split("\n")[1] + "\t")
    assert "item_highlights" in text
    assert "item-sku" not in text
    assert "item-highlights" not in text
    assert "TEST-001" in text
    assert "\t" in text


def test_empty_listings():
    assert to_csv_bytes([]) == b""
    assert to_json_bytes([]) == b"[]"
