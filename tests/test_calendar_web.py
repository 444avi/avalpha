"""Calendar web tab: agenda render, manual CRUD, bio gate, dashboard surfacing."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from avalpha import db
from avalpha.calendar_store import Event, macro_key, upsert_event
from avalpha.config import Config
from avalpha.web.app import create_app


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(db_path=tmp_path / "test.db", digest_dir=tmp_path / "digests",
                  email_recipient="", email_sender="", web_fund_name="The Silo Fund")


@pytest.fixture
def seeded(cfg: Config) -> Config:
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO watchlist (ticker, cik, legal_name, weight, active, added_at, industry) "
        "VALUES ('MRNA', '0001682852', 'MODERNA INC', 5, 1, ?, 'Biotechnology')",
        (db.utcnow(),),
    )
    conn.execute(
        "INSERT INTO watchlist (ticker, cik, legal_name, weight, active, added_at) "
        "VALUES ('NVDA', '0001045810', 'NVIDIA CORP', 12, 1, ?)",
        (db.utcnow(),),
    )
    soon = (date.today() + timedelta(days=3)).isoformat()
    upsert_event(conn, Event(
        kind="earnings", ticker="NVDA", title="NVDA earnings — 2026Q4",
        event_date=soon, status="scheduled", source="finnhub", confidence="medium",
        fiscal_period="2026Q4", dedup_key="earnings:NVDA:2026Q4", meta={"hour": "amc"}))
    cpi_d = (date.today() + timedelta(days=2)).isoformat()
    upsert_event(conn, Event(
        kind="cpi", ticker=None, title="CPI", event_date=cpi_d, status="scheduled",
        source="fred", confidence="high", tz="America/New_York", is_timed=True,
        event_at="2026-09-11T12:30:00Z", dedup_key=macro_key("cpi", cpi_d)))
    claims_d = (date.today() + timedelta(days=1)).isoformat()
    upsert_event(conn, Event(
        kind="jobless_claims", ticker=None, title="Jobless claims", event_date=claims_d,
        status="scheduled", source="derived", confidence="high",
        dedup_key=macro_key("jobless_claims", claims_d)))
    conn.commit()
    conn.close()
    return cfg


def client(cfg: Config, monkeypatch) -> TestClient:
    monkeypatch.setenv("AVALPHA_WEB_DEV_USER", "member@thesilofund.com")
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    return TestClient(create_app(cfg), follow_redirects=False)


def test_calendar_tab_renders_agenda(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    body = c.get("/calendar").text
    assert c.get("/calendar").status_code == 200
    assert "NVDA" in body and "CPI" in body            # company + Tier A macro inline
    assert "after close" in body                        # earnings amc shown honestly
    assert "More macro" in body                         # Tier B under the collapse


def test_dashboard_shows_upcoming_strip_and_badge(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    body = c.get("/").text
    assert "Upcoming · next 7 days" in body
    assert "cal-badge" in body                           # per-row "Earnings in Nd"
    # Tier B (jobless claims) never leaks into the strip.
    assert "Jobless" not in body


def test_bio_holding_offers_pdufa_quick_add(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    assert "PDUFA" in c.get("/holding/MRNA").text
    assert "PDUFA" not in c.get("/holding/NVDA").text    # non-bio: no quick-add


def test_manual_add_edit_delete_roundtrip(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    soon = (date.today() + timedelta(days=5)).isoformat()
    r = c.post("/calendar/add", data={"title": "Investor day", "event_date": soon,
                                      "kind": "analyst_day", "ticker": "NVDA"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]

    conn = db.connect(seeded.db_path)
    eid = conn.execute("SELECT id FROM calendar_events WHERE source='manual'").fetchone()["id"]
    conn.close()

    r = c.post(f"/calendar/{eid}/edit", data={"title": "Investor Day 2026", "event_date": soon})
    assert r.status_code == 303
    conn = db.connect(seeded.db_path)
    assert conn.execute("SELECT title FROM calendar_events WHERE id=?", (eid,)).fetchone()[0] \
        == "Investor Day 2026"
    conn.close()

    r = c.post(f"/calendar/{eid}/delete")
    assert "msg=" in r.headers["location"]


def test_feed_owned_event_cannot_be_deleted(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    conn = db.connect(seeded.db_path)
    fid = conn.execute("SELECT id FROM calendar_events WHERE source='finnhub'").fetchone()["id"]
    conn.close()
    r = c.post(f"/calendar/{fid}/delete")
    assert "err=" in r.headers["location"]               # would just reappear on refresh


def test_manual_add_validation(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    # bad date
    assert "err=" in c.post("/calendar/add", data={
        "title": "x", "event_date": "not-a-date", "kind": "manual"}).headers["location"]
    # pdufa on a non-bio holding is refused
    soon = (date.today() + timedelta(days=5)).isoformat()
    assert "err=" in c.post("/calendar/add", data={
        "title": "x", "event_date": soon, "kind": "pdufa", "ticker": "NVDA"}).headers["location"]
    # unknown kind
    assert "err=" in c.post("/calendar/add", data={
        "title": "x", "event_date": soon, "kind": "bogus"}).headers["location"]


def test_calendar_collector_is_a_valid_job(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    # calendar is wired into SOURCES → collector:calendar is a known job and a
    # health tile. (It will fail on missing keys, but must be *accepted*.)
    r = c.post("/jobs/collector:calendar")
    assert r.status_code == 303 and "err=Unknown" not in r.headers["location"]
