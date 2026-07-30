#!/usr/bin/env python3
"""Auto-feed the Business Watchdog — pull the Amazon Sales & Traffic report for
every user with tracked ASINs and write fresh performance snapshots.

Standalone, for a scheduled Railway Cron (e.g. daily 05:00 UTC). Separate from
the weekly digest because report generation is slow (minutes each), so this
runs on its own cadence with more patience.

Railway: second cron service pointing at railway.sync.json
    Start command:  python scripts/sync_business_data.py
    Cron schedule:  0 5 * * *
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_db
from app.services.business_watchdog import sync_business_snapshots


async def _run() -> int:
    conn = get_db()
    emails = [r["email"] for r in conn.execute("SELECT email FROM users").fetchall()]
    conn.close()

    synced_total = pending = errors = 0
    for email in emails:
        try:
            res = await sync_business_snapshots(email)
            if res.get("status") == "pending":
                pending += 1
                print(f"[sync] {email} — rapport en cours", flush=True)
            elif res.get("synced"):
                synced_total += res["synced"]
                print(f"[sync] {email} — {res['synced']} snapshot(s) ecrit(s)", flush=True)
        except Exception as e:
            errors += 1
            print(f"[sync] ERREUR {email}: {type(e).__name__}: {e}", flush=True)

    print(f"[sync] Termine — {synced_total} snapshots, {pending} en attente, "
          f"{errors} erreurs sur {len(emails)} utilisateurs.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
