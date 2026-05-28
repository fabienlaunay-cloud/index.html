"""Routes API pour l'extraction Google Drive."""

import os
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.services.drive import (
    extract_from_folder, to_csv_bytes, summary_by_type,
    GOOGLE_SDK_AVAILABLE, PRODUCT_TYPES, PATTERN_KEYWORDS,
)

router = APIRouter(prefix="/api/drive", tags=["drive"])


class DriveExtractRequest(BaseModel):
    folder_id: str
    brand: str = ""
    client: str = ""
    keywords: List[str] = []
    product_type_filter: List[str] = []
    max_files: int = 500
    oauth_token: Optional[str] = None  # Token OAuth2 passé depuis le front (optionnel)


class DriveFileOut(BaseModel):
    file_id: str
    file_name: str
    product_type: str
    pattern: str
    reference: str
    brand: str
    client: str
    web_view_link: str
    download_url: str
    thumbnail_url: str
    mime_type: str
    size_bytes: int
    modified_time: str


class DriveExtractResponse(BaseModel):
    total: int
    by_product_type: dict
    files: List[DriveFileOut]


@router.get("/status")
async def drive_status():
    """Vérifie si le SDK Google est disponible et si les credentials sont configurés."""
    sa_json = bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
    sa_file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    sa_file = bool(sa_file_path and os.path.exists(sa_file_path))
    return {
        "sdk_available": GOOGLE_SDK_AVAILABLE,
        "service_account_json": sa_json,
        "service_account_file": sa_file,
        "configured": GOOGLE_SDK_AVAILABLE and (sa_json or sa_file),
        "product_types": list(PRODUCT_TYPES.keys()),
        "patterns": list(PATTERN_KEYWORDS.keys()),
    }


@router.post("/extract", response_model=DriveExtractResponse)
async def drive_extract(body: DriveExtractRequest, request: Request):
    """
    Extrait et filtre les photos produit d'un dossier Google Drive.

    - folder_id  : ID du dossier Drive (tiré de l'URL : .../folders/<ID>)
    - keywords   : filtres sur le nom de fichier (ex: ["léopard", "leo"])
    - product_type_filter : limiter à certains types produit (optionnel)
    """
    if not body.folder_id.strip():
        raise HTTPException(400, "folder_id est requis")

    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    oauth_token = body.oauth_token

    try:
        files = extract_from_folder(
            folder_id=body.folder_id.strip(),
            brand=body.brand,
            client=body.client,
            keywords=body.keywords or None,
            product_type_filter=body.product_type_filter or None,
            credentials_json=credentials_json,
            oauth_token=oauth_token,
            max_files=min(body.max_files, 1000),
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        detail = str(e)
        if "HttpError 404" in detail or "not found" in detail.lower():
            raise HTTPException(404, "Dossier Drive introuvable ou non partagé avec le Service Account")
        if "HttpError 403" in detail or "permission" in detail.lower():
            raise HTTPException(403, "Accès refusé. Partagez le dossier avec l'email du Service Account")
        raise HTTPException(500, f"Erreur Drive : {detail}")

    return DriveExtractResponse(
        total=len(files),
        by_product_type=summary_by_type(files),
        files=[DriveFileOut(**{k: v for k, v in f.__dict__.items() if k != "extra"}) for f in files],
    )


@router.post("/export/csv")
async def drive_export_csv(body: DriveExtractRequest):
    """Extrait et retourne directement un CSV téléchargeable."""
    if not body.folder_id.strip():
        raise HTTPException(400, "folder_id est requis")

    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    try:
        files = extract_from_folder(
            folder_id=body.folder_id.strip(),
            brand=body.brand,
            client=body.client,
            keywords=body.keywords or None,
            product_type_filter=body.product_type_filter or None,
            credentials_json=credentials_json,
            oauth_token=body.oauth_token,
            max_files=min(body.max_files, 1000),
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erreur Drive : {e}")

    safe_brand = (body.brand or "drive").replace(" ", "_").lower()
    filename = f"extraction_{safe_brand}.csv"
    csv_bytes = to_csv_bytes(files)

    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
