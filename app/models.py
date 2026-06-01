from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum


class Marketplace(str, Enum):
    AMAZON_FR = "amazon_fr"
    AMAZON_DE = "amazon_de"
    AMAZON_IT = "amazon_it"
    AMAZON_ES = "amazon_es"
    AMAZON_UK = "amazon_uk"
    AMAZON_NL = "amazon_nl"
    AMAZON_SE = "amazon_se"
    AMAZON_PL = "amazon_pl"
    AMAZON_BE = "amazon_be"


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
    size: Optional[str] = None
    features: Optional[List[str]] = []
    images: Optional[List[str]] = []
    focus_keywords: Optional[List[str]] = []   # mots-clés spécifiques à ce produit
    extra: Optional[dict] = {}
    # Variation fields
    parent_sku: Optional[str] = None           # SKU du parent pour les déclinaisons
    variation_theme: Optional[str] = None      # ex: "ColorName-SizeClass", "ColorName", "SizeClass"
    variation_value: Optional[str] = None      # ex: "Rouge / L", "Bleu / M"


class VariationChild(BaseModel):
    sku: str
    price: Optional[float] = None
    ean: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    stock: int = 1
    variation_value: str = ""  # ex: "Rouge / L"


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
    color: Optional[str] = None
    material: Optional[str] = None
    weight_kg: Optional[float] = None
    a_plus_content: Optional[dict] = None
    seo_score: Optional[int] = None
    marketplace: Marketplace = Marketplace.AMAZON_FR
    # Variation fields
    parent_sku: Optional[str] = None           # Renseigné pour les enfants
    variation_theme: Optional[str] = None      # ex: "ColorName-SizeClass"
    is_parent: bool = False                    # True = fiche parent (pas de contenu propre)
    children: List[VariationChild] = []        # Déclinaisons (renseigné sur le parent)


class APlusContent(BaseModel):
    headline: str
    modules: List[dict]


class BrandVoice(BaseModel):
    tone: str = "professionnel"
    target_audience: str = ""
    brand_values: str = ""
    signature_words: List[str] = []
    avoid_words: List[str] = []


class GenerationRequest(BaseModel):
    products: List[RawProduct]
    marketplace: Marketplace = Marketplace.AMAZON_FR
    marketplaces: Optional[List[Marketplace]] = None   # multi-market: overrides marketplace
    language: str = "fr"
    style_tone: str = "professionnel"
    focus_keywords: Optional[List[str]] = []
    brand_voice: Optional[BrandVoice] = None


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
