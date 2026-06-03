import pytest
import json
from unittest.mock import patch


def _usage_at_limit(plan="starter", skus_used=200, skus_quota=200):
    return {
        "month": "2025-01",
        "plan": plan,
        "plan_label": "Starter",
        "skus_used": skus_used,
        "skus_quota": skus_quota,
        "images_used": 0,
        "images_quota": 50,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
    }


def test_generation_blocked_at_quota(client, auth):
    headers, email = auth
    payload = {
        "products": [{"sku": "T01", "name": "Test", "brand": "B", "category": "cat"}],
        "marketplace": "amazon_fr",
    }
    with patch("app.main.get_user_usage", return_value=_usage_at_limit()):
        resp = client.post("/api/generate", headers=headers, json=payload)
    assert resp.status_code == 429
    assert "Quota" in resp.json()["detail"]


def test_generation_allowed_under_quota(client, auth):
    headers, email = auth
    payload = {
        "products": [{"sku": "T02", "name": "Test", "brand": "B", "category": "cat"}],
        "marketplace": "amazon_fr",
    }
    under_quota = {**_usage_at_limit(), "skus_used": 0}
    # Mock both the usage check AND the actual generation so no real API calls happen
    with patch("app.main.get_user_usage", return_value=under_quota), \
         patch("app.main.asyncio.create_task"):
        resp = client.post("/api/generate", headers=headers, json=payload)
    # Should create the job (200) not block it (429)
    assert resp.status_code == 200
    assert "job_id" in resp.json()


def test_images_blocked_at_monthly_quota(client, auth):
    headers, email = auth
    at_limit = {**_usage_at_limit(), "images_used": 50, "images_quota": 50}
    payload = {
        "sku": "T03", "product_name": "Test", "brand": "B",
        "category": "cat", "features": [],
    }
    with patch("app.main.get_user_usage", return_value=at_limit):
        resp = client.post("/api/generate-images", headers=headers, json=payload)
    assert resp.status_code == 429
