import sqlite3

from avalpha.db import SCHEMA_VERSION, connect, utcnow


def test_schema_applies_and_wal(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "watchlist",
        "items",
        "item_matches",
        "scores",
        "reddit_mentions",
        "prices",
        "collector_runs",
        "digests",
        "calendar_events",
    } <= tables
    # schema v3 added the bio-gate column
    wl_cols = {r[1] for r in conn.execute("PRAGMA table_info(watchlist)")}
    assert "industry" in wl_cols


def test_reconnect_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_swing_schema_v5_fresh_db(tmp_path):
    conn = connect(tmp_path / "t.db")
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "swing_alerts_sent" in tables
    portfolio_cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolios)")}
    assert "swing_alerts_enabled" in portfolio_cols


def test_v4_to_v5_migration_adds_swing_state(tmp_path):
    """An existing v4 DB gains the opt-in column (default off) and audit table."""
    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE portfolios (
            id INTEGER PRIMARY KEY,
            owner_user_id INTEGER NOT NULL UNIQUE REFERENCES users (id),
            name TEXT NOT NULL
        );
        INSERT INTO users (id, email, created_at) VALUES (1, 'v4@example.com', 'x');
        INSERT INTO portfolios (id, owner_user_id, name) VALUES (7, 1, 'Legacy');
        PRAGMA user_version = 4;
        """
    )
    conn.close()

    migrated = connect(path)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # The pre-existing portfolio survives and defaults to opted-out.
    assert migrated.execute(
        "SELECT swing_alerts_enabled FROM portfolios WHERE id = 7"
    ).fetchone()[0] == 0
    assert migrated.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='swing_alerts_sent'"
    ).fetchone() is not None


def test_item_url_hash_dedup(tmp_path):
    conn = connect(tmp_path / "t.db")
    row = ("gnews", "id1", "https://x/a", "hash1", "title", "", None, utcnow(), "{}")
    conn.execute(
        "INSERT INTO items (source, source_id, url, url_hash, title, raw_text,"
        " published_at, fetched_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        row,
    )
    try:
        conn.execute(
            "INSERT INTO items (source, source_id, url, url_hash, title, raw_text,"
            " published_at, fetched_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
        assert False, "duplicate url_hash should be rejected"
    except sqlite3.IntegrityError:
        pass


def test_scores_append_only_per_prompt_version(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO items (id, source, url, url_hash, title, fetched_at)"
        " VALUES (1, 'edgar', 'u', 'h', 't', ?)",
        (utcnow(),),
    )
    for version in ("v1", "v2"):
        conn.execute(
            "INSERT INTO scores (item_id, ticker, prompt_version, model, materiality,"
            " direction, category, mechanism, summary, raw_json, scored_at)"
            " VALUES (1, 'NVDA', ?, 'm', 5, 'positive', 'earnings', 'x', 's', '{}', ?)",
            (version, utcnow()),
        )
    count = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert count == 2
