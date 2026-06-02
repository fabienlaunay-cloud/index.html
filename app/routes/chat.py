import os
import json
from uuid import uuid4
from typing import List

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form

from app.db import get_db

router = APIRouter(prefix="/api/chat", tags=["chat"])

_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


@router.get("/sessions")
async def list_sessions(request: Request):
    email = request.state.user_email
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at FROM chat_sessions "
        "WHERE user_email = ? ORDER BY updated_at DESC LIMIT 50",
        (email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/sessions")
async def create_session(request: Request):
    email = request.state.user_email
    session_id = str(uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_sessions (id, user_email, title, messages_json) VALUES (?, ?, ?, ?)",
        (session_id, email, "Nouvelle conversation", "[]"),
    )
    conn.commit()
    conn.close()
    return {"id": session_id, "title": "Nouvelle conversation"}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    email = request.state.user_email
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM chat_sessions WHERE id = ? AND user_email = ?",
        (session_id, email),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Session introuvable")
    d = dict(row)
    raw_msgs = json.loads(d.pop("messages_json", "[]"))
    d["messages"] = _simplify_messages(raw_msgs)
    return d


@router.post("/sessions/{session_id}/message")
async def send_message(
    session_id: str,
    request: Request,
    message: str = Form(default=""),
    files: List[UploadFile] = File(default=[]),
):
    email = request.state.user_email

    conn = get_db()
    row = conn.execute(
        "SELECT id, messages_json FROM chat_sessions WHERE id = ? AND user_email = ?",
        (session_id, email),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Session introuvable")

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")

    processed_files = []
    for f in files:
        data = await f.read()
        if len(data) > _MAX_FILE_SIZE:
            raise HTTPException(413, f"Fichier trop volumineux (max 20 Mo)")
        ct = (f.content_type or "").lower()
        fname = f.filename or ""
        if ct in _IMAGE_TYPES or ct.startswith("image/"):
            processed_files.append({"type": "image", "media_type": ct or "image/jpeg", "data": data, "name": fname})
        elif ct == "application/pdf" or fname.lower().endswith(".pdf"):
            processed_files.append({"type": "pdf", "media_type": "application/pdf", "data": data, "name": fname})

    from app.services.chat_agent import chat
    from app.services.usage import log_usage

    msgs_before = json.loads(row["messages_json"] or "[]")

    try:
        result = await chat(
            session_id, email, message, processed_files,
            existing_messages=list(msgs_before),
        )
    except Exception as e:
        raise HTTPException(500, f"Erreur agent: {e}")

    tokens = result.get("tokens", {})
    if tokens.get("input"):
        log_usage(email, "tokens_in", tokens["input"])
    if tokens.get("output"):
        log_usage(email, "tokens_out", tokens["output"])

    # Update title from first user message
    if message.strip() and not any(m.get("role") == "user" for m in msgs_before):
        title = message[:60] + ("…" if len(message) > 60 else "")
        conn = get_db()
        conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ?",
            (title, session_id),
        )
        conn.commit()
        conn.close()

    return result


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    email = request.state.user_email
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM chat_sessions WHERE id = ? AND user_email = ?",
        (session_id, email),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Session introuvable")
    return {"ok": True}


def _simplify_messages(messages: list) -> list:
    """Convert raw Anthropic message history to simple {role, text, has_files} dicts."""
    result = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            has_files = any(b.get("type") in ("image", "document") for b in content)
            result.append({"role": m["role"], "text": "\n".join(texts), "has_files": has_files})
        else:
            result.append({"role": m["role"], "text": str(content), "has_files": False})
    return result
