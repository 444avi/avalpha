from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from avalpha import watchlist
from avalpha.db import connect, utcnow
from avalpha.digest.build import _price_action, _reddit_stats, _scored_items, _window

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "avalpha" / "digest"


def test_window_uses_last_digest(tmp_path):
    conn = connect(tmp_path / "t.db")
    now = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    start, end = _window(conn, now)
    assert start == "2026-08-02T13:00:00Z"  # first run: trailing 48h
    conn.execute(
        "INSERT INTO digests (date, built_at, pdf_path) VALUES "
        "('2026-08-03', '2026-08-03T13:00:00Z', 'x.pdf')"
    )
    start, end = _window(conn, now)
    assert start == "2026-08-03T13:00:00Z"
    assert end == "2026-08-04T13:00:00Z"


def test_price_action(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO prices (ticker, date, close) VALUES ('NVDA', '2026-08-03', 110)")
    conn.execute("INSERT INTO prices (ticker, date, close) VALUES ('NVDA', '2026-07-31', 100)")
    close, pct = _price_action(conn, "NVDA", "2026-08-03")
    assert close == 110 and round(pct, 1) == 10.0
    # Label date before any data -> nothing.
    assert _price_action(conn, "NVDA", "2026-07-01") == (None, None)


def test_reddit_stats(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO reddit_mentions VALUES ('NVDA', '2026-08-04T10:00:00Z', 6)"
    )
    conn.execute(
        "INSERT INTO reddit_mentions VALUES ('NVDA', '2026-08-01T10:00:00Z', 8)"
    )
    count, baseline = _reddit_stats(
        conn, "NVDA", "2026-08-03T13:00:00Z", "2026-08-04T13:00:00Z"
    )
    assert count == 6
    assert round(baseline, 2) == 2.0  # (6+8)/7


def test_scored_items_window_and_order(tmp_path):
    from avalpha.scorer import PROMPT_VERSION

    conn = connect(tmp_path / "t.db")
    for i, (fetched, mat) in enumerate(
        [("2026-08-04T10:00:00Z", 3), ("2026-08-04T11:00:00Z", 7), ("2026-08-01T10:00:00Z", 9)],
        start=1,
    ):
        conn.execute(
            "INSERT INTO items (id, source, url, url_hash, title, fetched_at) "
            "VALUES (?, 'gnews', ?, ?, 't', ?)",
            (i, f"u{i}", f"h{i}", fetched),
        )
        conn.execute(
            "INSERT INTO scores (item_id, ticker, prompt_version, model, materiality,"
            " direction, category, mechanism, summary, raw_json, scored_at) "
            "VALUES (?, 'NVDA', ?, 'm', ?, 'positive', 'earnings', 'mech', 'sum', '{}', ?)",
            (i, PROMPT_VERSION, mat, utcnow()),
        )
    items = _scored_items(conn, "NVDA", "2026-08-03T13:00:00Z", "2026-08-04T13:00:00Z")
    # Out-of-window item (mat 9) excluded; remaining sorted by materiality desc.
    assert [it["materiality"] for it in items] == [7, 3]


def test_template_renders_quiet_day():
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    html = env.get_template("template.html").render(
        label_date="2026-08-03",
        built_at="2026-08-04 13:00",
        cover_text="Quiet day across the portfolio.",
        holdings=[
            {
                "ticker": "NVDA",
                "name": "NVIDIA CORP",
                "close": None,
                "pct": None,
                "direction": "flat",
                "narrative": "",
                "bullets": [],
                "insider_filings": [],
                "reddit_count": 0,
                "reddit_baseline": 0.0,
            }
        ],
    )
    assert "Quiet day — nothing material." in html
    assert "NVDA" in html
    assert "no price data" in html
