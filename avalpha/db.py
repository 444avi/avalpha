"""SQLite access. WAL mode, migrations via PRAGMA user_version."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3
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
