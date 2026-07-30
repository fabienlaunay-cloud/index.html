"""Conformity Watchdog — deterministic, zero-cost compliance scan of a catalog.

Runs Amazon rule checks over listing-shaped dicts. No AI per listing (a catalog
can hold hundreds of items), so every rule is a cheap deterministic check. Each
rule only fires on the fields actually present, so it works both on the thin
SP-API catalog (title/sku/asin/ean) and on richly generated listings
(title, bullets, keywords, description).

check_listing(listing) -> list[Issue]
scan_listings(listings) -> {score, total, compliant, by_severity, issues, listings}
"""
import re
from typing import List, Optional

from app.models import TITLE_MAX  # 75 (Amazon policy effective 2026-07-27)

_KEYWORDS_MAX_BYTES = 249
_FORBIDDEN_TITLE = set("!$?_{}^¡¿")
_SUPERLATIVES = ("meilleur", "n°1", "no1", "n1", "best", "top vente", "numéro 1",
                 "le plus", "parfait", "incroyable", "révolutionnaire")

# Media categories keep a 200-char title allowance; without a reliable category
# we treat >75 as a warning (not critical) so we never cry wolf on media items.
CRITICAL = "critique"
WARNING = "avertissement"


def _issue(code, severity, field, message, fixable=False):
    return {"code": code, "severity": severity, "field": field,
            "message": message, "fixable": fixable}


def check_listing(listing: dict) -> List[dict]:
    """Return the list of compliance issues for one listing (empty = compliant)."""
    issues: List[dict] = []
    title = (listing.get("title") or "").strip()
    bullets = listing.get("bullet_points") or listing.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    bullets = [b for b in bullets if (b or "").strip()]
    keywords = (listing.get("backend_keywords") or "").strip()
    description = (listing.get("description") or "").strip()
    has_bullets_field = "bullet_points" in listing or "bullets" in listing
    has_keywords_field = "backend_keywords" in listing
    has_desc_field = "description" in listing

    status = (listing.get("status") or "").lower()
    is_live_inactive = status in ("inactive", "incomplete")

    # ── Title (always present in both sources) ───────────────────────────────
    if not title:
        # An unpublished/inactive draft with no title is not a live compliance
        # failure — don't cry wolf. Active listings still flag it as critical.
        if not is_live_inactive:
            issues.append(_issue("title_missing", CRITICAL, "title", "Titre manquant"))
    else:
        n = len(title)
        if n > TITLE_MAX:
            issues.append(_issue(
                "title_too_long", WARNING, "title",
                f"Titre de {n} caractères — dépasse la limite Amazon de {TITLE_MAX} (sera tronqué)",
                fixable=True))
        bad = sorted(_FORBIDDEN_TITLE & set(title))
        if bad:
            issues.append(_issue("title_forbidden_chars", WARNING, "title",
                                 f"Caractères interdits dans le titre : {' '.join(bad)}", fixable=True))
        letters = [c for c in title if c.isalpha()]
        if len(letters) >= 8 and all(c.isupper() for c in letters):
            issues.append(_issue("title_all_caps", WARNING, "title",
                                 "Titre entièrement en majuscules", fixable=True))
        low = title.lower()
        hit = next((s for s in _SUPERLATIVES if s in low), None)
        if hit:
            issues.append(_issue("title_superlative", WARNING, "title",
                                 f"Superlatif interdit dans le titre : « {hit} »", fixable=True))

    # ── Bullets (only if the field exists — i.e. a generated listing) ────────
    if has_bullets_field:
        if len(bullets) < 5:
            issues.append(_issue("bullets_incomplete", WARNING, "bullets",
                                 f"{len(bullets)}/5 bullet points — Amazon en recommande 5", fixable=True))
        short = [b for b in bullets if len(b) < 80]
        if bullets and len(short) >= 3:
            issues.append(_issue("bullets_too_short", WARNING, "bullets",
                                 "Plusieurs bullet points trop courts (< 80 caractères)", fixable=True))

    # ── Backend keywords ─────────────────────────────────────────────────────
    if has_keywords_field:
        if not keywords:
            issues.append(_issue("keywords_missing", WARNING, "keywords",
                                 "Mots-clés backend vides — perte de visibilité", fixable=True))
        else:
            nb = len(keywords.encode("utf-8"))
            if nb > _KEYWORDS_MAX_BYTES:
                issues.append(_issue("keywords_too_long", CRITICAL, "keywords",
                                     f"Mots-clés backend : {nb}/{_KEYWORDS_MAX_BYTES} octets — dépassement rejeté par Amazon",
                                     fixable=True))

    # ── Description ──────────────────────────────────────────────────────────
    if has_desc_field and not description:
        issues.append(_issue("description_missing", WARNING, "description",
                             "Description absente", fixable=True))

    # ── EAN / GTIN (recommended for discoverability) ─────────────────────────
    if "ean" in listing and not (listing.get("ean") or "").strip():
        issues.append(_issue("ean_missing", WARNING, "ean",
                             "EAN/GTIN manquant — recommandé dans la plupart des catégories"))

    return issues


def detect_degradation(current: dict, previous: Optional[dict]) -> List[str]:
    """Compare a fresh scan to the previous health snapshot and return the list of
    degradation reasons (empty = no alert). Proactive trigger: score dropped
    sharply, new critical issues appeared, or many listings turned non-compliant.
    `previous` is a snapshot dict from get_health_history (keys: score, critical,
    non_compliant) or None on the very first scan."""
    if not previous:
        return []
    reasons: List[str] = []
    drop = (previous.get("score") or 0) - (current.get("score") or 0)
    if drop >= 10:
        reasons.append(f"Score en baisse de {drop} points ({previous.get('score')} → {current.get('score')}/100)")
    new_crit = (current.get("critical_count") or 0) - (previous.get("critical") or 0)
    if new_crit >= 1:
        reasons.append(f"{new_crit} nouveau(x) problème(s) critique(s)")
    new_nc = (current.get("non_compliant") or 0) - (previous.get("non_compliant") or 0)
    if new_nc >= 5:
        reasons.append(f"{new_nc} fiche(s) devenues non conformes")
    return reasons


def scan_listings(listings: List[dict]) -> dict:
    """Scan a catalog and return a health report."""
    results = []
    n_critical = 0
    n_warning = 0
    compliant = 0
    counted = 0  # headline metrics cover active/published listings only
    for l in listings:
        status = (l.get("status") or "").lower()
        is_inactive = status in ("inactive", "incomplete")
        issues = check_listing(l)
        crit = sum(1 for i in issues if i["severity"] == CRITICAL)
        warn = sum(1 for i in issues if i["severity"] == WARNING)
        if not is_inactive:
            counted += 1
            n_critical += crit
            n_warning += warn
            if not issues:
                compliant += 1
        if issues:
            units = int(l.get("_units") or 0)
            qty = l.get("quantity")
            results.append({
                "sku": l.get("sku") or "",
                "title": (l.get("title") or "")[:120],
                "marketplace": l.get("marketplace") or "",
                "source": l.get("_source") or "",  # "generated" (fixable) | "live"
                "status": status or ("active" if not is_inactive else "inactive"),
                "in_stock": bool(qty and qty > 0),
                "price": l.get("price"),
                "units": units,
                "revenue": round(float(l.get("_revenue") or 0), 2),
                "selling": units > 0,
                "critical": crit, "warning": warn,
                "issues": issues,
            })
    total = counted
    # Health score: share of compliant listings, but any critical issue caps it hard
    score = round(100 * compliant / total) if total else 100
    # sort worst first
    results.sort(key=lambda r: (r["critical"], r["warning"]), reverse=True)
    return {
        "score": score,
        "total": total,
        "compliant": compliant,
        "non_compliant": total - compliant,
        "critical_count": n_critical,
        "warning_count": n_warning,
        "listings": results,
    }
