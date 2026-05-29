import os
import io
import json
import time
import asyncio
import base64
from uuid import uuid4
from dotenv import load_dotenv
load_dotenv()

from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
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
from app.utils.export import to_csv_bytes, to_json_bytes, to_amazon_flat_file_bytes
from app.db import init_db
from app.routes.auth import router as auth_router, admin_router
from app.routes.amazon_oauth import router as amazon_router

# Routes sans authentification
PUBLIC_PATHS = {"/", "/health", "/api/auth/login", "/api/auth/setup", "/api/auth/needs-setup", "/api/marketplaces", "/api/amazon/callback", "/api/template"}
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


@app.on_event("startup")
async def startup():
    _check_secrets()
    init_db()
    _bootstrap_admin()


def _check_secrets():
    import secrets as _secrets
    jwt = os.getenv("JWT_SECRET", "")
    insecure_default = "change-me-use-a-long-random-string-in-production"
    if not jwt or jwt == insecure_default or len(jwt) < 32:
        # Generate a suggestion and log a loud warning — don't crash so Railway
        # cold-starts still work, but the problem is unmissable in logs.
        suggestion = _secrets.token_hex(32)
        print(
            f"\n{'='*60}\n"
            f"⚠️  SECURITY WARNING: JWT_SECRET is weak or missing.\n"
            f"   Set this env var in Railway:\n"
            f"   JWT_SECRET={suggestion}\n"
            f"{'='*60}\n",
            flush=True,
        )


def _bootstrap_admin():
    """Crée l'admin depuis ADMIN_EMAIL / ADMIN_PASSWORD si absent de la DB."""
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not email or not password:
        return
    from app.db import get_db
    from app.services.auth import create_user
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if exists:
        return
    try:
        create_user(email, password, name="Admin", is_admin=True)
    except Exception:
        pass


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    from app.db import DB_PATH
    return {
        "status": "ok",
        "ai_ready": bool(os.getenv("ANTHROPIC_API_KEY")),
        "version": "1.0.0",
        "db_ok": os.path.isfile(os.path.abspath(DB_PATH)),
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
        "Poids_kg", "Dimensions_cm", "Couleur", "Matière",
        "Description", "Caractéristiques", "Image_URL", "Mots_cles",
    ]
    mandatory = {"SKU", "Nom", "Marque", "Segment"}

    # Style en-tête
    fill_mandatory = PatternFill("solid", fgColor="764BA2")
    fill_optional  = PatternFill("solid", fgColor="B39DDB")
    font_header    = Font(color="FFFFFF", bold=True, size=11)
    font_example   = Font(color="555555", italic=True, size=10)
    center         = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h + (" *" if h in mandatory else ""))
        cell.fill   = fill_mandatory if h in mandatory else fill_optional
        cell.font   = font_header
        cell.alignment = center

    # Exemple 1 — collier
    ex1 = [
        "BC-COL-001", "Collier Étoile Dorée Chien M", "Bande de Canailles",
        "collier chien", "3760123456789", "24.90",
        "0.085", "35x2 cm", "Rose/Doré", "Nylon recyclé | Métal doré",
        "Collier tendance pour chien avec pendentif étoile dorée, boucle de sécurité et réglage 5 positions",
        "Pendentif étoile doré|Boucle sécurité|Réglage 5 positions|Nylon recyclé certifié",
        "https://monsite.com/photos/BC-COL-001.jpg",
        "collier chien tendance, collier chien pendentif, collier chien fantaisie",
    ]
    # Exemple 2 — laisse
    ex2 = [
        "BC-LAI-012", "Laisse Fleurie Chien 1.2m", "Bande de Canailles",
        "laisse chien", "3760123456790", "19.90",
        "0.120", "120x2 cm", "Multicolore", "Nylon",
        "Laisse chien 1.2m motif fleuri, mousqueton inox, poignée rembourrée",
        "Mousqueton inox|Poignée rembourrée|Motif fleuri|Longueur 1.2m",
        "https://monsite.com/photos/BC-LAI-012.jpg",
        "laisse chien design, laisse chien fantaisie, laisse chien colorée",
    ]

    for col, v in enumerate(ex1, 1):
        cell = ws.cell(row=2, column=col, value=v)
        cell.font = font_example
    for col, v in enumerate(ex2, 1):
        cell = ws.cell(row=3, column=col, value=v)
        cell.font = font_example

    # Largeurs de colonnes
    widths = [14, 34, 20, 18, 16, 8, 10, 14, 14, 22, 48, 44, 40, 48]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = w

    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    # ── Onglet 2 : Guide ─────────────────────────────────────────────────────
    guide = wb.create_sheet("Guide")
    guide.column_dimensions["A"].width = 20
    guide.column_dimensions["B"].width = 14
    guide.column_dimensions["C"].width = 60
    guide.column_dimensions["D"].width = 40

    guide_headers = ["Colonne", "Obligatoire ?", "Description", "Exemple"]
    for col, h in enumerate(guide_headers, 1):
        cell = guide.cell(row=1, column=col, value=h)
        cell.fill = PatternFill("solid", fgColor="1F2937")
        cell.font = Font(color="FFFFFF", bold=True)

    guide_rows = [
        ("SKU",            "✅ Oui", "Référence unique du produit. Doit être stable — c'est la clé de matching.", "BC-COL-001"),
        ("Nom",            "✅ Oui", "Nom brut du produit, sans optimisation SEO — l'IA s'en charge.", "Collier Étoile Dorée M"),
        ("Marque",         "✅ Oui", "Nom exact de la marque, tel qu'il apparaît sur Amazon.", "Bande de Canailles"),
        ("Segment",        "✅ Oui", "Famille produit. Tous les produits du même segment partagent les mêmes mots-clés si la colonne Mots_cles est vide.", "collier chien"),
        ("EAN",            "Recommandé", "Code-barres GTIN-13. Obligatoire pour créer une fiche Amazon.", "3760123456789"),
        ("Prix",           "Recommandé", "Prix de vente TTC en euros. Décimales avec point ou virgule.", "24.90"),
        ("Poids_kg",       "Optionnel", "Poids en kg. Aide l'IA à rédiger les bullet points techniques.", "0.085"),
        ("Dimensions_cm",  "Optionnel", "Format L×l×h ou L×l en cm.", "35x2 cm"),
        ("Couleur",        "Optionnel", "Couleur principale du produit.", "Rose/Doré"),
        ("Matière",        "Optionnel", "Matière(s) principale(s). Séparées par | si plusieurs.", "Nylon recyclé | Métal doré"),
        ("Description",    "Recommandé", "Description brute du produit. Plus elle est riche, meilleure est la fiche générée.", "Collier tendance avec pendentif étoile…"),
        ("Caractéristiques","Recommandé","Points clés du produit, séparés par |. Alimentent les bullet points.", "Pendentif étoile|Boucle sécurité|5 positions"),
        ("Image_URL",      "Optionnel", "URL publique d'une photo du produit (Dropbox, Google Drive, CDN…). Sert de référence visuelle pour la génération d'images.", "https://monsite.com/photo.jpg"),
        ("Mots_cles",      "Optionnel", "Mots-clés Search Query Performance spécifiques à CE produit, séparés par virgule. Écrasent le champ global de l'interface pour ce produit.", "collier chien tendance, collier pendentif"),
    ]

    for r, row_data in enumerate(guide_rows, 2):
        for col, val in enumerate(row_data, 1):
            cell = guide.cell(row=r, column=col, value=val)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if r % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F9FAFB")
        guide.row_dimensions[r].height = 36

    guide.row_dimensions[1].height = 24

    # ── Légende ──────────────────────────────────────────────────────────────
    legend_row = len(guide_rows) + 3
    guide.cell(row=legend_row, column=1, value="* Colonnes violettes foncées = obligatoires").font = Font(bold=True, color="764BA2")
    guide.cell(row=legend_row+1, column=1, value="* Colonnes violettes claires = recommandées ou optionnelles").font = Font(italic=True, color="9333EA")
    guide.cell(row=legend_row+3, column=1, value="Conseil : travaillez par segment (un upload = un type de produit) pour des mots-clés plus précis.").font = Font(italic=True, color="6B7280")

    # ── Export ────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=synqio_template.xlsx"},
    )


# ── Temp photo store ──────────────────────────────────────────────────────────

_temp_photos: dict = {}  # filename → bytes (session-scoped, ephemeral)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_IMAGE_CONTENT_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}


_ZIP_MAX_COMPRESSED   = 100 * 1024 * 1024   # 100 MB compressed
_ZIP_MAX_UNCOMPRESSED = 500 * 1024 * 1024   # 500 MB total decompressed
_ZIP_MAX_SINGLE_FILE  =  50 * 1024 * 1024   # 50 MB per image


@app.post("/api/upload-photos-zip")
async def upload_photos_zip(file: UploadFile = File(...)):
    import zipfile

    content = await file.read()
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
        if entry.endswith("/") or "__MACOSX" in entry or "/." in entry or entry.startswith("."):
            continue
        basename = entry.split("/")[-1]
        stem, ext = os.path.splitext(basename)
        if ext.lower() not in _IMAGE_EXTS:
            continue
        info = zf.getinfo(entry)
        if info.file_size > _ZIP_MAX_SINGLE_FILE:
            skipped.append(basename)
            continue
        sku = stem
        filename = f"{sku}{ext.lower()}"
        _temp_photos[filename] = zf.read(entry)
        matched.append({"sku": sku, "url": f"/api/photos/{filename}", "filename": filename})

    return {"matched": matched, "count": len(matched)}


@app.get("/api/photos/{filename}")
async def serve_photo(filename: str):
    photo = _temp_photos.get(filename)
    if not photo:
        raise HTTPException(404, "Photo non trouvée ou session expirée")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return Response(content=photo, media_type=_IMAGE_CONTENT_TYPES.get(ext, "image/jpeg"))


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
    for k in [k for k, v in _jobs.items() if v.get("created_at", 0) < cutoff]:
        del _jobs[k]


async def _run_generation_job(job_id: str, request: GenerationRequest, email: str):
    _jobs[job_id]["status"] = "running"

    def on_progress(done: int, total: int):
        if job_id in _jobs:
            _jobs[job_id]["progress"] = done

    try:
        listings, failed, gen_tokens = await generate_listings_batch(
            products=request.products,
            marketplace=request.marketplace,
            focus_keywords=request.focus_keywords or [],
            style_tone=request.style_tone,
            on_progress=on_progress,
        )
        if listings and email:
            log_usage(email, "sku_generated", len(listings))
        if email and gen_tokens:
            if gen_tokens.get("input_tokens"):
                log_usage(email, "tokens_in", gen_tokens["input_tokens"])
            if gen_tokens.get("output_tokens"):
                log_usage(email, "tokens_out", gen_tokens["output_tokens"])
        result = GenerationResult(
            listings=listings, failed=failed,
            total=len(request.products), success_count=len(listings),
            marketplace=request.marketplace,
        )
        _jobs[job_id].update({"status": "done", "progress": len(request.products),
                               "result": result.model_dump()})
    except Exception as e:
        _jobs[job_id].update({"status": "failed", "error": str(e)})


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

    job_id = str(uuid4())
    _jobs[job_id] = {"status": "pending", "progress": 0,
                     "total": len(request.products), "created_at": time.time()}
    _cleanup_jobs()
    asyncio.create_task(_run_generation_job(job_id, request, email))
    return {"job_id": job_id, "total": len(request.products)}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
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


# ── Export ────────────────────────────────────────────────────────────────────

@app.post("/api/export/csv")
async def export_csv(listings: List[AmazonListing]):
    return Response(content=to_csv_bytes(listings), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=listings.csv"})


@app.post("/api/export/json")
async def export_json_file(listings: List[AmazonListing]):
    return Response(content=to_json_bytes(listings), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=listings.json"})


@app.post("/api/export/flat-file")
async def export_flat_file(listings: List[AmazonListing]):
    return Response(content=to_amazon_flat_file_bytes(listings), media_type="text/plain",
                    headers={"Content-Disposition": "attachment; filename=amazon_flat_file.txt"})


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


async def _run_image_job(job_id: str, req: ImageRequest, email: str):
    _jobs[job_id]["status"] = "running"
    try:
        images, img_tokens = await generate_product_images(
            sku=req.sku,
            product_name=req.product_name,
            brand=req.brand,
            category=req.category,
            features=req.features,
            color=req.color,
            material=req.material,
            selected_types=req.selected_types,
        )
        generated = [i for i in images if i.get("has_image")]
        if generated and email:
            log_usage(email, "image_generated", len(generated))
        if email and img_tokens:
            if img_tokens.get("input_tokens"):
                log_usage(email, "tokens_in", img_tokens["input_tokens"])
            if img_tokens.get("output_tokens"):
                log_usage(email, "tokens_out", img_tokens["output_tokens"])
        openai_ok = bool(os.getenv("OPENAI_API_KEY"))
        _jobs[job_id].update({
            "status": "done",
            "result": {
                "sku": req.sku,
                "images": images,
                "images_generated": openai_ok,
                "openai_configured": openai_ok,
                "total": len(images),
            },
        })
    except Exception as e:
        _jobs[job_id].update({"status": "failed", "error": str(e)})


@app.post("/api/generate-images")
async def generate_images(req: ImageRequest, request: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")
    email = getattr(request.state, "user_email", None)
    job_id = str(uuid4())
    _jobs[job_id] = {"status": "pending", "progress": 0, "total": 7, "created_at": time.time()}
    _cleanup_jobs()
    asyncio.create_task(_run_image_job(job_id, req, email))
    return {"job_id": job_id}


class SingleImageRequest(BaseModel):
    sku: str
    image_id: str
    prompt: str


async def _run_single_image_job(job_id: str, req: SingleImageRequest, email: str):
    _jobs[job_id]["status"] = "running"
    try:
        url = await _generate_image_dalle3(req.prompt, req.image_id)
        openai_ok = bool(os.getenv("OPENAI_API_KEY"))
        if url and email:
            log_usage(email, "image_generated", 1)
        _jobs[job_id].update({
            "status": "done",
            "result": {
                "sku": req.sku,
                "image_id": req.image_id,
                "url": url,
                "prompt": req.prompt,
                "openai_configured": openai_ok,
            },
        })
    except Exception as e:
        _jobs[job_id].update({"status": "failed", "error": str(e)})


@app.post("/api/generate-image-single")
async def generate_image_single(req: SingleImageRequest, request: Request):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "OPENAI_API_KEY manquante — impossible de régénérer l'image")
    email = getattr(request.state, "user_email", None)
    job_id = str(uuid4())
    _jobs[job_id] = {"status": "pending", "progress": 0, "total": 1, "created_at": time.time()}
    _cleanup_jobs()
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
    from app.services.auth import is_admin
    email = getattr(request.state, "user_email", None)
    if not email or not is_admin(email):
        raise HTTPException(403, "Accès réservé aux administrateurs")
    return get_all_users_usage()


@app.get("/api/image-types")
async def get_image_types():
    return AMAZON_IMAGE_TYPES


# ── Marketplaces ──────────────────────────────────────────────────────────────

@app.get("/api/marketplaces")
async def list_marketplaces():
    return [
        {"id": "amazon_fr",  "name": "Amazon France",    "flag": "🇫🇷", "status": "active"},
        {"id": "amazon_de",  "name": "Amazon Allemagne",  "flag": "🇩🇪", "status": "active"},
        {"id": "amazon_it",  "name": "Amazon Italie",     "flag": "🇮🇹", "status": "active"},
        {"id": "amazon_es",  "name": "Amazon Espagne",    "flag": "🇪🇸", "status": "active"},
        {"id": "amazon_uk",  "name": "Amazon UK",         "flag": "🇬🇧", "status": "active"},
        {"id": "cdiscount",  "name": "Cdiscount",         "flag": "🇫🇷", "status": "beta"},
        {"id": "fnac",       "name": "Fnac",              "flag": "🇫🇷", "status": "beta"},
        {"id": "bol",        "name": "Bol.com",           "flag": "🇳🇱", "status": "beta"},
    ]
