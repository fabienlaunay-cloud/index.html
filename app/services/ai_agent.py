import os
import json
import asyncio
from typing import List, Optional
import anthropic

from app.models import RawProduct, AmazonListing, APlusContent, Marketplace

_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


MARKETPLACE_CONSTRAINTS = {
    Marketplace.AMAZON_FR: {
        "lang": "français",
        "title_max": 200,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.fr",
    },
    Marketplace.AMAZON_DE: {
        "lang": "allemand",
        "title_max": 200,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.de",
    },
    Marketplace.AMAZON_IT: {
        "lang": "italien",
        "title_max": 200,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.it",
    },
    Marketplace.AMAZON_ES: {
        "lang": "espagnol",
        "title_max": 200,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.es",
    },
    Marketplace.AMAZON_UK: {
        "lang": "anglais",
        "title_max": 200,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.co.uk",
    },
    Marketplace.CDISCOUNT: {
        "lang": "français",
        "title_max": 150,
        "bullets": 5,
        "keywords_max": 0,
        "platform": "Cdiscount",
    },
    Marketplace.FNAC: {
        "lang": "français",
        "title_max": 150,
        "bullets": 4,
        "keywords_max": 0,
        "platform": "Fnac",
    },
    Marketplace.BOL: {
        "lang": "néerlandais",
        "title_max": 150,
        "bullets": 5,
        "keywords_max": 0,
        "platform": "Bol.com",
    },
}


def _build_system_prompt(constraints: dict) -> str:
    return f"""Tu es un expert en e-commerce et en optimisation de fiches produits pour {constraints['platform']}.
Tu génères des contenus SEO haute performance en {constraints['lang']}.

Règles strictes :
- Titre : {constraints['title_max']} caractères max, intègre les mots-clés principaux en début de titre
- Bullet points : exactement {constraints['bullets']} points, commencent par une majuscule, 150-200 caractères chacun
- Mots-clés backend : {constraints['keywords_max']} caractères max, séparés par des espaces, sans répétition du titre
- Description : 1500-2000 caractères, HTML simple autorisé, richesse sémantique
- Contenu A+ : structure modulaire avec headline + 3 modules (comparatif, lifestyle, storytelling)
- Score SEO : évaluation 0-100 basée sur densité keywords, longueur titre, richesse description

Tu réponds UNIQUEMENT en JSON valide selon le schéma demandé, sans commentaire ni markdown.
"""


def _build_user_prompt(product: RawProduct, constraints: dict, focus_keywords: List[str], style_tone: str) -> str:
    product_data = {
        "sku": product.sku,
        "nom": product.name,
        "marque": product.brand,
        "catégorie": product.category,
        "description_brute": product.description,
        "prix": product.price,
        "ean": product.ean,
        "poids_kg": product.weight_kg,
        "dimensions_cm": product.dimensions_cm,
        "couleur": product.color,
        "matière": product.material,
        "caractéristiques": product.features,
        "données_supplémentaires": product.extra,
    }
    kw_instruction = f"\nMots-clés prioritaires à intégrer : {', '.join(focus_keywords)}" if focus_keywords else ""

    return f"""Données produit brutes :
{json.dumps(product_data, ensure_ascii=False, indent=2)}

Ton de rédaction : {style_tone}
Plateforme cible : {constraints['platform']}
{kw_instruction}

Génère la fiche produit optimisée au format JSON exact suivant :
{{
  "title": "...",
  "bullet_points": ["...", "...", "...", "...", "..."],
  "description": "...",
  "backend_keywords": "...",
  "a_plus_content": {{
    "headline": "...",
    "modules": [
      {{"type": "brand_story", "title": "...", "body": "..."}},
      {{"type": "comparison", "title": "...", "body": "..."}},
      {{"type": "lifestyle", "title": "...", "body": "..."}}
    ]
  }},
  "seo_score": 0
}}"""


def _compute_seo_score(listing_data: dict, constraints: dict) -> int:
    score = 0
    title = listing_data.get("title", "")
    bullets = listing_data.get("bullet_points", [])
    desc = listing_data.get("description", "")
    kw = listing_data.get("backend_keywords", "")

    # Titre (30 pts)
    title_len = len(title)
    if title_len >= 100:
        score += 15
    if title_len >= 150:
        score += 10
    if title_len <= constraints["title_max"]:
        score += 5

    # Bullets (30 pts)
    if len(bullets) == constraints["bullets"]:
        score += 10
    avg_bullet_len = sum(len(b) for b in bullets) / max(len(bullets), 1)
    if avg_bullet_len >= 100:
        score += 10
    if avg_bullet_len >= 150:
        score += 10

    # Description (20 pts)
    if len(desc) >= 1000:
        score += 10
    if len(desc) >= 1500:
        score += 10

    # Keywords backend (20 pts)
    if constraints["keywords_max"] > 0:
        kw_words = len(kw.split())
        if kw_words >= 10:
            score += 10
        if len(kw) <= constraints["keywords_max"]:
            score += 10
    else:
        score += 20  # Not applicable for non-Amazon

    return min(score, 100)


async def generate_listing(
    product: RawProduct,
    marketplace: Marketplace,
    focus_keywords: List[str],
    style_tone: str,
    retries: int = 2,
) -> AmazonListing:
    constraints = MARKETPLACE_CONSTRAINTS.get(marketplace, MARKETPLACE_CONSTRAINTS[Marketplace.AMAZON_FR])
    system = _build_system_prompt(constraints)
    user = _build_user_prompt(product, constraints, focus_keywords, style_tone)

    for attempt in range(retries + 1):
        try:
            response = await get_client().messages.create(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(raw)

            data["seo_score"] = _compute_seo_score(data, constraints)

            # Enforce hard limits
            data["title"] = data["title"][: constraints["title_max"]]
            data["bullet_points"] = data["bullet_points"][: constraints["bullets"]]
            if constraints["keywords_max"] > 0:
                data["backend_keywords"] = data["backend_keywords"][: constraints["keywords_max"]]

            return AmazonListing(
                sku=product.sku,
                brand=product.brand or "",
                category=product.category or "",
                price=product.price,
                ean=product.ean,
                marketplace=marketplace,
                **{k: v for k, v in data.items() if k in AmazonListing.model_fields},
            )
        except json.JSONDecodeError:
            if attempt == retries:
                raise
            await asyncio.sleep(1)
        except anthropic.APIError as e:
            if attempt == retries:
                raise
            await asyncio.sleep(2 ** attempt)


async def generate_listings_batch(
    products: List[RawProduct],
    marketplace: Marketplace,
    focus_keywords: List[str],
    style_tone: str,
    concurrency: int = 3,
) -> tuple[List[AmazonListing], List[dict]]:
    semaphore = asyncio.Semaphore(concurrency)
    listings = []
    failed = []

    async def _process(product: RawProduct):
        async with semaphore:
            try:
                listing = await generate_listing(product, marketplace, focus_keywords, style_tone)
                listings.append(listing)
            except Exception as e:
                failed.append({"sku": product.sku, "error": str(e)})

    await asyncio.gather(*[_process(p) for p in products])
    return listings, failed
