"""SQLite access. WAL mode, migrations via PRAGMA user_version."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4
_SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema.sql"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Schema v3: the calendar_events table + watchlist.industry column."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            id            INTEGER PRIMARY KEY,
            ticker        TEXT,
            kind          TEXT NOT NULL,
            title         TEXT NOT NULL,
            event_date    TEXT NOT NULL,
            event_at      TEXT,
            tz            TEXT,
            is_timed      INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'scheduled'
                            CHECK (status IN ('scheduled','confirmed','tentative','passed','cancelled')),
            source        TEXT NOT NULL,
            source_ref    TEXT,
            confidence    TEXT CHECK (confidence IN ('high','medium','low')),
            fiscal_period TEXT,
            dedup_key     TEXT NOT NULL UNIQUE,
            meta_json     TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_calendar_date   ON calendar_events (event_date);
        CREATE INDEX IF NOT EXISTS idx_calendar_ticker ON calendar_events (ticker, event_date);
        """
    )
    # Guarded ALTER: SQLite has no ADD COLUMN IF NOT EXISTS.
    if not _column_exists(conn, "watchlist", "industry"):
        conn.execute("ALTER TABLE watchlist ADD COLUMN industry TEXT")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Schema v4: users and strictly scoped, one-owner portfolios."""
    now = utcnow()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY,
            email         TEXT NOT NULL COLLATE NOCASE UNIQUE,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE IF NOT EXISTS portfolios (
            id            INTEGER PRIMARY KEY,
            owner_user_id INTEGER NOT NULL UNIQUE REFERENCES users (id),
            name          TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO users (email, is_admin, created_at) VALUES (?, 1, ?)",
        ("avi@arboretuminvestments.net", now),
    )
    conn.execute(
        "UPDATE users SET is_admin = 1 WHERE email = ?",
        ("avi@arboretuminvestments.net",),
    )
    avi_id = conn.execute(
        "SELECT id FROM users WHERE email = ?", ("avi@arboretuminvestments.net",)
    ).fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO portfolios (owner_user_id, name) VALUES (?, ?)",
        (avi_id, "Avi's Portfolio"),
    )
    avi_portfolio_id = conn.execute(
        "SELECT id FROM portfolios WHERE owner_user_id = ?", (avi_id,)
    ).fetchone()[0]

    # Rebuild the old per-position watchlist as a global security catalog.
    # Copy its position fields into Avi's portfolio before dropping them.
    if _column_exists(conn, "watchlist", "weight"):
        conn.executescript(
            """
            ALTER TABLE watchlist RENAME TO watchlist_v3;
            CREATE TABLE watchlist (
                ticker                TEXT PRIMARY KEY,
                cik                   TEXT NOT NULL,
                legal_name            TEXT NOT NULL,
                aliases_json          TEXT NOT NULL DEFAULT '[]',
                products_json         TEXT NOT NULL DEFAULT '[]',
                executives_json       TEXT NOT NULL DEFAULT '[]',
                ir_feed_url           TEXT,
                ir_feed_status        TEXT NOT NULL DEFAULT 'none'
                                          CHECK (ir_feed_status IN ('ok', 'none')),
                shares_outstanding    INTEGER,
                enrichment_confidence TEXT
                                          CHECK (enrichment_confidence IN ('high', 'medium', 'low')),
                enriched_at           TEXT,
                industry              TEXT
            );
            INSERT INTO watchlist (
                ticker, cik, legal_name, aliases_json, products_json,
                executives_json, ir_feed_url, ir_feed_status,
                shares_outstanding, enrichment_confidence, enriched_at, industry
            )
            SELECT ticker, cik, legal_name, aliases_json, products_json,
                   executives_json, ir_feed_url, ir_feed_status,
                   shares_outstanding, enrichment_confidence, enriched_at, industry
            FROM watchlist_v3;
            """
        )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS portfolio_holdings (
            portfolio_id   INTEGER NOT NULL REFERENCES portfolios (id),
            ticker         TEXT NOT NULL REFERENCES watchlist (ticker),
            weight         REAL NOT NULL DEFAULT 0,
            active         INTEGER NOT NULL DEFAULT 1,
            added_at       TEXT NOT NULL,
            deactivated_at TEXT,
            PRIMARY KEY (portfolio_id, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_holdings_active
            ON portfolio_holdings (portfolio_id, active, ticker);
        """
    )
    if _table_exists(conn, "watchlist_v3"):
        conn.execute(
            """
            INSERT OR IGNORE INTO portfolio_holdings
                (portfolio_id, ticker, weight, active, added_at, deactivated_at)
            SELECT ?, ticker, weight, active, added_at, deactivated_at
            FROM watchlist_v3
            """,
            (avi_portfolio_id,),
        )
        conn.execute("DROP TABLE watchlist_v3")

    if not _column_exists(conn, "calendar_events", "portfolio_id"):
        conn.execute(
            "ALTER TABLE calendar_events ADD COLUMN portfolio_id INTEGER "
            "REFERENCES portfolios(id)"
        )
    conn.execute(
        "UPDATE calendar_events SET portfolio_id = ? "
        "WHERE source = 'manual' AND portfolio_id IS NULL",
        (avi_portfolio_id,),
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_portfolio "
        "ON calendar_events (portfolio_id, event_date)"
    )

    if not _column_exists(conn, "web_jobs", "portfolio_id"):
        conn.execute(
            "ALTER TABLE web_jobs ADD COLUMN portfolio_id INTEGER "
            "REFERENCES portfolios(id)"
        )

    # SQLite cannot replace a primary key in place, so rebuild the date-keyed
    # digest archive and assign all historical PDFs to Avi.
    digest_pk = [
        r["name"]
        for r in conn.execute("PRAGMA table_info(digests)")
        if r["pk"]
    ]
    if digest_pk == ["date"]:
        conn.executescript(
            """
            ALTER TABLE digests RENAME TO digests_v3;
            CREATE TABLE digests (
                portfolio_id INTEGER NOT NULL REFERENCES portfolios (id),
                date         TEXT NOT NULL,
                built_at     TEXT NOT NULL,
                sent_at      TEXT,
                pdf_path     TEXT NOT NULL,
                PRIMARY KEY (portfolio_id, date)
            );
            """
        )
        conn.execute(
            "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) "
            "SELECT ?, date, built_at, sent_at, pdf_path FROM digests_v3",
            (avi_portfolio_id,),
        )
        conn.execute("DROP TABLE digests_v3")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


# Incremental migrations keyed by the version they upgrade *to*. Each is applied
# in order for DBs older than SCHEMA_VERSION. Fresh DBs get the full schema.sql
# (already at SCHEMA_VERSION) and skip these. A value is either an idempotent SQL
# script or a callable(conn) for steps that need Python (e.g. guarded ALTERs).
_MIGRATIONS: dict[int, "str | object"] = {
    2: """
        CREATE TABLE IF NOT EXISTS web_jobs (
            id           INTEGER PRIMARY KEY,
            job          TEXT NOT NULL,
            status       TEXT NOT NULL,
            triggered_by TEXT,
            started_at   TEXT NOT NULL,
            finished_at  TEXT,
            output       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_web_jobs_started ON web_jobs (started_at);
    """,
    3: _migrate_v3,
    4: _migrate_v4,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version == 0:
        # Fresh DB: schema.sql is authored at the current SCHEMA_VERSION.
        conn.executescript(_SCHEMA_FILE.read_text())
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return
    # Existing DB: apply each incremental step above `version`, bumping the
    # user_version after each so a crash mid-upgrade resumes cleanly.
    for target in range(version + 1, SCHEMA_VERSION + 1):
        migration = _MIGRATIONS.get(target)
        if migration is None:
            raise RuntimeError(f"no migration to schema version {target}")
        if callable(migration):
            migration(conn)
        else:
            conn.executescript(migration)
        conn.execute(f"PRAGMA user_version = {target}")
        conn.commit()
