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
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_alert_sent INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_alert_period TEXT DEFAULT ''")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_unsubscribed INTEGER DEFAULT 0")

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
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_listings (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            sku TEXT DEFAULT '',
            asin TEXT DEFAULT '',
            title TEXT DEFAULT '',
            marketplace TEXT DEFAULT 'amazon_fr',
            published_at DATE,
            seo_score INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performance_snapshots (
            id SERIAL PRIMARY KEY,
            listing_id TEXT NOT NULL,
            user_email TEXT NOT NULL,
            snapshot_date DATE NOT NULL,
            period_label TEXT DEFAULT '',
            sessions INTEGER DEFAULT 0,
            page_views INTEGER DEFAULT 0,
            units_ordered INTEGER DEFAULT 0,
            conversion_rate REAL DEFAULT 0,
            revenue REAL DEFAULT 0,
            keyword TEXT DEFAULT '',
            keyword_rank INTEGER,
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS saved_sessions (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            data_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS generated_images (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            sku TEXT NOT NULL,
            slot TEXT NOT NULL,
            data_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ab_experiments (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            sku TEXT NOT NULL DEFAULT '',
            asin TEXT NOT NULL DEFAULT '',
            marketplace TEXT NOT NULL DEFAULT 'amazon_fr',
            name TEXT NOT NULL DEFAULT '',
            variant_a_json TEXT NOT NULL DEFAULT '{}',
            variant_b_json TEXT NOT NULL DEFAULT '{}',
            amazon_experiment_id TEXT DEFAULT '',
            amazon_status TEXT DEFAULT 'draft',
            winner TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS google_drive_tokens (
            user_email TEXT PRIMARY KEY,
            refresh_token TEXT NOT NULL,
            drive_email TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS google_oauth_states (
            state TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrate existing tables: add columns added after initial deployment
    conn.execute("ALTER TABLE saved_sessions ADD COLUMN IF NOT EXISTS data_json TEXT NOT NULL DEFAULT '{}'")

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
    try:
        conn.execute("ALTER TABLE users ADD COLUMN quota_alert_sent INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN quota_alert_period TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN email_unsubscribed INTEGER DEFAULT 0")
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
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_listings (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            sku TEXT DEFAULT '',
            asin TEXT DEFAULT '',
            title TEXT DEFAULT '',
            marketplace TEXT DEFAULT 'amazon_fr',
            published_at DATE,
            seo_score INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL,
            user_email TEXT NOT NULL,
            snapshot_date DATE NOT NULL,
            period_label TEXT DEFAULT '',
            sessions INTEGER DEFAULT 0,
            page_views INTEGER DEFAULT 0,
            units_ordered INTEGER DEFAULT 0,
            conversion_rate REAL DEFAULT 0,
            revenue REAL DEFAULT 0,
            keyword TEXT DEFAULT '',
            keyword_rank INTEGER,
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS saved_sessions (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            data_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ab_experiments (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            sku TEXT NOT NULL DEFAULT '',
            asin TEXT NOT NULL DEFAULT '',
            marketplace TEXT NOT NULL DEFAULT 'amazon_fr',
            name TEXT NOT NULL DEFAULT '',
            variant_a_json TEXT NOT NULL DEFAULT '{}',
            variant_b_json TEXT NOT NULL DEFAULT '{}',
            amazon_experiment_id TEXT DEFAULT '',
            amazon_status TEXT DEFAULT 'draft',
            winner TEXT DEFAULT '',
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
    # PostgreSQL: ADD COLUMN IF NOT EXISTS avoids aborting the transaction when
    # the column already exists (unlike bare ALTER TABLE which throws and leaves
    # psycopg2 in an aborted-transaction state for subsequent queries).
    # SQLite: falls back to try/except because it doesn't support IF NOT EXISTS.
    if os.getenv("DATABASE_URL"):
        conn.execute("ALTER TABLE generation_history ADD COLUMN IF NOT EXISTS images_json TEXT NOT NULL DEFAULT '{}'")
    else:
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
    d["listings"] = _json.loads(d.pop("listings_json", None) or "[]")
    d["images"] = _json.loads(d.pop("images_json", None) or "{}")
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
        # Normalize PostgreSQL datetime/None → Unix timestamp float so _cleanup_jobs() can compare
        import time as _time
        for ts_key in ("created_at", "updated_at"):
            val = d.get(ts_key)
            if val is None:
                d[ts_key] = _time.time()
            elif not isinstance(val, (int, float)):
                try:
                    d[ts_key] = val.timestamp()
                except Exception:
                    d[ts_key] = _time.time()
        result[d["id"]] = d
    return result


# ── Performance tracking ──────────────────────────────────────────────────────

def add_tracked_listing(user_email: str, sku: str, asin: str, title: str,
                        marketplace: str, published_at: str, seo_score: int) -> dict:
    import uuid as _uuid
    lid = str(_uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO tracked_listings (id,user_email,sku,asin,title,marketplace,published_at,seo_score) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (lid, user_email, sku, asin, title, marketplace, published_at or None, seo_score),
    )
    conn.commit()
    conn.close()
    return {"id": lid, "sku": sku, "asin": asin, "title": title,
            "marketplace": marketplace, "published_at": published_at, "seo_score": seo_score}


def list_tracked_listings(user_email: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tracked_listings WHERE user_email=? ORDER BY created_at DESC", (user_email,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("published_at") and not isinstance(d["published_at"], str):
            d["published_at"] = str(d["published_at"])
        result.append(d)
    return result


def delete_tracked_listing(listing_id: str, user_email: str) -> bool:
    conn = get_db()
    r = conn.execute("DELETE FROM tracked_listings WHERE id=? AND user_email=?", (listing_id, user_email))
    conn.execute("DELETE FROM performance_snapshots WHERE listing_id=? AND user_email=?", (listing_id, user_email))
    conn.commit()
    conn.close()
    return r.rowcount > 0


def add_snapshot(listing_id: str, user_email: str, snapshot_date: str, period_label: str,
                 sessions: int, page_views: int, units_ordered: int, conversion_rate: float,
                 revenue: float, keyword: str, keyword_rank, notes: str) -> dict:
    conn = get_db()
    conn.execute(
        "INSERT INTO performance_snapshots "
        "(listing_id,user_email,snapshot_date,period_label,sessions,page_views,"
        "units_ordered,conversion_rate,revenue,keyword,keyword_rank,notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (listing_id, user_email, snapshot_date, period_label, sessions, page_views,
         units_ordered, conversion_rate, revenue, keyword, keyword_rank, notes),
    )
    conn.commit()
    conn.close()
    return {"listing_id": listing_id, "snapshot_date": snapshot_date, "period_label": period_label,
            "sessions": sessions, "conversion_rate": conversion_rate, "units_ordered": units_ordered}


def get_snapshots(listing_id: str, user_email: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM performance_snapshots WHERE listing_id=? AND user_email=? ORDER BY snapshot_date ASC",
        (listing_id, user_email),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("snapshot_date") and not isinstance(d["snapshot_date"], str):
            d["snapshot_date"] = str(d["snapshot_date"])
        if d.get("created_at") and not isinstance(d["created_at"], str):
            try: d["created_at"] = d["created_at"].isoformat()
            except Exception: d["created_at"] = str(d["created_at"])
        result.append(d)
    return result


def delete_snapshot(snapshot_id: int, user_email: str) -> bool:
    conn = get_db()
    r = conn.execute("DELETE FROM performance_snapshots WHERE id=? AND user_email=?", (snapshot_id, user_email))
    conn.commit()
    conn.close()
    return r.rowcount > 0


def get_tracking_summary(user_email: str) -> dict:
    """Account-level proof loop: avg conversion improvement across tracked listings."""
    conn = get_db()
    listings = conn.execute(
        "SELECT id FROM tracked_listings WHERE user_email=?", (user_email,)
    ).fetchall()
    conn.close()
    improvements = []
    for row in listings:
        snaps = get_snapshots(row["id"], user_email)
        conv_vals = [s["conversion_rate"] for s in snaps if (s.get("conversion_rate") or 0) > 0]
        if len(conv_vals) >= 2:
            pct = round((conv_vals[-1] - conv_vals[0]) / conv_vals[0] * 100, 1) if conv_vals[0] else 0
            improvements.append(pct)
    return {
        "tracked_count": len(listings),
        "avg_conversion_improvement": round(sum(improvements) / len(improvements), 1) if improvements else None,
        "listings_with_data": len(improvements),
    }


# ── Generated images ──────────────────────────────────────────────────────────

def save_generated_image(user_email: str, sku: str, slot: str, data_url: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO generated_images (id, user_email, sku, slot, data_url) VALUES (?, ?, ?, ?, ?)",
        (f"{user_email}:{sku}:{slot}", user_email, sku, slot, data_url),
    )
    conn.commit()
    conn.close()


def get_generated_images(user_email: str, sku: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT slot, data_url FROM generated_images WHERE user_email = ? AND sku = ?",
        (user_email, sku),
    ).fetchall()
    conn.close()
    return [{"slot": r["slot"], "data_url": r["data_url"]} for r in rows]


def get_generated_images_bulk(user_email: str, skus: list[str]) -> dict[str, list[dict]]:
    if not skus:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(skus))
    rows = conn.execute(
        f"SELECT sku, slot, data_url FROM generated_images WHERE user_email = ? AND sku IN ({placeholders})",
        [user_email] + list(skus),
    ).fetchall()
    conn.close()
    result: dict[str, list] = {}
    for r in rows:
        result.setdefault(r["sku"], []).append({"slot": r["slot"], "data_url": r["data_url"]})
    return result


# ── Saved sessions ────────────────────────────────────────────────────────────

def save_session(session_id: str, user_email: str, name: str, data: dict) -> None:
    import json as _json
    conn = get_db()
    conn.execute(
        "INSERT INTO saved_sessions (id, user_email, name, data_json) VALUES (?, ?, ?, ?)",
        (session_id, user_email, name, _json.dumps(data, ensure_ascii=False, default=str)),
    )
    conn.commit()
    conn.close()


def list_saved_sessions(user_email: str) -> list:
    import json as _json
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, created_at FROM saved_sessions WHERE user_email = ? ORDER BY created_at DESC",
        (user_email,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "created_at": str(r["created_at"])} for r in rows]


def get_saved_session(session_id: str, user_email: str) -> dict | None:
    import json as _json
    conn = get_db()
    row = conn.execute(
        "SELECT id, name, created_at, data_json FROM saved_sessions WHERE id = ? AND user_email = ?",
        (session_id, user_email),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"], "name": row["name"], "created_at": str(row["created_at"]),
        "data": _json.loads(row["data_json"] or "{}"),
    }


def delete_saved_session(session_id: str, user_email: str) -> bool:
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM saved_sessions WHERE id = ? AND user_email = ?",
        (session_id, user_email),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ── A/B Experiments ───────────────────────────────────────────────────────────

def save_ab_experiment(exp_id: str, user_email: str, sku: str, asin: str,
                       marketplace: str, name: str, variant_a: dict, variant_b: dict) -> None:
    import json as _json
    conn = get_db()
    conn.execute(
        "INSERT INTO ab_experiments (id, user_email, sku, asin, marketplace, name, variant_a_json, variant_b_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (exp_id, user_email, sku, asin, marketplace, name,
         _json.dumps(variant_a, ensure_ascii=False, default=str),
         _json.dumps(variant_b, ensure_ascii=False, default=str)),
    )
    conn.commit()
    conn.close()


def list_ab_experiments(user_email: str) -> list:
    import json as _json
    conn = get_db()
    rows = conn.execute(
        "SELECT id, sku, asin, marketplace, name, amazon_experiment_id, amazon_status, winner, created_at "
        "FROM ab_experiments WHERE user_email = ? ORDER BY created_at DESC",
        (user_email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ab_experiment(exp_id: str, user_email: str) -> dict | None:
    import json as _json
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM ab_experiments WHERE id = ? AND user_email = ?",
        (exp_id, user_email),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["variant_a"] = _json.loads(d.pop("variant_a_json", "{}") or "{}")
    d["variant_b"] = _json.loads(d.pop("variant_b_json", "{}") or "{}")
    return d


def update_ab_experiment(exp_id: str, user_email: str, **kwargs) -> bool:
    """Update fields: amazon_experiment_id, amazon_status, winner, asin."""
    allowed = {"amazon_experiment_id", "amazon_status", "winner", "asin", "name"}
    parts, vals = [], []
    for k, v in kwargs.items():
        if k in allowed:
            parts.append(f"{k} = ?")
            vals.append(v)
    if not parts:
        return False
    parts.append("updated_at = CURRENT_TIMESTAMP")
    vals.extend([exp_id, user_email])
    conn = get_db()
    cur = conn.execute(
        f"UPDATE ab_experiments SET {', '.join(parts)} WHERE id = ? AND user_email = ?", vals
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def delete_ab_experiment(exp_id: str, user_email: str) -> bool:
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM ab_experiments WHERE id = ? AND user_email = ?", (exp_id, user_email)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ── Google Drive OAuth helpers ────────────────────────────────────────────────

def get_drive_token(user_email: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT refresh_token, drive_email FROM google_drive_tokens WHERE user_email = ?",
        (user_email,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    if hasattr(row, "keys"):
        return {"refresh_token": row["refresh_token"], "drive_email": row["drive_email"]}
    return {"refresh_token": row[0], "drive_email": row[1]}


def set_drive_token(user_email: str, refresh_token: str, drive_email: str = "") -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO google_drive_tokens (user_email, refresh_token, drive_email, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_email) DO UPDATE SET
               refresh_token=EXCLUDED.refresh_token,
               drive_email=EXCLUDED.drive_email,
               updated_at=CURRENT_TIMESTAMP""",
        (user_email, refresh_token, drive_email),
    )
    conn.commit()
    conn.close()


def delete_drive_token(user_email: str) -> None:
    conn = get_db()
    conn.execute("DELETE FROM google_drive_tokens WHERE user_email = ?", (user_email,))
    conn.commit()
    conn.close()
