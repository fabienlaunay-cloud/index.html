import os
from dotenv import load_dotenv
load_dotenv()

from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.responses import Response, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.models import (
    GenerationRequest, GenerationResult, PublishRequest, PublishResult,
    RawProduct, Marketplace, AmazonListing,
)
from app.services.ingestion import parse_file
from app.services.ai_agent import generate_listings_batch
from app.services.amazon_sp import publish_listings
from app.services.auth import verify_token
from app.utils.export import to_csv_bytes, to_json_bytes, to_amazon_flat_file_bytes
from app.db import init_db
from app.routes.auth import router as auth_router, admin_router

# Routes sans authentification
PUBLIC_PATHS = {"/", "/health", "/api/auth/login", "/api/marketplaces"}


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)

app.include_router(auth_router)
app.include_router(admin_router)


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

@app.post("/api/ingest", response_model=List[RawProduct])
async def ingest_file(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Fichier vide")
    try:
        products = parse_file(file.filename or "upload.csv", content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erreur de parsing: {e}")
    if not products:
        raise HTTPException(422, "Aucun produit trouvé dans le fichier")
    return products


# ── Generation ────────────────────────────────────────────────────────────────

@app.post("/api/generate", response_model=GenerationResult)
async def generate(request: GenerationRequest):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")
    if not request.products:
        raise HTTPException(400, "Liste de produits vide")
    if len(request.products) > 50:
        raise HTTPException(400, "Maximum 50 produits par requête")
    listings, failed = await generate_listings_batch(
        products=request.products,
        marketplace=request.marketplace,
        focus_keywords=request.focus_keywords or [],
        style_tone=request.style_tone,
    )
    return GenerationResult(
        listings=listings, failed=failed,
        total=len(request.products), success_count=len(listings),
        marketplace=request.marketplace,
    )


# ── Publish ───────────────────────────────────────────────────────────────────

@app.post("/api/publish", response_model=PublishResult)
async def publish(request: PublishRequest):
    if not request.listings:
        raise HTTPException(400, "Aucune fiche à publier")
    return await publish_listings(
        listings=request.listings,
        marketplace=request.marketplace,
        dry_run=request.dry_run,
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
