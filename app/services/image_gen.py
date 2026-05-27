"""
Génération d'images produit Amazon via Claude (prompts) + gpt-image-1 (images).
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
        "amazon_rule": (
            "Fond blanc ABSOLU RGB(255,255,255) obligatoire. "
            "Produit seul, aucun accessoire non inclus dans la vente. "
            "Produit occupe 85%+ du cadre. "
            "Aucun texte, logo, filigrane, URL, bordure. "
            "Pas de mise en scène lifestyle. Pas de mannequin. "
            "Pas de dessin ou illustration. Pas d'arrière-plan coloré."
        ),
        "style_hint": (
            "professional studio product photography, pure white background RGB(255,255,255), "
            "product alone centered, fills 85% of frame, soft even studio lighting, "
            "razor sharp focus on entire product, no shadows, no reflections, "
            "no text overlay, no watermark, no borders, photorealistic"
        ),
    },
    {
        "id": "lifestyle_1",
        "label": "Lifestyle — en situation",
        "slot": "PT01",
        "amazon_rule": (
            "Produit utilisé dans son contexte naturel. "
            "Accessoires visibles uniquement s'ils sont inclus dans la vente. "
            "Pas de fond de couleur criarde. Pas de filigrane ni texte. "
            "Pas de mannequin. Ambiance lumineuse et positive."
        ),
        "style_hint": (
            "lifestyle product photography, natural warm light, product in use in realistic setting, "
            "aspirational but authentic scene, soft natural background, "
            "no text overlay, no watermark, no logo, photorealistic, professional quality"
        ),
    },
    {
        "id": "infographic",
        "label": "Infographie features",
        "slot": "PT02",
        "amazon_rule": (
            "Produit centré sur fond clair (blanc ou gris très clair). "
            "Annotations pointant les fonctionnalités clés. "
            "Pas d'arrière-plan coloré vif. Pas de filigrane ni URL. "
            "Texte des annotations lisible et factuel."
        ),
        "style_hint": (
            "product infographic photography, clean light gray or white background, "
            "product centered and sharp, clean annotation arrows pointing to key features, "
            "professional minimal layout, no colored backgrounds, no watermark, photorealistic product"
        ),
    },
    {
        "id": "detail",
        "label": "Zoom matière / qualité",
        "slot": "PT03",
        "amazon_rule": (
            "Gros plan sur une partie spécifique du produit montrant la qualité des matériaux. "
            "Fond neutre (blanc, gris clair ou noir). "
            "Aucun accessoire non vendu avec. Pas de texte ni logo."
        ),
        "style_hint": (
            "extreme macro product photography, close-up detail of material texture and finish quality, "
            "neutral background, studio lighting, tack sharp focus, "
            "showing premium craftsmanship, no text, no watermark, photorealistic"
        ),
    },
    {
        "id": "dimensions",
        "label": "Dimensions / taille",
        "slot": "PT04",
        "amazon_rule": (
            "Vue du produit avec références de taille (main humaine, objet du quotidien connu) "
            "ou représentation visuelle des dimensions. "
            "Fond clair ou neutre. Pas de filigrane ni URL."
        ),
        "style_hint": (
            "product size comparison photography, product next to a human hand or common object for scale, "
            "clean neutral or white background, clear sharp image, "
            "no text overlay, no watermark, professional studio lighting, photorealistic"
        ),
    },
    {
        "id": "packaging",
        "label": "Packaging / unboxing",
        "slot": "PT05",
        "amazon_rule": (
            "Produit + emballage + tous accessoires INCLUS dans la vente présentés ensemble. "
            "Fond neutre (blanc ou gris clair). Pas d'accessoires non inclus. "
            "Pas de texte ajouté ni logo vendeur."
        ),
        "style_hint": (
            "product unboxing flat lay photography, product with its original packaging and all included accessories, "
            "overhead or slight angle shot, clean white or light gray background, "
            "neat organized arrangement, soft studio lighting, no text overlay, no watermark, photorealistic"
        ),
    },
    {
        "id": "lifestyle_2",
        "label": "Lifestyle — angle 2",
        "slot": "PT06",
        "amazon_rule": (
            "Deuxième mise en scène lifestyle, angle ou contexte différent du PT01. "
            "Pas de fond de couleur criarde. Pas de filigrane ni texte. "
            "Pas de mannequin. Ambiance complémentaire à la première lifestyle."
        ),
        "style_hint": (
            "lifestyle product photography from a different angle or setting than the first lifestyle shot, "
            "complementary natural environment, warm professional lighting, "
            "product prominently featured, no text overlay, no watermark, no logo, photorealistic"
        ),
    },
]


async def _generate_prompts_with_claude(product_info: dict) -> dict[str, str]:
    """Claude génère des prompts DALL-E optimisés pour chaque type d'image Amazon."""
    system = """Tu es expert en photographie produit e-commerce Amazon et en rédaction de prompts pour gpt-image-1.
Tu génères des prompts photo ultra-précis en anglais, strictement conformes aux règles officielles Amazon.

RÈGLES AMAZON QUE CHAQUE PROMPT DOIT RESPECTER :

IMAGE PRINCIPALE (hero/MAIN) — règles strictes :
- Fond BLANC ABSOLU RGB(255,255,255) — aucune exception
- Produit seul, occupe 85%+ du cadre
- Aucun accessoire non vendu avec le produit
- Aucune mise en scène lifestyle (pas de personne, pas de décor)
- Aucun texte, logo, filigrane, URL, bordure dans l'image
- Pas de mannequin
- Pas de dessin, illustration ou image animée
- Pas d'arrière-plan coloré
- Éclairage studio professionnel, produit net et précis

IMAGES SECONDAIRES (PT01-PT06) :
- Peuvent avoir un contexte/décor adapté au type
- Aucun accessoire non inclus dans la vente
- Pas d'arrière-plans de couleurs vives ou criardes
- Aucun texte, logo, filigrane, URL ajouté sur l'image
- Pas de mannequin
- Pas de dessin ou illustration — photographie réaliste uniquement

Pour chaque type d'image, génère un prompt détaillé (100-200 mots) qui décrit :
1. Le sujet et sa mise en scène exacte
2. L'éclairage (type, direction, intensité)
3. L'arrière-plan précis
4. L'angle de prise de vue et le cadrage
5. Les détails visuels importants du produit à montrer

Réponds UNIQUEMENT en JSON valide."""

    image_specs = "\n".join(
        f'- {t["id"]} ({t["label"]}): {t["amazon_rule"]} | Style: {t["style_hint"]}'
        for t in AMAZON_IMAGE_TYPES
    )

    user = f"""Produit à photographier :
{json.dumps(product_info, ensure_ascii=False, indent=2)}

Génère un prompt gpt-image-1 pour chacun de ces 7 types d'images Amazon.
Chaque prompt DOIT respecter les règles Amazon du type correspondant.
Inclure dans chaque prompt : le produit exact, l'éclairage, l'arrière-plan, l'angle, les détails visuels.

Types d'images à générer :
{image_specs}

IMPORTANT : Pour le hero, impose TOUJOURS "pure white background RGB(255,255,255)" dans le prompt.
Pour les autres, respecte la règle de chaque type (lifestyle, infographie, etc.).

Format JSON attendu :
{{
  "hero": "prompt détaillé en anglais de 100-200 mots...",
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
