#!/usr/bin/env python3
"""Weekly Conformity Watchdog digest — run by Railway Cron.

For every user, scan their catalog and email the health digest. Standalone
(no HTTP, no auth): it talks to the same DATABASE_URL and SMTP_* env vars as
the web service. Safe to run repeatedly; users with an empty catalog or who
unsubscribed are skipped.

Railway: add a second service on this repo with
    Start command:  python scripts/send_weekly_digests.py
    Cron schedule:  0 7 * * 1        (Mondays 07:00 UTC)
Share the same env vars (DATABASE_URL, SMTP_*, APP_URL).
"""
import sys
import os

# Ensure the project root is importable when run as `python scripts/...`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import (get_db, get_catalog, get_catalog_summary,
                    record_health_snapshot, get_health_history)
from app.routes.public_api import _latest_listings
from app.services.compliance import scan_listings, detect_degradation
from app.services.business_watchdog import scan_business
from app.services.email import (send_catalog_health, send_health_alert,
                                send_business_alert, _can_send)


def _gather_catalog_for_scan(email: str) -> list:
    """Same merge as the web endpoint: live SP-API catalog + richest generated
    listing per SKU (live title wins)."""
    merged: dict = {}
    for mkt in (get_catalog_summary(email) or {}):
        for it in get_catalog(email, mkt):
            merged[(mkt, it.get("sku") or "")] = {
                "sku": it.get("sku") or "", "marketplace": mkt,
                "title": it.get("title") or "", "ean": it.get("ean") or "", "_source": "live"}
    for g in _latest_listings(email):
        key = (g.get("marketplace") or "", g.get("sku") or "")
        if key in merged:
            base = merged[key]
            for f in ("bullet_points", "backend_keywords", "description"):
                if f in g:
                    base[f] = g[f]
            if not base.get("title"):
                base["title"] = g.get("title") or ""
        else:
            merged[key] = {**g, "_source": "generated"}
    return list(merged.values())


def main() -> int:
    if not _can_send():
        print("[digest] SMTP non configuré (SMTP_HOST/USER/PASS) — abandon.", flush=True)
        return 1
    conn = get_db()
    emails = [r["email"] for r in conn.execute("SELECT email FROM users").fetchall()]
    conn.close()

    sent = skipped = errors = alerts = 0
    for email in emails:
        try:
            report = scan_listings(_gather_catalog_for_scan(email))
            if not report.get("total"):
                skipped += 1
                continue
            # Compare to the previous snapshot BEFORE recording today's point
            prev = None
            try:
                hist = get_health_history(email, 1)
                prev = hist[-1] if hist else None
            except Exception:
                pass
            reasons = detect_degradation(report, prev)
            try:
                record_health_snapshot(email, report)  # feed the weekly trend curve
            except Exception:
                pass
            if reasons and send_health_alert(email, report, reasons):
                alerts += 1
                print(f"[digest] ALERTE conformité {email} — {'; '.join(reasons)}", flush=True)
            # Business degradation alert (sales/conversion/rank)
            try:
                biz = scan_business(email)
                if biz.get("alerting") and send_business_alert(email, biz):
                    alerts += 1
                    print(f"[digest] ALERTE perf {email} — {biz['alerting']} fiche(s) en baisse", flush=True)
            except Exception:
                pass
            if send_catalog_health(email, report):
                sent += 1
                print(f"[digest] {email} — score {report['score']}/100, "
                      f"{report['non_compliant']} à corriger", flush=True)
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            print(f"[digest] ERREUR {email}: {type(e).__name__}: {e}", flush=True)

    print(f"[digest] Terminé — {sent} digests, {alerts} alertes, {skipped} ignorés, "
          f"{errors} erreurs sur {len(emails)} utilisateurs.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
