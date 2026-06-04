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
    "amazon_fr":     "Amazon.fr (France)",
    "amazon_de":     "Amazon.de (Allemagne)",
    "amazon_it":     "Amazon.it (Italie)",
    "amazon_es":     "Amazon.es (Espagne)",
    "amazon_co_uk":  "Amazon.co.uk (Royaume-Uni)",
    "amazon_nl":     "Amazon.nl (Pays-Bas)",
    "amazon_se":     "Amazon.se (Suède)",
    "amazon_pl":     "Amazon.pl (Pologne)",
}

_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


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
        f"**{cat_label}** sur **{mkt_label}**.\n"
        f"Produits en cours : {titles_str}\n\n"
        f"Réponds UNIQUEMENT en JSON valide (pas de markdown, pas de texte avant/après) :\n"
        f'{{\"headline\":\"[1 donnée chiffrée percutante : taille du marché, croissance ou nombre de vendeurs]\","'
        f'\"body\":\"[1-2 phrases sur la concurrence et les facteurs clés de succès]\","'
        f'\"tip\":\"[1 conseil SEO/keyword très spécifique avec un exemple concret entre guillemets]\","'
        f'\"competition\":\"high|medium|low\"}}\n\n'
        f"Règles : tutoiement, français, percutant, chiffres réalistes, max 180 caractères par champ."
    )

    try:
        msg = await _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json\n"):
                raw = raw[5:]
        data = json.loads(raw)
        # Validate expected keys
        for key in ("headline", "body", "tip", "competition"):
            if key not in data:
                data[key] = ""
        return data
    except json.JSONDecodeError:
        raise HTTPException(502, "AI response could not be parsed")
    except Exception as e:
        raise HTTPException(502, str(e))
