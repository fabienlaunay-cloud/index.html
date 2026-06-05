"""
SynqIO AI Agent — natural language interface that executes app actions.
"""
import os
import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
import anthropic

from app.db import get_drive_token
from app.logger import log

router = APIRouter(prefix="/api/agent", tags=["agent"])

_aclient = None


def _get_client():
    global _aclient
    if _aclient is None:
        _aclient = anthropic.AsyncAnthropic()
    return _aclient


SYSTEM = """Tu es SynqIO Agent, un assistant IA intégré dans SynqIO — une plateforme de génération de fiches produits Amazon.

Tu aides les utilisateurs à utiliser l'app en langage naturel. Tu peux exécuter des actions directement dans l'interface.

Actions disponibles :
- Connecter Google Drive (connect_drive)
- Ouvrir le navigateur Drive pour choisir un dossier/fichier (open_drive_browser)
- Définir l'URL d'un dossier/fichier Drive (set_drive_folder)
- Lister le contenu d'un dossier Drive (list_drive_folder)
- Définir les mots-clés prioritaires (set_keywords)
- Définir le ton de rédaction (set_tone)
- Lancer l'import des fichiers Drive (import_drive_files)
- Lancer la génération des fiches (generate_listings)
- Exporter les résultats (export_results)

Instructions :
- Réponds toujours en français, de façon concise
- Enchaîne les actions logiquement sans demander confirmation inutile
- Si l'utilisateur donne toutes les infos, exécute directement
- Confirme brièvement ce que tu fais
- Si une info manque, demande-la en une seule question"""

TOOLS = [
    {
        "name": "connect_drive",
        "description": "Lance la connexion OAuth Google Drive.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "open_drive_browser",
        "description": "Ouvre le navigateur de fichiers Drive pour que l'utilisateur sélectionne un dossier ou fichier.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["photos", "documents"],
                    "description": "photos = dossier d'images produits ; documents = fichier catalogue CSV/Excel",
                }
            },
            "required": ["target"],
        },
    },
    {
        "name": "set_drive_folder",
        "description": "Définit directement l'URL ou l'ID d'un dossier/fichier Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_url": {"type": "string", "description": "URL complète Google Drive ou ID"},
                "target": {"type": "string", "enum": ["photos", "documents", "unified"]},
            },
            "required": ["folder_url", "target"],
        },
    },
    {
        "name": "list_drive_folder",
        "description": "Liste le contenu d'un dossier Google Drive (sous-dossiers et fichiers).",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_url": {"type": "string", "description": "URL Google Drive ou ID du dossier"},
            },
            "required": ["folder_url"],
        },
    },
    {
        "name": "set_keywords",
        "description": "Définit les mots-clés prioritaires pour la génération.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "Mots-clés séparés par des virgules"},
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "set_tone",
        "description": "Définit le ton de rédaction des fiches produits.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tone": {
                    "type": "string",
                    "enum": ["professionnel", "enthousiaste", "technique", "premium_luxe", "eco_responsable"],
                }
            },
            "required": ["tone"],
        },
    },
    {
        "name": "import_drive_files",
        "description": "Lance l'import/extraction des fichiers depuis le dossier Drive configuré.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "generate_listings",
        "description": "Lance la génération des fiches produits Amazon.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "export_results",
        "description": "Exporte les fiches générées.",
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["csv", "zip"]},
            },
            "required": ["format"],
        },
    },
]

_CLIENT_TOOLS = {
    "connect_drive", "open_drive_browser", "set_drive_folder",
    "set_keywords", "set_tone", "import_drive_files",
    "generate_listings", "export_results",
}


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def agent_chat(request: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")

    email = request.state.user_email
    body = await request.json()
    messages = list(body.get("messages", []))
    context = body.get("context", {})

    drive_token_data = get_drive_token(email)
    refresh_token = drive_token_data["refresh_token"] if drive_token_data else None

    ctx_parts = []
    if context.get("drive_connected"):
        ctx_parts.append(f"Drive connecté ({context.get('drive_email', '')})")
    else:
        ctx_parts.append("Drive non connecté")
    if context.get("files_count"):
        ctx_parts.append(f"{context['files_count']} fichiers chargés")
    if context.get("marketplace"):
        ctx_parts.append(f"Marketplace: {context['marketplace']}")

    system = SYSTEM
    if ctx_parts:
        system += f"\n\nContexte actuel : {', '.join(ctx_parts)}."

    async def generate():
        history = list(messages)

        for _round in range(6):
            try:
                async with _get_client().messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=512,
                    system=system,
                    messages=history,
                    tools=TOOLS,
                ) as stream:
                    async for text in stream.text_stream:
                        yield _sse({"type": "text", "delta": text})
                    final = await stream.get_final_message()
            except Exception as e:
                log.error(f"[agent] stream error: {e}")
                yield _sse({"type": "error", "message": str(e)})
                return

            if final.stop_reason != "tool_use":
                break

            tool_results = []
            content_dicts = []

            for block in final.content:
                if not hasattr(block, "type"):
                    continue
                if block.type == "text":
                    content_dicts.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content_dicts.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                    tool_input = block.input if hasattr(block, "input") else {}

                    if block.name in _CLIENT_TOOLS:
                        yield _sse({"type": "action", "name": block.name, "input": tool_input})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Action UI exécutée.",
                        })

                    elif block.name == "list_drive_folder":
                        if refresh_token:
                            try:
                                from app.services.drive import list_files
                                result = list_files(
                                    tool_input.get("folder_url", ""),
                                    refresh_token=refresh_token,
                                    images_only=False,
                                    documents_only=False,
                                )
                                summary = json.dumps({
                                    "total": result["total"],
                                    "by_type": result["by_type"],
                                    "files": [
                                        {"name": f["name"], "type": f["product_type"]}
                                        for f in result["files"][:20]
                                    ],
                                }, ensure_ascii=False)
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": summary,
                                })
                            except Exception as e:
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": f"Erreur: {e}",
                                    "is_error": True,
                                })
                        else:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": "Drive non connecté.",
                            })
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Outil inconnu.",
                        })

            history.append({"role": "assistant", "content": content_dicts})
            history.append({"role": "user", "content": tool_results})

        yield _sse({"type": "done"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
