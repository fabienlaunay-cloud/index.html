import sqlite3
import os

# Use __file__-relative path so the DB is always under the project root's data/
# regardless of the working directory. On Railway with a volume at /app/data,
# this resolves to /app/data/users.db.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", os.path.join(_PROJECT_ROOT, "data", "users.db"))

# ── PostgreSQL support ────────────────────────────────────────────────────────

_pg_pool = None


def _get_pg_pool():
    """Lazy-initialize and return the module-level ThreadedConnectionPool."""
    global _pg_pool
    if _pg_pool is None:
        import psycopg2.pool  # imported here to avoid ImportError when psycopg2 not installed
        database_url = os.getenv("DATABASE_URL")
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=database_url)
    return _pg_pool


class _PGConn:
    """
    Wraps a psycopg2 connection so it behaves like sqlite3.Connection.
    All existing callers (get_db / conn.execute / conn.commit / conn.close)
    work without modification.
    """

    def __init__(self, raw_conn, pool):
        self._conn = raw_conn
        self._pool = pool

    # Make `conn.row_factory = sqlite3.Row` a harmless no-op
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass  # psycopg2 RealDictCursor already returns dict-like rows

    def execute(self, sql: str, params=()):
        import psycopg2.extras
        # Convert SQLite positional placeholders to psycopg2 style
        pg_sql = sql.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(pg_sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        try:
            self._conn.rollback()
        except Exception:
            pass
        self._pool.putconn(self._conn)


# ── Public API ────────────────────────────────────────────────────────────────

def get_db():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        pool = _get_pg_pool()
        return _PGConn(pool.getconn(), pool)
    # SQLite fallback for local development
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        _init_db_pg()
    else:
        _init_db_sqlite()


def _init_db_pg():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            is_admin INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'starter',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'starter'")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS amazon_credentials (
            id SERIAL PRIMARY KEY,
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
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            action TEXT NOT NULL,
            count INTEGER DEFAULT 1,
            month TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            title TEXT DEFAULT 'Nouvelle conversation',
            messages_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

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
    conn.execute("ALTER TABLE generation_history ADD COLUMN IF NOT EXISTS images_json TEXT NOT NULL DEFAULT '{}'")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_catalog (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            sku TEXT NOT NULL,
            asin TEXT DEFAULT '',
            ean TEXT DEFAULT '',
            title TEXT DEFAULT '',
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_email, marketplace, sku)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_email TEXT,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            result_json TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def _init_db_sqlite():
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
    try:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'starter'")
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            title TEXT DEFAULT 'Nouvelle conversation',
            messages_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            sku TEXT NOT NULL,
            asin TEXT DEFAULT '',
            ean TEXT DEFAULT '',
            title TEXT DEFAULT '',
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_email, marketplace, sku)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_email TEXT,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            result_json TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
    # Add images_json column if it doesn't exist (SQLite migration;
    # PG uses ADD COLUMN IF NOT EXISTS in init_db)
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
        "INSERT INTO generation_history "
        "(id, user_email, marketplace, product_count, avg_seo_score, label, listings_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "    user_email=EXCLUDED.user_email, "
        "    marketplace=EXCLUDED.marketplace, "
        "    product_count=EXCLUDED.product_count, "
        "    avg_seo_score=EXCLUDED.avg_seo_score, "
        "    label=EXCLUDED.label, "
        "    listings_json=EXCLUDED.listings_json",
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


# ── Product catalog ───────────────────────────────────────────────────────────

def save_catalog_items(user_email: str, marketplace: str, items: list) -> int:
    """Upsert items into product_catalog. Returns count saved."""
    if not items:
        return 0
    conn = get_db()
    database_url = os.getenv("DATABASE_URL")
    count = 0
    for item in items:
        sku = item.get("sku", "")
        if not sku:
            continue
        if database_url:
            conn.execute(
                """
                INSERT INTO product_catalog (user_email, marketplace, sku, asin, ean, title, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_email, marketplace, sku) DO UPDATE SET
                    asin = EXCLUDED.asin,
                    ean = EXCLUDED.ean,
                    title = EXCLUDED.title,
                    synced_at = CURRENT_TIMESTAMP
                """,
                (user_email, marketplace, sku,
                 item.get("asin", ""), item.get("ean", ""), item.get("title", "")),
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO product_catalog (user_email, marketplace, sku, asin, ean, title, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (user_email, marketplace, sku,
                 item.get("asin", ""), item.get("ean", ""), item.get("title", "")),
            )
        count += 1
    conn.commit()
    conn.close()
    return count


def get_catalog(user_email: str, marketplace: str) -> list:
    """Return all catalog items for user+marketplace as list of dicts."""
    conn = get_db()
    rows = conn.execute(
        "SELECT sku, asin, ean, title, synced_at FROM product_catalog "
        "WHERE user_email = ? AND marketplace = ? ORDER BY sku",
        (user_email, marketplace),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_catalog_summary(user_email: str) -> dict:
    """Return {marketplace: count} summary across all marketplaces."""
    conn = get_db()
    rows = conn.execute(
        "SELECT marketplace, COUNT(*) as cnt FROM product_catalog "
        "WHERE user_email = ? GROUP BY marketplace",
        (user_email,),
    ).fetchall()
    conn.close()
    return {r["marketplace"]: r["cnt"] for r in rows}


# ── Job persistence ───────────────────────────────────────────────────────────

def save_job(job_id: str, user_email: str, job_type: str, total: int) -> None:
    """Persist a new job record on creation."""
    conn = get_db()
    conn.execute(
        "INSERT INTO jobs (id, user_email, type, status, total) VALUES (?, ?, ?, 'pending', ?)",
        (job_id, user_email or "", job_type, total),
    )
    conn.commit()
    conn.close()


def update_job_db(job_id: str, status: str = None, progress: int = None,
                  result_json: str = None, error: str = None) -> None:
    """Update a job's state in the DB."""
    parts, vals = [], []
    if status is not None:
        parts.append("status = ?"); vals.append(status)
    if progress is not None:
        parts.append("progress = ?"); vals.append(progress)
    if result_json is not None:
        parts.append("result_json = ?"); vals.append(result_json)
    if error is not None:
        parts.append("error = ?"); vals.append(error)
    if not parts:
        return
    parts.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(job_id)
    conn = get_db()
    conn.execute(f"UPDATE jobs SET {', '.join(parts)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def load_recent_jobs(limit: int = 200) -> dict:
    """Return dict keyed by job_id for the most recent jobs (startup restore)."""
    import json as _json
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    result = {}
    for row in rows:
        d = dict(row)
        raw = d.pop("result_json", None)
        if raw:
            try:
                d["result"] = _json.loads(raw)
            except Exception:
                pass
        result[d["id"]] = d
    return result
