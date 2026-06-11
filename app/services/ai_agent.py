import os
import json
import asyncio
from typing import List, Optional
import anthropic

from app.models import RawProduct, AmazonListing, APlusContent, Marketplace, BrandVoice

_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


MARKETPLACE_CONSTRAINTS = {
    Marketplace.AMAZON_FR: {
        "lang": "français",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.fr",
    },
    Marketplace.AMAZON_DE: {
        "lang": "allemand",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.de",
    },
    Marketplace.AMAZON_IT: {
        "lang": "italien",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.it",
    },
    Marketplace.AMAZON_ES: {
        "lang": "espagnol",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.es",
    },
    Marketplace.AMAZON_UK: {
        "lang": "anglais",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.co.uk",
    },
    Marketplace.AMAZON_NL: {
        "lang": "néerlandais",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.nl",
    },
    Marketplace.AMAZON_SE: {
        "lang": "suédois",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.se",
    },
    Marketplace.AMAZON_PL: {
        "lang": "polonais",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.pl",
    },
    Marketplace.AMAZON_BE: {
        "lang": "français",
        "title_max": 75,
        "item_highlights_max": 125,
        "bullets": 5,
        "keywords_max": 249,
        "platform": "Amazon.com.be",
    },
}


def _build_system_prompt(constraints: dict) -> str:
    return f"""Tu es un expert en optimisation de fiches produit Amazon travaillant sous l'algorithme COSMO pour {constraints['platform']}.
Ton objectif n'est PAS d'écrire du joli texte : c'est de communiquer une CONNAISSANCE PRODUIT STRUCTURÉE qu'Amazon peut interpréter — quoi, pour qui, quel problème, dans quel contexte d'achat.
Tu génères du contenu en {constraints['lang']}, strictement conforme aux règles officielles Amazon.

══ TITRE — RÈGLES OBLIGATOIRES (NOUVELLE POLITIQUE AMAZON 27/07/2026) ══
Longueur : MAXIMUM {constraints['title_max']} caractères espaces compris — limite STRICTE et non négociable.
Le titre DOIT tenir en {constraints['title_max']} caractères. Compte les caractères avant de répondre. Un titre plus long sera tronqué.
Front-load : le mot-clé principal doit apparaître en tête de titre.

Ordre des informations (en restant ≤ {constraints['title_max']} caractères, ne garder que l'essentiel) :
  Marque → Type de produit → Attribut clé → Couleur → Taille/Contenance
  Exemple (72 car.) : « Revlon Professional UniqOne - Masque Spray Sans Rinçage Rouge 150ml »

Comme le titre est court, déplace les bénéfices secondaires et qualificatifs (vegan, nourrissant,
multi-usages, public cible…) vers le champ "Item Highlights" ci-dessous — JAMAIS dans le titre.

Caractères INTERDITS : ! $ ? _ {{ }} ^ ¬ ¦
Ponctuation autorisée : tiret (-), barre oblique (/), virgule (,), esperluette (&), point (.)
Majuscules : première lettre de chaque mot important. JAMAIS tout en majuscules.
Chiffres : format numérique (« 2 » non « deux »). Mesures : abréger (cm, kg, ml, L).
Un même mot : maximum 2 occurrences (hors prépositions/articles/conjonctions).

CONTENU INTERDIT dans le titre :
- Mentions promotionnelles, superlatifs, commentaires subjectifs (N°1, best-seller, etc.)
- HTML, caractères ASCII non-linguistiques (★, ©, ®, ™, Æ, Š…), URL, coordonnées

══ ITEM HIGHLIGHTS — NOUVEAU CHAMP OBLIGATOIRE (politique 27/07/2026) ══
Longueur : MAXIMUM {constraints['item_highlights_max']} caractères espaces compris — limite STRICTE.
Format : texte continu, fluide, SANS puces ni listes. 1 à 3 phrases courtes.
Rôle : champ INDEXABLE par le moteur de recherche, affiché sous le titre dans les résultats et sur la page produit.
Il récupère les bénéfices et qualificatifs qui ne tiennent plus dans le titre court.
Contenu : bénéfices clés + mots-clés secondaires intégrés naturellement + public/usage.
Exemple (124 car.) : « Soin cheveux nourrissant et réparateur à formule vegan. Convient à tous types de cheveux. Traitement multi-bénéfices complet. »
Interdit : prix, promotions, superlatifs (« meilleur », « unique »), HTML, caractères spéciaux interdits du titre.

══ BULLET POINTS ══
- Exactement {constraints['bullets']} bullet points
- Format obligatoire : BÉNÉFICE EN 2-3 MOTS EN CAPITALES — caractéristique factuelle qui le prouve + 1 mot-clé secondaire intégré naturellement
  Exemple : « MONTAGE EN 2 MIN — La fixation à clipser sans outil s'adapte à tous les guidons de 22-32 mm »
- Chaque bullet répond à une OBJECTION D'ACHAT différente (durabilité, compatibilité, facilité, sécurité, rapport qualité-prix…)
- Affectation COSMO par bullet :
    Bullet 1 → problème résolu / bénéfice principal
    Bullet 2 → pour qui / cas d'usage concret (nommer le profil utilisateur)
    Bullet 3 → ce qu'est le produit : spécification technique différenciante
    Bullet 4 → contexte d'achat (cadeau, remplacement, première acquisition, usage en déplacement…)
    Bullet 5 → preuve : certification, chiffres mesurables, garantie
- 150-200 caractères par bullet
- Faits concrets, chiffres, matières, certifications — zéro superlatif, zéro langue marketing vague
- Interdit : prix, promotions, « meilleur », « unique », « révolutionnaire »

══ MOTS-CLÉS BACKEND ══
- Maximum {constraints['keywords_max']} caractères, séparés par des espaces, tout en minuscules
- EXCLUSIVEMENT les variantes longue traîne fournies — ne pas en inventer d'autres
- Aucune répétition de mots déjà présents dans le titre ou les bullets
- Pas de marque, pas de doublons

══ DESCRIPTION ══
- 800-1200 caractères — format storytelling optimisé scan mobile
- Pose le contexte d'usage, répond aux objections, crée le désir
- Les 4 dimensions COSMO doivent être présentes dans la narrative
- HTML simple autorisé : <b>, <br>, <ul>, <li> uniquement
- Pas de prix, promotions, logos Amazon/Prime/Alexa ni badges

══ CONTENU A+ ══
- Headline accrocheur, 60 caractères max
- 2 modules (2-3 phrases chacun, concis) :
    comparison  → avantages concrets vs alternatives du marché
    lifestyle   → ancrage dans un contexte de vie et d'achat réels (pour qui, quand, pourquoi maintenant)

══ KEYWORD MAP — Search Query Performance ══
Les mots-clés fournis proviennent de Search Query Performance (requêtes réelles clients).
Utilise EXCLUSIVEMENT ces mots-clés — ne pas en inventer d'autres.
Distribution par couche :
  Couche 1 — mot-clé principal (1-2 mots) : front-load titre + bullet 1
  Couche 2 — mots-clés secondaires (2-3 mots) : bullets 2-4 + description
  Couche 3 — longue traîne (4+ mots) : backend keywords uniquement
Ne répète pas un mot-clé à l'identique dans plusieurs champs.

Tu réponds UNIQUEMENT en JSON valide selon le schéma demandé, sans commentaire ni markdown.
"""


_TONE_DESCRIPTIONS = {
    "premium":     "Premium et exclusif — vocabulaire élégant, évoque la qualité supérieure et le savoir-faire",
    "accessible":  "Accessible et chaleureux — simple, direct, inclusif, proche du quotidien",
    "technique":   "Technique et expert — précis, factuel, données chiffrées et spécifications en avant",
    "lifestyle":   "Lifestyle et aspirationnel — ancré dans des moments de vie, émotionnel, inspirant",
    "professionnel": "Professionnel et équilibré — sérieux, clair, orienté bénéfices concrets",
}


def _build_brand_voice_section(bv: "Optional[BrandVoice]") -> str:
    if not bv:
        return ""
    parts = []
    tone_desc = _TONE_DESCRIPTIONS.get(bv.tone, bv.tone)
    parts.append(f"Ton de marque : {tone_desc}")
    if bv.brand_values:
        parts.append(f"Valeurs / Promesse de marque : {bv.brand_values}")
    if bv.target_audience:
        parts.append(f"Cible principale : {bv.target_audience}")
    if bv.signature_words:
        parts.append(f"Mots signature à intégrer naturellement (1-2 occurrences) : {', '.join(bv.signature_words)}")
    if bv.avoid_words:
        parts.append(f"Mots / expressions à ne JAMAIS utiliser : {', '.join(bv.avoid_words)}")
    if not parts:
        return ""
    return "\n\n══ VOIX DE MARQUE — RÈGLES IMPÉRATIVES ══\n" + "\n".join(f"- {p}" for p in parts)


def _build_user_prompt(product: RawProduct, constraints: dict, focus_keywords: List[str], style_tone: str, brand_voice: "Optional[BrandVoice]" = None) -> str:
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
    if focus_keywords:
        # Split by rough length into layers
        short_kw  = [k for k in focus_keywords if len(k.split()) <= 2]
        medium_kw = [k for k in focus_keywords if len(k.split()) == 3]
        long_kw   = [k for k in focus_keywords if len(k.split()) >= 4]
        # Fallback: if no stratification possible, put all in secondary
        if not short_kw and not medium_kw and not long_kw:
            medium_kw = focus_keywords
        kw_instruction = f"""
KEYWORD MAP — Search Query Performance ({len(focus_keywords)} requêtes) :
  Mot-clé principal (couche 1 — front-load titre + bullet 1) :
    {', '.join(short_kw) if short_kw else focus_keywords[0]}
  Mots-clés secondaires (couche 2 — bullets 2-4 + description) :
    {', '.join(medium_kw) if medium_kw else '—'}
  Longue traîne (couche 3 — backend keywords UNIQUEMENT) :
    {', '.join(long_kw) if long_kw else '—'}

Règle absolue : utilise EXCLUSIVEMENT ces mots-clés. Ne pas en inventer d'autres."""
    else:
        kw_instruction = ""

    image_note = ""
    if product.images:
        image_note = f"\nImage de référence (style/esthétique) : {product.images[0]}"

    bv_section = _build_brand_voice_section(brand_voice)

    improvement_section = ""
    if product.improvement_notes:
        notes = "\n".join(f"  • {n}" for n in product.improvement_notes)
        improvement_section = f"""

⚠️ CORRECTIFS PRIORITAIRES issus des avis clients négatifs — intégrer OBLIGATOIREMENT :
{notes}

Ces correctifs sont non-négociables et priment sur tout autre choix de rédaction."""

    return f"""CONTEXTE PRODUIT :
{json.dumps(product_data, ensure_ascii=False, indent=2)}

Ton de rédaction : {_TONE_DESCRIPTIONS.get(style_tone, style_tone)}
Plateforme cible : {constraints['platform']}{bv_section}{improvement_section}
{image_note}{kw_instruction}

CHECKLIST AVANT DE GÉNÉRER :
— Amazon —
1. Titre : mot-clé principal en tête (front-load), ≤ {constraints['title_max']} chars OBLIGATOIRE (compter), aucun char interdit (! $ ? _ {{ }} ^)
2. Titre : ordre Marque → Type → Attribut → Couleur → Taille ; jamais tout en majuscules
2b. Item Highlights : ≤ {constraints['item_highlights_max']} chars, texte continu sans puces, bénéfices + mots-clés secondaires repris du titre raccourci
3. Bullets : format BÉNÉFICE EN CAPITALES — preuve factuelle + 1 mot-clé secondaire naturel
4. Backend keywords : zéro doublon avec titre/bullets, tout en minuscules, ≤ {constraints['keywords_max']} chars
5. Aucun superlatif ni badge interdit dans aucun champ
— COSMO —
6. Titre : identité produit claire (CE QU'EST le produit)
7. Bullet 1 : problème résolu / bénéfice principal
8. Bullet 2 : POUR QUI — profil utilisateur nommé explicitement
9. Bullet 4 : CONTEXTE D'ACHAT (cadeau, remplacement, usage pro, déplacement…)
10. Description : les 4 dimensions COSMO tissées en narrative (quoi / pour qui / problème / contexte)
11. Lifestyle A+ : ancrage dans un contexte de vie et d'achat réels
— Keyword Map —
12. Mot-clé principal (couche 1) en position front-load dans le titre
13. Longue traîne réservée aux backend keywords — pas répétée dans titre ou bullets

Avant de rédiger, effectue mentalement l'analyse COSMO (intentions d'achat, attributs manquants, contextes d'usage, structure sémantique) — mais ne l'inclus PAS dans le JSON de sortie.

Génère la fiche produit optimisée au format JSON exact suivant :
{{
  "title": "... (≤ {constraints['title_max']} caractères)",
  "item_highlights": "... (texte continu, ≤ {constraints['item_highlights_max']} caractères)",
  "bullet_points": [
    "BÉNÉFICE 1 — preuve factuelle + mot-clé secondaire...",
    "BÉNÉFICE 2 — ...",
    "BÉNÉFICE 3 — ...",
    "BÉNÉFICE 4 — ...",
    "BÉNÉFICE 5 — ..."
  ],
  "description": "...",
  "backend_keywords": "longue traîne mot1 mot2 ...",
  "a_plus_content": {{
    "headline": "...",
    "modules": [
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
    highlights = listing_data.get("item_highlights", "")
    bullets = listing_data.get("bullet_points", [])
    desc = listing_data.get("description", "")
    kw = listing_data.get("backend_keywords", "")
    title_max = constraints["title_max"]
    highlights_max = constraints.get("item_highlights_max", 125)

    # ── Titre (25 pts) ──────────────────────────────────────────────
    title_len = len(title)
    if title_len > 0:
        score += 5
    if title_len <= title_max:
        score += 15   # conforme à la nouvelle limite stricte (75 car.)
    elif title_len <= title_max + 25:
        score += 5
    # Bonus : exploite bien l'espace disponible sans le gaspiller
    if title_max * 0.6 <= title_len <= title_max:
        score += 5

    # ── Item Highlights (10 pts) ────────────────────────────────────
    h_len = len(highlights)
    if h_len > 0:
        score += 4
    if 0 < h_len <= highlights_max:
        score += 4
    if highlights_max * 0.6 <= h_len <= highlights_max:
        score += 2

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
    brand_voice: Optional[BrandVoice] = None,
    retries: int = 2,
) -> tuple:  # (AmazonListing, {"input_tokens": int, "output_tokens": int})
    constraints = MARKETPLACE_CONSTRAINTS.get(marketplace, MARKETPLACE_CONSTRAINTS[Marketplace.AMAZON_FR])
    # Per-product keywords take priority; global keywords fill in after (deduped)
    product_kw = list(product.focus_keywords or [])
    merged_kw = product_kw + [k for k in (focus_keywords or []) if k not in product_kw]
    system = _build_system_prompt(constraints)
    user = _build_user_prompt(product, constraints, merged_kw, style_tone, brand_voice)
    token_usage = {"input_tokens": 0, "output_tokens": 0}

    for attempt in range(retries + 1):
        try:
            response = await get_client().messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2500,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
            token_usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
            }
            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            # Detect truncated response (stop_reason != end_turn)
            if response.stop_reason != "end_turn":
                raise ValueError(f"Réponse tronquée (stop_reason={response.stop_reason}). Réessai en cours...")
            data = json.loads(raw)

            data["seo_score"] = _compute_seo_score(data, constraints)

            # Enforce hard limits
            data["title"] = data["title"][: constraints["title_max"]]
            if data.get("item_highlights"):
                data["item_highlights"] = data["item_highlights"][: constraints.get("item_highlights_max", 125)]
            data["bullet_points"] = data["bullet_points"][: constraints["bullets"]]
            if constraints["keywords_max"] > 0:
                data["backend_keywords"] = data["backend_keywords"][: constraints["keywords_max"]]

            return AmazonListing(
                sku=product.sku,
                brand=product.brand or "",
                category=product.category or "",
                price=product.price,
                ean=product.ean,
                color=product.color,
                material=product.material,
                weight_kg=product.weight_kg,
                marketplace=marketplace,
                **{k: v for k, v in data.items() if k in AmazonListing.model_fields},
            ), token_usage
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
    brand_voice: Optional[BrandVoice] = None,
    concurrency: int = 15,
    on_progress=None,  # callable(done: int, total: int)
) -> tuple:  # (listings, failed, {"input_tokens": int, "output_tokens": int})
    semaphore = asyncio.Semaphore(concurrency)
    listings = []
    failed = []
    done_count = 0
    total_input_tokens = 0
    total_output_tokens = 0

    async def _process(product: RawProduct):
        nonlocal done_count, total_input_tokens, total_output_tokens
        async with semaphore:
            try:
                listing, tokens = await generate_listing(product, marketplace, focus_keywords, style_tone, brand_voice)
                listings.append(listing)
                total_input_tokens += tokens.get("input_tokens", 0)
                total_output_tokens += tokens.get("output_tokens", 0)
            except Exception as e:
                failed.append({"sku": product.sku, "error": str(e)})
            finally:
                done_count += 1
                if on_progress:
                    on_progress(done_count, len(products))

    await asyncio.gather(*[_process(p) for p in products])
    return listings, failed, {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}
