import pytest
from app.services.ingestion import parse_csv, parse_json, parse_excel


SAMPLE_CSV_SEMICOLON = b"""sku;nom;marque;prix;ean
SKU-001;Casque Audio;SoundMax;89.99;1234567890123
SKU-002;Tapis Yoga;ZenFit;39.95;
"""

SAMPLE_CSV_COMMA = b"""sku,name,brand,price,description
SKU-001,Wireless Headphones,SoundMax,89.99,Great audio quality
SKU-002,Yoga Mat,ZenFit,39.95,
"""

SAMPLE_JSON = b"""[
  {"sku": "SKU-001", "name": "Casque", "brand": "Test", "price": 49.99},
  {"sku": "SKU-002", "nom": "Tapis", "marque": "ZenFit", "prix": "39.95"}
]"""


def test_parse_csv_semicolon():
    products = parse_csv(SAMPLE_CSV_SEMICOLON)
    assert len(products) == 2
    assert products[0].sku == "SKU-001"
    assert products[0].name == "Casque Audio"
    assert products[0].price == 89.99
    assert products[0].ean == "1234567890123"


def test_parse_csv_comma():
    products = parse_csv(SAMPLE_CSV_COMMA)
    assert len(products) == 2
    assert products[0].brand == "SoundMax"


def test_parse_json():
    products = parse_json(SAMPLE_JSON)
    assert len(products) == 2
    assert products[0].sku == "SKU-001"
    assert products[1].name == "Tapis"  # mapped from "nom"
    assert products[1].price == 39.95   # mapped from "prix", coerced from string


def test_parse_csv_pipe_features():
    csv = "sku;nom;caractéristiques\nSKU-001;Produit;Feature1|Feature2|Feature3\n".encode("utf-8")
    products = parse_csv(csv)
    assert products[0].features == ["Feature1", "Feature2", "Feature3"]


def test_price_coercion():
    csv = b"sku;nom;prix\nSKU-001;Produit;12,99\n"
    products = parse_csv(csv)
    assert products[0].price == 12.99


def test_missing_sku_fallback():
    csv = b"nom;marque\nProduit Sans SKU;Marque\n"
    products = parse_csv(csv)
    assert len(products) == 1
    assert products[0].sku  # should have a fallback
