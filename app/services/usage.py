from datetime import datetime
from app.db import get_db

PLAN_QUOTAS = {
    "starter":  {"skus": 200,  "label": "Starter"},
    "business": {"skus": 600,  "label": "Business"},
    "scale":    {"skus": 1500, "label": "Scale"},
}


def _current_month() -> str:
    return datetime.utcnow().strftime("%Y-%m")


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
    rows = conn.execute(
        "SELECT action, SUM(count) as total FROM usage WHERE user_email = ? AND month = ? GROUP BY action",
        (user_email, month),
    ).fetchall()
    plan_row = conn.execute(
        "SELECT plan FROM users WHERE email = ?", (user_email,)
    ).fetchone()
    conn.close()

    usage = {r["action"]: r["total"] for r in rows}
    plan = plan_row["plan"] if plan_row and plan_row["plan"] else "starter"
    quota = PLAN_QUOTAS.get(plan, PLAN_QUOTAS["starter"])

    return {
        "month": month,
        "plan": plan,
        "plan_label": quota["label"],
        "skus_used": usage.get("sku_generated", 0),
        "skus_quota": quota["skus"],
        "images_used": usage.get("image_generated", 0),
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
        result.append({
            "email": u["email"],
            "name": u["name"],
            "plan": plan,
            "skus_used": usage.get("sku_generated", 0),
            "skus_quota": quota["skus"],
            "images_used": usage.get("image_generated", 0),
        })
    conn.close()
    return result
