"""
Chat agent — Amazon listing assistant.

Collects product info through conversation (text, images, PDFs) and emits
[READY_TO_GENERATE]...[/READY_TO_GENERATE] JSON when ready to trigger
the standard generation pipeline.
"""
import os
import json
import base64
from typing import Optional
import anthropic

from app.db import get_db

_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


_SYSTEM_PROMPT = """Tu es un expert en fiches produits Amazon, spécialisé dans l'optimisation SEO pour les marketplaces européennes (FR, DE, IT, ES, UK, NL, DE, PL, SE).

Ton rôle : aider les équipes e-commerce et agences à préparer des fiches Amazon optimisées. Tu collectes les informations produits depuis des conversations naturelles, des images, des PDF de catalogues, ou des fiches fournisseur.

## Comment tu travailles

1. L'utilisateur te partage des informations produits (texte libre, images, PDF, liens, fiches)
2. Tu analyses tout ce qui t'est partagé et extrais les informations produits directement
3. Tu poses des questions ciblées pour compléter les informations manquantes — maximum 3 questions à la fois
4. Quand tu as suffisamment d'informations pour une fiche de qualité, tu proposes de générer

## Informations à collecter pour chaque produit

**Obligatoires :**
- SKU (référence unique) — propose-en un si l'utilisateur n'en a pas
- Nom du produit (brut, pas optimisé — tu t'en charges après)
- Marque
- Segment / Catégorie Amazon (ex: "collier chien", "soin visage", "complément alimentaire")

**Recommandées :**
- Prix (€)
- EAN / code-barres
- Caractéristiques principales (matière, dimensions, poids, certifications)
- Points forts / avantages produit
- Mots-clés SEO si l'utilisateur les connaît

## Règles

- Réponds toujours en français (sauf si l'utilisateur écrit dans une autre langue)
- Sois concis et direct — pas de longs blabla
- Quand tu analyses une image ou un PDF, extrais directement les infos visibles sans redemander
- Si des informations manquent mais sont suffisantes, propose quand même de générer
- Quand l'utilisateur dit "génère", "c'est bon", "lance" ou similaire → émets le bloc JSON immédiatement

## Déclenchement de la génération

Quand tu as SKU + nom + marque + catégorie (minimum), place ce bloc exactement à la fin de ton message :

[READY_TO_GENERATE]
{
  "products": [
    {
      "sku": "PROD-001",
      "name": "Nom du produit",
      "brand": "Marque",
      "category": "segment amazon",
      "description": "Description brute si disponible",
      "price": null,
      "ean": null,
      "features": ["Caractéristique 1", "Caractéristique 2"],
      "focus_keywords": ["mot-clé 1", "mot-clé 2"]
    }
  ]
}
[/READY_TO_GENERATE]

N'inclus ce bloc QUE lorsque les informations sont prêtes. Chaque produit dans "products" doit avoir au minimum sku, name, brand et category.
"""


async def chat(
    session_id: str,
    user_email: str,
    user_message: str,
    files: list,
    existing_messages: list = None,
) -> dict:
    """
    Process one chat turn. Files is a list of dicts:
    {"type": "image"|"pdf", "data": bytes, "media_type": str, "name": str}
    existing_messages: pre-loaded message history (avoids redundant DB read)
    """
    client = get_client()

    if existing_messages is not None:
        messages = existing_messages
    else:
        conn = get_db()
        row = conn.execute(
            "SELECT messages_json FROM chat_sessions WHERE id = ? AND user_email = ?",
            (session_id, user_email),
        ).fetchone()
        conn.close()
        if not row:
            raise ValueError(f"Session {session_id} introuvable")
        messages = json.loads(row["messages_json"] or "[]")

    # Build user content: files first, then text
    content = []
    for f in files:
        ftype = f.get("type", "")
        data_b64 = base64.b64encode(f["data"]).decode()
        if ftype == "image":
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": f["media_type"],
                    "data": data_b64,
                },
            })
        elif ftype == "pdf":
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data_b64,
                },
                "title": f.get("name", "document.pdf"),
            })

    if user_message.strip():
        content.append({"type": "text", "text": user_message})
    elif not content:
        content.append({"type": "text", "text": "(message vide)"})

    messages.append({"role": "user", "content": content})

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    assistant_text = response.content[0].text if response.content else ""
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    # Store assistant text only (not file bytes) in history
    messages.append({"role": "assistant", "content": assistant_text})

    conn = get_db()
    conn.execute(
        "UPDATE chat_sessions SET messages_json = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (json.dumps(messages, ensure_ascii=False), session_id),
    )
    conn.commit()
    conn.close()

    # Detect [READY_TO_GENERATE] marker
    ready_products = None
    if "[READY_TO_GENERATE]" in assistant_text and "[/READY_TO_GENERATE]" in assistant_text:
        try:
            start = assistant_text.index("[READY_TO_GENERATE]") + len("[READY_TO_GENERATE]")
            end = assistant_text.index("[/READY_TO_GENERATE]")
            data = json.loads(assistant_text[start:end].strip())
            ready_products = data.get("products", [])
        except Exception:
            pass

    return {
        "reply": assistant_text,
        "ready_products": ready_products,
        "tokens": {"input": input_tokens, "output": output_tokens},
    }
