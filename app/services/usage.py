from datetime import datetime
from fastapi import HTTPException
from app.db import get_db

PLAN_QUOTAS = {
    # skus = quota mensuel sauf maintenance (annual_pool=True → quota annuel de 300)
    "starter":     {"skus": 200,  "images": 50,  "label": "Starter",          "commitment_months": 0,  "price_monthly": 390,  "price_setup": 490,  "annual_pool": False},
    "business":    {"skus": 600,  "images": 200, "label": "Business",         "commitment_months": 3,  "price_monthly": 790,  "price_setup": 990,  "annual_pool": False},
    "scale":       {"skus": 1500, "images": 500, "label": "Scale",            "commitment_months": 6,  "price_monthly": 1490, "price_setup": 1990, "annual_pool": False},
    "maintenance": {"skus": 300,  "images": 100, "label": "Pro / Maintenance","commitment_months": 12, "price_monthly": 90,   "price_setup": 0,    "annual_pool": True},
}

# Features disponibles par plan.
# L'admin peut toujours tout faire — ces gates s'appliquent uniquement aux utilisateurs normaux.
PLAN_FEATURES = {
    #                      a_plus  multi_mkt  multi_user  image_gen  publish
    "starter":     dict(a_plus=False, multi_marketplace=False, multi_user=False, image_gen=True, publish=True),
    "business":    dict(a_plus=True,  multi_marketplace=True,  multi_user=False, image_gen=True, publish=True),
    "scale":       dict(a_plus=True,  multi_marketplace=True,  multi_user=True,  image_gen=True, publish=True),
    "maintenance": dict(a_plus=True,  multi_marketplace=False, multi_user=False, image_gen=True, publish=True),
}

_FEATURE_LABELS = {
    "a_plus":             "Contenu A+ (disponible à partir de Business)",
    "multi_marketplace":  "Multi-marketplace (disponible à partir de Business)",
    "multi_user":         "Multi-utilisateurs (disponible sur Scale uniquement)",
    "image_gen":          "Génération d'images IA (non disponible sur À la carte)",
    "publish":            "Publication Amazon SP-API",
}

# Tarifs Claude Sonnet + gpt-image-1
_COST_PER_M_IN  = 3.0    # USD / million tokens input
_COST_PER_M_OUT = 15.0   # USD / million tokens output
_COST_PER_IMAGE = 0.04   # USD / image gpt-image-1 standard


def _compute_cost(tokens_in: int, tokens_out: int, images: int) -> float:
    return round(
        tokens_in  * _COST_PER_M_IN  / 1_000_000 +
        tokens_out * _COST_PER_M_OUT / 1_000_000 +
        images     * _COST_PER_IMAGE,
        4,
    )


def _current_month() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def _current_year_months() -> list[str]:
    """Retourne les 12 mois de l'année courante (pour le pool annuel maintenance)."""
    year = datetime.utcnow().strftime("%Y")
    return [f"{year}-{m:02d}" for m in range(1, 13)]


def _get_user_plan(user_email: str) -> str:
    conn = get_db()
    row = conn.execute("SELECT plan, is_admin FROM users WHERE email = ?", (user_email,)).fetchone()
    conn.close()
    if row and row["is_admin"]:
        return "_admin"
    return (row["plan"] if row and row["plan"] else None) or "starter"


def require_feature(user_email: str, feature: str):
    """Lève HTTP 403 si le plan de l'utilisateur n'a pas accès à cette feature.
    Les admins passent toujours."""
    plan = _get_user_plan(user_email)
    if plan == "_admin":
        return
    features = PLAN_FEATURES.get(plan, PLAN_FEATURES["starter"])
    if not features.get(feature, False):
        label = PLAN_QUOTAS.get(plan, {}).get("label", plan)
        feature_label = _FEATURE_LABELS.get(feature, feature)
        raise HTTPException(
            403,
            f"{feature_label}. Votre plan actuel : {label}. "
            "Contactez-nous pour évoluer vers l'offre supérieure.",
        )


def log_usage(user_email: str, action: str, count: int = 1):
    conn = get_db()
    conn.execute(
        "INSERT INTO usage (user_email, action, count, month) VALUES (?, ?, ?, ?)",
        (user_email, action, count, _current_month()),
    )
    conn.commit()
    conn.close()


def get_user_usage(user_email: str, month: str = None) -> dict:
    month = month or _current_month()
    conn = get_db()
    plan_row = conn.execute(
        "SELECT plan FROM users WHERE email = ?", (user_email,)
    ).fetchone()
    plan = (plan_row["plan"] if plan_row and plan_row["plan"] else None) or "starter"
    quota = PLAN_QUOTAS.get(plan, PLAN_QUOTAS["starter"])

    if quota.get("annual_pool"):
        # Pool annuel : on agrège tous les mois de l'année en cours
        months = _current_year_months()
        placeholders = ",".join("?" * len(months))
        rows = conn.execute(
            f"SELECT action, SUM(count) as total FROM usage "
            f"WHERE user_email = ? AND month IN ({placeholders}) GROUP BY action",
            (user_email, *months),
        ).fetchall()
        period_label = datetime.utcnow().strftime("%Y")
    else:
        rows = conn.execute(
            "SELECT action, SUM(count) as total FROM usage WHERE user_email = ? AND month = ? GROUP BY action",
            (user_email, month),
        ).fetchall()
        period_label = month
    conn.close()

    usage = {r["action"]: r["total"] for r in rows}
    tokens_in   = usage.get("tokens_in", 0)
    tokens_out  = usage.get("tokens_out", 0)
    images_used = usage.get("image_generated", 0)

    return {
        "month": period_label,
        "plan": plan,
        "plan_label": quota["label"],
        "annual_pool": quota.get("annual_pool", False),
        "skus_used": usage.get("sku_generated", 0),
        "skus_quota": quota["skus"],
        "images_used": images_used,
        "images_quota": quota["images"],
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "cost_usd": _compute_cost(tokens_in, tokens_out, images_used),
        "features": PLAN_FEATURES.get(plan, PLAN_FEATURES["starter"]),
        "commitment_months": quota.get("commitment_months", 0),
    }


def get_all_users_usage(month: str = None) -> list:
    month = month or _current_month()
    conn = get_db()
    users = conn.execute(
        "SELECT email, name, plan FROM users WHERE is_active = 1 ORDER BY email"
    ).fetchall()
    result = []
    for u in users:
        rows = conn.execute(
            "SELECT action, SUM(count) as total FROM usage WHERE user_email = ? AND month = ? GROUP BY action",
            (u["email"], month),
        ).fetchall()
        usage = {r["action"]: r["total"] for r in rows}
        plan = u["plan"] or "starter"
        quota = PLAN_QUOTAS.get(plan, PLAN_QUOTAS["starter"])
        tokens_in   = usage.get("tokens_in", 0)
        tokens_out  = usage.get("tokens_out", 0)
        images_used = usage.get("image_generated", 0)
        result.append({
            "email": u["email"],
            "name": u["name"],
            "plan": plan,
            "plan_label": quota["label"],
            "skus_used": usage.get("sku_generated", 0),
            "skus_quota": quota["skus"],
            "images_used": images_used,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_in + tokens_out,
            "cost_usd": _compute_cost(tokens_in, tokens_out, images_used),
        })
    conn.close()
    return result
