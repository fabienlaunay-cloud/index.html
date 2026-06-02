"""
Génération d'images produit Amazon via Claude (prompts) + gpt-image-2 (images).
Si OPENAI_API_KEY absent → retourne uniquement les prompts.
"""

import os
import asyncio
import json
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs
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
            "Produit occupe 85%+ du cadre. Une seule vue du produit. "
            "AUCUN texte, logo, filigrane, URL, bordure. "
            "PAS de mise en scène lifestyle. PAS de mannequin ni personne. "
            "PAS de dessin ou illustration. PAS d'arrière-plan coloré."
        ),
        "style_hint": (
            "professional studio product photography, pure white background RGB(255,255,255), "
            "product alone centered, fills 85% of frame, soft even studio lighting from multiple angles, "
            "razor sharp focus on entire product, no shadows on background, no reflections, "
            "no text overlay, no watermark, no borders, photorealistic, commercial quality"
        ),
    },
    {
        "id": "lifestyle_1",
        "label": "Lifestyle — en situation",
        "slot": "PT01",
        "amazon_rule": (
            "Produit utilisé par une vraie personne dans un contexte naturel et authentique. "
            "Montrer un être humain réel utilisant le produit (pas de mannequin statique). "
            "Ambiance lumineuse, positive, aspirationnelle. "
            "Pas de fond de couleur criarde. Pas de filigrane ni texte ajouté."
        ),
        "style_hint": (
            "high-end lifestyle advertising photography, real person naturally using the product, "
            "authentic candid moment, cinematic composition, beautiful natural or urban setting, "
            "golden hour or soft daylight, shallow depth of field, bokeh background, "
            "editorial magazine quality, emotion and lifestyle aspiration, "
            "no text overlay, no watermark, photorealistic, professional retouching"
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
            "professional minimal tech layout, typography clean and modern, "
            "no colored backgrounds, no watermark, photorealistic product"
        ),
    },
    {
        "id": "detail",
        "label": "Zoom matière / qualité",
        "slot": "PT03",
        "amazon_rule": (
            "Gros plan extrême sur une partie spécifique du produit montrant la qualité des matériaux. "
            "Fond neutre ou contextuel élégant. "
            "Aucun accessoire non vendu avec. Pas de texte ni logo."
        ),
        "style_hint": (
            "extreme close-up macro product photography, tack sharp focus on material texture and craftsmanship, "
            "premium quality details — stitching, finish, grain, surface — beautifully lit, "
            "dark or neutral elegant background, dramatic studio lighting, "
            "luxury brand quality, no text, no watermark, photorealistic"
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
            "product size comparison photography, product held naturally in a human hand or placed next to "
            "a recognizable everyday object for scale reference, "
            "clean neutral or white background, clear sharp image, "
            "soft studio lighting, no text overlay, no watermark, photorealistic"
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
            "premium unboxing flat lay photography, product with original packaging and all included accessories "
            "elegantly arranged, overhead shot with slight angle, "
            "clean white or warm light gray background, "
            "soft diffused studio lighting, neat organized composition, "
            "Apple-style premium aesthetic, no text overlay, no watermark, photorealistic"
        ),
    },
    {
        "id": "lifestyle_2",
        "label": "Lifestyle — angle 2",
        "slot": "PT06",
        "amazon_rule": (
            "Deuxième mise en scène lifestyle avec une personne réelle, "
            "angle ou contexte différent du PT01 (autre activité, autre moment de la journée, autre lieu). "
            "Ambiance complémentaire et cohérente avec la marque. "
            "Pas de fond de couleur criarde. Pas de filigrane ni texte."
        ),
        "style_hint": (
            "high-end lifestyle advertising photography, different scenario from first lifestyle image, "
            "person naturally interacting with the product in a complementary setting, "
            "cinematic lighting, rich colors, editorial composition, "
            "different time of day or environment than PT01, "
            "magazine cover quality, emotion and storytelling, "
            "no text overlay, no watermark, photorealistic"
        ),
    },
]


async def _describe_product_from_image(image_bytes: bytes) -> str:
    """Claude Vision → description visuelle précise du produit (couleurs exactes, matériaux, hardware).

    On analyse la photo AVANT de générer les prompts gpt-image-2 pour que
    les prompts contiennent les vraies couleurs du produit, pas des couleurs inventées.
    """
    import base64

    sig = image_bytes[:12]
    if sig[:2] == b'\xff\xd8':
        media_type = "image/jpeg"
    elif sig[:8] == b'\x89PNG\r\n\x1a\n':
        media_type = "image/png"
    elif sig[8:12] == b'WEBP':
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    image_b64 = base64.standard_b64encode(image_bytes).decode()

    response = await get_claude().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Describe this product's exact visual appearance for AI image generation. "
                        "Be extremely specific:\n"
                        "1. Base/background color of the product (exact shade, e.g. 'dark rust-brown' NOT 'dark')\n"
                        "2. Pattern and secondary colors (e.g. 'jet-black leopard spots' NOT 'multicolor')\n"
                        "3. Hardware/metal color and finish (e.g. 'shiny gold-toned carabiner and D-rings')\n"
                        "4. Material texture (e.g. 'matte nylon webbing')\n"
                        "5. Any other distinctive visual feature\n\n"
                        "Write in English, 60-80 words max. "
                        "NEVER use vague terms like 'multicolor', 'various', 'colorful' — always name the exact shade."
                    ),
                },
            ],
        }]
    )
    return response.content[0].text.strip()


async def _generate_prompts_with_claude(product_info: dict) -> dict[str, str]:
    """Claude génère des prompts DALL-E optimisés pour chaque type d'image Amazon."""
    system = """Tu es un directeur artistique expert en photographie produit haut de gamme et en publicité e-commerce.
Tu rédiges des prompts photo ultra-cinématiques en anglais pour gpt-image-2, au niveau d'une campagne publicitaire Apple ou Nike.

═══ IMAGE PRINCIPALE (hero / MAIN) — règles strictes ═══
- Fond blanc ABSOLU RGB(255,255,255) — aucune exception
- Produit seul, occupe 85%+ du cadre, une seule vue
- PAS de personne, mannequin, lifestyle, texte, logo, filigrane, bordure
- PAS d'emballage sauf si c'est le produit lui-même
- Éclairage studio professionnel, produit net et complet dans le cadre

═══ IMAGES LIFESTYLE (PT01, PT06) — liberté créative ═══
Les images lifestyle PEUVENT et DOIVENT montrer de vraies personnes utilisant le produit.
- Décrire une scène précise et cinématique : qui, où, quand, quelle activité
- Personne réelle, naturelle, authentique (pas de pose figée)
- Décor réel et immersif (rue, appartement moderne, nature, café, bureau...)
- Lumière naturelle ou lumière de qualité cinématographique
- Composition photographique de qualité magazine / publicité haut de gamme
- Émotion visible, storytelling, aspiration

═══ RÈGLES COMMUNES À TOUTES LES IMAGES ═══
- Aucun texte ajouté sur l'image, aucun filigrane, aucun logo de marque
- Aucun badge Amazon (Amazon's Choice, Best Seller, Prime...)
- Photographie réaliste uniquement — pas de dessin ni illustration
- Pas de contenu sexuellement suggestif

═══ QUALITÉ ATTENDUE ═══
Chaque prompt doit produire une image digne d'une campagne publicitaire professionnelle.
Sois précis sur : le sujet, la personne (si applicable), la scène, l'éclairage, l'angle, l'ambiance, les couleurs.
Longueur : 120-200 mots par prompt.

═══ SPECS TECHNIQUES OBLIGATOIRES — images studio (hero, infographic, detail, dimensions, packaging) ═══
Inclus systématiquement ces éléments dans les prompts de type studio :
- "studio product photography"
- "dual softbox lighting, diffused light, no harsh shadows"
- "85mm lens, f/8, ISO 100, shallow depth of field"
- "white seamless background"
- Décrire la matière exacte du produit et ses reflets : mat, brillant, satiné, métallique, tissé, translucide, etc.
  Ex : "matte soft-touch rubber casing with subtle light reflection", "brushed aluminium with specular highlight", "glossy lacquered surface with crisp reflections"
- Post-traitement décrit dans le prompt : "post-processed, upscaled to 4K, professional shadow correction, clean edges, color graded"

Réponds UNIQUEMENT en JSON valide."""

    image_specs = "\n".join(
        f'- {t["id"]} ({t["label"]}): {t["amazon_rule"]} | Style: {t["style_hint"]}'
        for t in AMAZON_IMAGE_TYPES
    )

    visual_block = ""
    if product_info.get("visual_exact_description"):
        visual_block = f"""
⚠️ DESCRIPTION VISUELLE EXACTE ISSUE DE LA PHOTO DU VRAI PRODUIT :
{product_info["visual_exact_description"]}

→ Tu DOIS utiliser ces couleurs et ces détails exacts dans CHACUN de tes 7 prompts.
→ Ne jamais inventer ni changer les couleurs : si la photo montre "brun rouille foncé avec taches noires et quincaillerie dorée", tous les prompts doivent décrire exactement cela.
→ Le produit généré doit être instantanément reconnaissable comme le même produit que sur la photo cliente.
"""

    user = f"""Produit à photographier :
{json.dumps(product_info, ensure_ascii=False, indent=2)}
{visual_block}
Génère 7 prompts gpt-image-2 de qualité publicitaire haut de gamme pour ce produit.

RÈGLES PAR TYPE :
{image_specs}

INSTRUCTIONS CRÉATIVES :
- Pour hero : fond blanc pur RGB(255,255,255), produit seul, 85%+ du cadre, aucun texte ni logo
- Pour lifestyle_1 et lifestyle_2 : invente une scène de vie réaliste avec une vraie personne utilisant ce produit.
  Précise : description physique de la personne (âge, style), lieu exact, activité, lumière, émotion ressentie.
  Exemple pour un casque : "A young woman in her late 20s, wearing the headphones while walking through a sunlit Paris street,
  light jacket, confident and joyful expression, golden hour light, shallow depth of field, bokeh background, editorial photography"
- Pour infographic : produit sur fond clair avec annotations techniques épurées
- Pour detail : gros plan macro sur la matière/finition la plus impressionnante du produit
- Pour dimensions : produit tenu naturellement en main pour montrer l'échelle
- Pour packaging : tout le contenu de la boîte disposé élégamment (flat lay Apple-style)

Format JSON attendu (prompts en anglais, 120-200 mots chacun) :
{{
  "hero": "...",
  "lifestyle_1": "...",
  "infographic": "...",
  "detail": "...",
  "dimensions": "...",
  "packaging": "...",
  "lifestyle_2": "..."
}}"""

    response = await get_claude().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    tok_in = response.usage.input_tokens
    tok_out = response.usage.output_tokens
    raw = response.content[0].text.strip()
    # Nettoyer les blocs markdown éventuels
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    # Extraire le JSON si du texte précède ou suit
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"Aucun JSON trouvé dans la réponse Claude : {raw[:200]}")
    raw = raw[start:end]
    return json.loads(raw), tok_in, tok_out


async def _download_reference_image(url: str) -> Optional[bytes]:
    """Télécharge la photo produit de référence en gérant les URLs Google Drive.

    Google Drive retourne une page HTML de confirmation antivirus pour les fichiers
    > 100 KB via uc?export=download. On utilise l'URL thumbnail en priorité car elle
    retourne l'image directement (content-type: image/jpeg) sans page intermédiaire.
    """
    if not url:
        return None

    urls_to_try = [url]

    # Extraire le file_id Google Drive
    file_id = None
    m = re.search(r'drive\.google\.com/file/d/([^/?]+)', url)
    if m:
        file_id = m.group(1)
    elif 'drive.google.com' in url:
        params = parse_qs(urlparse(url).query)
        file_id = params.get('id', [None])[0]

    if file_id:
        urls_to_try = [
            # thumbnail: retourne l'image directement, pas de page de confirmation
            f"https://drive.google.com/thumbnail?id={file_id}&sz=w1600",
            # fallback: téléchargement direct (peut marcher pour petits fichiers)
            f"https://drive.google.com/uc?export=download&id={file_id}",
        ]

    for download_url in urls_to_try:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(download_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.is_success and len(resp.content) > 2000:
                    ct = resp.headers.get("content-type", "")
                    if "image" in ct or "octet-stream" in ct:
                        return resp.content
        except Exception:
            continue
    return None


async def _generate_image_dalle3(prompt: str, image_id: str, reference_image: Optional[bytes] = None, _retry: int = 4) -> Optional[str]:
    """Génère une image via gpt-image-2.
    Si reference_image fourni → endpoint /images/edits (produit réel comme base).
    Sinon → endpoint /images/generations (text-to-image).
    Retry automatique sur rate-limit 429.
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None

    prompt = prompt[:3500] if len(prompt) > 3500 else prompt

    # Quand une photo de référence est fournie, forcer gpt-image-2 à conserver
    # le produit réel plutôt que d'en inventer un nouveau.
    if reference_image:
        preservation = (
            "IMPORTANT: The attached image is the REAL product photo provided by the client. "
            "You MUST reproduce this exact product in the output with complete fidelity — "
            "same shape, same colors, same design, same textures, same proportions, same details. "
            "Do NOT invent, redesign or replace the product. "
            "The product in the output must be instantly recognizable as the same product as in the reference image. "
        )
        prompt = (preservation + prompt)[:3900]

    async with httpx.AsyncClient(timeout=120) as client:
        if reference_image:
            resp = await client.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "model": "gpt-image-2",
                    "prompt": prompt,
                    "n": "1",
                    "size": "1024x1024",
                    "quality": "medium",
                },
                files={"image": ("product.jpg", reference_image, "image/jpeg")},
            )
        else:
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "gpt-image-2", "prompt": prompt, "n": 1, "size": "1024x1024", "quality": "medium"},
            )

        if resp.status_code == 429 and _retry > 0:
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except Exception:
                msg = ""
            m = re.search(r"try again in (\d+)s", msg)
            wait = int(m.group(1)) + 2 if m else 15
            await asyncio.sleep(wait)
            return await _generate_image_dalle3(prompt, image_id, reference_image, _retry - 1)
        if not resp.is_success:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise Exception(f"OpenAI {resp.status_code}: {detail}")
        item = resp.json()["data"][0]
        if "url" in item:
            return item["url"]
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
    reference_image_url: str = None,
    reference_image_bytes: bytes = None,
) -> list[dict]:
    """
    Pipeline complet : Claude → prompts → gpt-image-2 → images.
    Si reference_image_bytes fourni (photo déjà en mémoire via ZIP upload) ou
    reference_image_url (URL externe publique), utilise /images/edits pour que
    les visuels correspondent au vrai produit.
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

    # 1. Image de référence : bytes déjà dispo (ZIP) ou à télécharger (URL externe)
    reference_image = reference_image_bytes
    if not reference_image and reference_image_url:
        reference_image = await _download_reference_image(reference_image_url)

    # 2. Si photo dispo → Claude Vision extrait les couleurs/détails exacts AVANT les prompts
    if reference_image:
        try:
            product_info["visual_exact_description"] = await _describe_product_from_image(reference_image)
        except Exception:
            pass

    # 3. Génération des prompts via Claude (avec description visuelle exacte si disponible)
    prompts, tok_in, tok_out = await _generate_prompts_with_claude(product_info)

    # 3. Types sélectionnés (par défaut : tous)
    types_to_generate = selected_types or [t["id"] for t in AMAZON_IMAGE_TYPES]

    # 4. Génération des images (3 en parallèle — gpt-image-2 supporte 10 img/min)
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
                url = await _generate_image_dalle3(prompt, img_id, reference_image)
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

    return results, {"input_tokens": tok_in, "output_tokens": tok_out, "reference_image_used": reference_image is not None}
