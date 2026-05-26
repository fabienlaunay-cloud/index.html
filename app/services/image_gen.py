"""
Génération d'images produit Amazon via Claude (prompts) + DALL-E 3 (images).
Si OPENAI_API_KEY absent → retourne uniquement les prompts.
"""

import os
import asyncio
import json
from typing import Optional
import anthropic
import httpx

from app.services.ai_agent import get_client as get_claude

AMAZON_IMAGE_TYPES = [
    {
        "id": "hero",
        "label": "Photo principale",
        "slot": "MAIN",
        "amazon_rule": "Fond blanc pur RGB(255,255,255), produit seul, occupe 85%+ du cadre, pas de texte",
        "style_hint": "professional product photography, pure white background, studio lighting, sharp focus, no shadows",
    },
    {
        "id": "lifestyle_1",
        "label": "Lifestyle — en situation",
        "slot": "PT01",
        "amazon_rule": "Produit utilisé dans son contexte naturel, ambiance lumineuse et positive",
        "style_hint": "lifestyle photography, natural light, product in use, aspirational scene, warm tones",
    },
    {
        "id": "infographic",
        "label": "Infographie features",
        "slot": "PT02",
        "amazon_rule": "Produit avec 5 callouts pointant les fonctionnalités clés, fond clair",
        "style_hint": "product infographic, clean light background, product centered, annotation arrows, professional layout",
    },
    {
        "id": "detail",
        "label": "Zoom matière / qualité",
        "slot": "PT03",
        "amazon_rule": "Gros plan sur la qualité des matériaux, textures, finitions",
        "style_hint": "macro product photography, close-up detail shot, texture and material quality, sharp focus, studio light",
    },
    {
        "id": "dimensions",
        "label": "Dimensions / taille",
        "slot": "PT04",
        "amazon_rule": "Vue du produit avec références de taille (main, objet connu) ou schéma coté",
        "style_hint": "product size comparison, scale reference, clean background, measurement visualization",
    },
    {
        "id": "packaging",
        "label": "Packaging / unboxing",
        "slot": "PT05",
        "amazon_rule": "Produit + emballage + accessoires inclus, fond neutre",
        "style_hint": "product unboxing flat lay, packaging and contents displayed, clean neutral background, overhead shot",
    },
    {
        "id": "lifestyle_2",
        "label": "Lifestyle — angle 2",
        "slot": "PT06",
        "amazon_rule": "Deuxième mise en scène lifestyle, angle ou contexte différent du PT01",
        "style_hint": "lifestyle photography, different angle, complementary scene, natural environment, professional quality",
    },
]


async def _generate_prompts_with_claude(product_info: dict) -> dict[str, str]:
    """Claude génère des prompts DALL-E optimisés pour chaque type d'image Amazon."""
    system = """Tu es expert en photographie produit e-commerce et en prompts pour DALL-E 3.
Tu génères des prompts photo ultra-précis pour Amazon, en anglais, optimisés pour DALL-E 3.
Chaque prompt doit être visuel, concret, technique. Maximum 200 mots par prompt.
Réponds UNIQUEMENT en JSON valide."""

    image_specs = "\n".join(
        f'- {t["id"]} ({t["label"]}): {t["amazon_rule"]} | Style: {t["style_hint"]}'
        for t in AMAZON_IMAGE_TYPES
    )

    user = f"""Produit :
{json.dumps(product_info, ensure_ascii=False, indent=2)}

Génère un prompt DALL-E 3 pour chacun de ces 7 types d'images Amazon :
{image_specs}

Format JSON attendu :
{{
  "hero": "prompt détaillé en anglais...",
  "lifestyle_1": "prompt...",
  "infographic": "prompt...",
  "detail": "prompt...",
  "dimensions": "prompt...",
  "packaging": "prompt...",
  "lifestyle_2": "prompt..."
}}"""

    response = await get_claude().messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)


async def _generate_image_dalle3(prompt: str, image_id: str) -> Optional[str]:
    """Génère une image via DALL-E 3 et retourne l'URL."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None

    # DALL-E 3 limite les prompts à 4000 caractères
    prompt = prompt[:3900] if len(prompt) > 3900 else prompt

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "quality": "medium",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers=headers,
            json=body,
        )
        if not resp.is_success:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise Exception(f"OpenAI {resp.status_code}: {detail}")
        item = resp.json()["data"][0]
        if "url" in item:
            return item["url"]
        # gpt-image-1 retourne b64_json par défaut
        return f"data:image/png;base64,{item['b64_json']}"


async def generate_product_images(
    sku: str,
    product_name: str,
    brand: str,
    category: str,
    features: list,
    color: str = None,
    material: str = None,
    selected_types: list = None,
) -> list[dict]:
    """
    Pipeline complet : Claude → prompts → DALL-E 3 → images.
    Retourne liste de dicts avec prompt + url_image (None si pas de clé OpenAI).
    """
    product_info = {
        "sku": sku,
        "nom": product_name,
        "marque": brand,
        "catégorie": category,
        "caractéristiques": features,
        "couleur": color,
        "matière": material,
    }

    # 1. Génération des prompts via Claude
    prompts = await _generate_prompts_with_claude(product_info)

    # 2. Types sélectionnés (par défaut : tous)
    types_to_generate = selected_types or [t["id"] for t in AMAZON_IMAGE_TYPES]

    # 3. Génération des images (parallèle, max 3 simultanées)
    semaphore = asyncio.Semaphore(3)
    results = []

    async def _gen_one(image_type: dict):
        img_id = image_type["id"]
        if img_id not in types_to_generate:
            return
        prompt = prompts.get(img_id, "")
        async with semaphore:
            url = None
            error = None
            try:
                url = await _generate_image_dalle3(prompt, img_id)
            except Exception as e:
                error = str(e)
            results.append({
                "id": img_id,
                "slot": image_type["slot"],
                "label": image_type["label"],
                "prompt": prompt,
                "url": url,
                "error": error,
                "has_image": url is not None,
            })

    await asyncio.gather(*[_gen_one(t) for t in AMAZON_IMAGE_TYPES])

    # Trier dans l'ordre MAIN → PT01 → PT06
    order = {t["id"]: i for i, t in enumerate(AMAZON_IMAGE_TYPES)}
    results.sort(key=lambda x: order.get(x["id"], 99))

    return results
