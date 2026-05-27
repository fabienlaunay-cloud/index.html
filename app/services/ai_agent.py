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
Tu génères des contenus SEO haute performance en {constraints['lang']}, strictement conformes aux règles officielles Amazon.

══ TITRE — RÈGLES OBLIGATOIRES ══
Longueur : maximum {constraints['title_max']} caractères espaces compris. Viser ≤ 80 caractères (recommandation Amazon pour mobiles).

Ordre des informations (respecter cet ordre) :
  Marque → Goût/Style → Type de produit → Attribut clé → Couleur → Taille/Nb d'emballages → Numéro de modèle
  Exemple conforme : « Café Amazon Fresh Décaféiné Colombie Grains Entiers, Torréfié Moyen, 12 oz (Lot de 3) »

Caractères INTERDITS dans le titre : ! $ ? _ {{ }} ^ ¬ ¦
Caractères autorisés uniquement en contexte fonctionnel (identifiant, mesure) : ~ # < > *
  Exemple autorisé : « Style #4301 » ou « < 10 kg » — mais pas à titre décoratif.
  Ponctuation autorisée : tiret (-), barre oblique (/), virgule (,), esperluette (&), point (.)

Répétition de mots : un même mot ne peut apparaître plus de 2 fois dans le titre.
  Exception : prépositions (dans, sur, avec, de, en), conjonctions (et, ou, pour), articles (le, la, les, un, une) → peuvent se répéter.
  Les noms de marque sont aussi limités à 2 occurrences.

Majuscules : première lettre de chaque mot important en majuscule.
  Mettre en minuscules : prépositions, conjonctions, articles (sauf en début de titre).
  JAMAIS tout en majuscules (ex. « CHAUSSURES NIKE » est non conforme).

Chiffres : toujours en format numérique (« 2 » et non « deux », « 24 x 48 pouces » et non « vingt-quatre »).
Mesures : abréger (cm, oz, in, kg, lb, ml, L).
Numéro de modèle : inclure s'il existe (ex : « Sony WH-1000XM5 »).

CONTENU INTERDIT dans le titre :
- Mentions promotionnelles : « % de réduction », « livraison gratuite », « qualité garantie », « meilleur prix »
- Commentaires subjectifs : « article populaire », « N°1 des ventes », « best-seller », adjectifs vagues
- Informations vendeur : nom du vendeur, URL, coordonnées
- HTML ou balises quelconques
- Caractères ASCII non-linguistiques : Æ, Š, Œ, Ÿ, Ž, ★, ©, ®, ™

══ BULLET POINTS ══
- Exactement {constraints['bullets']} bullet points
- Chaque bullet commence par UN MOT-CLÉ DESCRIPTIF EN MAJUSCULES suivi d'un tiret et du bénéfice factuel
  Exemple : « AUTONOMIE 20H — La batterie lithium-ion de 2500 mAh assure... »
- 150-200 caractères par bullet
- Faits concrets, chiffres, matières, certifications — pas de superlatifs ni de langue marketing vague
- Interdit : prix, promotions, « meilleur », « unique », « révolutionnaire », commentaires subjectifs

══ MOTS-CLÉS BACKEND ══
- Maximum {constraints['keywords_max']} caractères, séparés par des espaces, tout en minuscules
- Aucune répétition de mots déjà présents dans le titre (ni leurs variantes directes)
- Synonymes, variantes d'orthographe, termes de recherche complémentaires, traductions pertinentes

══ DESCRIPTION ══
- 1500-2000 caractères
- HTML simple autorisé : <b>, <br>, <ul>, <li> uniquement
- Richesse sémantique, mots-clés secondaires intégrés naturellement
- Pas de prix, promotions ni informations temporaires
- Pas de logos Amazon, Prime, Alexa ni badges (Amazon's Choice, Best Seller, etc.)

══ CONTENU A+ ══
- Headline accrocheur, 60 caractères max
- 3 modules : brand_story (valeurs/histoire), comparison (avantages vs alternatives), lifestyle (usage quotidien)

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

CHECKLIST AVANT DE GÉNÉRER (vérifier chaque point) :
1. Titre : commence par la marque, ≤ 80 chars si possible, aucun des chars interdits (! $ ? _ {{ }} ^), aucun mot répété plus de 2 fois
2. Titre : ordre respecté → Marque / Type / Attribut clé / Couleur / Taille / Modèle
3. Titre : majuscules sur mots importants, minuscules pour prépositions/articles/conjonctions, JAMAIS tout en majuscules
4. Bullets : chaque bullet commence par MOT-CLÉ EN MAJUSCULES — bénéfice factuel (chiffres, matières, certifications)
5. Mots-clés backend : zéro répétition des mots du titre, tout en minuscules
6. Aucun commentaire subjectif nulle part : pas de "meilleur", "N°1", "révolutionnaire", "unique", "populaire"
7. Aucun logo/badge Amazon, Prime, Alexa, "Amazon's Choice", "Best Seller" dans aucun champ

Génère la fiche produit optimisée au format JSON exact suivant :
{{
  "title": "...",
  "bullet_points": ["MOT-CLÉ — bénéfice factuel détaillé...", "...", "...", "...", "..."],
  "description": "...",
  "backend_keywords": "mot1 mot2 mot3 ...",
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


_SUBJECTIVE_TERMS = {
    "meilleur", "best-seller", "bestseller", "populaire", "révolutionnaire",
    "unique", "incroyable", "exceptionnel", "n°1", "numero 1", "top vente",
    "livraison gratuite", "free shipping", "qualité garantie", "100%",
}
_FORBIDDEN_CHARS = set("!$?_{}^¬¦")
_STOP_WORDS = {"le","la","les","un","une","des","de","du","en","dans","sur","avec","et","ou","pour","par","au","aux"}


def _compute_seo_score(listing_data: dict, constraints: dict) -> int:
    score = 0
    title = listing_data.get("title", "")
    bullets = listing_data.get("bullet_points", [])
    desc = listing_data.get("description", "")
    kw = listing_data.get("backend_keywords", "")

    # ── Titre (35 pts) ──────────────────────────────────────────────
    title_len = len(title)
    if title_len > 0:
        score += 5
    if title_len <= constraints["title_max"]:
        score += 5
    if title_len <= 80:
        score += 10   # recommandation Amazon mobile
    elif title_len <= 150:
        score += 5

    # Pénalités titre
    if any(c in title for c in _FORBIDDEN_CHARS):
        score -= 10   # caractères interdits Amazon
    if title and len(title) > 5 and title == title.upper():
        score -= 10   # tout en majuscules interdit
    if any(tag in title for tag in ("<", ">", "&lt;", "&gt;")):
        score -= 5    # HTML interdit
    title_lower = title.lower()
    if any(term in title_lower for term in _SUBJECTIVE_TERMS):
        score -= 10   # commentaires subjectifs interdits

    # Répétition de mots (max 2x hors stop words)
    title_words = [w.strip(".,;:-()/").lower() for w in title.split() if w.lower() not in _STOP_WORDS]
    from collections import Counter
    word_counts = Counter(title_words)
    if any(v > 2 for v in word_counts.values()):
        score -= 5

    # Bonus : commence par une majuscule (marque en tête)
    if title and title[0].isupper():
        score += 5
    # Bonus : contient un chiffre (numéro de modèle, dimensions, etc.)
    if any(c.isdigit() for c in title):
        score += 5

    # ── Bullets (30 pts) ────────────────────────────────────────────
    if len(bullets) == constraints["bullets"]:
        score += 10
    avg_bullet_len = sum(len(b) for b in bullets) / max(len(bullets), 1)
    if avg_bullet_len >= 100:
        score += 10
    if avg_bullet_len >= 150:
        score += 10

    # ── Description (20 pts) ────────────────────────────────────────
    if len(desc) >= 1000:
        score += 10
    if len(desc) >= 1500:
        score += 10

    # ── Mots-clés backend (15 pts) ──────────────────────────────────
    if constraints["keywords_max"] > 0:
        kw_words = len(kw.split())
        if kw_words >= 10:
            score += 8
        if len(kw) <= constraints["keywords_max"]:
            score += 7
    else:
        score += 15

    return max(0, min(score, 100))


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
