"""
Google Drive extraction service.
Uses a Service Account (GOOGLE_SERVICE_ACCOUNT_JSON env var) to list, classify,
and proxy files from a shared Drive folder.
"""
import os
import io
import re
import csv
import json
from typing import Optional

_PRODUCT_TYPES: dict[str, list[str]] = {
    "collier":    ["collier", "collar"],
    "laisse":     ["laisse", "leash", "lead"],
    "harnais":    ["harnais", "harness"],
    "bandana":    ["bandana", "foulard"],
    "manteau":    ["manteau", "veste", "jacket", "coat", "imperméable"],
    "peluche":    ["peluche", "stuffed"],
    "jouet":      ["jouet", "toy", "balle", "corde"],
    "gamelle":    ["gamelle", "bowl", "mangeoire", "abreuvoir"],
    "accessoire": ["accessoire", "acces"],
}

_PATTERNS: dict[str, list[str]] = {
    "léopard":  ["leopard", "leo", "léopard"],
    "zèbre":    ["zebre", "zeb", "zèbre", "zebra"],
    "tigre":    ["tigre", "tiger", "tig"],
    "vichy":    ["vichy", "carreaux", "check"],
    "rayures":  ["rayure", "stripe", "raie"],
    "fleuri":   ["fleuri", "fleur", "floral"],
    "écossais": ["ecossais", "écossais", "tartan", "plaid"],
    "damier":   ["damier", "checkerboard"],
    "pois":     ["pois", "polka", "dots"],
    "uni":      ["uni", "plain", "solid"],
}

IMAGE_MIMETYPES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/bmp",
})
DOCUMENT_MIMETYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
    "text/plain",
    "application/json",
    "application/vnd.oasis.opendocument.spreadsheet",
})
ALL_MIMETYPES = IMAGE_MIMETYPES | DOCUMENT_MIMETYPES

_TYPE_CODES = {
    "collier": "COL", "laisse": "LAI", "harnais": "HAR",
    "bandana": "BAN", "manteau": "MAN", "peluche": "PEL",
    "jouet": "JOU",  "gamelle": "GAM", "accessoire": "ACC",
}


def _get_service(refresh_token: Optional[str] = None):
    """Build an authenticated Drive API v3 service.
    Uses user OAuth token if provided, otherwise falls back to service account.
    """
    from googleapiclient.discovery import build
    if refresh_token:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        import os as _os
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=_os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            client_secret=_os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        creds.refresh(GRequest())
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise ValueError("Google Drive non configuré — connectez votre compte Google Drive")
    from google.oauth2 import service_account
    info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def extract_folder_id(url_or_id: str) -> str:
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    clean = url_or_id.strip()
    if re.match(r"^[a-zA-Z0-9_-]{20,}$", clean):
        return clean
    raise ValueError(f"Impossible d'extraire l'ID Drive depuis : {url_or_id}")


def _classify(name: str) -> tuple[Optional[str], Optional[str]]:
    low = name.lower()
    ptype = next((t for t, kws in _PRODUCT_TYPES.items() if any(k in low for k in kws)), None)
    pattern = next((p for p, kws in _PATTERNS.items() if any(k in low for k in kws)), None)
    return ptype, pattern


def _make_sku(brand: str, ptype: Optional[str], idx: int) -> str:
    prefix = re.sub(r"[^A-Z0-9]", "", (brand or "XX").upper())[:4]
    code = _TYPE_CODES.get(ptype or "", "ART")
    return f"{prefix}-{code}-{idx:03d}"


def _collect_files(service, folder_id: str, images_only: bool, documents_only: bool, depth: int = 0) -> list[dict]:
    """Recursively collect files from a folder and all its subfolders (max depth 5)."""
    if depth > 5:
        return []
    results = []
    if images_only:
        type_filter = " and mimeType contains 'image/'"
    elif documents_only:
        mime_q = " or ".join(f"mimeType='{m}'" for m in sorted(DOCUMENT_MIMETYPES))
        type_filter = f" and ({mime_q})"
    else:
        type_filter = ""
    query = f"'{folder_id}' in parents and trashed=false and mimeType != 'application/vnd.google-apps.folder'{type_filter}"
    page_token = None
    while True:
        resp = service.files().list(
            q=query, spaces="drive",
            fields="nextPageToken, files(id,name,mimeType,size,webViewLink)",
            pageToken=page_token, pageSize=500,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    # Recurse into subfolders
    sub_q = f"'{folder_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'"
    page_token = None
    while True:
        resp = service.files().list(
            q=sub_q, spaces="drive",
            fields="nextPageToken, files(id,name)",
            pageToken=page_token, pageSize=200,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute()
        for subfolder in resp.get("files", []):
            results.extend(_collect_files(service, subfolder["id"], images_only, documents_only, depth + 1))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def list_files(
    folder_url: str,
    brand: str = "",
    client_name: str = "",
    keywords: list[str] | None = None,
    product_type_filter: Optional[str] = None,
    images_only: bool = True,
    documents_only: bool = False,
    refresh_token: Optional[str] = None,
) -> dict:
    service = _get_service(refresh_token)
    folder_id = extract_folder_id(folder_url)

    raw = _collect_files(service, folder_id, images_only, documents_only)

    # Keyword filter
    kws = [k.lower().strip() for k in (keywords or []) if k.strip()]
    if kws:
        raw = [f for f in raw if any(k in f["name"].lower() for k in kws)]

    results = []
    type_counts: dict[str, int] = {}
    for i, f in enumerate(raw, 1):
        ptype, pattern = _classify(f["name"])
        if product_type_filter and ptype != product_type_filter:
            continue
        bucket = ptype or "autre"
        type_counts[bucket] = type_counts.get(bucket, 0) + 1
        results.append({
            "file_id":    f["id"],
            "name":       f["name"],
            "mime_type":  f["mimeType"],
            "sku":        _make_sku(brand, ptype, i),
            "product_type": bucket,
            "pattern":    pattern or "",
            "brand":      brand,
            "client":     client_name,
            "is_image":   f["mimeType"].startswith("image/"),
            "web_view_link": f.get("webViewLink", ""),
        })

    return {"files": results, "total": len(results), "by_type": type_counts, "folder_id": folder_id}


def build_csv(files: list[dict], proxy_base: str = "") -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["sku", "nom", "marque", "catégorie", "motif", "image_url", "description"])
    writer.writeheader()
    for f in files:
        if not f.get("is_image"):
            continue
        image_url = (
            f"{proxy_base}/api/drive/image/{f['file_id']}"
            if proxy_base else f.get("web_view_link", "")
        )
        parts = [p for p in [
            f["product_type"].capitalize() if f["product_type"] != "autre" else None,
            f["pattern"].capitalize() if f["pattern"] else None,
            f["brand"] if f["brand"] else None,
        ] if p]
        nom = " ".join(parts) or f["name"].rsplit(".", 1)[0]
        writer.writerow({
            "sku":       f["sku"],
            "nom":       nom,
            "marque":    f["brand"],
            "catégorie": f["product_type"],
            "motif":     f["pattern"],
            "image_url": image_url,
            "description": f"Photo {nom}" + (f" — {f['client']}" if f["client"] else ""),
        })
    return out.getvalue()


# Google-native files can't be downloaded directly; they must be exported
_GOOGLE_EXPORTS = {
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
}


def get_file_bytes(file_id: str, refresh_token: Optional[str] = None) -> tuple[bytes, str]:
    data, mime, _ = get_file_with_name(file_id, refresh_token)
    return data, mime


def get_file_with_name(file_id: str, refresh_token: Optional[str] = None) -> tuple[bytes, str, str]:
    """Download a Drive file and return (bytes, mime_type, name).
    Google-native files (Sheets, Docs) are exported to their Office equivalent.
    """
    from googleapiclient.http import MediaIoBaseDownload
    service = _get_service(refresh_token)
    meta = service.files().get(fileId=file_id, fields="mimeType,name", supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "application/octet-stream")
    name = meta.get("name", "file")

    export = _GOOGLE_EXPORTS.get(mime)
    if export:
        export_mime, ext = export
        req = service.files().export_media(fileId=file_id, mimeType=export_mime)
        mime = export_mime
        if not name.lower().endswith(ext):
            name += ext
    else:
        req = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue(), mime, name
