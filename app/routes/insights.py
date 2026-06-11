import os
import json
from typing import Optional

import anthropic
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/insights", tags=["insights"])

_CATEGORY_LABELS: dict[str, str] = {
    "alimentaire": "Alimentation & Épicerie",
    "animaux":     "Animalerie & Accessoires animaux",
    "textile":     "Mode & Vêtements",
    "electronique":"High-Tech & Électronique",
    "beaute":      "Beauté & Cosmétiques",
    "jouets":      "Jouets & Jeux",
    "sport":       "Sport & Plein Air",
    "maison":      "Maison, Jardin & Cuisine",
}

_MARKETPLACE_LABELS: dict[str, str] = {
    "amazon_fr":    "Amazon.fr (France)",
    "amazon_de":    "Amazon.de (Allemagne)",
    "amazon_it":    "Amazon.it (Italie)",
    "amazon_es":    "Amazon.es (Espagne)",
    "amazon_co_uk": "Amazon.co.uk (Royaume-Uni)",
    "amazon_nl":    "Amazon.nl (Pays-Bas)",
    "amazon_se":    "Amazon.se (Suède)",
    "amazon_pl":    "Amazon.pl (Pologne)",
}

_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json\n"):
            raw = raw[5:]
    return json.loads(raw)


# ── /market ──────────────────────────────────────────────────────────────────

class InsightRequest(BaseModel):
    category: Optional[str] = None
    product_titles: list[str] = []
    marketplace: str = "amazon_fr"


@router.post("/market")
async def get_market_insight(body: InsightRequest, request: Request):
    cat_label = _CATEGORY_LABELS.get(body.category or "", body.category or "e-commerce généraliste")
    mkt_label = _MARKETPLACE_LABELS.get(body.marketplace, "Amazon")
    titles_str = ", ".join(t for t in body.product_titles[:5] if t) or "(non précisé)"

    prompt = (
        f"Tu es un expert Amazon FBA avec 10 ans d'expérience sur les marketplaces européennes.\n"
        f"Génère un insight marché concis pour un vendeur qui crée des fiches dans la catégorie "
        f"**{cat_label}** sur **{mkt_label}**.\nProduits : {titles_str}\n\n"
        f"Réponds UNIQUEMENT en JSON valide (pas de markdown) :\n"
        f'{{\"headline\":\"[1 donnée chiffrée percutante]\",\"body\":\"[1-2 phrases concurrence + facteurs clés]\","'
        f'\"tip\":\"[1 conseil SEO spécifique avec exemple]\",\"competition\":\"high|medium|low\"}}\n\n'
        f"Règles : tutoiement, français, percutant, chiffres réalistes, max 180 caractères par champ."
    )

    try:
        msg = await _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(msg.content[0].text)
        for key in ("headline", "body", "tip", "competition"):
            data.setdefault(key, "")
        return data
    except json.JSONDecodeError:
        raise HTTPException(502, "AI response could not be parsed")
    except Exception as e:
        raise HTTPException(502, str(e))


# ── /scan ─────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    sku: str = ""
    title: str = ""
    category: Optional[str] = None
    marketplace: str = "amazon_fr"
    seo_score: Optional[int] = None
    bullet_points: list[str] = []
    description: str = ""
    backend_keywords: str = ""
    brand: str = ""
    price: Optional[float] = None


@router.post("/scan")
async def scan_listing(body: ScanRequest, request: Request):
    cat_label = _CATEGORY_LABELS.get(body.category or "", body.category or "non déterminée")
    mkt_label = _MARKETPLACE_LABELS.get(body.marketplace, "Amazon")
    bullets_str = "\n".join(f"  {i+1}. {bp}" for i, bp in enumerate(body.bullet_points)) or "  (aucun)"
    score_str = f"{body.seo_score}/100" if body.seo_score is not None else "non calculé"

    prompt = f"""Tu es un expert Amazon FBA, SEO marketplace et copywriting e-commerce.
Analyse cette fiche produit Amazon et génère des recommandations précises et actionnables.

FICHE PRODUIT :
- SKU : {body.sku}
- Marketplace : {mkt_label}
- Catégorie : {cat_label}
- Marque : {body.brand or "(non précisée)"}
- Prix : {f"{body.price:.2f} €" if body.price else "(non précisé)"}
- Score SEO actuel : {score_str}
- Titre : {body.title}
- Bullet points :
{bullets_str}
- Description : {(body.description or "(vide)")[:600]}
- Mots-clés backend : {(body.backend_keywords or "(vides)")[:300]}

Réponds UNIQUEMENT en JSON valide (pas de markdown, pas de texte avant/après) avec cette structure exacte :
{{
  "market": {{
    "headline": "[1 stat chiffrée sur ce marché sur {mkt_label}]",
    "body": "[1-2 phrases sur la concurrence et les facteurs de succès]",
    "tip": "[1 conseil positionnement spécifique à cette catégorie]",
    "competition": "high|medium|low"
  }},
  "keywords": [
    {{"term": "[mot-clé]", "type": "principale|longue_traine|LSI", "impact": "haute|moyenne|faible"}},
    {{"term": "...", "type": "...", "impact": "..."}}
  ],
  "title_tip": "[1 conseil concret pour améliorer ce titre. NOUVELLE POLITIQUE AMAZON (27/07/2026) : titre ≤ 75 caractères obligatoire. Si le titre dépasse 75 car., propose une version raccourcie ≤ 75 car. et suggère de déplacer le surplus vers le champ Item Highlights (125 car.)]",
  "bullets_analysis": [
    {{"index": 0, "status": "ok|improve", "comment": "[feedback court si improve]"}}
  ],
  "description_tip": "[1 conseil concret pour améliorer la description]",
  "quick_wins": ["[action rapide 1]", "[action rapide 2]", "[action rapide 3]"]
}}

Règles :
- Tutoiement, français, conseils très concrets et spécifiques à CE produit
- keywords : 5 à 7 mots-clés, du plus important au moins important
- bullets_analysis : une entrée par bullet point existant
- quick_wins : 3 actions simples à faire immédiatement
- max 200 caractères par champ texte
"""

    try:
        msg = await _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(msg.content[0].text)
        return data
    except json.JSONDecodeError:
        raise HTTPException(502, "AI response could not be parsed")
    except Exception as e:
        raise HTTPException(502, str(e))
