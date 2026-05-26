import os
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, BackgroundTasks
from fastapi.responses import Response, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.models import (
    GenerationRequest, GenerationResult, PublishRequest, PublishResult,
    RawProduct, Marketplace, AmazonListing,
)
from app.services.ingestion import parse_file
from app.services.ai_agent import generate_listings_batch
from app.services.amazon_sp import publish_listings
from app.utils.export import to_csv_bytes, to_json_bytes, to_amazon_flat_file_bytes

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


# ── Static frontend ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    amazon_mode = os.getenv("AMAZON_SP_MODE", "demo")
    return {
        "status": "ok",
        "ai_ready": has_key,
        "amazon_mode": amazon_mode,
        "version": "1.0.0",
    }


# ── Ingestion ────────────────────────────────────────────────────────────────

@app.post("/api/ingest", response_model=List[RawProduct])
async def ingest_file(file: UploadFile = File(...)):
    """Parse un fichier CSV/Excel/JSON et retourne les produits bruts."""
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


# ── Generation ───────────────────────────────────────────────────────────────

@app.post("/api/generate", response_model=GenerationResult)
async def generate(request: GenerationRequest):
    """Génère les fiches optimisées via IA pour une liste de produits."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante — configurez votre clé API")
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
        listings=listings,
        failed=failed,
        total=len(request.products),
        success_count=len(listings),
        marketplace=request.marketplace,
    )


@app.post("/api/ingest-and-generate", response_model=GenerationResult)
async def ingest_and_generate(
    file: UploadFile = File(...),
    marketplace: Marketplace = Form(Marketplace.AMAZON_FR),
    focus_keywords: Optional[str] = Form(""),
    style_tone: str = Form("professionnel"),
):
    """Pipeline complet: upload fichier → parse → génération IA."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Fichier vide")
    try:
        products = parse_file(file.filename or "upload.csv", content)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not products:
        raise HTTPException(422, "Aucun produit trouvé")

    kw_list = [k.strip() for k in (focus_keywords or "").split(",") if k.strip()]

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY manquante")

    listings, failed = await generate_listings_batch(
        products=products,
        marketplace=marketplace,
        focus_keywords=kw_list,
        style_tone=style_tone,
    )

    return GenerationResult(
        listings=listings,
        failed=failed,
        total=len(products),
        success_count=len(listings),
        marketplace=marketplace,
    )


# ── Publish ──────────────────────────────────────────────────────────────────

@app.post("/api/publish", response_model=PublishResult)
async def publish(request: PublishRequest):
    """Injecte les fiches dans Amazon Seller Central (ou mode demo)."""
    if not request.listings:
        raise HTTPException(400, "Aucune fiche à publier")
    result = await publish_listings(
        listings=request.listings,
        marketplace=request.marketplace,
        dry_run=request.dry_run,
    )
    return result


# ── Export ───────────────────────────────────────────────────────────────────

@app.post("/api/export/csv")
async def export_csv(listings: List[AmazonListing]):
    data = to_csv_bytes(listings)
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=marketplace_listings.csv"},
    )


@app.post("/api/export/json")
async def export_json_file(listings: List[AmazonListing]):
    data = to_json_bytes(listings)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=marketplace_listings.json"},
    )


@app.post("/api/export/flat-file")
async def export_flat_file(listings: List[AmazonListing]):
    """Amazon Flat File format (.txt tab-separated) pour import Seller Central."""
    data = to_amazon_flat_file_bytes(listings)
    return Response(
        content=data,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=amazon_flat_file.txt"},
    )


# ── Marketplaces metadata ────────────────────────────────────────────────────

@app.get("/api/marketplaces")
async def list_marketplaces():
    return [
        {"id": "amazon_fr", "name": "Amazon France", "flag": "🇫🇷", "status": "active"},
        {"id": "amazon_de", "name": "Amazon Allemagne", "flag": "🇩🇪", "status": "active"},
        {"id": "amazon_it", "name": "Amazon Italie", "flag": "🇮🇹", "status": "active"},
        {"id": "amazon_es", "name": "Amazon Espagne", "flag": "🇪🇸", "status": "active"},
        {"id": "amazon_uk", "name": "Amazon UK", "flag": "🇬🇧", "status": "active"},
        {"id": "cdiscount", "name": "Cdiscount", "flag": "🇫🇷", "status": "beta"},
        {"id": "fnac", "name": "Fnac", "flag": "🇫🇷", "status": "beta"},
        {"id": "bol", "name": "Bol.com", "flag": "🇳🇱", "status": "beta"},
    ]
