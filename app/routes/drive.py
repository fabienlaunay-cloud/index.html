import io
import os
import zipfile
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.services.drive import list_files, build_csv, get_file_bytes, get_file_with_name
from app.db import get_drive_token
from app.logger import log

router = APIRouter(prefix="/api/drive", tags=["drive"])

DOCUMENT_MIMETYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
    "text/plain",
    "application/json",
})


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/status")
async def drive_status(request: Request):
    configured = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())
    sa_email = ""
    if configured:
        import json
        try:
            info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
            sa_email = info.get("client_email", "")
        except Exception:
            pass
    return {"configured": configured, "service_account_email": sa_email}


# ── Extract ────────────────────────────────────────────────────────────────────

class ExtractRequest(BaseModel):
    folder_url: str
    brand: str = ""
    client_name: str = ""
    keywords: list[str] = []
    product_type_filter: Optional[str] = None
    images_only: bool = True
    documents_only: bool = False


@router.post("/extract")
async def extract_drive(body: ExtractRequest, request: Request):
    email = request.state.user_email
    token_data = get_drive_token(email)
    refresh_token = token_data["refresh_token"] if token_data else None
    try:
        result = list_files(
            folder_url=body.folder_url,
            brand=body.brand,
            client_name=body.client_name,
            keywords=body.keywords,
            product_type_filter=body.product_type_filter,
            images_only=body.images_only,
            documents_only=body.documents_only,
            refresh_token=refresh_token,
        )
        log.info(f"[drive] {email} extracted {result['total']} files from folder {result['folder_id']}")
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error(f"[drive] extract error for {email}: {e}")
        raise HTTPException(500, f"Erreur Drive : {e}")


# ── CSV export ─────────────────────────────────────────────────────────────────

class ExportCSVRequest(BaseModel):
    files: list[dict]


@router.post("/export/csv")
async def export_drive_csv(body: ExportCSVRequest, request: Request):
    base = str(request.base_url).rstrip("/")
    csv_content = build_csv(body.files, proxy_base=base)
    return Response(
        content=csv_content.encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="synqio_drive_export.csv"'},
    )


# ── ZIP export ─────────────────────────────────────────────────────────────────

class ExportZIPRequest(BaseModel):
    files: list[dict]  # [{file_id, name}]


@router.post("/export/zip")
async def export_drive_zip(body: ExportZIPRequest, request: Request):
    email = request.state.user_email
    token_data = get_drive_token(email)
    refresh_token = token_data["refresh_token"] if token_data else None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in body.files:
            try:
                data, _ = get_file_bytes(f["file_id"], refresh_token)
                zf.writestr(f.get("name", f["file_id"]), data)
            except Exception as e:
                log.warning(f"[drive] zip skip {f.get('name')}: {e}")
    buf.seek(0)
    log.info(f"[drive] {email} downloaded ZIP with {len(body.files)} files")
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="synqio_drive_photos.zip"'},
    )


# ── File proxy (for product data import) ──────────────────────────────────────

@router.get("/file/{file_id}")
async def proxy_file(file_id: str, name: str = Query(default=""), request: Request = None):
    token_data = get_drive_token(request.state.user_email) if request else None
    refresh_token = token_data["refresh_token"] if token_data else None
    try:
        data, mime, real_name = get_file_with_name(file_id, refresh_token)
        safe_name = (name or real_name or "file").replace('"', '')
        return Response(
            content=data,
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "Cache-Control": "max-age=300",
            },
        )
    except Exception as e:
        raise HTTPException(404, f"Fichier introuvable : {e}")


# ── Image proxy ────────────────────────────────────────────────────────────────

@router.get("/image/{file_id}")
async def proxy_image(file_id: str, request: Request = None):
    token_data = get_drive_token(request.state.user_email) if request else None
    refresh_token = token_data["refresh_token"] if token_data else None
    try:
        data, mime = get_file_bytes(file_id, refresh_token)
        return Response(content=data, media_type=mime, headers={"Cache-Control": "max-age=3600"})
    except Exception as e:
        raise HTTPException(404, f"Image introuvable : {e}")
