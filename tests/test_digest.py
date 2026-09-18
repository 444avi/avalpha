from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from avalpha import calendar_outcomes, watchlist
from avalpha.calendar_store import Event, earnings_key, macro_key, upsert_event
from avalpha.config import Config
from avalpha.db import connect, utcnow
from avalpha.digest.build import (
    _digest_date,
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


def test_window_uses_last_sent_digest(tmp_path):
    conn = connect(tmp_path / "t.db")
    now = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    start, end = _window(conn, now)
    assert start == "2026-08-02T13:00:00Z"  # first run: trailing 48h
    portfolio_id = conn.execute("SELECT id FROM portfolios").fetchone()[0]
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) VALUES "
        "(?, '2026-08-03', '2026-08-03T13:00:00Z', '2026-08-03T13:00:00Z', 'x.pdf')",
        (portfolio_id,),
    )
    start, end = _window(conn, now)
    assert start == "2026-08-03T13:00:00Z"
    assert end == "2026-08-04T13:00:00Z"


def test_window_ignores_unsent_preview_build(tmp_path):
    """A preview/rebuild that never got sent must not shrink the next window —
    otherwise it silently drops items and macro releases that fell before it
    (the two-user PPI divergence)."""
    conn = connect(tmp_path / "t.db")
    now = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    portfolio_id = conn.execute("SELECT id FROM portfolios").fetchone()[0]
    # Yesterday's digest was actually sent — the real high-water mark.
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) VALUES "
        "(?, '2026-08-02', '2026-08-03T13:00:00Z', '2026-08-03T13:00:00Z', 'y.pdf')",
        (portfolio_id,),
    )
    # An unsent preview built this morning, after a macro release (~12:30Z).
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, pdf_path) VALUES "
        "(?, '2026-08-03', '2026-08-04T12:50:00Z', 'preview.pdf')",
        (portfolio_id,),
    )
    start, end = _window(conn, now)
    assert start == "2026-08-03T13:00:00Z"  # anchored on the sent digest, not the preview
    assert end == "2026-08-04T13:00:00Z"


def test_window_rebuild_ignores_todays_own_sent_edition(tmp_path):
    """Rebuilding today's digest (the console "digest" button, a manual re-run)
    must anchor on the *previous* edition, never on today's own just-sent row.
    Anchoring on today collapsed the window to minutes, so `_macro_events` came
    back empty and the whole "what happened — macro" section dropped out of the
    regenerated PDF — which overwrites the delivered one on disk. This is the
    since-last-sent sibling of the two-user macro divergence."""
    conn = connect(tmp_path / "t.db")
    now = datetime(2026, 9, 17, 13, 5, tzinfo=timezone.utc)
    portfolio_id = conn.execute("SELECT id FROM portfolios").fetchone()[0]
    # Yesterday's sent digest — the real prior coverage boundary.
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) VALUES "
        "(?, '2026-09-16', '2026-09-16T13:00:00Z', '2026-09-16T13:00:05Z', 'y.pdf')",
        (portfolio_id,),
    )
    # Today's digest, already sent at 13:00. The FOMC print (~09-16 18:00Z) landed
    # inside the 09-16 -> 09-17 window this edition used and rode out in the email.
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) VALUES "
        "(?, '2026-09-17', '2026-09-17T13:00:00Z', '2026-09-17T13:00:04Z', 't.pdf')",
        (portfolio_id,),
    )
    start, end = _window(conn, now, portfolio_id, label_date="2026-09-17")
    # Previous edition, not today's send — so the rebuild reproduces the FOMC-
    # covering window rather than a 5-minute one that would drop the macro block.
    assert start == "2026-09-16T13:00:00Z"
    assert end == "2026-09-17T13:05:00Z"


def test_macro_window_is_fund_wide_and_excludes_todays_edition(tmp_path):
    """Macro is market-wide, so its window must be identical for every recipient
    and independent of any single portfolio's send timing — the root fix for
    members getting different macro coverage edition after edition. It anchors on
    the most recent *sent* edition across the whole fund, never on today's own."""
    from avalpha import accounts
    from avalpha.digest.build import _macro_window

    conn = connect(tmp_path / "t.db")
    now = datetime(2026, 9, 17, 13, 5, tzinfo=timezone.utc)
    avi = accounts.resolve_login(conn, accounts.ADMIN_EMAIL)
    bob = accounts.resolve_login(conn, "bob@example.com")
    # Yesterday: both members' editions were sent, at slightly different times.
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) VALUES "
        "(?, '2026-09-16', '2026-09-16T13:00:00Z', '2026-09-16T13:00:02Z', 'a.pdf')",
        (avi.portfolio_id,),
    )
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) VALUES "
        "(?, '2026-09-16', '2026-09-16T13:00:40Z', '2026-09-16T13:00:42Z', 'b.pdf')",
        (bob.portfolio_id,),
    )
    # Today's admin edition is already sent — it must NOT become the macro anchor.
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, sent_at, pdf_path) VALUES "
        "(?, '2026-09-17', '2026-09-17T13:00:00Z', '2026-09-17T13:00:03Z', 'c.pdf')",
        (avi.portfolio_id,),
    )
    start, end = _macro_window(conn, now, label_date="2026-09-17")
    # Most recent *prior* edition across the fund (Bob's later build), not today's.
    assert start == "2026-09-16T13:00:40Z"
    assert end == "2026-09-17T13:05:00Z"


def test_build_and_send_hands_every_recipient_the_same_macro_block(tmp_path, monkeypatch):
    """Every portfolio in a run is built from one shared macro block, so macro
    coverage cannot diverge between members within an edition."""
    from avalpha import accounts, mailer
    from avalpha.digest import build as digest_build

    conn = connect(tmp_path / "t.db")
    accounts.resolve_login(conn, accounts.ADMIN_EMAIL)
    accounts.resolve_login(conn, "bob@example.com")
    shared = [{"label": "FOMC decision", "lines": ["Fed funds target held at 3.50–3.75%"],
               "consensus": None}]
    monkeypatch.setattr(digest_build, "_macro_block", lambda *a, **k: shared)

    seen = []

    def fake_build(config, db_conn, date_str=None, portfolio_id=None, macro=None):
        seen.append(macro)
        path = tmp_path / f"{portfolio_id}.pdf"
        path.write_bytes(b"%PDF")
        db_conn.execute(
            "INSERT INTO digests (portfolio_id, date, built_at, pdf_path) VALUES (?, ?, ?, ?)",
            (portfolio_id, date_str, utcnow(), str(path)),
        )
        db_conn.commit()
        return path

    monkeypatch.setattr(digest_build, "build_digest", fake_build)
    monkeypatch.setattr(mailer, "send_digest_email", lambda *a, **k: None)
    digest_build.build_and_send(_cfg(tmp_path), conn, date_str="2026-09-17")

    assert len(seen) == 2                     # both members built
    assert all(block is shared for block in seen)  # identical object handed to each


def test_digest_date_is_calendar_day_not_prior_trading_day():
    """The digest ships every day, so each calendar day — weekends included —
    must get a distinct identity/dedup key. Keying on the prior trading day
    collapsed Fri/Sat/Sun/Mon onto Friday, so only Saturday sent and Monday was
    silently skipped. 2026-09-11..14 is Fri/Sat/Sun/Mon in Pacific."""
    at = lambda d: datetime(2026, 9, d, 13, 0, tzinfo=timezone.utc)  # 6am PT
    dates = [_digest_date(at(d), None) for d in (11, 12, 13, 14)]
    assert dates == ["2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"]
    assert len(set(dates)) == 4  # every day is its own digest


def test_digest_date_honors_explicit_date():
    assert _digest_date(datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc),
                        "2026-08-03") == "2026-08-03"


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
    monkeypatch.setattr(calendar_outcomes, "macro_outcome", lambda kind, key, event_date=None: {
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
                        lambda kind, key, event_date=None: {"kind": kind, "label": "CPI", "lines": ["x"]})
    assert _macro_events(_cfg(tmp_path), conn, *WIN) == []


def test_macro_events_empty_without_fred_key(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert _macro_events(_cfg(tmp_path), conn, *WIN) == []


def test_llm_text_safe_returns_fallback_on_failure(tmp_path, monkeypatch):
    """One section's LLM error must degrade to the fallback, not raise and sink
    the whole digest (so every other section and recipient still ships)."""
    from avalpha.digest import build as digest_build

    def boom(*a, **k):
        raise RuntimeError("anthropic overloaded")

    monkeypatch.setattr(digest_build, "_llm_text", boom)
    out = digest_build._llm_text_safe(_cfg(tmp_path), "p", fallback="FB", label="x")
    assert out == "FB"


def test_llm_text_safe_passes_through_on_success(tmp_path, monkeypatch):
    from avalpha.digest import build as digest_build

    monkeypatch.setattr(digest_build, "_llm_text", lambda *a, **k: "real narrative")
    out = digest_build._llm_text_safe(_cfg(tmp_path), "p", fallback="FB", label="x")
    assert out == "real narrative"


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
