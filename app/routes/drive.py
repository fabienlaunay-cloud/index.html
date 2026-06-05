import os
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.services.drive import list_files, build_csv, get_image_bytes
from app.logger import log

router = APIRouter(prefix="/api/drive", tags=["drive"])


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


@router.post("/extract")
async def extract_drive(body: ExtractRequest, request: Request):
    email = request.state.user_email
    try:
        result = list_files(
            folder_url=body.folder_url,
            brand=body.brand,
            client_name=body.client_name,
            keywords=body.keywords,
            product_type_filter=body.product_type_filter,
            images_only=body.images_only,
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


# ── Image proxy ────────────────────────────────────────────────────────────────

@router.get("/image/{file_id}")
async def proxy_image(file_id: str, request: Request):
    try:
        data, mime = get_image_bytes(file_id)
        return Response(content=data, media_type=mime, headers={"Cache-Control": "max-age=3600"})
    except Exception as e:
        raise HTTPException(404, f"Image introuvable : {e}")
