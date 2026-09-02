"""Calendar upsert invariants: dedup merge, no-demote, manual-wins, sweep, confirm."""

from datetime import datetime, timedelta, timezone

from avalpha.calendar_store import (
    Event,
    apply_manual_edit,
    confirm_earnings,
    delete_event,
    earnings_key,
    is_bio,
    macro_key,
    manual_key,
    sweep_passed,
    upsert_event,
)
from avalpha.db import connect


def _conn(tmp_path):
    return connect(tmp_path / "t.db")


def _earnings(ticker, event_date, **kw):
    return Event(
        kind="earnings",
        ticker=ticker,
        title=f"{ticker} earnings",
        event_date=event_date,
        source=kw.pop("source", "finnhub"),
        confidence=kw.pop("confidence", "medium"),
        fiscal_period="2026Q4",
        dedup_key=earnings_key(ticker, "2026Q4"),
        **kw,
    )


def test_upsert_inserts_then_merges(tmp_path):
    conn = _conn(tmp_path)
    assert upsert_event(conn, _earnings("NVDA", "2026-10-28", meta={"hour": "amc"}))
    # Same dedup_key with a drifted date + new meta → update in place, not a 2nd row.
    assert not upsert_event(
        conn, _earnings("NVDA", "2026-10-29", meta={"epsEstimate": 1.2})
    )
    rows = conn.execute("SELECT * FROM calendar_events").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_date"] == "2026-10-29"
    import json

    meta = json.loads(row["meta_json"])
    assert meta == {"hour": "amc", "epsEstimate": 1.2}  # merged, not replaced


def test_confirmed_is_not_demoted_by_routine_refresh(tmp_path):
    conn = _conn(tmp_path)
    upsert_event(conn, _earnings("NVDA", "2026-10-28"))
    assert confirm_earnings(
        conn, "NVDA", event_at="2026-10-28T20:05:00Z", within_days=10_000
    )
    # A later scheduled refresh (Finnhub) must not knock it back to 'scheduled'.
    upsert_event(conn, _earnings("NVDA", "2026-10-28", status="scheduled"))
    assert (
        conn.execute("SELECT status FROM calendar_events").fetchone()["status"]
        == "confirmed"
    )


def test_manual_edit_survives_feed_refresh(tmp_path):
    conn = _conn(tmp_path)
    # A computed lockup row, then a hand-correction, then a routine recompute.
    lock = Event(
        kind="ipo_lockup",
        ticker="ABCD",
        title="ABCD IPO lockup expiry (est.)",
        event_date="2026-12-01",
        source="computed",
        confidence="low",
        dedup_key="lockup:ABCD",
    )
    upsert_event(conn, lock)
    eid = conn.execute("SELECT id FROM calendar_events").fetchone()["id"]
    apply_manual_edit(conn, eid, title="ABCD lockup (confirmed terms)", event_date="2027-01-15")
    upsert_event(conn, lock)  # collector recomputes the estimate again
    row = conn.execute("SELECT * FROM calendar_events").fetchone()
    assert row["event_date"] == "2027-01-15"  # hand edit preserved
    assert row["source"] == "manual" and row["confidence"] == "high"


def test_sweep_marks_past_events_passed_but_keeps_them(tmp_path):
    conn = _conn(tmp_path)
    upsert_event(
        conn,
        Event(kind="earnings", ticker="NVDA", title="past", event_date="2020-01-01",
              source="finnhub", dedup_key="earnings:NVDA:2020Q1"),
    )
    upsert_event(
        conn,
        Event(kind="earnings", ticker="AMD", title="future", event_date="2099-01-01",
              source="finnhub", dedup_key="earnings:AMD:2099Q1"),
    )
    n = sweep_passed(conn, "2026-09-01")
    assert n == 1
    statuses = {
        r["ticker"]: r["status"]
        for r in conn.execute("SELECT ticker, status FROM calendar_events")
    }
    assert statuses["NVDA"] == "passed"  # kept, not deleted
    assert statuses["AMD"] == "scheduled"


def test_confirm_earnings_respects_window(tmp_path):
    conn = _conn(tmp_path)
    far = (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()
    upsert_event(conn, _earnings("NVDA", far))
    # Outside the 14-day pre-earnings window → no confirmation.
    assert not confirm_earnings(conn, "NVDA", within_days=14)
    assert (
        conn.execute("SELECT status FROM calendar_events").fetchone()["status"]
        == "scheduled"
    )


def test_delete_removes_row(tmp_path):
    conn = _conn(tmp_path)
    upsert_event(
        conn,
        Event(
            kind="manual",
            title="x",
            event_date="2026-10-01",
            source="manual",
            dedup_key=manual_key(),
        ),
    )
    eid = conn.execute("SELECT id FROM calendar_events").fetchone()["id"]
    assert delete_event(conn, eid)
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 0


def test_macro_dedup_key_is_kind_plus_date():
    assert macro_key("cpi", "2026-09-11") == "macro:cpi:2026-09-11"


def test_is_bio_gate():
    assert is_bio("Biotechnology")
    assert is_bio("Pharmaceuticals")
    assert is_bio("Drug Manufacturers—Specialty & Generic")
    assert not is_bio("Semiconductors")
    assert not is_bio(None)
