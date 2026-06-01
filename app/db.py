import sqlite3
import os

# Use __file__-relative path so the DB is always under the project root's data/
# regardless of the working directory. On Railway with a volume at /app/data,
# this resolves to /app/data/users.db.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", os.path.join(_PROJECT_ROOT, "data", "users.db"))


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    except Exception:
        pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS amazon_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT UNIQUE NOT NULL,
            seller_id TEXT DEFAULT '',
            refresh_token TEXT NOT NULL,
            marketplace_id TEXT DEFAULT 'A13V1IB3VIYZZH',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS amazon_oauth_states (
            state TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            action TEXT NOT NULL,
            count INTEGER DEFAULT 1,
            month TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'starter'")
    except Exception:
        pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS invitation_tokens (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def get_config(key: str, default: str = None) -> str:
    """Read config: DB first, then env var, then default."""
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
        conn.close()
        if row and row["value"]:
            return row["value"]
    except Exception:
        pass
    return os.getenv(key, default)


def set_config(key: str, value: str):
    """Write config value to app_config table."""
    conn = get_db()
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (key, value),
    )
    conn.commit()
    conn.close()


# ── Generation history ────────────────────────────────────────────────────────

def _ensure_history_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generation_history (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            marketplace TEXT NOT NULL,
            product_count INTEGER NOT NULL DEFAULT 0,
            avg_seo_score INTEGER DEFAULT 0,
            label TEXT DEFAULT '',
            listings_json TEXT NOT NULL DEFAULT '[]',
            images_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    # Add images_json column if it doesn't exist (migration)
    try:
        conn.execute("ALTER TABLE generation_history ADD COLUMN images_json TEXT NOT NULL DEFAULT '{}'")
    except Exception:
        pass


def save_generation(user_email: str, batch_id: str, marketplace: str,
                    listings: list, label: str = "") -> None:
    import json as _json
    conn = get_db()
    _ensure_history_table(conn)
    top = [l for l in listings if not (l.get("parent_sku") and not l.get("is_parent"))]
    product_count = len(top)
    scores = [l.get("seo_score") or 0 for l in top if l.get("seo_score")]
    avg_seo = round(sum(scores) / len(scores)) if scores else 0
    conn.execute(
        "INSERT OR REPLACE INTO generation_history "
        "(id, user_email, marketplace, product_count, avg_seo_score, label, listings_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, user_email, marketplace, product_count, avg_seo, label,
         _json.dumps(listings, ensure_ascii=False, default=str)),
    )
    # Keep only the 50 most recent batches per user
    conn.execute(
        "DELETE FROM generation_history WHERE user_email = ? AND id NOT IN "
        "(SELECT id FROM generation_history WHERE user_email = ? "
        " ORDER BY created_at DESC LIMIT 50)",
        (user_email, user_email),
    )
    conn.commit()
    conn.close()


def list_generations(user_email: str) -> list:
    conn = get_db()
    _ensure_history_table(conn)
    rows = conn.execute(
        "SELECT id, created_at, marketplace, product_count, avg_seo_score, label, images_json "
        "FROM generation_history WHERE user_email = ? ORDER BY created_at DESC",
        (user_email,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            import json as _json
            imgs = _json.loads(d.pop("images_json", "{}") or "{}")
            d["image_count"] = sum(
                len([i for i in v if isinstance(v, list) and i.get("has_image")])
                if isinstance(v, list) else 0
                for v in imgs.values()
            )
        except Exception:
            d.pop("images_json", None)
            d["image_count"] = 0
        result.append(d)
    return result


def get_generation(batch_id: str, user_email: str):
    import json as _json
    conn = get_db()
    _ensure_history_table(conn)
    row = conn.execute(
        "SELECT * FROM generation_history WHERE id = ? AND user_email = ?",
        (batch_id, user_email),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["listings"] = _json.loads(d.pop("listings_json", "[]"))
    d["images"] = _json.loads(d.pop("images_json", "{}"))
    return d


def delete_generation(batch_id: str, user_email: str) -> bool:
    conn = get_db()
    _ensure_history_table(conn)
    cur = conn.execute(
        "DELETE FROM generation_history WHERE id = ? AND user_email = ?",
        (batch_id, user_email),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def save_generation_images(batch_id: str, user_email: str, sku: str, images: list) -> None:
    """Attach/update images for one SKU in a generation history batch."""
    import json as _json
    conn = get_db()
    _ensure_history_table(conn)
    row = conn.execute(
        "SELECT images_json FROM generation_history WHERE id = ? AND user_email = ?",
        (batch_id, user_email),
    ).fetchone()
    if not row:
        conn.close()
        return
    try:
        cache = _json.loads(row["images_json"] or "{}")
    except Exception:
        cache = {}
    cache[sku] = images
    conn.execute(
        "UPDATE generation_history SET images_json = ? WHERE id = ? AND user_email = ?",
        (_json.dumps(cache, ensure_ascii=False, default=str), batch_id, user_email),
    )
    conn.commit()
    conn.close()


def update_generation_label(batch_id: str, user_email: str, label: str) -> bool:
    conn = get_db()
    _ensure_history_table(conn)
    cur = conn.execute(
        "UPDATE generation_history SET label = ? WHERE id = ? AND user_email = ?",
        (label, batch_id, user_email),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0
