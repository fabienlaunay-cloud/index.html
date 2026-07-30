"""Business Watchdog — the same degradation-detection pattern as the Conformity
Watchdog, but applied to sales performance instead of SEO compliance.

Runs over the performance_snapshots a seller already feeds (CSV import of the
Amazon Business Report today; SP-API auto-pull later). For each tracked
listing it compares the latest period to the previous one and flags real
drops: units/revenue down, conversion down, traffic down, keyword rank lost.

Deterministic and cheap — no AI. Thresholds are relative (%) with a minimum
base so tiny numbers don't cry wolf.
"""
from typing import List, Optional

from app.db import list_tracked_listings, get_snapshots


def _pct_drop(prev: float, cur: float) -> float:
    """Percentage drop from prev to cur (positive = declined)."""
    if not prev:
        return 0.0
    return (prev - cur) / prev * 100.0


def detect_business_degradation(snaps: List[dict]) -> List[str]:
    """Compare the latest snapshot to the previous one; return drop reasons."""
    if not snaps or len(snaps) < 2:
        return []
    cur, prev = snaps[-1], snaps[-2]
    reasons: List[str] = []

    pu, cu = prev.get("units_ordered") or 0, cur.get("units_ordered") or 0
    if pu >= 3 and _pct_drop(pu, cu) >= 30:
        reasons.append(f"Ventes en baisse de {round(_pct_drop(pu, cu))}% ({pu} → {cu} unités)")

    pr, cr = prev.get("revenue") or 0, cur.get("revenue") or 0
    if pr >= 20 and _pct_drop(pr, cr) >= 30:
        reasons.append(f"Chiffre d'affaires en baisse de {round(_pct_drop(pr, cr))}% ({round(pr)}€ → {round(cr)}€)")

    pc, cc = prev.get("conversion_rate") or 0, cur.get("conversion_rate") or 0
    if pc >= 1 and _pct_drop(pc, cc) >= 25:
        reasons.append(f"Taux de conversion en baisse de {round(_pct_drop(pc, cc))}% ({round(pc,1)}% → {round(cc,1)}%)")

    ps, cs = prev.get("sessions") or 0, cur.get("sessions") or 0
    if ps >= 20 and _pct_drop(ps, cs) >= 40:
        reasons.append(f"Trafic en baisse de {round(_pct_drop(ps, cs))}% ({ps} → {cs} sessions)")

    prk, crk = prev.get("keyword_rank"), cur.get("keyword_rank")
    if prk and crk and (crk - prk) >= 5:
        kw = cur.get("keyword") or "mot-clé principal"
        reasons.append(f"Perte de position sur « {kw} » : {prk}ᵉ → {crk}ᵉ")

    return reasons


def scan_business(user_email: str) -> dict:
    """Business health report: which tracked listings are declining and why."""
    tracked = list_tracked_listings(user_email)
    alerting = []
    for l in tracked:
        snaps = get_snapshots(l["id"], user_email)
        reasons = detect_business_degradation(snaps)
        if reasons:
            latest = snaps[-1]
            alerting.append({
                "listing_id": l["id"],
                "title": (l.get("title") or l.get("sku") or "")[:120],
                "sku": l.get("sku") or "",
                "asin": l.get("asin") or "",
                "reasons": reasons,
                "latest": {
                    "date": latest.get("snapshot_date"),
                    "units_ordered": latest.get("units_ordered"),
                    "revenue": latest.get("revenue"),
                    "conversion_rate": latest.get("conversion_rate"),
                    "sessions": latest.get("sessions"),
                },
            })
    return {
        "total_tracked": len(tracked),
        "alerting": len(alerting),
        "listings": alerting,
    }
