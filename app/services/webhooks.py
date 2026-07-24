"""Outbound webhooks — notify client systems when SynqIO produces content.

Events:
- ``listing.generated`` — fired after a generation batch is saved. Payload
  carries the batch metadata and the full listings JSON, so a PIM / site can
  ingest the content without calling back.

Deliveries are signed: ``X-Synqio-Signature: sha256=<hmac-hex>`` computed over
the raw body with the webhook's secret. Receivers should verify it.
One retry after 3s on network error or 5xx. Never raises — a failing client
endpoint must not break generation.
"""
import asyncio
import hashlib
import hmac
import json

import httpx

from app.db import get_active_webhooks, update_webhook_status
from app.logger import log

_TIMEOUT = 10.0


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _deliver_one(hook: dict, event: str, body: bytes) -> None:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SynqIO-Webhooks/1.0",
        "X-Synqio-Event": event,
        "X-Synqio-Webhook-Id": hook["id"],
    }
    if hook.get("secret"):
        headers["X-Synqio-Signature"] = _signature(hook["secret"], body)

    last_err = "unknown"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(hook["url"], content=body, headers=headers)
            if resp.status_code < 500:
                update_webhook_status(hook["id"], f"{resp.status_code}")
                if resp.status_code >= 400:
                    log.warning(f"[webhook] {hook['id']} -> {hook['url']} responded {resp.status_code}")
                return
            last_err = f"{resp.status_code}"
        except Exception as e:
            last_err = type(e).__name__
        if attempt == 1:
            await asyncio.sleep(3)
    update_webhook_status(hook["id"], f"failed:{last_err}")
    log.warning(f"[webhook] delivery failed {hook['id']} -> {hook['url']}: {last_err}")


async def dispatch_webhooks(user_email: str, event: str, payload: dict) -> int:
    """Deliver `event` to every active subscribed webhook. Returns hook count."""
    try:
        hooks = get_active_webhooks(user_email, event)
    except Exception as e:
        log.error(f"[webhook] lookup failed for {user_email}: {type(e).__name__}")
        return 0
    if not hooks:
        return 0
    body = json.dumps({"event": event, **payload}, ensure_ascii=False, default=str).encode()
    await asyncio.gather(*(_deliver_one(h, event, body) for h in hooks), return_exceptions=True)
    return len(hooks)


def fire_and_forget(user_email: str, event: str, payload: dict) -> None:
    """Schedule dispatch on the running loop without blocking the caller."""
    try:
        asyncio.get_running_loop().create_task(dispatch_webhooks(user_email, event, payload))
    except RuntimeError:
        pass  # no running loop (sync context / tests) — skip silently
