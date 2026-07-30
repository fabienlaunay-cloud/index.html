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
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_full_period TEXT DEFAULT ''")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_unsubscribed INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS agency_enabled INTEGER DEFAULT 0")

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
        CREATE TABLE IF NOT EXISTS amazon_templates (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            product_type TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            data_b64 TEXT NOT NULL,
            user_email TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        conn.execute("ALTER TABLE users ADD COLUMN quota_full_period TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN email_unsubscribed INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN agency_enabled INTEGER DEFAULT 0")
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
        CREATE TABLE IF NOT EXISTS amazon_templates (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            product_type TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            data_b64 TEXT NOT NULL,
            user_email TEXT NOT NULL DEFAULT '',
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


def update_generation_listings(batch_id: str, user_email: str, listings: list) -> bool:
    """Replace the stored listings of a history batch (preserves declination
    children and any client-side edits so a restored batch matches the UI)."""
    import json as _json
    conn = get_db()
    _ensure_history_table(conn)
    top = [l for l in listings if not (l.get("parent_sku") and not l.get("is_parent"))]
    product_count = len(top)
    scores = [l.get("seo_score") or 0 for l in top if l.get("seo_score")]
    avg_seo = round(sum(scores) / len(scores)) if scores else 0
    cur = conn.execute(
        "UPDATE generation_history SET listings_json = ?, product_count = ?, "
        "avg_seo_score = ? WHERE id = ? AND user_email = ?",
        (_json.dumps(listings, ensure_ascii=False, default=str),
         product_count, avg_seo, batch_id, user_email),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0



# ── Product catalog ───────────────────────────────────────────────────────────

def _ensure_catalog_extra(conn):
    """Idempotently add status/quantity/price columns to product_catalog."""
    is_pg = bool(os.getenv("DATABASE_URL"))
    for col, typ in (("status", "TEXT DEFAULT ''"), ("quantity", "INTEGER"), ("price", "REAL")):
        try:
            if is_pg:
                conn.execute(f"ALTER TABLE product_catalog ADD COLUMN IF NOT EXISTS {col} {typ}")
            else:
                conn.execute(f"ALTER TABLE product_catalog ADD COLUMN {col} {typ}")
        except Exception:
            pass


def save_catalog_items(user_email: str, marketplace: str, items: list) -> int:
    """Upsert items into product_catalog. Returns count saved."""
    if not items:
        return 0
    conn = get_db()
    _ensure_catalog_extra(conn)
    database_url = os.getenv("DATABASE_URL")
    count = 0
    for item in items:
        sku = item.get("sku", "")
        if not sku:
            continue
        vals = (user_email, marketplace, sku, item.get("asin", ""), item.get("ean", ""),
                item.get("title", ""), (item.get("status") or ""), item.get("quantity"), item.get("price"))
        if database_url:
            conn.execute(
                """
                INSERT INTO product_catalog (user_email, marketplace, sku, asin, ean, title, status, quantity, price, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_email, marketplace, sku) DO UPDATE SET
                    asin = EXCLUDED.asin, ean = EXCLUDED.ean, title = EXCLUDED.title,
                    status = EXCLUDED.status, quantity = EXCLUDED.quantity, price = EXCLUDED.price,
                    synced_at = CURRENT_TIMESTAMP
                """,
                vals,
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO product_catalog (user_email, marketplace, sku, asin, ean, title, status, quantity, price, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                vals,
            )
        count += 1
    conn.commit()
    conn.close()
    return count


def get_catalog_overview(user_email: str) -> dict:
    """Catalog status breakdown across all marketplaces (active/inactive/stock)."""
    conn = get_db()
    _ensure_catalog_extra(conn)
    rows = conn.execute(
        "SELECT status, quantity FROM product_catalog WHERE user_email = ?", (user_email,)
    ).fetchall()
    conn.close()
    total = len(rows)
    active = sum(1 for r in rows if (r["status"] or "").lower() == "active")
    in_stock = sum(1 for r in rows if (r["quantity"] or 0) > 0)
    return {"total": total, "active": active, "inactive": total - active, "in_stock": in_stock}


def get_catalog(user_email: str, marketplace: str) -> list:
    """Return all catalog items for user+marketplace as list of dicts."""
    conn = get_db()
    _ensure_catalog_extra(conn)
    rows = conn.execute(
        "SELECT sku, asin, ean, title, status, quantity, price, synced_at FROM product_catalog "
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


# ── Amazon category templates library ─────────────────────────────────────────

def save_amazon_template(tpl_id: str, label: str, product_type: str,
                         filename: str, data_b64: str, user_email: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO amazon_templates (id, label, product_type, filename, data_b64, user_email) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tpl_id, label, product_type, filename, data_b64, user_email),
    )
    conn.commit()
    conn.close()


def list_amazon_templates() -> list:
    """Metadata only — the data_b64 payload is heavy (1MB+ per template)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, label, product_type, filename, user_email, created_at "
        "FROM amazon_templates ORDER BY label, created_at DESC"
    ).fetchall()
    conn.close()
    # Hide internal/system templates (product_type prefixed with "_", e.g. the
    # generic Listing Loader) from the category picker.
    return [{"id": r["id"], "label": r["label"], "product_type": r["product_type"],
             "filename": r["filename"], "user_email": r["user_email"],
             "created_at": str(r["created_at"])}
            for r in rows if not str(r["product_type"]).startswith("_")]


def get_amazon_template(tpl_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT id, label, product_type, filename, data_b64 FROM amazon_templates WHERE id = ?",
        (tpl_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "label": row["label"], "product_type": row["product_type"],
            "filename": row["filename"], "data_b64": row["data_b64"]}


def get_amazon_template_by_type(product_type: str) -> dict | None:
    """Return the most recent stored template (with data) for a product type."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, label, product_type, filename, data_b64 FROM amazon_templates "
        "WHERE product_type = ? ORDER BY created_at DESC LIMIT 1",
        (product_type,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "label": row["label"], "product_type": row["product_type"],
            "filename": row["filename"], "data_b64": row["data_b64"]}


def amazon_template_exists(product_type: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM amazon_templates WHERE product_type = ? LIMIT 1", (product_type,)
    ).fetchone()
    conn.close()
    return bool(row)


def delete_amazon_template(tpl_id: str) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM amazon_templates WHERE id = ?", (tpl_id,))
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


# ── Public API keys & webhooks ───────────────────────────────────────────────

def _ensure_api_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            prefix TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            revoked INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            url TEXT NOT NULL,
            secret TEXT NOT NULL DEFAULT '',
            events TEXT NOT NULL DEFAULT 'listing.generated',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_status TEXT DEFAULT '',
            last_delivery_at TIMESTAMP
        )
    """)


def create_api_key(key_id: str, user_email: str, key_hash: str, prefix: str, label: str) -> None:
    conn = get_db()
    _ensure_api_tables(conn)
    conn.execute(
        "INSERT INTO api_keys (id, user_email, key_hash, prefix, label) VALUES (?, ?, ?, ?, ?)",
        (key_id, user_email, key_hash, prefix, label),
    )
    conn.commit()
    conn.close()


def list_api_keys(user_email: str) -> list:
    conn = get_db()
    _ensure_api_tables(conn)
    rows = conn.execute(
        "SELECT id, prefix, label, created_at, last_used_at, revoked "
        "FROM api_keys WHERE user_email = ? ORDER BY created_at DESC",
        (user_email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_api_key(key_id: str, user_email: str) -> bool:
    conn = get_db()
    _ensure_api_tables(conn)
    cur = conn.execute(
        "UPDATE api_keys SET revoked = 1 WHERE id = ? AND user_email = ?",
        (key_id, user_email),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def resolve_api_key(key_hash: str):
    """Return the owning user_email for an active key hash, or None."""
    conn = get_db()
    _ensure_api_tables(conn)
    row = conn.execute(
        "SELECT id, user_email FROM api_keys WHERE key_hash = ? AND revoked = 0",
        (key_hash,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return row["user_email"]


def create_webhook(hook_id: str, user_email: str, url: str, secret: str, events: str) -> None:
    conn = get_db()
    _ensure_api_tables(conn)
    conn.execute(
        "INSERT INTO webhooks (id, user_email, url, secret, events) VALUES (?, ?, ?, ?, ?)",
        (hook_id, user_email, url, secret, events),
    )
    conn.commit()
    conn.close()


def list_webhooks(user_email: str) -> list:
    conn = get_db()
    _ensure_api_tables(conn)
    rows = conn.execute(
        "SELECT id, url, events, active, created_at, last_status, last_delivery_at "
        "FROM webhooks WHERE user_email = ? ORDER BY created_at DESC",
        (user_email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_webhook(hook_id: str, user_email: str) -> bool:
    conn = get_db()
    _ensure_api_tables(conn)
    cur = conn.execute(
        "DELETE FROM webhooks WHERE id = ? AND user_email = ?", (hook_id, user_email),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_active_webhooks(user_email: str, event: str) -> list:
    """Active webhooks for this user subscribed to `event` (with secrets)."""
    conn = get_db()
    _ensure_api_tables(conn)
    rows = conn.execute(
        "SELECT id, url, secret, events FROM webhooks WHERE user_email = ? AND active = 1",
        (user_email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows
            if event in [e.strip() for e in (r["events"] or "").split(",") if e.strip()]]


def update_webhook_status(hook_id: str, status: str) -> None:
    conn = get_db()
    _ensure_api_tables(conn)
    conn.execute(
        "UPDATE webhooks SET last_status = ?, last_delivery_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, hook_id),
    )
    conn.commit()
    conn.close()


# ── Feed tokens (pull feeds: Google Merchant, sites) ─────────────────────────

def _ensure_feed_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feed_tokens (
            token TEXT PRIMARY KEY,
            user_email TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_fetched_at TIMESTAMP,
            last_channel TEXT DEFAULT '',
            fetch_count INTEGER NOT NULL DEFAULT 0
        )
    """)


def get_feed_token(user_email: str):
    """Return the user's feed token row, or None if not yet created."""
    conn = get_db()
    _ensure_feed_table(conn)
    row = conn.execute(
        "SELECT token, created_at, last_fetched_at, last_channel, fetch_count "
        "FROM feed_tokens WHERE user_email = ?", (user_email,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_feed_token(user_email: str, token: str) -> None:
    """Create or replace (regenerate) the user's feed token."""
    conn = get_db()
    _ensure_feed_table(conn)
    conn.execute("DELETE FROM feed_tokens WHERE user_email = ?", (user_email,))
    conn.execute(
        "INSERT INTO feed_tokens (token, user_email) VALUES (?, ?)",
        (token, user_email),
    )
    conn.commit()
    conn.close()


def resolve_feed_token(token: str, channel: str = ""):
    """Return the owning user_email for a feed token (and bump fetch stats)."""
    conn = get_db()
    _ensure_feed_table(conn)
    row = conn.execute(
        "SELECT user_email FROM feed_tokens WHERE token = ?", (token,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute(
        "UPDATE feed_tokens SET last_fetched_at = CURRENT_TIMESTAMP, "
        "last_channel = ?, fetch_count = fetch_count + 1 WHERE token = ?",
        (channel, token),
    )
    conn.commit()
    conn.close()
    return row["user_email"]


# ── Native push connectors (Shopify, WooCommerce…) ───────────────────────────

def _ensure_connectors_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS connectors (
            user_email TEXT NOT NULL,
            platform TEXT NOT NULL,
            config_enc TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_push_at TIMESTAMP,
            last_status TEXT DEFAULT '',
            PRIMARY KEY (user_email, platform)
        )
    """)


def set_connector(user_email: str, platform: str, config_enc: str) -> None:
    conn = get_db()
    _ensure_connectors_table(conn)
    conn.execute(
        """INSERT INTO connectors (user_email, platform, config_enc)
           VALUES (?, ?, ?)
           ON CONFLICT(user_email, platform) DO UPDATE SET
               config_enc=EXCLUDED.config_enc""",
        (user_email, platform, config_enc),
    )
    conn.commit()
    conn.close()


def get_connector(user_email: str, platform: str):
    conn = get_db()
    _ensure_connectors_table(conn)
    row = conn.execute(
        "SELECT config_enc, last_push_at, last_status FROM connectors "
        "WHERE user_email = ? AND platform = ?", (user_email, platform),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_connectors(user_email: str) -> list:
    conn = get_db()
    _ensure_connectors_table(conn)
    rows = conn.execute(
        "SELECT platform, created_at, last_push_at, last_status FROM connectors "
        "WHERE user_email = ? ORDER BY platform", (user_email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_connector(user_email: str, platform: str) -> bool:
    conn = get_db()
    _ensure_connectors_table(conn)
    cur = conn.execute(
        "DELETE FROM connectors WHERE user_email = ? AND platform = ?",
        (user_email, platform),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def update_connector_status(user_email: str, platform: str, status: str) -> None:
    conn = get_db()
    _ensure_connectors_table(conn)
    conn.execute(
        "UPDATE connectors SET last_status = ?, last_push_at = CURRENT_TIMESTAMP "
        "WHERE user_email = ? AND platform = ?",
        (status, user_email, platform),
    )
    conn.commit()
    conn.close()


# ── Catalog health snapshots (conformity score over time) ────────────────────

def _ensure_health_snapshots(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS health_snapshots (
            user_email TEXT NOT NULL,
            snap_date DATE NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            compliant INTEGER NOT NULL DEFAULT 0,
            non_compliant INTEGER NOT NULL DEFAULT 0,
            critical_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_email, snap_date)
        )
    """)


def record_health_snapshot(user_email: str, report: dict) -> None:
    """Store one health point per day (last scan of the day wins). Skips empty
    catalogs so the curve only reflects real data."""
    if not report or not report.get("total"):
        return
    conn = get_db()
    _ensure_health_snapshots(conn)
    is_pg = bool(os.getenv("DATABASE_URL"))
    today = "CURRENT_DATE" if is_pg else "date('now')"
    conn.execute(
        f"""INSERT INTO health_snapshots
            (user_email, snap_date, score, total, compliant, non_compliant, critical_count, warning_count)
            VALUES (?, {today}, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_email, snap_date) DO UPDATE SET
                score=excluded.score, total=excluded.total, compliant=excluded.compliant,
                non_compliant=excluded.non_compliant, critical_count=excluded.critical_count,
                warning_count=excluded.warning_count, created_at=CURRENT_TIMESTAMP""",
        (user_email, report.get("score", 0), report.get("total", 0),
         report.get("compliant", 0), report.get("non_compliant", 0),
         report.get("critical_count", 0), report.get("warning_count", 0)),
    )
    conn.commit()
    conn.close()


def get_health_history(user_email: str, limit: int = 30) -> list:
    """Return the health snapshots oldest→newest for charting."""
    conn = get_db()
    _ensure_health_snapshots(conn)
    rows = conn.execute(
        "SELECT snap_date, score, total, non_compliant, critical_count "
        "FROM health_snapshots WHERE user_email = ? ORDER BY snap_date DESC LIMIT ?",
        (user_email, limit),
    ).fetchall()
    conn.close()
    out = [{"date": str(r["snap_date"]), "score": r["score"], "total": r["total"],
            "non_compliant": r["non_compliant"], "critical": r["critical_count"]} for r in rows]
    out.reverse()  # oldest first
    return out


# ── Agency workspaces (multi-client isolation) ───────────────────────────────
# A workspace id (ws_…) is used AS the user_email data-key for every content
# table, so all existing queries become workspace-scoped for free. Usage/quota
# resolves back to the owner (see app/services/usage.py).

def _ensure_workspaces(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            owner_email TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def create_workspace(ws_id: str, owner_email: str, name: str) -> None:
    conn = get_db()
    _ensure_workspaces(conn)
    conn.execute("INSERT INTO workspaces (id, owner_email, name) VALUES (?, ?, ?)",
                 (ws_id, owner_email, name))
    conn.commit()
    conn.close()


def list_workspaces(owner_email: str) -> list:
    conn = get_db()
    _ensure_workspaces(conn)
    rows = conn.execute(
        "SELECT id, name, created_at FROM workspaces WHERE owner_email = ? ORDER BY created_at ASC",
        (owner_email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def workspace_owner(ws_id: str):
    """Owner email for a workspace id, or None. Used to authorize scoping and to
    attribute usage back to the agency account."""
    if not ws_id:
        return None
    conn = get_db()
    _ensure_workspaces(conn)
    row = conn.execute("SELECT owner_email FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
    conn.close()
    return row["owner_email"] if row else None


def rename_workspace(ws_id: str, owner_email: str, name: str) -> bool:
    conn = get_db()
    _ensure_workspaces(conn)
    cur = conn.execute("UPDATE workspaces SET name = ? WHERE id = ? AND owner_email = ?",
                       (name, ws_id, owner_email))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# Content tables keyed by user_email that a workspace owns — cascaded on delete.
_WORKSPACE_DATA_TABLES = (
    "generation_history", "product_catalog", "tracked_listings", "performance_snapshots",
    "health_snapshots", "saved_sessions", "api_keys", "webhooks", "feed_tokens",
    "connectors", "amazon_credentials", "usage", "generated_images",
)


def delete_workspace(ws_id: str, owner_email: str) -> bool:
    conn = get_db()
    _ensure_workspaces(conn)
    row = conn.execute("SELECT id FROM workspaces WHERE id = ? AND owner_email = ?",
                       (ws_id, owner_email)).fetchone()
    if not row:
        conn.close()
        return False
    for table in _WORKSPACE_DATA_TABLES:
        try:
            conn.execute(f"DELETE FROM {table} WHERE user_email = ?", (ws_id,))
        except Exception:
            pass  # table may not exist yet
    conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
    conn.commit()
    conn.close()
    return True


# ── Competitor price watch (scoped by user_email → per-workspace) ─────────────

def _ensure_competitors(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS competitors (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            asin TEXT NOT NULL,
            marketplace TEXT NOT NULL DEFAULT 'amazon_fr',
            label TEXT DEFAULT '',
            my_sku TEXT DEFAULT '',
            last_price REAL,
            last_title TEXT DEFAULT '',
            last_rating REAL,
            last_checked_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS competitor_snapshots (
            competitor_id TEXT NOT NULL,
            user_email TEXT NOT NULL,
            snap_date DATE NOT NULL,
            price REAL,
            PRIMARY KEY (competitor_id, snap_date)
        )
    """)


def add_competitor(cid: str, user_email: str, asin: str, marketplace: str, label: str, my_sku: str) -> None:
    conn = get_db()
    _ensure_competitors(conn)
    conn.execute(
        "INSERT INTO competitors (id, user_email, asin, marketplace, label, my_sku) VALUES (?, ?, ?, ?, ?, ?)",
        (cid, user_email, asin.upper(), marketplace, label, my_sku),
    )
    conn.commit()
    conn.close()


def list_competitors(user_email: str) -> list:
    conn = get_db()
    _ensure_competitors(conn)
    rows = conn.execute(
        "SELECT id, asin, marketplace, label, my_sku, last_price, last_title, last_rating, last_checked_at "
        "FROM competitors WHERE user_email = ? ORDER BY created_at ASC", (user_email,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_competitor(cid: str, user_email: str) -> bool:
    conn = get_db()
    _ensure_competitors(conn)
    cur = conn.execute("DELETE FROM competitors WHERE id = ? AND user_email = ?", (cid, user_email))
    conn.execute("DELETE FROM competitor_snapshots WHERE competitor_id = ? AND user_email = ?", (cid, user_email))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def update_competitor_price(cid: str, user_email: str, price, title: str, rating) -> None:
    conn = get_db()
    _ensure_competitors(conn)
    conn.execute(
        "UPDATE competitors SET last_price = ?, last_title = ?, last_rating = ?, "
        "last_checked_at = CURRENT_TIMESTAMP WHERE id = ? AND user_email = ?",
        (price, title or "", rating, cid, user_email),
    )
    is_pg = bool(os.getenv("DATABASE_URL"))
    today = "CURRENT_DATE" if is_pg else "date('now')"
    if price is not None:
        conn.execute(
            f"INSERT INTO competitor_snapshots (competitor_id, user_email, snap_date, price) "
            f"VALUES (?, ?, {today}, ?) ON CONFLICT(competitor_id, snap_date) DO UPDATE SET price = excluded.price",
            (cid, user_email, price),
        )
    conn.commit()
    conn.close()


# ── Catalog-wide sales (Sales & Traffic per ASIN, matched to the catalog) ─────

def _ensure_catalog_sales(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS catalog_sales (
            user_email TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            asin TEXT NOT NULL,
            units_ordered INTEGER DEFAULT 0,
            revenue REAL DEFAULT 0,
            sessions INTEGER DEFAULT 0,
            conversion_rate REAL DEFAULT 0,
            period_start DATE,
            period_end DATE,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_email, marketplace, asin)
        )
    """)


def save_catalog_sales(user_email: str, marketplace: str, metrics_by_asin: dict,
                       period_start: str, period_end: str) -> int:
    conn = get_db()
    _ensure_catalog_sales(conn)
    is_pg = bool(os.getenv("DATABASE_URL"))
    n = 0
    for asin, m in (metrics_by_asin or {}).items():
        vals = (user_email, marketplace, asin.upper(),
                int(m.get("units_ordered") or 0), float(m.get("revenue") or 0),
                int(m.get("sessions") or 0), float(m.get("conversion_rate") or 0),
                period_start, period_end)
        if is_pg:
            conn.execute(
                """INSERT INTO catalog_sales (user_email, marketplace, asin, units_ordered, revenue,
                       sessions, conversion_rate, period_start, period_end, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_email, marketplace, asin) DO UPDATE SET
                       units_ordered=EXCLUDED.units_ordered, revenue=EXCLUDED.revenue,
                       sessions=EXCLUDED.sessions, conversion_rate=EXCLUDED.conversion_rate,
                       period_start=EXCLUDED.period_start, period_end=EXCLUDED.period_end,
                       synced_at=CURRENT_TIMESTAMP""", vals)
        else:
            conn.execute(
                """INSERT OR REPLACE INTO catalog_sales (user_email, marketplace, asin, units_ordered, revenue,
                       sessions, conversion_rate, period_start, period_end, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""", vals)
        n += 1
    conn.commit()
    conn.close()
    return n


def get_catalog_marketplaces(user_email: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT marketplace FROM product_catalog WHERE user_email = ?", (user_email,)
    ).fetchall()
    conn.close()
    return [r["marketplace"] for r in rows]


def get_catalog_sales_overview(user_email: str) -> dict:
    """Join the synced catalog with catalog-wide Sales & Traffic (by asin) to
    report: how many listings sell, total revenue, and active listings with no
    sales. Returns has_data=False when no sales have been pulled yet."""
    conn = get_db()
    _ensure_catalog_extra(conn)
    _ensure_catalog_sales(conn)
    cat = conn.execute(
        "SELECT marketplace, asin, status FROM product_catalog WHERE user_email = ?", (user_email,)
    ).fetchall()
    sales = conn.execute(
        "SELECT marketplace, asin, units_ordered, revenue FROM catalog_sales WHERE user_email = ?",
        (user_email,),
    ).fetchall()
    conn.close()
    if not sales:
        return {"has_data": False}
    smap = {(s["marketplace"], (s["asin"] or "").upper()): s for s in sales}
    selling = revenue = active_no_sales = matched = 0
    for r in cat:
        key = (r["marketplace"], (r["asin"] or "").upper())
        s = smap.get(key)
        if s:
            matched += 1
            u = s["units_ordered"] or 0
            if u > 0:
                selling += 1
                revenue += s["revenue"] or 0
            elif (r["status"] or "").lower() == "active":
                active_no_sales += 1
        elif (r["status"] or "").lower() == "active":
            active_no_sales += 1
    return {"has_data": True, "selling": selling, "revenue": round(revenue, 2),
            "active_no_sales": active_no_sales, "matched": matched}


def get_catalog_insights(user_email: str) -> dict:
    """Business insights for Santé du catalogue — joins the synced catalog with
    per-ASIN sales. Returns top-revenue fiches, weakest active fiches, stock-outs on
    selling fiches, count of active fiches with no sales, per-marketplace breakdown
    and the count of fiches missing an EAN. has_data flags whether sales are known."""
    conn = get_db()
    _ensure_catalog_extra(conn)
    _ensure_catalog_sales(conn)
    cat = conn.execute(
        "SELECT marketplace, sku, asin, ean, title, status, quantity, price "
        "FROM product_catalog WHERE user_email = ?", (user_email,)
    ).fetchall()
    sales = conn.execute(
        "SELECT marketplace, asin, units_ordered, revenue FROM catalog_sales WHERE user_email = ?",
        (user_email,),
    ).fetchall()
    conn.close()
    smap = {(s["marketplace"], (s["asin"] or "").upper()):
            {"units": s["units_ordered"] or 0, "revenue": s["revenue"] or 0} for s in sales}
    has_sales = bool(sales)
    rows = []
    no_ean = 0
    mkt: dict = {}
    for c in cat:
        status = (c["status"] or "").lower()
        active = status not in ("inactive", "incomplete")
        s = smap.get((c["marketplace"], (c["asin"] or "").upper())) or {}
        units = int(s.get("units") or 0)
        revenue = float(s.get("revenue") or 0)
        qty = c["quantity"]
        item = {"sku": c["sku"] or "", "asin": c["asin"] or "", "title": (c["title"] or "")[:80],
                "marketplace": c["marketplace"], "status": "active" if active else "inactive",
                "price": c["price"], "quantity": qty, "in_stock": bool(qty and qty > 0),
                "units": units, "revenue": round(revenue, 2)}
        rows.append(item)
        if not (c["ean"] or "").strip():
            no_ean += 1
        m = mkt.setdefault(c["marketplace"], {"marketplace": c["marketplace"], "total": 0,
                                              "active": 0, "selling": 0, "revenue": 0.0})
        m["total"] += 1
        if active:
            m["active"] += 1
        if units > 0:
            m["selling"] += 1
            m["revenue"] = round(m["revenue"] + revenue, 2)

    selling = [r for r in rows if r["units"] > 0]
    top_revenue = sorted(selling, key=lambda r: r["revenue"], reverse=True)[:5]
    weakest_active = sorted([r for r in selling if r["status"] == "active"],
                            key=lambda r: r["revenue"])[:5]
    stockout_selling = [r for r in selling if not r["in_stock"]]
    active_no_sales = [r for r in rows if r["status"] == "active" and r["units"] == 0]

    return {
        "has_data": has_sales,
        "top_revenue": top_revenue,
        "weakest_active": weakest_active,
        "stockout_selling": stockout_selling,
        "active_no_sales_count": len(active_no_sales),
        "active_no_sales": active_no_sales[:50],
        "no_ean_count": no_ean,
        "by_marketplace": sorted(mkt.values(), key=lambda m: m["revenue"], reverse=True),
    }


def get_catalog_full_export(user_email: str) -> list:
    """Flat rows for a CSV export: every catalog fiche with status, price, stock and
    its sales (units/revenue) joined by ASIN."""
    ins = get_catalog_insights(user_email)  # reuse the join to stay consistent
    conn = get_db()
    _ensure_catalog_extra(conn)
    _ensure_catalog_sales(conn)
    cat = conn.execute(
        "SELECT marketplace, sku, asin, ean, title, status, quantity, price "
        "FROM product_catalog WHERE user_email = ? ORDER BY marketplace, sku", (user_email,)
    ).fetchall()
    sales = conn.execute(
        "SELECT marketplace, asin, units_ordered, revenue FROM catalog_sales WHERE user_email = ?",
        (user_email,),
    ).fetchall()
    conn.close()
    smap = {(s["marketplace"], (s["asin"] or "").upper()):
            {"units": s["units_ordered"] or 0, "revenue": s["revenue"] or 0} for s in sales}
    out = []
    for c in cat:
        s = smap.get((c["marketplace"], (c["asin"] or "").upper())) or {}
        out.append({
            "marketplace": c["marketplace"], "sku": c["sku"] or "", "asin": c["asin"] or "",
            "ean": c["ean"] or "", "title": c["title"] or "", "status": (c["status"] or ""),
            "quantity": c["quantity"] if c["quantity"] is not None else "",
            "price": c["price"] if c["price"] is not None else "",
            "units_ordered": int(s.get("units") or 0), "revenue": round(float(s.get("revenue") or 0), 2),
        })
    return out


def get_catalog_sales_map(user_email: str) -> dict:
    """Per-ASIN sales for a seller: {(marketplace, ASIN): {units, revenue}}.
    Used to tag each catalog listing with its own sales in the health scan."""
    conn = get_db()
    _ensure_catalog_sales(conn)
    rows = conn.execute(
        "SELECT marketplace, asin, units_ordered, revenue FROM catalog_sales WHERE user_email = ?",
        (user_email,),
    ).fetchall()
    conn.close()
    return {(r["marketplace"], (r["asin"] or "").upper()):
            {"units": r["units_ordered"] or 0, "revenue": r["revenue"] or 0}
            for r in rows}


def get_catalog_rows_by_skus(user_email: str, skus: list) -> list:
    """Fetch catalog rows (sku, asin, ean, title, price, status, marketplace) for
    a set of SKUs — used to load selected listings into the generator."""
    if not skus:
        return []
    conn = get_db()
    _ensure_catalog_extra(conn)
    placeholders = ",".join("?" * len(skus))
    rows = conn.execute(
        f"SELECT sku, asin, ean, title, price, status, marketplace "
        f"FROM product_catalog WHERE user_email = ? AND sku IN ({placeholders})",
        (user_email, *skus),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
