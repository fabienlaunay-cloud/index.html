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
    # Amazon fptcustom UPLOAD field IDs are underscore-style (item_sku/item_name/
    # item_highlights). Hyphenated forms come from the Category Listings Report
    # and are rejected on upload (90012 / 90061).
    header = text.split("\n")[1]
    assert "item_sku" in header
    assert "item_name" in header
    assert "item_highlights" in header
    assert "item-sku" not in text
    assert "item-name" not in text
    assert "item-highlights" not in text
    assert "TEST-001" in text
    assert "\t" in text


def test_offer_template_rejected_for_new_products():
    """An offer/Listing-Loader template (no product columns) must not be used to
    CREATE new products — it would fail on Amazon with errors 8560 / 13013."""
    import os
    from app.utils.amazon_template_filler import (
        is_offer_only_template, fill_amazon_template, OFFER_TEMPLATE_FOR_NEW_PRODUCTS_MSG,
    )
    ll = os.path.join("data", "amazon_templates", "_LISTING_LOADER.xlsm")
    if not os.path.exists(ll):
        pytest.skip("bundled Listing Loader not present")
    data = open(ll, "rb").read()
    assert is_offer_only_template(data) is True
    with pytest.raises(ValueError):
        fill_amazon_template(data, [make_listing()], for_new_products=True)
    # Without the flag it still fills as an offer file (existing-product update).
    out = fill_amazon_template(data, [make_listing()], for_new_products=False)
    assert isinstance(out, (bytes, bytearray)) and len(out) > 0


def test_full_category_template_allowed_for_new_products():
    """A real category template (with product columns) fills fine for creation."""
    import os
    from app.utils.amazon_template_filler import is_offer_only_template, fill_amazon_template
    cat = os.path.join("data", "amazon_templates", "ANIMAL_COLLAR.xlsm")
    if not os.path.exists(cat):
        pytest.skip("bundled category template not present")
    data = open(cat, "rb").read()
    assert is_offer_only_template(data) is False
    out = fill_amazon_template(data, [make_listing()], for_new_products=True)
    assert isinstance(out, (bytes, bytearray)) and len(out) > 0


def test_empty_listings():
    assert to_csv_bytes([]) == b""
    assert to_json_bytes([]) == b"[]"
