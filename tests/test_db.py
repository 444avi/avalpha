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
    } <= tables


def test_reconnect_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


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
