from avalpha import scorer, watchlist
from avalpha.db import connect, utcnow


def _setup(conn):
    watchlist.upsert(
        conn,
        ticker="NVDA",
        cik="0001045810",
        legal_name="NVIDIA CORP",
        aliases=["Nvidia"],
        products=[],
        executives=[],
        ir_feed_url=None,
        ir_feed_status="none",
        weight=10,
        shares_outstanding=2_000_000_000,
        enrichment_confidence="high",
    )
    conn.execute(
        "INSERT INTO items (id, source, url, url_hash, title, raw_text, fetched_at)"
        " VALUES (1, 'gnews', 'u', 'h', 'Nvidia news', 'body', ?)",
        (utcnow(),),
    )
    conn.execute(
        "INSERT INTO item_matches (item_id, ticker, method, confirmed, matched_at)"
        " VALUES (1, 'NVDA', 'confirm', 1, ?)",
        (utcnow(),),
    )
    conn.commit()


def test_pending_only_confirmed_and_unscored(tmp_path):
    conn = connect(tmp_path / "t.db")
    _setup(conn)
    # An unconfirmed match must not be scored.
    conn.execute(
        "INSERT INTO items (id, source, url, url_hash, title, fetched_at)"
        " VALUES (2, 'gnews', 'u2', 'h2', 't2', ?)",
        (utcnow(),),
    )
    conn.execute(
        "INSERT INTO item_matches (item_id, ticker, method, confirmed, matched_at)"
        " VALUES (2, 'NVDA', 'confirm', 0, ?)",
        (utcnow(),),
    )
    pending = scorer._pending(conn, 100)
    assert [(r["item_id"], r["ticker"]) for r in pending] == [(1, "NVDA")]

    # Scoring at the current version removes it from the queue...
    conn.execute(
        "INSERT INTO scores (item_id, ticker, prompt_version, model, materiality,"
        " direction, category, mechanism, summary, raw_json, scored_at)"
        " VALUES (1, 'NVDA', ?, 'm', 3, 'unclear', 'other', 'x', 's', '{}', ?)",
        (scorer.PROMPT_VERSION, utcnow()),
    )
    assert scorer._pending(conn, 100) == []

    # ...but a score at an *old* version does not satisfy the current one.
    conn.execute("DELETE FROM scores")
    conn.execute(
        "INSERT INTO scores (item_id, ticker, prompt_version, model, materiality,"
        " direction, category, mechanism, summary, raw_json, scored_at)"
        " VALUES (1, 'NVDA', 'v0-old', 'm', 3, 'unclear', 'other', 'x', 's', '{}', ?)",
        (utcnow(),),
    )
    assert len(scorer._pending(conn, 100)) == 1


def test_market_cap_string(tmp_path):
    conn = connect(tmp_path / "t.db")
    _setup(conn)
    holding = watchlist.get(conn, "NVDA")
    assert scorer._market_cap_str(conn, holding) == "unknown"  # no prices yet
    conn.execute(
        "INSERT INTO prices (ticker, date, close) VALUES ('NVDA', '2026-08-03', 150.0)"
    )
    assert scorer._market_cap_str(conn, holding) == "$300.0B"


def test_schema_matches_spec_shape():
    props = scorer.SCORE_SCHEMA["properties"]
    assert set(scorer.SCORE_SCHEMA["required"]) == {
        "ticker",
        "materiality",
        "direction",
        "category",
        "mechanism",
        "summary",
    }
    assert props["materiality"]["enum"] == list(range(11))
    assert "M&A" in props["category"]["enum"]
