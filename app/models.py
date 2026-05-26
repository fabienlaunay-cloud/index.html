from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum


class Marketplace(str, Enum):
    AMAZON_FR = "amazon_fr"
    AMAZON_DE = "amazon_de"
    AMAZON_IT = "amazon_it"
    AMAZON_ES = "amazon_es"
    AMAZON_UK = "amazon_uk"
    CDISCOUNT = "cdiscount"
    FNAC = "fnac"
    BOL = "bol"


class RawProduct(BaseModel):
    sku: str
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    ean: Optional[str] = None
    weight_kg: Optional[float] = None
    dimensions_cm: Optional[str] = None
    color: Optional[str] = None
    material: Optional[str] = None
    features: Optional[List[str]] = []
    images: Optional[List[str]] = []
    extra: Optional[dict] = {}


class AmazonListing(BaseModel):
    sku: str
    title: str = Field(..., max_length=200)
    bullet_points: List[str] = Field(..., min_length=1, max_length=5)
    description: str
    backend_keywords: str = Field(..., max_length=249)
    brand: str
    category: str
    price: Optional[float] = None
    ean: Optional[str] = None
    a_plus_content: Optional[dict] = None
    seo_score: Optional[int] = None
    marketplace: Marketplace = Marketplace.AMAZON_FR


class APlusContent(BaseModel):
    headline: str
    modules: List[dict]


class GenerationRequest(BaseModel):
    products: List[RawProduct]
    marketplace: Marketplace = Marketplace.AMAZON_FR
    language: str = "fr"
    style_tone: str = "professionnel"
    focus_keywords: Optional[List[str]] = []


class GenerationResult(BaseModel):
    listings: List[AmazonListing]
    failed: List[dict] = []
    total: int
    success_count: int
    marketplace: Marketplace


class PublishRequest(BaseModel):
    listings: List[AmazonListing]
    marketplace: Marketplace = Marketplace.AMAZON_FR
    dry_run: bool = True


class PublishResult(BaseModel):
    published: int
    failed: int
    errors: List[dict] = []
    report: List[dict] = []
