import os
import io
import json
import time
import asyncio
import base64
import httpx
from uuid import uuid4
from dotenv import load_dotenv
from app.logger import log
load_dotenv(override=False)  # local dev only — Railway injects vars directly

from collections import defaultdict
from threading import Lock as _Lock
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request

# ── In-memory per-user rate limiter ──────────────────────────────────────────
# State is per-process — fine for single-instance Railway deployment.
_rl_data: dict[str, list[float]] = defaultdict(list)
_rl_lock = _Lock()

def _rate_limit(key: str, limit: int, window: int = 3600) -> bool:
    """Return True if request is allowed; False if rate-limit exceeded.
    key: e.g. f"{email}:generate". limit: max calls. window: seconds."""
    now = time.time()
    with _rl_lock:
        _rl_data[key] = [t for t in _rl_data[key] if now - t < window]
        if len(_rl_data[key]) >= limit:
            return False
        _rl_data[key].append(now)
        return True
from fastapi.responses import Response, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from app.models import (
    GenerationRequest, GenerationResult, PublishRequest, PublishResult,
    RawProduct, Marketplace, AmazonListing,
)
from app.services.ingestion import parse_file, get_headers_and_sample
from app.services.ai_agent import generate_listings_batch
from app.services.amazon_sp import publish_listings
from app.services.auth import verify_token
from app.services.image_gen import generate_product_images, AMAZON_IMAGE_TYPES, _generate_image_dalle3
from app.services.usage import log_usage, get_user_usage, get_all_users_usage
from app.services import storage
from app.utils.export import to_csv_bytes, to_json_bytes, to_amazon_flat_file_bytes, to_listing_loader_bytes, to_amazon_flat_file_xlsx, to_listing_loader_xlsx, to_variation_flat_file_xlsx
from app.utils.variation_handler import group_by_parent, build_parent_product, expand_to_variation_listings
from app.db import (init_db, save_generation, list_generations, get_generation,
                    delete_generation, update_generation_label, save_job, update_job_db,
                    load_recent_jobs, add_tracked_listing, list_tracked_listings,
                    delete_tracked_listing, add_snapshot, get_snapshots,
                    delete_snapshot, get_tracking_summary,
                    save_session, list_saved_sessions, get_saved_session, delete_saved_session)
from app.routes.auth import router as auth_router, admin_router
from app.routes.amazon_oauth import router as amazon_router
from app.routes.chat import router as chat_router

# Routes sans authentification
PUBLIC_PATHS = {"/", "/health", "/api/auth/login", "/api/auth/setup", "/api/auth/needs-setup", "/api/auth/reset-admin", "/api/auth/debug-admin", "/api/amazon/debug-config", "/api/marketplaces", "/api/amazon/callback", "/api/template"}
# Invite paths are public (token-based auth)
PUBLIC_PREFIX_PATHS = ("/api/auth/invite/", "/api/photos/")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Laisser passer les routes publiques, les fichiers statiques et les chemins à préfixe public
        if path in PUBLIC_PATHS or not path.startswith("/api/") or any(path.startswith(p) for p in PUBLIC_PREFIX_PATHS):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "Non authentifié"}, status_code=401)

        email = verify_token(auth.split(" ", 1)[1])
        if not email:
            return JSONResponse({"detail": "Session expirée, reconnectez-vous"}, status_code=401)

        request.state.user_email = email
        return await call_next(request)


app = FastAPI(
    title="Marketplace AI Agent",
    description="Transforme des données produits brutes en fiches marketplaces optimisées SEO",
    version="1.0.0",
)

_ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "https://synqio.io,https://www.synqio.io",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)
app.add_middleware(AuthMiddleware)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(amazon_router)
app.include_router(chat_router)


@app.on_event("startup")
async def startup():
    _check_secrets()
    sentry_dsn = os.getenv("SENTRY_DSN")
    if sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=sentry_dsn,
            integrations=[StarletteIntegration(transaction_style="endpoint"),
                          FastApiIntegration()],
            traces_sample_rate=0.05,
            environment=os.getenv("RAILWAY_ENVIRONMENT", "development"),
            release=os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"),
        )
        log.info("Sentry initialised", extra={"endpoint": "startup"})
    from app.db import DB_PATH
    db_abs = os.path.abspath(DB_PATH)
    log.info(f"[SynqIO] DB path: {db_abs}")
    log.info(f"[SynqIO] DB on volume: {db_abs.startswith('/app/data')}")
    log.info(f"[SynqIO] DB exists: {os.path.isfile(db_abs)}")
    init_db()
    _bootstrap_admin()
    try:
        _jobs.update(load_recent_jobs())
    except Exception:
        pass


def _check_secrets():
    import secrets as _secrets
    jwt = os.getenv("JWT_SECRET", "")
    insecure_default = "change-me-use-a-long-random-string-in-production"
    if not jwt or jwt == insecure_default or len(jwt) < 32:
        suggestion = _secrets.token_hex(32)
        log.warning(
            f"\n{'='*60}\n"
            f"SECURITY WARNING: JWT_SECRET is weak or missing.\n"
            f"   Set this env var in Railway:\n"
            f"   JWT_SECRET={suggestion}\n"
            f"{'='*60}\n"
        )


def _bootstrap_admin():
    """Crée ou restaure le compte admin depuis ADMIN_EMAIL / ADMIN_PASSWORD.
    Appelé au démarrage : garantit que l'admin garde is_admin=1 même après
    un redéploiement Railway qui efface la DB éphémère.
    """
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not email or not password:
        print("[bootstrap_admin] ADMIN_EMAIL ou ADMIN_PASSWORD manquant — admin non créé", flush=True)
        return
    from app.db import get_db
    from app.services.auth import create_user, hash_password
    try:
        conn = get_db()
        exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        if not exists:
            conn.close()
            create_user(email, password, name="Admin", is_admin=True)
            print(f"[bootstrap_admin] Admin créé : {email}", flush=True)
        else:
            conn.execute(
                "UPDATE users SET is_admin=1, is_active=1, password_hash=? WHERE email=?",
                (hash_password(password), email),
            )
            conn.commit()
            conn.close()
            print(f"[bootstrap_admin] Admin mis à jour : {email}", flush=True)
    except Exception as exc:
        print(f"[bootstrap_admin] ERREUR : {exc}", flush=True)


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    with open("frontend/sitemap.xml", "r", encoding="utf-8") as f:
        return Response(content=f.read(), media_type="application/xml")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    from app.db import DB_PATH
    db_abs = os.path.abspath(DB_PATH)
    db_exists = os.path.isfile(db_abs)
    db_size = os.path.getsize(db_abs) if db_exists else 0
    user_count = 0
    try:
        from app.db import get_db
        conn = get_db()
        row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
        user_count = row["cnt"] if row else 0
        conn.close()
    except Exception:
        pass
    return {
        "status": "ok",
        "ai_ready": bool(os.getenv("ANTHROPIC_API_KEY")),
        "version": "1.0.0",
        "db_path": db_abs,
        "db_on_volume": db_abs.startswith("/app/data"),
        "db_exists": db_exists,
        "db_size_bytes": db_size,
        "user_count": user_count,
    }


# ── Template ─────────────────────────────────────────────────────────────────

@app.get("/api/template")
async def download_template():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = openpyxl.Workbook()

    # ── Onglet 1 : Produits ──────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Produits"

    headers = [
        "SKU", "Nom", "Marque", "Segment", "EAN", "Prix",
        "Image_Fichier", "Poids_kg", "Dimensions_cm",
        "Caractéristiques", "Mots_cles",
        "Parent_SKU", "Variation_Theme", "Variation_Value", "Taille",
    ]
    mandatory = {"SKU", "Nom", "Marque", "Segment"}
    photo_col = {"Image_Fichier"}
    variation_col = {"Parent_SKU", "Variation_Theme", "Variation_Value", "Taille"}

    # Styles en-tête
    fill_mandatory = PatternFill("solid", fgColor="764BA2")
    fill_photo     = PatternFill("solid", fgColor="0F766E")   # vert sarcelle pour Image_Fichier
    fill_optional  = PatternFill("solid", fgColor="B39DDB")
    fill_variation = PatternFill("solid", fgColor="2563EB")   # bleu pour colonnes variation
    font_header    = Font(color="FFFFFF", bold=True, size=11)
    font_example   = Font(color="555555", italic=True, size=10)
    center         = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col, h in enumerate(headers, 1):
        label = h + (" *" if h in mandatory else "")
        cell = ws.cell(row=1, column=col, value=label)
        if h in mandatory:
            cell.fill = fill_mandatory
        elif h in photo_col:
            cell.fill = fill_photo
        elif h in variation_col:
            cell.fill = fill_variation
        else:
            cell.fill = fill_optional
        cell.font = font_header
        cell.alignment = center

    # Exemple 1 — collier (standalone)
    ex1 = [
        "BC-COL-001", "Collier Étoile Dorée Chien M", "Bande de Canailles",
        "collier chien", "3760123456789", "24.90",
        "collier-etoile-doree.jpg", "0.085", "35x2 cm",
        "Pendentif étoile doré|Boucle sécurité|Réglage 5 positions|Nylon recyclé certifié",
        "collier chien tendance, collier chien pendentif, collier chien fantaisie",
        "", "", "", "",
    ]
    # Exemple 2 — laisse déclinaison couleur/taille (variation)
    ex2 = [
        "BC-LAI-012-ROUGE-S", "Laisse Léopard Chien Rouge S", "Bande de Canailles",
        "laisse chien", "3760123456790", "29.90",
        "laisse-leopard-rouge.jpg", "0.120", "120x2 cm",
        "Mousqueton inox doré|Poignée rembourrée|Motif léopard|Longueur 1.2m",
        "laisse chien design, laisse chien fantaisie, laisse chien colorée",
        "BC-LAI-012", "ColorName-SizeClass", "Rouge / S", "S",
    ]
    ex3 = [
        "BC-LAI-012-BLEU-M", "Laisse Léopard Chien Bleu M", "Bande de Canailles",
        "laisse chien", "3760123456791", "29.90",
        "laisse-leopard-bleu.jpg", "0.120", "120x2 cm",
        "Mousqueton inox doré|Poignée rembourrée|Motif léopard|Longueur 1.2m",
        "laisse chien design, laisse chien fantaisie, laisse chien colorée",
        "BC-LAI-012", "ColorName-SizeClass", "Bleu / M", "M",
    ]

    for col, v in enumerate(ex1, 1):
        cell = ws.cell(row=2, column=col, value=v)
        cell.font = font_example
    for col, v in enumerate(ex2, 1):
        cell = ws.cell(row=3, column=col, value=v)
        cell.font = font_example
    for col, v in enumerate(ex3, 1):
        cell = ws.cell(row=4, column=col, value=v)
        cell.font = font_example

    # Largeurs de colonnes
    widths = [18, 34, 20, 18, 16, 8, 30, 10, 14, 48, 48, 16, 20, 18, 10]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    # ── Onglet 2 : Guide ─────────────────────────────────────────────────────
    guide = wb.create_sheet("Guide")
    guide.column_dimensions["A"].width = 20
    guide.column_dimensions["B"].width = 14
    guide.column_dimensions["C"].width = 68
    guide.column_dimensions["D"].width = 40

    guide_headers = ["Colonne", "Obligatoire ?", "Description", "Exemple"]
    for col, h in enumerate(guide_headers, 1):
        cell = guide.cell(row=1, column=col, value=h)
        cell.fill = PatternFill("solid", fgColor="1F2937")
        cell.font = Font(color="FFFFFF", bold=True)

    guide_rows = [
        ("SKU",             "✅ Oui",       "Référence unique du produit. Doit être stable — c'est la clé de matching.", "BC-COL-001"),
        ("Nom",             "✅ Oui",       "Nom brut du produit, sans optimisation SEO — l'IA s'en charge.", "Collier Étoile Dorée M"),
        ("Marque",          "✅ Oui",       "Nom exact de la marque, tel qu'il apparaît sur Amazon.", "Bande de Canailles"),
        ("Segment",         "✅ Oui",       "Famille produit. Tous les produits du même segment partagent les mêmes mots-clés si Mots_cles est vide.", "collier chien"),
        ("EAN",             "Recommandé",  "Code-barres GTIN-13. Obligatoire pour créer une fiche Amazon.", "3760123456789"),
        ("Prix",            "Recommandé",  "Prix de vente TTC en euros. Décimales avec point ou virgule.", "24.90"),
        ("Image_Fichier",   "📸 Clé images","Nom exact du fichier photo dans votre ZIP (sans chemin). "
                                           "Uploadez le ZIP via 'Uploader photos produits'. "
                                           "L'IA analyse la photo pour extraire couleurs, matières et détails — "
                                           "inutile de remplir ces champs à la main. "
                                           "Gardez les noms d'origine, aucun renommage nécessaire.", "laisse-leopard.jpg"),
        ("Poids_kg",        "Optionnel",   "Poids en kg. Aide l'IA à rédiger les bullet points techniques.", "0.085"),
        ("Dimensions_cm",   "Optionnel",   "Format L×l×h ou L×l en cm.", "35x2 cm"),
        ("Caractéristiques","Optionnel",   "Points clés du produit, séparés par |. Si vide, l'IA les déduit de la photo et du nom.", "Mousqueton inox|Poignée rembourrée|Motif léopard"),
        ("Mots_cles",       "Optionnel",   "Mots-clés SEO spécifiques à CE produit, séparés par virgule. Écrasent les mots-clés globaux de l'interface pour ce produit.", "laisse chien design, laisse fantaisie"),
        ("Parent_SKU",      "Déclinaison", "SKU du produit parent Amazon. Remplir uniquement pour les déclinaisons (plusieurs lignes du même produit avec des couleurs/tailles différentes). Toutes les lignes ayant le même Parent_SKU sont regroupées et génèrent UNE fiche parent + autant de fiches enfants.", "BC-LAI-012"),
        ("Variation_Theme", "Déclinaison", "Type de variation Amazon. Valeurs courantes : ColorName, SizeClass, ColorName-SizeClass. Doit être identique pour toutes les déclinaisons du même groupe.", "ColorName-SizeClass"),
        ("Variation_Value", "Déclinaison", "Valeur de la déclinaison pour cette ligne. Format libre, ex: 'Rouge / L', 'Bleu / M', 'Noir'. Apparaît dans la table des déclinaisons.", "Rouge / S"),
        ("Taille",          "Déclinaison", "Taille spécifique de cette déclinaison (S, M, L, XL, 30, 40…). Renseigné si la variation porte sur la taille.", "S"),
    ]

    for r, row_data in enumerate(guide_rows, 2):
        for col, val in enumerate(row_data, 1):
            cell = guide.cell(row=r, column=col, value=val)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if r % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F9FAFB")
            # Mettre en évidence la ligne Image_Fichier
            if row_data[0] == "Image_Fichier":
                cell.fill = PatternFill("solid", fgColor="CCFBF1")
                cell.font = Font(color="0F4C40", bold=(col == 1))
        guide.row_dimensions[r].height = 50 if row_data[0] == "Image_Fichier" else 36

    guide.row_dimensions[1].height = 24

    # ── Légende ──────────────────────────────────────────────────────────────
    legend_row = len(guide_rows) + 3
    guide.cell(row=legend_row,   column=1, value="* Colonnes violettes foncées = obligatoires").font = Font(bold=True, color="764BA2")
    guide.cell(row=legend_row+1, column=1, value="* Colonne verte = photo de référence (recommandée fortement)").font = Font(bold=True, color="0F766E")
    guide.cell(row=legend_row+2, column=1, value="* Colonnes violettes claires = optionnelles").font = Font(italic=True, color="9333EA")
    guide.cell(row=legend_row+3, column=1, value="* Colonnes bleues = déclinaisons (variations Amazon)").font = Font(bold=True, color="1D4ED8")
    guide.cell(row=legend_row+4, column=1,
        value="Conseil : fournissez toujours la photo via Image_Fichier + ZIP. "
              "L'IA analyse la photo et extrait automatiquement couleurs, matières et détails. "
              "Moins vous remplissez de texte, plus la photo prime.").font = Font(italic=True, color="6B7280")

    # ── Export ────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=synqio_template.xlsx"},
    )


# ── Photo store (R2 or local disk) ────────────────────────────────────────────

from app.db import _PROJECT_ROOT

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_IMAGE_CONTENT_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}


def _save_photo(filename: str, data: bytes, content_type: str = "application/octet-stream"):
    storage.put(filename, data, content_type)


def _load_photo(filename: str) -> bytes | None:
    return storage.get(filename)


_ZIP_MAX_COMPRESSED   = 100 * 1024 * 1024   # 100 MB compressed
_ZIP_MAX_UNCOMPRESSED = 500 * 1024 * 1024   # 500 MB total decompressed
_ZIP_MAX_SINGLE_FILE  =  50 * 1024 * 1024   # 50 MB per image


@app.post("/api/upload-photos-zip")
async def upload_photos_zip(request: Request):
    import zipfile

    content = await request.body()
    if not content:
        raise HTTPException(400, "Fichier vide")
    if len(content) > _ZIP_MAX_COMPRESSED:
        raise HTTPException(413, "ZIP trop volumineux (max 100 Mo)")

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except Exception:
        raise HTTPException(400, "Fichier ZIP invalide")

    # Zip-bomb guard: check total decompressed size before extracting anything
    total_uncompressed = sum(i.file_size for i in zf.infolist())
    if total_uncompressed > _ZIP_MAX_UNCOMPRESSED:
        raise HTTPException(413, f"Contenu ZIP trop volumineux décompressé (max 500 Mo)")

    matched = []
    skipped = []

    for entry in zf.namelist():
        # Ignorer dossiers et métadonnées macOS
        if entry.endswith("/") or "__MACOSX" in entry:
            continue
        basename = entry.split("/")[-1]
        # Ignorer fichiers cachés (ex: .DS_Store) mais pas les entrées "./photo.jpg"
        if not basename or basename.startswith("."):
            continue
        stem, ext = os.path.splitext(basename)
        if ext.lower() not in _IMAGE_EXTS:
            continue
        info = zf.getinfo(entry)
        if info.file_size > _ZIP_MAX_SINGLE_FILE:
            skipped.append(basename)
            continue
        sku = stem
        filename = f"{sku}{ext.lower()}"
        data = zf.read(entry)
        _save_photo(filename, data)
        matched.append({"sku": sku, "url": f"/api/photos/{filename}", "filename": filename})

    return {"matched": matched, "skipped": skipped, "count": len(matched)}


@app.get("/api/photos/list")
async def list_photos():
    from app.services.storage import USE_R2, LOCAL_DIR
    files = storage.list_keys()
    image_exts = set(_IMAGE_EXTS)
    image_files = [f for f in files if os.path.splitext(f)[1].lower() in image_exts]
    return {
        "backend": "r2" if USE_R2 else "disk",
        "location": f"r2://{os.getenv('R2_BUCKET_NAME','')}" if USE_R2 else LOCAL_DIR,
        "count": len(image_files),
        "files": sorted(image_files),
    }


@app.get("/api/photos/{filename}")
async def serve_photo(filename: str):
    # When R2 public access is enabled, redirect directly to CDN (no server bandwidth)
    cdn = storage.public_url(filename)
    if cdn:
        return Response(status_code=302, headers={"Location": cdn})
    photo = _load_photo(filename)
    if not photo:
        raise HTTPException(404, "Photo non trouvée")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return Response(content=photo, media_type=_IMAGE_CONTENT_TYPES.get(ext, "image/jpeg"))


# ── URL Scraping ──────────────────────────────────────────────────────────────

@app.post("/api/scrape-url")
async def scrape_url_endpoint(request: Request):
    """Fetch a product URL and extract name, brand, price, EAN, images, etc."""
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide — elle doit commencer par http:// ou https://")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0,
            headers={"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(504, "Le site met trop de temps à répondre (délai 15s dépassé)")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Le site a renvoyé une erreur {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"Impossible d'accéder à cette URL : {e}")

    from app.utils.url_scraper import scrape_product
    try:
        data = scrape_product(resp.text, url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return data


# ── Ingestion ─────────────────────────────────────────────────────────────────

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


class FilePreviewRequest(BaseModel):
    filename: str
    content_b64: str


class FileIngestRequest(BaseModel):
    filename: str
    content_b64: str
    custom_mapping: Optional[dict] = None


def _decode_upload(filename: str, content_b64: str) -> bytes:
    try:
        content = base64.b64decode(content_b64)
    except Exception:
        raise HTTPException(400, "Contenu base64 invalide")
    if not content:
        raise HTTPException(400, "Fichier vide")
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "Fichier trop volumineux (max 10 Mo)")
    return content


@app.post("/api/ingest/preview")
async def ingest_preview(payload: FilePreviewRequest):
    """Return headers, sample rows and auto-mapping info without full parsing."""
    content = _decode_upload(payload.filename, payload.content_b64)
    try:
        result = get_headers_and_sample(payload.filename or "upload.csv", content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erreur de prévisualisation: {e}")
    return result


@app.post("/api/ingest", response_model=List[RawProduct])
async def ingest_file(payload: FileIngestRequest):
    content = _decode_upload(payload.filename, payload.content_b64)
    try:
        products = parse_file(payload.filename or "upload.csv", content, custom_mapping=payload.custom_mapping)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erreur de parsing: {e}")
    if not products:
        raise HTTPException(422, "Aucun produit trouvé dans le fichier")
    return products


# ── Async job store ───────────────────────────────────────────────────────────

_jobs: dict = {}  # job_id → {status, progress, total, result, error, created_at}


def _cleanup_jobs():
    cutoff = time.time() - 3600
    for k in [k for k, v in _jobs.items() if (v.get("created_at") or 0) < cutoff]:
        del _jobs[k]


async def _run_generation_job(job_id: str, request: GenerationRequest, email: str):
    _jobs[job_id]["status"] = "running"
    log.info("job started", extra={"job_id": job_id})
    target_mkts = request.marketplaces or [request.marketplace]

    # Group products by parent_sku to detect variation groups
    groups = group_by_parent(request.products)

    # Build list of products to send to AI (one per group: synthetic parent or standalone)
    products_for_ai = []
    variation_groups: dict = {}  # parent_sku → List[RawProduct] (only for variation groups)
    for parent_sku, group_children in groups.items():
        is_variation = len(group_children) > 1 or group_children[0].parent_sku
        if is_variation:
            parent_product = build_parent_product(parent_sku, group_children)
            products_for_ai.append(parent_product)
            variation_groups[parent_sku] = group_children
        else:
            products_for_ai.append(group_children[0])

    n_total = len(products_for_ai) * len(target_mkts)
    done_count = 0

    def on_progress(done: int, _total: int):
        nonlocal done_count
        done_count += 1
        if job_id in _jobs:
            _jobs[job_id]["progress"] = done_count

    try:
        tasks = [
            generate_listings_batch(
                products=products_for_ai,
                marketplace=mkt,
                focus_keywords=request.focus_keywords or [],
                style_tone=request.style_tone,
                brand_voice=request.brand_voice,
                on_progress=on_progress,
            )
            for mkt in target_mkts
        ]
        results_all = await asyncio.gather(*tasks)

        all_listings, all_failed, total_in, total_out = [], [], 0, 0
        for listings, failed, tokens in results_all:
            # Expand variation groups into parent + child listings
            expanded = []
            for listing in listings:
                if listing.sku in variation_groups:
                    parent_l, child_ls = expand_to_variation_listings(listing, variation_groups[listing.sku])
                    expanded.append(parent_l)
                    expanded.extend(child_ls)
                else:
                    expanded.append(listing)
            all_listings.extend(expanded)
            all_failed.extend(failed)
            total_in  += tokens.get("input_tokens", 0)
            total_out += tokens.get("output_tokens", 0)

        # Count SKUs (children count individually)
        sku_count = sum(1 + len(l.children) if l.is_parent else 1 for l in all_listings)
        if sku_count and email:
            log_usage(email, "sku_generated", sku_count)
        if email:
            if total_in:  log_usage(email, "tokens_in",  total_in)
            if total_out: log_usage(email, "tokens_out", total_out)

        result = GenerationResult(
            listings=all_listings, failed=all_failed,
            total=n_total, success_count=len(all_listings),
            marketplace=target_mkts[0],
        )
        result_dict = result.model_dump()
        _jobs[job_id].update({"status": "done", "progress": n_total,
                               "result": result_dict})
        log.info("job done", extra={"job_id": job_id, "user": email})
        try:
            update_job_db(job_id, status="done", progress=n_total,
                          result_json=json.dumps(result_dict, default=str))
        except Exception:
            pass
        if email:
            try:
                save_generation(
                    user_email=email,
                    batch_id=job_id,
                    marketplace=str(target_mkts[0].value),
                    listings=result_dict.get("listings", []),
                )
            except Exception:
                pass
    except Exception as e:
        _jobs[job_id].update({"status": "failed", "error": str(e)})
        log.error("job failed", extra={"job_id": job_id, "user": email})
        try:
            update_job_db(job_id, status="failed", error=str(e))
        except Exception:
            pass


# ── Generation ────────────────────────────────────────────────────────────────

@app.post("/api/generate")
async def generate(request: GenerationRequest, req: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")
    if not request.products:
        raise HTTPException(400, "Liste de produits vide")

    email = getattr(req.state, "user_email", None)
    if email:
        usage = get_user_usage(email)
        remaining = usage["skus_quota"] - usage["skus_used"]
        if remaining <= 0:
            raise HTTPException(429,
                f"Quota atteint — plan {usage['plan_label']} : {usage['skus_quota']} SKU/mois. "
                "Contactez-nous pour passer à l'offre supérieure.")
        if len(request.products) > remaining:
            raise HTTPException(429,
                f"Quota insuffisant — il vous reste {remaining} SKU ce mois "
                f"(plan {usage['plan_label']} : {usage['skus_quota']}/mois). "
                f"Vous demandez {len(request.products)} SKU.")
        if not _rate_limit(f"{email}:generate", limit=20, window=3600):
            raise HTTPException(429, "Trop de générations — maximum 20/heure. Réessayez dans quelques minutes.")

    n_mkts = len(request.marketplaces) if request.marketplaces else 1
    groups = group_by_parent(request.products)
    n_total = len(groups) * n_mkts
    job_id = str(uuid4())
    _jobs[job_id] = {"status": "pending", "progress": 0,
                     "total": n_total, "created_at": time.time()}
    _cleanup_jobs()
    try:
        save_job(job_id, email, "generation", n_total)
    except Exception:
        pass
    asyncio.create_task(_run_generation_job(job_id, request, email))
    return {"job_id": job_id, "total": n_total}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        try:
            db_jobs = load_recent_jobs(limit=500)
            job = db_jobs.get(job_id)
        except Exception:
            pass
    if not job:
        raise HTTPException(404, "Job introuvable ou expiré (1h max)")
    return job


# ── Publish ───────────────────────────────────────────────────────────────────

@app.post("/api/publish", response_model=PublishResult)
async def publish(request_data: PublishRequest, request: Request):
    if not request_data.listings:
        raise HTTPException(400, "Aucune fiche à publier")
    return await publish_listings(
        listings=request_data.listings,
        marketplace=request_data.marketplace,
        dry_run=request_data.dry_run,
        user_email=getattr(request.state, "user_email", None),
    )


from app.utils.amazon_template_filler import fill_amazon_template

# ── Export ────────────────────────────────────────────────────────────────────

@app.post("/api/export/csv")
async def export_csv(listings: List[AmazonListing]):
    return Response(content=to_csv_bytes(listings), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=listings.csv"})


@app.post("/api/export/json")
async def export_json_file(listings: List[AmazonListing]):
    return Response(content=to_json_bytes(listings), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=listings.json"})


@app.post("/api/export/fill-amazon-template")
async def fill_amazon_template_endpoint(request: Request):
    """Accept multipart: amazon_template (file) + listings (JSON string)."""
    form = await request.form()
    template_file = form.get("amazon_template")
    listings_json = form.get("listings")
    if not template_file or not listings_json:
        raise HTTPException(400, "Fichier template et listings requis")
    template_bytes = await template_file.read()
    import json as _json
    raw = _json.loads(listings_json)
    listings = [AmazonListing(**item) for item in raw]
    try:
        filled_bytes = fill_amazon_template(template_bytes, listings)
    except Exception as e:
        raise HTTPException(422, f"Erreur lors du remplissage : {e}")
    return Response(
        content=filled_bytes,
        media_type="application/vnd.ms-excel.sheet.macroEnabled.12",
        headers={"Content-Disposition": "attachment; filename=amazon_template_rempli.xlsm"},
    )


@app.post("/api/export/flat-file")
async def export_flat_file(listings: List[AmazonListing]):
    return Response(
        content=to_amazon_flat_file_xlsx(listings),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=amazon_nouveaux_produits.xlsx"},
    )


@app.post("/api/export/listing-loader")
async def export_listing_loader(listings: List[AmazonListing]):
    return Response(
        content=to_listing_loader_xlsx(listings),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=amazon_produits_existants.xlsx"},
    )


@app.post("/api/export/variation-flat-file")
async def export_variation_flat_file(listings: List[AmazonListing]):
    return Response(
        content=to_variation_flat_file_xlsx(listings),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=amazon_variations.xlsx"},
    )


# ── Images ───────────────────────────────────────────────────────────────────

class ImageRequest(BaseModel):
    sku: str
    product_name: str
    brand: str = ""
    category: str = ""
    features: List[str] = []
    color: str = ""
    material: str = ""
    selected_types: Optional[List[str]] = None
    reference_image_url: Optional[str] = None
    batch_id: Optional[str] = None   # generation history id to attach images to
    marketplace: Optional[str] = None


async def _persist_images(sku: str, images: list) -> list:
    """Download temp DALL-E URLs / decode base64 → save to storage permanently.
    Returns the same list with urls replaced by /api/photos/... paths."""
    import base64 as _b64
    for img in images:
        url = img.get("url") or ""
        if not url or not img.get("has_image"):
            continue
        ext = "png"
        filename = f"gen_{sku}_{img.get('id','img')}.{ext}"
        if storage.exists(filename):
            img["url"] = f"/api/photos/{filename}"
            continue
        try:
            if url.startswith("data:image"):
                data_part = url.split(",", 1)[1]
                _save_photo(filename, _b64.b64decode(data_part))
                img["url"] = f"/api/photos/{filename}"
            elif url.startswith("http"):
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        _save_photo(filename, resp.content)
                        img["url"] = f"/api/photos/{filename}"
        except Exception:
            pass  # keep original url as fallback
    return images


async def _run_image_job(job_id: str, req: ImageRequest, email: str):
    _jobs[job_id]["status"] = "running"
    log.info("job started", extra={"job_id": job_id})
    try:
        # Résoudre la photo de référence :
        # - /api/photos/{nom} → bytes depuis _temp_photos (ZIP uploadé, zéro HTTP)
        # - URL externe → téléchargement dans image_gen
        ref_url = req.reference_image_url or None
        ref_bytes = None
        if ref_url and ref_url.startswith("/api/photos/"):
            fname = ref_url[len("/api/photos/"):]
            ref_bytes = _load_photo(fname)
            ref_url = None  # on a les bytes, inutile de télécharger

        images, img_tokens = await generate_product_images(
            sku=req.sku,
            product_name=req.product_name,
            brand=req.brand,
            category=req.category,
            features=req.features,
            color=req.color,
            material=req.material,
            selected_types=req.selected_types,
            reference_image_url=ref_url,
            reference_image_bytes=ref_bytes,
            marketplace=req.marketplace,
        )
        generated = [i for i in images if i.get("has_image")]
        if generated and email:
            log_usage(email, "image_generated", len(generated))
        if email and img_tokens:
            if img_tokens.get("input_tokens"):
                log_usage(email, "tokens_in", img_tokens["input_tokens"])
            if img_tokens.get("output_tokens"):
                log_usage(email, "tokens_out", img_tokens["output_tokens"])

        # Persist images to disk so URLs never expire
        images = await _persist_images(req.sku, images)

        # Attach images to generation history if batch_id provided
        if req.batch_id and email:
            try:
                from app.db import save_generation_images
                save_generation_images(req.batch_id, email, req.sku, images)
            except Exception:
                pass

        openai_ok = bool(os.getenv("OPENAI_API_KEY"))
        result_img = {
            "sku": req.sku,
            "images": images,
            "images_generated": openai_ok,
            "openai_configured": openai_ok,
            "total": len(images),
            "reference_image_used": img_tokens.get("reference_image_used", False),
        }
        _jobs[job_id].update({"status": "done", "result": result_img})
        log.info("job done", extra={"job_id": job_id, "user": email})
        try:
            update_job_db(job_id, status="done", result_json=json.dumps(result_img, default=str))
        except Exception:
            pass
    except Exception as e:
        _jobs[job_id].update({"status": "failed", "error": str(e)})
        log.error("job failed", extra={"job_id": job_id, "user": email})
        try:
            update_job_db(job_id, status="failed", error=str(e))
        except Exception:
            pass


@app.post("/api/generate-images")
async def generate_images(req: ImageRequest, request: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")
    email = getattr(request.state, "user_email", None)
    if email:
        usage = get_user_usage(email)
        imgs_remaining = max(0, usage.get("images_quota", 200) - usage.get("images_used", 0))
        if imgs_remaining <= 0:
            raise HTTPException(429,
                f"Quota d'images atteint ce mois (plan {usage['plan_label']}). "
                "Contactez-nous pour augmenter votre quota.")
        if not _rate_limit(f"{email}:images", limit=10, window=3600):
            raise HTTPException(429, "Trop de générations d'images — maximum 10/heure.")
    job_id = str(uuid4())
    _jobs[job_id] = {"status": "pending", "progress": 0, "total": 7, "created_at": time.time()}
    _cleanup_jobs()
    try:
        save_job(job_id, email, "images", 7)
    except Exception:
        pass
    asyncio.create_task(_run_image_job(job_id, req, email))
    return {"job_id": job_id}


class SingleImageRequest(BaseModel):
    sku: str
    image_id: str
    prompt: str


async def _run_single_image_job(job_id: str, req: SingleImageRequest, email: str):
    _jobs[job_id]["status"] = "running"
    log.info("job started", extra={"job_id": job_id})
    try:
        url = await _generate_image_dalle3(req.prompt, req.image_id)
        openai_ok = bool(os.getenv("OPENAI_API_KEY"))
        if url and email:
            log_usage(email, "image_generated", 1)
        # Persist to disk
        if url:
            persisted = await _persist_images(req.sku, [{"id": req.image_id, "url": url, "has_image": True}])
            url = persisted[0]["url"] if persisted else url
        result_single = {
            "sku": req.sku,
            "image_id": req.image_id,
            "url": url,
            "prompt": req.prompt,
            "openai_configured": openai_ok,
        }
        _jobs[job_id].update({"status": "done", "result": result_single})
        log.info("job done", extra={"job_id": job_id, "user": email})
        try:
            update_job_db(job_id, status="done", result_json=json.dumps(result_single, default=str))
        except Exception:
            pass
    except Exception as e:
        _jobs[job_id].update({"status": "failed", "error": str(e)})
        log.error("job failed", extra={"job_id": job_id, "user": email})
        try:
            update_job_db(job_id, status="failed", error=str(e))
        except Exception:
            pass


@app.post("/api/generate-image-single")
async def generate_image_single(req: SingleImageRequest, request: Request):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "OPENAI_API_KEY manquante — impossible de régénérer l'image")
    email = getattr(request.state, "user_email", None)
    if email and not _rate_limit(f"{email}:images", limit=10, window=3600):
        raise HTTPException(429, "Trop de générations d'images — maximum 10/heure.")
    job_id = str(uuid4())
    _jobs[job_id] = {"status": "pending", "progress": 0, "total": 1, "created_at": time.time()}
    _cleanup_jobs()
    try:
        save_job(job_id, email, "single_image", 1)
    except Exception:
        pass
    asyncio.create_task(_run_single_image_job(job_id, req, email))
    return {"job_id": job_id}


# ── Usage ─────────────────────────────────────────────────────────────────────

@app.get("/api/usage/me")
async def usage_me(request: Request):
    email = getattr(request.state, "user_email", None)
    if not email:
        raise HTTPException(401, "Non authentifié")
    return get_user_usage(email)


@app.get("/api/admin/usage")
async def usage_all(request: Request):
    from app.services.auth import is_admin, _decode_token_data
    email = getattr(request.state, "user_email", None)
    if not email:
        raise HTTPException(403, "Accès réservé aux administrateurs")
    # Accept JWT adm claim (survives DB wipes) or DB flag
    auth = request.headers.get("Authorization", "")
    jwt_data = _decode_token_data(auth.split(" ", 1)[1]) if auth.startswith("Bearer ") else {}
    if not (bool((jwt_data or {}).get("adm")) or is_admin(email)):
        raise HTTPException(403, "Accès réservé aux administrateurs")
    return get_all_users_usage()


@app.get("/api/image-types")
async def get_image_types():
    return AMAZON_IMAGE_TYPES


# ── Marketplaces ──────────────────────────────────────────────────────────────

# ── Generation history ────────────────────────────────────────────────────────

@app.get("/api/history")
async def get_history(request: Request):
    email = request.state.user_email
    return list_generations(email)


@app.get("/api/history/{batch_id}")
async def get_history_batch(batch_id: str, request: Request):
    email = request.state.user_email
    batch = get_generation(batch_id, email)
    if not batch:
        raise HTTPException(status_code=404, detail="Lot introuvable")
    return batch


@app.delete("/api/history/{batch_id}")
async def delete_history_batch(batch_id: str, request: Request):
    email = request.state.user_email
    if not delete_generation(batch_id, email):
        raise HTTPException(status_code=404, detail="Lot introuvable")
    return {"ok": True}


class LabelUpdate(BaseModel):
    label: str


@app.patch("/api/history/{batch_id}/label")
async def update_history_label(batch_id: str, body: LabelUpdate, request: Request):
    email = request.state.user_email
    if not update_generation_label(batch_id, email, body.label):
        raise HTTPException(status_code=404, detail="Lot introuvable")
    return {"ok": True}


@app.get("/api/marketplaces")
async def list_marketplaces():
    return [
        {"id": "amazon_fr",  "name": "Amazon France",    "flag": "🇫🇷"},
        {"id": "amazon_de",  "name": "Amazon Allemagne",  "flag": "🇩🇪"},
        {"id": "amazon_it",  "name": "Amazon Italie",     "flag": "🇮🇹"},
        {"id": "amazon_es",  "name": "Amazon Espagne",    "flag": "🇪🇸"},
        {"id": "amazon_uk",  "name": "Amazon UK",         "flag": "🇬🇧"},
        {"id": "amazon_nl",  "name": "Amazon Pays-Bas",   "flag": "🇳🇱"},
        {"id": "amazon_se",  "name": "Amazon Suède",      "flag": "🇸🇪"},
        {"id": "amazon_pl",  "name": "Amazon Pologne",    "flag": "🇵🇱"},
        {"id": "amazon_be",  "name": "Amazon Belgique",   "flag": "🇧🇪"},
    ]


# ── Help chatbot ──────────────────────────────────────────────────────────────

_HELP_SYSTEM = """Tu es l'assistant d'aide de SynqIO, une application de création de fiches produits Amazon optimisées.

Tu réponds UNIQUEMENT sur deux sujets :
1. Comment utiliser l'interface SynqIO (boutons, étapes, workflow, fonctionnalités)
2. Les règles et bonnes pratiques Amazon pour bien vendre sur Seller Central

Tu ne réponds PAS sur : la technique, le code, les APIs, l'infrastructure, le prix des plans ou toute autre question hors-sujet. Si quelqu'un te pose une question hors-sujet, redirige poliment vers ces deux domaines.
Tu ne mentionnes jamais de technologie sous-jacente.
Tu réponds toujours en français, de façon claire, concise et bienveillante. Utilise des listes à puces quand c'est utile.

=== GUIDE SYNQIO ===
Workflow en 3 étapes :
1. Importer ses produits — glisser-déposer un fichier Excel/CSV (colonnes : SKU, Titre, Description, EAN, URL image) ou décrire ses produits via le Chat IA
2. Générer les fiches — cliquer sur « Générer » pour obtenir titre optimisé, 5 bullet points, description A+, mots-clés backend
3. Exporter et mettre en ligne — bouton « Exporter Excel » → déposer le fichier dans Seller Central via Catalogue > Ajouter des produits via importation

Fonctionnalités clés :
- Score SEO (0–100) : évalue la qualité de chaque fiche selon les critères Amazon
- Génération de visuels : 7 images IA par produit (hero fond blanc, lifestyle, infographie, dimensions, packaging)
- Bouton « Générer toutes les photos » : lance toutes les générations en arrière-plan, vous pouvez continuer à travailler
- Historique : retrouvez vos générations précédentes depuis l'icône horloge
- Multi-sessions : chaque nouvelle génération crée un onglet séparé, accessible via les chips en haut des résultats
- Suppression d'une fiche : bouton ✕ sur la carte produit
- Chat IA : décrivez vos produits en langage naturel, l'assistant extrait les informations et lance la génération

=== RÈGLES AMAZON ===
Titre :
- Max 200 caractères (idéal 150–180)
- Structure : Marque + Produit + Caractéristique principale + Matière/Couleur + Taille/Quantité
- Interdit : prix, promotions (« soldes », « promo »), livraison gratuite, majuscules excessives, superlatifs (« meilleur », « n°1 »)

Bullet points :
- Exactement 5 bullets, max 500 caractères chacun
- Commencer par une majuscule, pas de point final
- Mettre les bénéfices consommateur en premier, les caractéristiques techniques ensuite
- Éviter la répétition avec le titre

Description :
- Max 2 000 caractères
- Peut contenir du HTML basique : <b>, <br>, <ul><li>
- Développer les usages, le contexte d'utilisation, les arguments de réassurance

Images :
- Image principale (hero) : fond blanc obligatoire, pas de texte ni logo, produit occupe 85 % du cadre
- Format : JPEG ou PNG, min 1 000×1 000 px, recommandé 2 000×2 000 px
- Max 7 images au total par fiche
- Images secondaires : lifestyle, infographies, comparatifs autorisés

Mots-clés backend :
- Max 249 bytes (pas caractères) au total
- Séparés par des espaces (pas de virgules)
- Ne pas répéter les mots déjà présents dans le titre ou les bullets
- Inclure variantes orthographiques, synonymes, usages alternatifs

Catégories :
- Choisir la plus précise possible (sous-catégorie > catégorie générique)
- Une mauvaise catégorie = moins de visibilité dans les recherches
- Vérifier les attributs obligatoires de la catégorie dans le flat file

Flat file (fichier d'importation) :
- Télécharger le template depuis Seller Central : Catalogue > Télécharger une feuille de calcul
- Sélectionner la bonne catégorie avant de télécharger le template
- SynqIO exporte directement au bon format : Exporter > Excel
- Importer via : Seller Central > Catalogue > Ajouter des produits via importation > Charger un fichier

EAN / GTIN :
- Obligatoire dans la plupart des catégories
- Exemption GTIN possible mais déconseillée (moins de visibilité)
- L'EAN doit correspondre au GS1 officiel du produit
"""

class HelpMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class HelpChatRequest(BaseModel):
    messages: list[HelpMessage]

@app.post("/api/help-chat")
async def help_chat(req_body: HelpChatRequest, request: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "Assistant indisponible")
    from app.services.ai_agent import get_client
    messages = [{"role": m.role, "content": m.content} for m in req_body.messages[-12:]]
    resp = await get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=_HELP_SYSTEM,
        messages=messages,
    )
    return {"reply": resp.content[0].text}


# ── Audit ──────────────────────────────────────────────────────────────────────

_AUDIT_SYSTEM = """Tu es un expert Amazon SEO. Analyse la fiche produit fournie et retourne UNIQUEMENT un objet JSON valide, sans texte avant ni après, sans bloc markdown.

Structure JSON exacte :
{
  "global_score": <entier 0-100>,
  "title":    {"score": <0-100>, "issues": [<max 3 strings courtes>], "suggestions": [<max 3 strings courtes>]},
  "bullets":  {"score": <0-100>, "issues": [<max 3 strings courtes>], "suggestions": [<max 3 strings courtes>]},
  "keywords": {"score": <0-100>, "issues": [<max 3 strings courtes>], "suggestions": [<max 3 strings courtes>]},
  "images":   {"score": <0-100>, "issues": [<max 2 strings courtes>], "suggestions": [<max 2 strings courtes>]},
  "summary": "<2 phrases max>"
}

Critères de scoring :

TITRE (poids 30%) :
- Longueur 150-180 chars → 40 pts ; 100-149 ou 181-200 → 25 pts ; <100 ou >200 → 10 pts
- Mots-clés principaux dans les 80 premiers chars → 30 pts
- Pas de majuscules abusives, caractères interdits (!?$¡¿), répétitions → 30 pts

BULLETS (poids 30%) :
- 5 bullets → 25 pts ; 4 → 15 pts ; <4 → 5 pts
- Longueur moyenne >150 chars → 25 pts ; 80-150 → 15 pts ; <80 → 5 pts
- Bénéfice client (pas juste technique) dans ≥3 bullets → 25 pts
- Mots-clés secondaires variés → 25 pts

KEYWORDS BACKEND (poids 20%) :
- Vide → 0
- ≤249 bytes → 40 pts
- Pas de répétition avec le titre → 30 pts
- Diversité et pertinence → 30 pts

IMAGES (poids 20%) :
- 7+ → 100 pts ; 5-6 → 70 pts ; 3-4 → 40 pts ; 1-2 → 20 pts ; 0 → 0 pts

GLOBAL : titre×0.3 + bullets×0.3 + keywords×0.2 + images×0.2 (si keywords vide : titre×0.375 + bullets×0.375 + images×0.25).
Chaque string de issues/suggestions : <80 chars, actionnable, en français."""

_IMPROVE_SYSTEM = """Tu es un expert Amazon SEO. Améliore le champ demandé d'une fiche Amazon.
Retourne UNIQUEMENT un objet JSON valide, sans texte avant ni après :
- Pour "title"    → {"improved": "<titre optimisé>", "explanation": "<1 phrase>"}
- Pour "bullets"  → {"improved": ["bullet 1","bullet 2","bullet 3","bullet 4","bullet 5"], "explanation": "<1 phrase>"}
- Pour "keywords" → {"improved": "<keywords ≤249 bytes, séparés par espaces>", "explanation": "<1 phrase>"}

Règles :
- Titre : 150-180 chars, mots-clés en tête, Marque + Produit + 2-3 caractéristiques clés
- Bullets : 5 bullets, 150-200 chars chacun, commencer par un bénéfice client, pas de point final
- Keywords : ≤249 bytes UTF-8, pas de doublons avec le titre, pluriels/synonymes/longue traîne"""


def _scrape_amazon_for_audit(html_text: str) -> dict:
    """Extract title, bullets and image URLs from Amazon product page static HTML."""
    import re as _re, html as _html
    def _clean(t): return _re.sub(r'<[^>]+>', '', _html.unescape(t or '')).strip()

    result: dict = {"title": "", "bullets": [], "image_urls": []}

    # ── Title ──────────────────────────────────────────────────────────────────
    # Primary: id="productTitle"
    m = _re.search(r'<span[^>]+id=["\']productTitle["\'][^>]*>(.*?)</span>', html_text, _re.DOTALL | _re.IGNORECASE)
    if m:
        result["title"] = _clean(m.group(1))

    # Fallback: og:title
    if not result["title"]:
        for pat in (
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        ):
            mm = _re.search(pat, html_text, _re.IGNORECASE)
            if mm:
                result["title"] = _html.unescape(mm.group(1).strip())
                break

    # ── Bullets ────────────────────────────────────────────────────────────────
    # Strategy: find the feature-bullets section first, then extract list items
    feature_section = ""
    fs = _re.search(
        r'id=["\']feature-bullets["\'][^>]*>(.*?)(?:id=["\'](?:productDescription|aplus)["\']|</div>\s*</div>\s*</div>)',
        html_text, _re.DOTALL | _re.IGNORECASE,
    )
    if fs:
        feature_section = fs.group(1)

    search_area = feature_section or html_text
    bullet_matches = _re.findall(
        r'<span[^>]+class=["\'][^"\']*a-list-item[^"\']*["\'][^>]*>(.*?)</span>',
        search_area, _re.DOTALL | _re.IGNORECASE,
    )
    seen_b: set = set()
    for raw in bullet_matches:
        b = _clean(raw)
        # Skip very short strings or strings that look like navigation / UI labels
        if len(b) > 20 and b not in seen_b and not b.startswith(("Voir ", "En savoir", "See more", "Read more")):
            seen_b.add(b)
            result["bullets"].append(b)
        if len(result["bullets"]) >= 5:
            break

    # ── Images ─────────────────────────────────────────────────────────────────
    # Primary: colorImages JS object (hiRes > large > main)
    for key in ("hiRes", "large", "main"):
        img_urls = _re.findall(rf'"{key}"\s*:\s*"(https://[^"]+\.(?:jpg|jpeg|png)[^"]*)"', html_text, _re.IGNORECASE)
        if img_urls:
            seen_u: set = set()
            for u in img_urls:
                base = _re.sub(r'\._[A-Z0-9_,]+_\.', '.', u)  # strip size suffix
                if base not in seen_u:
                    seen_u.add(base)
                    result["image_urls"].append(base)
                if len(result["image_urls"]) >= 9:
                    break
            break

    # Fallback: data-old-hires attribute
    if not result["image_urls"]:
        for pat in (
            r'data-old-hires=["\']([^"\']+)["\']',
            r'"landingAsinColor"[^}]*?"large"\s*:\s*"(https://[^"]+)"',
        ):
            mm = _re.search(pat, html_text)
            if mm:
                result["image_urls"] = [mm.group(1)]
                break

    return result


class AuditRequest(BaseModel):
    title: str = ""
    bullets: list[str] = []
    backend_keywords: str = ""
    image_count: int = 0
    image_urls: list[str] = []
    marketplace: str = "amazon_fr"


class AuditImproveRequest(BaseModel):
    field: str
    title: str = ""
    bullets: list[str] = []
    backend_keywords: str = ""
    marketplace: str = "amazon_fr"


_AMAZON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


@app.post("/api/audit/prefill")
async def audit_prefill(request: Request):
    """Scrape an Amazon product URL, OR parse raw HTML if html_content is provided."""
    body = await request.json()
    html_content = (body.get("html_content") or "").strip()

    # Mode 2: user pasted raw HTML — parse directly, no HTTP fetch
    if html_content:
        data = _scrape_amazon_for_audit(html_content)
        if not data["title"] and not data["bullets"] and not data["image_urls"]:
            raise HTTPException(422, "Aucune donnée trouvée dans ce HTML. Vérifiez que vous avez bien copié le code source complet (Ctrl+U).")
        return {**data, "source": "html_paste"}

    # Mode 1: fetch from URL
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")

    html_text = None
    last_err = ""
    # Try main URL then mobile variant
    candidates = [url]
    if "amazon." in url:
        mob = url.replace("www.amazon.", "www.amazon.").replace("//amazon.", "//www.amazon.")
        # Try with /dp/ short form if it's a long URL
        import re as _re2
        dp_m = _re2.search(r'/dp/([A-Z0-9]{10})', url)
        if dp_m:
            base = _re2.match(r'(https?://[^/]+)', url)
            if base:
                candidates.insert(0, f"{base.group(1)}/dp/{dp_m.group(1)}")

    for candidate_url in candidates:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=12.0, headers=_AMAZON_HEADERS,
            ) as client:
                resp = await client.get(candidate_url)
                resp.raise_for_status()
                html_text = resp.text
                break
        except httpx.TimeoutException:
            last_err = "Délai dépassé"
        except httpx.HTTPStatusError as e:
            last_err = f"Erreur HTTP {e.response.status_code}"
        except Exception as e:
            last_err = str(e)

    if not html_text:
        raise HTTPException(502, f"Impossible d'accéder à l'URL ({last_err}). Utilisez le fallback 'Coller le source HTML'.")

    # Detect bot-detection / CAPTCHA
    bot_signals = [
        "api.security-challenge.aws.com",
        "Enter the characters you see below",
        "robot or an automated system",
        "captcha",
        "px-captcha",
    ]
    if any(s.lower() in html_text.lower() for s in bot_signals):
        raise HTTPException(
            403,
            "BLOCKED",  # frontend detects this code to show the fallback UI
        )

    data = _scrape_amazon_for_audit(html_text)
    if not data["title"] and not data["bullets"]:
        raise HTTPException(
            422,
            "BLOCKED",  # same fallback — page likely JS-rendered
        )
    return {**data, "source": "url_scrape"}


@app.post("/api/audit")
async def audit_listing(req: AuditRequest, request: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "Service indisponible")
    from app.services.ai_agent import get_client
    import json as _json

    lang = "français" if "fr" in req.marketplace else ("anglais" if "uk" in req.marketplace else "langue locale du marché")
    bullets_text = "\n".join(f"• {b}" for b in req.bullets if b.strip()) or "(aucun bullet)"
    kw_bytes = len(req.backend_keywords.encode("utf-8"))
    # Effective image count: prefer URL list length, fallback to slider
    effective_img_count = len(req.image_urls) if req.image_urls else req.image_count

    text_prompt = f"""Marketplace : {req.marketplace} (langue fiches : {lang})

TITRE ({len(req.title)} chars) :
{req.title or "(vide)"}

BULLETS ({len([b for b in req.bullets if b.strip()])}/5) :
{bullets_text}

KEYWORDS BACKEND ({kw_bytes}/249 bytes) :
{req.backend_keywords or "(vide)"}

IMAGES : {effective_img_count} image(s)"""

    # Build message content — download images as base64 for vision (max 4)
    urls_to_analyze = [u for u in req.image_urls if u.startswith("http")][:4]
    vision_used = False

    if urls_to_analyze:
        img_blocks: list = []
        async with httpx.AsyncClient(timeout=8.0) as img_client:
            for img_url in urls_to_analyze:
                try:
                    ir = await img_client.get(img_url, headers={"Referer": "https://www.amazon.fr/"})
                    if ir.status_code == 200:
                        ct = ir.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                        if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                            ct = "image/jpeg"
                        img_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": ct,
                                "data": base64.b64encode(ir.content).decode(),
                            },
                        })
                except Exception:
                    pass  # skip unreachable images silently

        if img_blocks:
            vision_used = True
            content: list = img_blocks + [{
                "type": "text",
                "text": text_prompt + "\n\nNOTE : Les images ci-dessus sont les vraies images produit Amazon. Analyse-les visuellement pour scorer la dimension IMAGES : fond blanc (image 1), produit bien visible, visuels lifestyle, infographie, angles multiples. Ajuste le score et les issues/suggestions en conséquence.",
            }]
        else:
            content = text_prompt
    else:
        content = text_prompt

    import re as _re_json
    resp = await get_client().messages.create(
        model="claude-haiku-4-5-20251001" if not vision_used else "claude-sonnet-4-6",
        max_tokens=1500,
        system=_AUDIT_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown code fences if Claude wraps its JSON
    raw = _re_json.sub(r'^```(?:json)?\s*', '', raw, flags=_re_json.MULTILINE)
    raw = _re_json.sub(r'\s*```$', '', raw, flags=_re_json.MULTILINE).strip()
    try:
        result = _json.loads(raw)
        result["vision_used"] = vision_used
        return result
    except Exception:
        raise HTTPException(500, "Erreur d'analyse — réessayez")


# ── Performance tracking ──────────────────────────────────────────────────────

class TrackedListingRequest(BaseModel):
    sku: str = ""
    asin: str = ""
    title: str = ""
    marketplace: str = "amazon_fr"
    published_at: str = ""
    seo_score: int = 0


class SnapshotRequest(BaseModel):
    snapshot_date: str
    period_label: str = ""
    sessions: int = 0
    page_views: int = 0
    units_ordered: int = 0
    conversion_rate: float = 0.0
    revenue: float = 0.0
    keyword: str = ""
    keyword_rank: Optional[int] = None
    notes: str = ""


@app.get("/api/tracking")
async def tracking_list(request: Request):
    return list_tracked_listings(request.state.user_email)


@app.get("/api/tracking/summary")
async def tracking_summary(request: Request):
    return get_tracking_summary(request.state.user_email)


@app.post("/api/tracking")
async def tracking_add(req: TrackedListingRequest, request: Request):
    if not req.asin and not req.sku:
        raise HTTPException(400, "ASIN ou SKU requis")
    return add_tracked_listing(
        request.state.user_email, req.sku, req.asin, req.title,
        req.marketplace, req.published_at, req.seo_score,
    )


@app.delete("/api/tracking/{listing_id}")
async def tracking_delete(listing_id: str, request: Request):
    if not delete_tracked_listing(listing_id, request.state.user_email):
        raise HTTPException(404, "Fiche introuvable")
    return {"ok": True}


@app.get("/api/tracking/{listing_id}/snapshots")
async def tracking_snapshots(listing_id: str, request: Request):
    return get_snapshots(listing_id, request.state.user_email)


@app.post("/api/tracking/{listing_id}/snapshots")
async def tracking_add_snapshot(listing_id: str, req: SnapshotRequest, request: Request):
    return add_snapshot(
        listing_id, request.state.user_email, req.snapshot_date, req.period_label,
        req.sessions, req.page_views, req.units_ordered, req.conversion_rate,
        req.revenue, req.keyword, req.keyword_rank, req.notes,
    )


@app.delete("/api/tracking/{listing_id}/snapshots/{snap_id}")
async def tracking_delete_snapshot(listing_id: str, snap_id: int, request: Request):
    if not delete_snapshot(snap_id, request.state.user_email):
        raise HTTPException(404, "Snapshot introuvable")
    return {"ok": True}


@app.post("/api/tracking/{listing_id}/import-csv")
async def tracking_import_csv(listing_id: str, request: Request):
    """Import Amazon Business Report CSV (Detail Page Sales and Traffic By Date)."""
    import csv as _csv, io as _io
    body = await request.json()
    csv_text = body.get("csv_text", "")
    if not csv_text:
        raise HTTPException(400, "CSV vide")
    email = request.state.user_email

    reader = _csv.DictReader(_io.StringIO(csv_text))
    # Normalise header names: lowercase + strip
    def _norm(k): return k.lower().strip().replace("﻿", "")
    rows = [{_norm(k): v.strip() for k, v in row.items()} for row in reader]

    # Column name aliases
    def _pick(row, *keys):
        for k in keys:
            v = row.get(k, "")
            if v and v != "-": return v
        return ""

    imported = 0
    for row in rows:
        date_val = _pick(row, "date", "day", "jour")
        if not date_val:
            continue
        try:
            import datetime as _dt
            # Try common date formats
            for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    d = _dt.datetime.strptime(date_val, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                continue
            snap_date = str(d)
        except Exception:
            continue

        sessions   = int(float(_pick(row, "sessions") or 0))
        page_views = int(float(_pick(row, "page views", "page_views", "vues de page") or 0))
        units      = int(float(_pick(row, "units ordered", "units_ordered", "unités commandées") or 0))
        conv_str   = _pick(row, "unit session percentage", "taux de conversion", "conversion rate") or "0"
        conv       = float(conv_str.replace("%", "").replace(",", ".")) if conv_str else 0.0
        rev_str    = _pick(row, "ordered product sales", "chiffre d'affaires", "revenue") or "0"
        revenue    = float(rev_str.replace("€", "").replace("$", "").replace(",", ".").replace(" ", "")) if rev_str else 0.0

        add_snapshot(listing_id, email, snap_date, "", sessions, page_views,
                     units, conv, revenue, "", None, "Import CSV")
        imported += 1

    return {"imported": imported}


@app.post("/api/audit/improve")
async def audit_improve(req: AuditImproveRequest, request: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "Service indisponible")
    if req.field not in ("title", "bullets", "keywords"):
        raise HTTPException(400, "Champ invalide")
    from app.services.ai_agent import get_client
    import json as _json
    lang = "français" if "fr" in req.marketplace else ("anglais" if "uk" in req.marketplace else "langue locale du marché")
    bullets_text = "\n".join(f"• {b}" for b in req.bullets if b.strip()) or "(aucun)"
    prompt = f"""Marketplace : {req.marketplace} (langue : {lang})
Champ à améliorer : {req.field}

Fiche actuelle :
TITRE : {req.title or "(vide)"}
BULLETS :
{bullets_text}
KEYWORDS : {req.backend_keywords or "(vide)"}"""

    resp = await get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        system=_IMPROVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    import re as _re_imp
    raw = _re_imp.sub(r'^```(?:json)?\s*', '', raw, flags=_re_imp.MULTILINE)
    raw = _re_imp.sub(r'\s*```$', '', raw, flags=_re_imp.MULTILINE).strip()
    try:
        return _json.loads(raw)
    except Exception:
        raise HTTPException(500, "Erreur d'amélioration — réessayez")

# ── Saved sessions ────────────────────────────────────────────────────────────

class SaveSessionRequest(BaseModel):
    name: str
    data: dict


@app.post("/api/sessions/save")
async def api_save_session(req: SaveSessionRequest, request: Request):
    email = request.state.user_email
    session_id = str(uuid.uuid4())
    save_session(session_id, email, req.name.strip() or "Session sans nom", req.data)
    return {"ok": True, "id": session_id}


@app.get("/api/sessions")
async def api_list_sessions(request: Request):
    return list_saved_sessions(request.state.user_email)


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str, request: Request):
    sess = get_saved_session(session_id, request.state.user_email)
    if not sess:
        raise HTTPException(404, "Session introuvable")
    return sess


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str, request: Request):
    if not delete_saved_session(session_id, request.state.user_email):
        raise HTTPException(404, "Session introuvable")
    return {"ok": True}
