from typing import List, Dict, Tuple, Optional
from app.models import RawProduct, AmazonListing, VariationChild


def group_by_parent(products: List[RawProduct]) -> Dict[str, List[RawProduct]]:
    """
    Group variation children under their parent SKU.
    Standalone products get their own single-item group keyed by their own SKU.
    Returns an OrderedDict preserving insertion order.
    """
    groups: Dict[str, List[RawProduct]] = {}
    for product in products:
        key = product.parent_sku if product.parent_sku else product.sku
        if key not in groups:
            groups[key] = []
        groups[key].append(product)
    return groups


def build_parent_product(parent_sku: str, children: List[RawProduct]) -> RawProduct:
    """
    Build a synthetic parent RawProduct from its variation children for AI generation.
    The AI sees one product representing the whole group.
    """
    base = children[0]
    theme = base.variation_theme or "ColorName-SizeClass"

    child_values = [c.variation_value for c in children if c.variation_value]
    name = base.name
    if child_values:
        preview = ", ".join(child_values[:4])
        suffix = "..." if len(child_values) > 4 else ""
        name = f"{base.name} ({theme}: {preview}{suffix})"

    all_features: List[str] = []
    seen_f: set = set()
    for c in children:
        for f in (c.features or []):
            if f not in seen_f:
                all_features.append(f)
                seen_f.add(f)

    all_images: List[str] = []
    seen_i: set = set()
    for c in children:
        for img in (c.images or []):
            if img not in seen_i:
                all_images.append(img)
                seen_i.add(img)

    prices = [c.price for c in children if c.price]
    price = min(prices) if prices else base.price

    variation_summary = [
        {
            "sku": c.sku,
            "variation_value": c.variation_value or _infer_variation_value(c),
            "color": c.color,
            "size": c.size,
            "price": c.price,
            "ean": c.ean,
        }
        for c in children
    ]

    return RawProduct(
        sku=parent_sku,
        name=name,
        brand=base.brand,
        category=base.category,
        description=base.description,
        price=price,
        ean=None,
        weight_kg=base.weight_kg,
        dimensions_cm=base.dimensions_cm,
        color=None,
        material=base.material,
        features=all_features[:8],
        images=all_images[:3],
        focus_keywords=list(base.focus_keywords or []),
        extra={
            **(base.extra or {}),
            "variation_theme": theme,
            "variations": variation_summary,
        },
    )


def expand_to_variation_listings(
    parent_listing: AmazonListing,
    children: List[RawProduct],
) -> Tuple[AmazonListing, List[AmazonListing]]:
    """
    Given the AI-generated parent listing and the original raw children,
    produce a proper parent AmazonListing + child AmazonListings.

    Returns (parent_listing_updated, [child_listing, ...])
    """
    theme = (children[0].variation_theme if children else None) or "ColorName-SizeClass"

    variation_children = [
        VariationChild(
            sku=c.sku,
            price=c.price,
            ean=c.ean,
            color=c.color,
            size=c.size,
            stock=1,
            variation_value=c.variation_value or _infer_variation_value(c),
        )
        for c in children
    ]

    parent = parent_listing.model_copy(update={
        "variation_theme": theme,
        "is_parent": True,
        "children": variation_children,
        "price": None,
        "ean": None,
        "color": None,
    })

    child_listings: List[AmazonListing] = []
    for c in children:
        child = parent_listing.model_copy(update={
            "sku": c.sku,
            "price": c.price,
            "ean": c.ean,
            "color": c.color,
            "material": c.material or parent_listing.material,
            "weight_kg": c.weight_kg or parent_listing.weight_kg,
            "parent_sku": parent_listing.sku,
            "variation_theme": theme,
            "is_parent": False,
            "children": [],
            "a_plus_content": None,
        })
        child_listings.append(child)

    return parent, child_listings


def _infer_variation_value(product: RawProduct) -> str:
    parts = []
    if product.size:
        parts.append(product.size)
    if product.color:
        parts.append(product.color)
    return " / ".join(parts) if parts else product.sku
