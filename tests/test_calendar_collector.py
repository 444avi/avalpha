"""Calendar collector: Finnhub earnings/profile shaping, macro fallback, escalation."""

from datetime import date, datetime, timedelta, timezone

from avalpha import db, watchlist
from avalpha.calendar_store import Event, earnings_key, upsert_event
from avalpha.collectors import calendar as cal
from avalpha.config import Config
from avalpha.market_state import earnings_escalated


def _cfg(tmp_path) -> Config:
    return Config(db_path=tmp_path / "t.db", digest_dir=tmp_path,
                  email_recipient="", email_sender="")


def _holding(conn, ticker="NVDA", cik="0001045810"):
    conn.execute(
        "INSERT INTO watchlist (ticker, cik, legal_name, weight, active, added_at) "
        "VALUES (?, ?, ?, 1, 1, ?)",
        (ticker, cik, ticker, db.utcnow()),
    )
    conn.commit()
    return watchlist.get(conn, ticker)


def test_collect_earnings_upserts_nearest_report(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    h = _holding(conn)
    monkeypatch.setattr(cal, "_finnhub_get", lambda url, params: {
        "earningsCalendar": [
            {"symbol": "NVDA", "date": "2027-02-20", "hour": "amc", "year": 2026, "quarter": 4},
            {"symbol": "NVDA", "date": "2026-11-19", "hour": "amc", "year": 2026,
             "quarter": 3, "epsEstimate": 1.1, "revenueEstimate": 5e10},
        ]
    })
    n = cal._collect_earnings(conn, h, "tok", date(2026, 11, 1), date(2027, 3, 1))
    assert n == 1
    row = conn.execute("SELECT * FROM calendar_events WHERE kind='earnings'").fetchone()
    assert row["event_date"] == "2026-11-19"          # the nearest upcoming report
    assert row["fiscal_period"] == "2026Q3"
    assert row["is_timed"] == 1 and row["event_at"].endswith("Z")
    assert row["confidence"] == "medium" and row["status"] == "scheduled"


def test_collect_profile_sets_industry_and_recent_ipo_lockup(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    h = _holding(conn, "RCNT")
    ipo = (date(2026, 9, 1) - timedelta(days=30)).isoformat()
    monkeypatch.setattr(cal, "_finnhub_get", lambda url, params: {
        "finnhubIndustry": "Biotechnology", "ipo": ipo, "shareOutstanding": 100.0,
    })
    n = cal._collect_profile(conn, h, "tok", date(2026, 9, 1))
    assert n == 1
    assert watchlist.get(conn, "RCNT").industry == "Biotechnology"
    row = conn.execute("SELECT * FROM calendar_events WHERE kind='ipo_lockup'").fetchone()
    assert row["confidence"] == "low"                  # labeled an estimate
    assert row["event_date"] == (date.fromisoformat(ipo) + timedelta(days=180)).isoformat()


def test_old_ipo_makes_no_lockup_but_still_sets_industry(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    h = _holding(conn, "OLDC")
    ipo = (date(2026, 9, 1) - timedelta(days=400)).isoformat()
    monkeypatch.setattr(cal, "_finnhub_get", lambda url, params: {
        "finnhubIndustry": "Semiconductors", "ipo": ipo})
    assert cal._collect_profile(conn, h, "tok", date(2026, 9, 1)) == 0
    assert watchlist.get(conn, "OLDC").industry == "Semiconductors"
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 0


def test_macro_falls_back_when_network_down(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    monkeypatch.setenv("FRED_API_KEY", "x")
    monkeypatch.setenv("AVALPHA_CONTACT_EMAIL", "a@b.co")
    cfg = _cfg(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(cal.requests, "get", boom)
    count, errors = cal._collect_macro(conn, cfg, date(2026, 9, 1), date(2026, 12, 31))
    assert errors  # degraded → surfaced so the health tile can go red
    assert count > 0  # ...but the calendar is never empty
    kinds = {r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM calendar_events")}
    # fallback anchors (cpi/fomc) + always-derived rules
    assert {"cpi", "fomc", "fomc_minutes", "beige_book", "ism", "sentiment"} <= kinds
    # the CPI rows came from the anchor table, not a live FRED pull
    cpi = conn.execute("SELECT source FROM calendar_events WHERE kind='cpi' LIMIT 1").fetchone()
    assert cpi["source"] == "derived"


def test_parse_fomc_page_reads_the_fed_layout():
    # Mirrors the real markup: month/day in separate divs, month wrapped in tags,
    # the decision day is the last of the span.
    html = """
    <div class="panel-heading"><h4><a id="1">2026 FOMC Meetings</a></h4></div>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month col-md-2"><strong>January</strong></div>
      <div class="fomc-meeting__date col-md-10">27-28</div>
    </div>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>March</strong></div>
      <div class="fomc-meeting__date">17-18</div>
    </div>
    """
    assert cal._parse_fomc_page(html) == ["2026-01-28", "2026-03-18"]


def test_parse_fomc_page_returns_empty_on_unknown_layout():
    assert cal._parse_fomc_page("<html><body>nothing here</body></html>") == []


def test_earnings_escalated_only_for_confirmed_near_events(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _holding(conn, "NVDA")
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    upsert_event(conn, Event(
        kind="earnings", ticker="NVDA", title="x", event_date="2026-10-29",
        status="scheduled", source="finnhub", fiscal_period="2026Q3",
        dedup_key=earnings_key("NVDA", "2026Q3"),
        event_at="2026-10-29T20:05:00Z", is_timed=True))
    conn.commit()
    assert not earnings_escalated(conn, now)  # scheduled ≠ confirmed → no ramp

    conn.execute("UPDATE calendar_events SET status='confirmed'")
    conn.commit()
    assert earnings_escalated(conn, now)      # confirmed & within 48h → ramp

    conn.execute(
        "UPDATE calendar_events SET event_date='2026-12-01', event_at='2026-12-01T20:05:00Z'"
    )
    conn.commit()
    assert not earnings_escalated(conn, now)   # confirmed but far off → no ramp


def test_escalation_ignores_inactive_holdings(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _holding(conn, "NVDA")
    conn.execute("UPDATE watchlist SET active = 0")
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    upsert_event(conn, Event(
        kind="earnings", ticker="NVDA", title="x", event_date="2026-10-29",
        status="confirmed", source="edgar", fiscal_period="2026Q3",
        dedup_key=earnings_key("NVDA", "2026Q3"),
        event_at="2026-10-29T20:05:00Z", is_timed=True))
    conn.commit()
    assert not earnings_escalated(conn, now)
