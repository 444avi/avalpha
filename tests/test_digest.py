from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from avalpha import calendar_outcomes, watchlist
from avalpha.calendar_store import Event, earnings_key, macro_key, upsert_event
from avalpha.config import Config
from avalpha.db import connect, utcnow
from avalpha.digest.build import (
    _earnings_in_window,
    _macro_events,
    _price_action,
    _reddit_stats,
    _scored_items,
    _window,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "avalpha" / "digest"
WIN = ("2026-08-04T00:00:00Z", "2026-08-05T00:00:00Z")


def _cfg(tmp_path) -> Config:
    return Config(db_path=tmp_path / "t.db", digest_dir=tmp_path,
                  email_recipient="", email_sender="")


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


def test_macro_events_in_window_enriched(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    upsert_event(conn, Event(
        kind="fomc", ticker=None, title="FOMC decision", event_date="2026-08-04",
        event_at="2026-08-04T18:00:00Z", tz="America/New_York", is_timed=True,
        status="passed", source="fred", dedup_key=macro_key("fomc", "2026-08-04")))
    conn.commit()
    monkeypatch.setenv("FRED_API_KEY", "k")
    monkeypatch.setattr(calendar_outcomes, "macro_outcome", lambda kind, key: {
        "kind": kind, "label": "FOMC decision",
        "lines": ["Fed funds target cut 25 bps to 3.50–3.75%"]})
    monkeypatch.setattr(calendar_outcomes, "macro_consensus", lambda *a: None)

    events = _macro_events(_cfg(tmp_path), conn, *WIN)
    assert len(events) == 1
    assert events[0]["label"] == "FOMC decision"
    assert "cut 25 bps" in events[0]["lines"][0]
    assert events[0]["consensus"] is None


def test_macro_events_skips_out_of_window(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    upsert_event(conn, Event(
        kind="cpi", ticker=None, title="CPI", event_date="2026-07-20",
        event_at="2026-07-20T12:30:00Z", tz="America/New_York", is_timed=True,
        status="passed", source="fred", dedup_key=macro_key("cpi", "2026-07-20")))
    conn.commit()
    monkeypatch.setenv("FRED_API_KEY", "k")
    monkeypatch.setattr(calendar_outcomes, "macro_outcome",
                        lambda kind, key: {"kind": kind, "label": "CPI", "lines": ["x"]})
    assert _macro_events(_cfg(tmp_path), conn, *WIN) == []


def test_macro_events_empty_without_fred_key(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert _macro_events(_cfg(tmp_path), conn, *WIN) == []


def test_earnings_in_window_returns_beat(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    upsert_event(conn, Event(
        kind="earnings", ticker="NVDA", title="NVDA earnings — 2026Q3",
        event_date="2026-08-04", event_at="2026-08-04T20:05:00Z", tz="America/New_York",
        is_timed=True, status="confirmed", source="finnhub", fiscal_period="2026Q3",
        dedup_key=earnings_key("NVDA", "2026Q3")))
    conn.commit()
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    seen = {}
    monkeypatch.setattr(calendar_outcomes, "earnings_outcome",
                        lambda t, k, fp=None: seen.update(fp=fp) or {"beat": True, "eps_actual": 2.2})
    out = _earnings_in_window(_cfg(tmp_path), conn, "NVDA", *WIN)
    assert out["beat"] is True
    assert seen["fp"] == "2026Q3"  # fiscal period passed through for the right quarter


def test_earnings_in_window_none_when_no_event(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    assert _earnings_in_window(_cfg(tmp_path), conn, "NVDA", *WIN) is None


def test_template_renders_macro_and_earnings():
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    html = env.get_template("template.html").render(
        label_date="2026-08-04", built_at="2026-08-05 13:00",
        cover_text="Portfolio note.",
        macro_analysis="Rates came down; supportive for the book.",
        macro_events=[{"label": "FOMC decision",
                       "lines": ["Fed funds target cut 25 bps to 3.50–3.75%"],
                       "consensus": None}],
        holdings=[{
            "ticker": "NVDA", "name": "NVIDIA CORP", "close": 110.0, "pct": 2.0,
            "direction": "up", "narrative": "n", "bullets": [], "insider_filings": [],
            "reddit_count": 0, "reddit_baseline": 0.0,
            "earnings": {"period": "2026-09-30", "eps_actual": 2.22,
                         "eps_estimate": 2.14, "surprise_pct": 3.8, "beat": True},
        }])
    assert "What happened — macro" in html
    assert "cut 25 bps to 3.50" in html
    assert "Rates came down" in html
    assert "EPS 2.22 vs 2.14 est" in html
    assert "beat +3.8%" in html


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
