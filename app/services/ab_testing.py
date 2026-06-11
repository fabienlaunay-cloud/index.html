"""A/B Testing service — variant generation + Amazon Manage Your Experiments API."""
import os
import json
import asyncio
from datetime import date, timedelta
from typing import Optional

import httpx


_VARIANT_SUFFIX = {
    "benefit": """
\n══ ANGLE DE TEST A/B — VARIANTE BÉNÉFICE ══
Ton objectif pour cette variante : maximiser la résonance émotionnelle et l'identification au problème résolu.
- Titre : commence par le bénéfice principal ou le problème résolu, l'acheteur doit se reconnaître immédiatement
- Chaque bullet CAPITALES = bénéfice vécu, ressenti concret — la caractéristique vient en preuve, pas en premier
- Description : storytelling "avant/après", ancré dans un moment de vie précis et concret
- Ton : chaleureux, direct, empathique — le lecteur se dit "c'est exactement ce qu'il me faut"
""",
    "technical": """
\n══ ANGLE DE TEST A/B — VARIANTE PRÉCISION TECHNIQUE ══
Ton objectif pour cette variante : convaincre par la preuve technique et la précision factuelle.
- Titre : commence par la marque puis les spécifications les plus différenciantes (matière, dimensions, certification)
- Chaque bullet CAPITALES = caractéristique technique mesurable — chiffres, certifications, matériaux, process
- Description : langage expert, données précises, comparaisons techniques, certifications, garanties
- Ton : professionnel, factuel, rigoureux — le lecteur est convaincu par les données objectives
""",
}


async def generate_ab_variants(
    listing_data: dict,
    marketplace_str: str,
    focus_keywords: list = None,
    style_tone: str = "professionnel",
) -> dict:
    """Generate 2 variant listings in parallel. Returns {variant_a, variant_b, tokens, sku}."""
    from app.models import RawProduct, Marketplace
    from app.services.ai_agent import (
        get_client, MARKETPLACE_CONSTRAINTS, _build_user_prompt, _build_system_prompt,
        _compute_seo_score, constraints_for_category
    )

    try:
        mkt = Marketplace(marketplace_str)
    except ValueError:
        mkt = Marketplace.AMAZON_FR
    constraints = MARKETPLACE_CONSTRAINTS.get(mkt, MARKETPLACE_CONSTRAINTS[Marketplace.AMAZON_FR])
    constraints = constraints_for_category(constraints, listing_data.get("category", ""))

    product = RawProduct(
        sku=listing_data.get("sku", "ab-test"),
        name=listing_data.get("title") or listing_data.get("name", ""),
        brand=listing_data.get("brand", ""),
        category=listing_data.get("category", ""),
        features=listing_data.get("bullet_points", []),
        price=listing_data.get("price"),
        ean=listing_data.get("ean", ""),
        color=listing_data.get("color", ""),
        material=listing_data.get("material", ""),
    )
    user_prompt = _build_user_prompt(product, constraints, focus_keywords or [], style_tone)

    async def _call_variant(variant_type: str):
        base_system = _build_system_prompt(constraints)
        suffix = _VARIANT_SUFFIX.get(variant_type, "")
        system = base_system.replace(
            "Tu réponds UNIQUEMENT en JSON valide",
            suffix + "\nTu réponds UNIQUEMENT en JSON valide",
        ) if "Tu réponds UNIQUEMENT en JSON valide" in base_system else base_system + suffix

        for attempt in range(3):
            try:
                resp = await get_client().messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_prompt}],
                )
                raw = resp.content[0].text.strip()
                # Strip markdown code fences if present
                if raw.startswith("```"):
                    lines = raw.split("\n")
                    raw = "\n".join(lines[1:])
                    if raw.endswith("```"):
                        raw = raw[:-3].rstrip()
                # Extract JSON object if wrapped in extra text
                if not raw.startswith("{"):
                    start = raw.find("{")
                    end = raw.rfind("}") + 1
                    if start >= 0 and end > start:
                        raw = raw[start:end]
                data = json.loads(raw)
                data.setdefault("title", "")
                data.setdefault("item_highlights", "")
                data.setdefault("bullet_points", [])
                data.setdefault("description", "")
                data.setdefault("backend_keywords", "")
                data["seo_score"] = _compute_seo_score(data, constraints)
                data["title"] = str(data["title"])[: constraints["title_max"]]
                data["item_highlights"] = str(data.get("item_highlights", ""))[: constraints.get("item_highlights_max", 125)]
                data["bullet_points"] = list(data["bullet_points"])[: constraints["bullets"]]
                if constraints["keywords_max"] > 0:
                    data["backend_keywords"] = str(data.get("backend_keywords", ""))[: constraints["keywords_max"]]
                return data, resp.usage.input_tokens, resp.usage.output_tokens
            except Exception as e:
                if attempt == 2:
                    raise ValueError(f"Variante {variant_type} échouée après 3 essais: {type(e).__name__}: {e}")
                await asyncio.sleep(1 + attempt)

    (var_a, in_a, out_a), (var_b, in_b, out_b) = await asyncio.gather(
        _call_variant("benefit"),
        _call_variant("technical"),
    )

    return {
        "variant_a": {**var_a, "variant_type": "benefit", "variant_label": "Bénéfice"},
        "variant_b": {**var_b, "variant_type": "technical", "variant_label": "Précision"},
        "tokens": {"input_tokens": in_a + in_b, "output_tokens": out_a + out_b},
        "sku": product.sku,
        "marketplace": marketplace_str,
    }


# ── Amazon Manage Your Experiments API ────────────────────────────────────────

async def create_amazon_experiment(
    user_email: str,
    asin: str,
    marketplace_str: str,
    experiment_name: str,
    control_title: str,
    treatment_title: str,
    duration_days: int = 30,
) -> dict:
    """Create an A/B experiment via Amazon Manage Your Experiments SP-API.
    Requires Amazon Brand Registry + MYE role enabled on the SP-API app.
    """
    from app.models import Marketplace
    from app.services.amazon_sp import (
        MARKETPLACE_IDS, _get_sp_credentials, _get_lwa_token,
        _assume_role, _sign_request, _sp_endpoint,
    )

    creds = _get_sp_credentials(user_email)
    if not creds.get("refresh_token"):
        raise ValueError("Compte Amazon non connecté — connectez votre Seller Central dans Paramètres")

    lwa_token = await _get_lwa_token(creds)
    temp_creds = _assume_role(creds)

    try:
        mkt = Marketplace(marketplace_str)
    except ValueError:
        mkt = Marketplace.AMAZON_FR
    marketplace_id = MARKETPLACE_IDS.get(mkt, "A13V1IB3VIYZZH")

    start_dt = date.today()
    end_dt = start_dt + timedelta(days=max(14, min(90, duration_days)))

    payload = {
        "marketplaceId": marketplace_id,
        "asin": asin.upper().strip(),
        "name": experiment_name[:200],
        "type": "CONTENT",
        "metricObjective": "CONVERSION_RATE",
        "startDate": str(start_dt),
        "endDate": str(end_dt),
        "proposedTreatments": [
            {
                "name": "Variante A — Contrôle",
                "type": "CONTROL",
                "listing": {"title": control_title},
            },
            {
                "name": "Variante B — Test",
                "type": "TREATMENT",
                "listing": {"title": treatment_title},
            },
        ],
    }

    url = f"{_sp_endpoint()}/experiments/2021-08-01/experiments"
    body_bytes = json.dumps(payload).encode()
    headers = _sign_request("POST", url, body_bytes, temp_creds, lwa_token)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, content=body_bytes, headers=headers)
        if resp.status_code == 403:
            raise ValueError(
                "Accès refusé (403) — Amazon Brand Registry requis pour Manage Your Experiments. "
                "Activez le rôle 'Experiments' dans votre application SP-API Developer Central."
            )
        if resp.status_code == 400:
            raise ValueError(f"Paramètres invalides ({resp.status_code}): {resp.text[:300]}")
        if not resp.is_success:
            raise ValueError(f"Erreur Amazon API ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()

    experiment_id = (
        data.get("experimentId")
        or data.get("id")
        or data.get("experiment", {}).get("experimentId", "")
    )
    return {
        "experiment_id": experiment_id,
        "amazon_status": "RUNNING",
        "start_date": str(start_dt),
        "end_date": str(end_dt),
    }


async def get_amazon_experiment_status(user_email: str, experiment_id: str, marketplace_str: str) -> dict:
    """Fetch current status of an Amazon experiment."""
    from app.services.amazon_sp import _get_sp_credentials, _get_lwa_token, _assume_role, _sign_request, _sp_endpoint

    creds = _get_sp_credentials(user_email)
    if not creds.get("refresh_token"):
        return {"error": "Amazon non connecté"}

    try:
        lwa_token = await _get_lwa_token(creds)
        temp_creds = _assume_role(creds)
    except Exception as e:
        return {"error": str(e)}

    url = f"{_sp_endpoint()}/experiments/2021-08-01/experiments/{experiment_id}"
    headers = _sign_request("GET", url, b"", temp_creds, lwa_token)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        if not resp.is_success:
            return {"error": f"Amazon API ({resp.status_code}): {resp.text[:200]}"}
        return resp.json()


async def cancel_amazon_experiment(user_email: str, experiment_id: str) -> bool:
    """Cancel a running Amazon experiment."""
    from app.services.amazon_sp import _get_sp_credentials, _get_lwa_token, _assume_role, _sign_request, _sp_endpoint

    creds = _get_sp_credentials(user_email)
    if not creds.get("refresh_token"):
        return False

    try:
        lwa_token = await _get_lwa_token(creds)
        temp_creds = _assume_role(creds)
    except Exception:
        return False

    url = f"{_sp_endpoint()}/experiments/2021-08-01/experiments/{experiment_id}/cancelExperiment"
    body = b"{}"
    headers = _sign_request("PUT", url, body, temp_creds, lwa_token)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(url, content=body, headers=headers)
        return resp.is_success
