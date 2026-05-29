import os
import json
import time
import asyncio
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
from app.services.ingestion import parse_file
from app.services.ai_agent import generate_listings_batch
from app.services.amazon_sp import publish_listings
from app.services.auth import verify_token
from app.services.image_gen import generate_product_images, AMAZON_IMAGE_TYPES
from app.services.usage import log_usage, get_user_usage, get_all_users_usage
from app.utils.export import to_csv_bytes, to_json_bytes, to_amazon_flat_file_bytes
from app.db import init_db
from app.routes.auth import router as auth_router, admin_router
from app.routes.amazon_oauth import router as amazon_router

# Routes sans authentification
PUBLIC_PATHS = {"/", "/health", "/api/auth/login", "/api/auth/setup", "/api/auth/needs-setup", "/api/marketplaces", "/api/amazon/callback"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Laisser passer les routes publiques et les fichiers statiques
        if path in PUBLIC_PATHS or not path.startswith("/api/"):
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
    init_db()


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ai_ready": bool(os.getenv("ANTHROPIC_API_KEY")),
        "amazon_mode": os.getenv("AMAZON_SP_MODE", "demo"),
        "version": "1.0.0",
    }


# ── Ingestion ─────────────────────────────────────────────────────────────────

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


@app.post("/api/ingest", response_model=List[RawProduct])
async def ingest_file(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Fichier vide")
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "Fichier trop volumineux (max 10 Mo)")
    try:
        products = parse_file(file.filename or "upload.csv", content)
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
        listings, failed = await generate_listings_batch(
            products=request.products,
            marketplace=request.marketplace,
            focus_keywords=request.focus_keywords or [],
            style_tone=request.style_tone,
            on_progress=on_progress,
        )
        if listings and email:
            log_usage(email, "sku_generated", len(listings))
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
    if len(request.products) > 500:
        raise HTTPException(400, "Maximum 500 produits par requête")

    email = getattr(req.state, "user_email", None)
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


@app.post("/api/generate-images")
async def generate_images(req: ImageRequest, request: Request):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")
    try:
        images = await generate_product_images(
            sku=req.sku,
            product_name=req.product_name,
            brand=req.brand,
            category=req.category,
            features=req.features,
            color=req.color,
            material=req.material,
            selected_types=req.selected_types,
        )
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"Réponse Claude invalide (JSON malformé) : {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Erreur génération images : {str(e)}")
    generated = [i for i in images if i.get("has_image")]
    if generated:
        email = getattr(request.state, "user_email", None)
        if email:
            log_usage(email, "image_generated", len(generated))
    openai_ok = bool(os.getenv("OPENAI_API_KEY"))
    return {
        "sku": req.sku,
        "images": images,
        "images_generated": openai_ok,
        "openai_configured": openai_ok,
        "total": len(images),
    }


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
