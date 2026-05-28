"""
Google Drive extraction service.

Auth: Service Account (recommandé) ou token OAuth2.
Le client partage son dossier Drive avec l'email du Service Account.
"""

import os
import re
import csv
import io
import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict

try:
    from googleapiclient.discovery import build
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    GOOGLE_SDK_AVAILABLE = True
except ImportError:
    GOOGLE_SDK_AVAILABLE = False

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

IMAGE_MIME_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp",
    "image/gif", "image/bmp", "image/tiff", "image/heic",
}

# Type produit → mots-clés (ordre de priorité : le plus spécifique en premier)
PRODUCT_TYPES: Dict[str, List[str]] = {
    "harnais":   ["harnais", "arnais", "arnait", "harness", "harnach"],
    "laisse":    ["laisse", "leash"],
    "collier":   ["collier", "collar"],
    "bandana":   ["bandana", "foulard", "neckerchief", "bavoir"],
    "manteau":   ["manteau", "veste", "coat", "jacket", "gilet"],
    "pull":      ["pull", "sweat", "sweater", "hoodie"],
    "peluche":   ["peluche", "doudou", "jouet", "toy", "balle"],
    "gamelle":   ["gamelle", "bol", "bowl", "mangeoire", "fontaine"],
    "lit":       ["lit", "coussin", "panier", "bed", "cushion", "niche"],
    "sac":       ["sac", "sacoche", "transport", "bag", "carrier"],
    "accessoire":["clip", "boucle", "anneau", "charm", "breloque", "medaille"],
}

# Motif → mots-clés
PATTERN_KEYWORDS: Dict[str, List[str]] = {
    "léopard":       ["leopard", "léopard", "leopar", "_leo_", "-leo-", "_leo.", "-leo."],
    "zèbre":         ["zebre", "zèbre", "zebra"],
    "tigre":         ["tigre", "tiger"],
    "pied-de-poule": ["pdp", "pied de poule", "houndstooth", "pied_de_poule"],
    "vichy":         ["vichy", "carreaux", "gingham"],
    "rayures":       ["rayure", "stripe", "mariniere", "marinière"],
    "floral":        ["fleur", "floral", "flower", "rose", "fleuri"],
    "camouflage":    ["camo", "camouflage"],
    "pois":          ["pois", "dot", "polka"],
}


@dataclass
class DriveFile:
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
    extra: Dict = field(default_factory=dict)


def _classify_product_type(filename: str) -> str:
    lower = filename.lower()
    for product_type, keywords in PRODUCT_TYPES.items():
        for kw in keywords:
            if kw in lower:
                return product_type
    return "autre"


def _classify_pattern(filename: str, extra_keywords: List[str] = None) -> str:
    lower = filename.lower()
    # Check user-provided keywords first
    if extra_keywords:
        for kw in extra_keywords:
            if kw.lower() in lower:
                return kw.lower()
    for pattern, keywords in PATTERN_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return pattern
    return ""


def _extract_reference(filename: str) -> str:
    name = re.sub(r"\.[^.]+$", "", filename)
    # Try structured SKU patterns: ABC-123, BDC_LEO_COL_001, etc.
    match = re.search(r"([A-Z]{2,}[-_][A-Z0-9]{2,}(?:[-_][A-Z0-9]+)*)", name.upper())
    if match:
        return match.group(1).replace("_", "-")
    # Fallback: clean the filename
    cleaned = re.sub(r"[^\w]", "-", name).strip("-")
    return cleaned[:40].upper()


def _build_service(credentials_json: str = None, oauth_token: str = None):
    if not GOOGLE_SDK_AVAILABLE:
        raise RuntimeError(
            "google-api-python-client non installé. "
            "Ajoutez google-api-python-client et google-auth à requirements.txt"
        )

    if oauth_token:
        creds = Credentials(token=oauth_token)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # Service Account path
    sa_json = credentials_json or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

    if sa_json:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
    elif sa_file and os.path.exists(sa_file):
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=DRIVE_SCOPES)
    else:
        raise RuntimeError(
            "Aucune authentification Google trouvée. "
            "Définissez GOOGLE_SERVICE_ACCOUNT_JSON ou GOOGLE_SERVICE_ACCOUNT_FILE."
        )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def extract_from_folder(
    folder_id: str,
    brand: str = "",
    client: str = "",
    keywords: List[str] = None,
    product_type_filter: List[str] = None,
    credentials_json: str = None,
    oauth_token: str = None,
    max_files: int = 500,
) -> List[DriveFile]:
    """
    Liste et filtre les fichiers images d'un dossier Drive.

    keywords     : filtres sur le nom de fichier (ex: ["léopard", "leopard"])
    product_type_filter : limiter à certains types (ex: ["collier", "laisse"])
    """
    service = _build_service(credentials_json, oauth_token)

    # Build Drive query
    q_parts = [
        f"'{folder_id}' in parents",
        "trashed = false",
        "(" + " or ".join(f"mimeType = '{m}'" for m in IMAGE_MIME_TYPES) + ")",
    ]
    q = " and ".join(q_parts)

    fields = (
        "nextPageToken, files("
        "id, name, mimeType, size, modifiedTime, "
        "webViewLink, thumbnailLink, parents"
        ")"
    )

    files: List[DriveFile] = []
    page_token = None

    while len(files) < max_files:
        resp = service.files().list(
            q=q,
            fields=fields,
            pageSize=min(100, max_files - len(files)),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()

        for f in resp.get("files", []):
            fname = f.get("name", "")

            # Keyword filter (at least one keyword must appear in filename)
            if keywords:
                lower = fname.lower()
                if not any(kw.lower() in lower for kw in keywords):
                    continue

            ptype = _classify_product_type(fname)

            # Product type filter
            if product_type_filter and ptype not in product_type_filter:
                continue

            fid = f["id"]
            df = DriveFile(
                file_id=fid,
                file_name=fname,
                product_type=ptype,
                pattern=_classify_pattern(fname, keywords),
                reference=_extract_reference(fname),
                brand=brand,
                client=client,
                web_view_link=f.get("webViewLink", f"https://drive.google.com/file/d/{fid}/view"),
                download_url=f"https://drive.google.com/uc?id={fid}&export=download",
                thumbnail_url=f.get("thumbnailLink", ""),
                mime_type=f.get("mimeType", ""),
                size_bytes=int(f.get("size", 0)),
                modified_time=f.get("modifiedTime", ""),
            )
            files.append(df)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # Sort by product type, then filename
    files.sort(key=lambda x: (x.product_type, x.file_name))
    return files


def to_csv_bytes(files: List[DriveFile]) -> bytes:
    """CSV compatible avec l'ingestion existante + colonnes Drive."""
    if not files:
        return b""

    fieldnames = [
        "sku", "name", "brand", "category", "color",
        "images", "drive_file_id", "drive_url", "file_name",
        "client", "modified_time", "size_bytes",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for f in files:
        label_parts = [f.brand, f.product_type.capitalize(), f.pattern.capitalize()]
        label = " ".join(p for p in label_parts if p)
        writer.writerow({
            "sku": f.reference,
            "name": label or f.file_name,
            "brand": f.brand,
            "category": f.product_type,
            "color": f.pattern,
            "images": f.download_url,
            "drive_file_id": f.file_id,
            "drive_url": f.web_view_link,
            "file_name": f.file_name,
            "client": f.client,
            "modified_time": f.modified_time,
            "size_bytes": f.size_bytes,
        })

    return buf.getvalue().encode("utf-8-sig")


def summary_by_type(files: List[DriveFile]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for f in files:
        counts[f.product_type] = counts.get(f.product_type, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))
