"""Calendar event store: the dedup-keyed upsert that every writer goes through.

The calendar is a scheduling layer — one small structured fact per event (the
"when"), never the content. Every write is an *upsert on ``dedup_key``* (never a
blind insert), so the same event arriving from multiple refreshes — or later
from an 8-K — collapses into one row and is upgraded in place (see docs/calendar.md
§4). Two invariants live here and nowhere else:

  * a hand-edited (``source='manual'``) row is never clobbered by a routine
    refresh from a feed; and
  * a ``confirmed`` / ``cancelled`` row is never demoted back to ``scheduled``.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass, field

from avalpha.db import utcnow

# -- event taxonomy ---------------------------------------------------------

# Macro tiers (docs/calendar.md §3). Tier A always shows and is the only macro
# in the digest; Tier B is web-tab-only under the "More macro" collapse.
TIER_A_MACRO = ("fomc", "cpi", "pce", "jobs", "ppi", "gdp")
TIER_B_MACRO = (
    "fomc_minutes",
    "retail_sales",
    "ism",
    "sentiment",
    "beige_book",
    "fed_speak",
    "jobless_claims",
)
MACRO_KINDS = TIER_A_MACRO + TIER_B_MACRO
# Off by default even within Tier B (docs/calendar.md §3).
TIER_B_DEFAULT_OFF = ("jobless_claims",)

COMPANY_KINDS = ("earnings", "ipo_lockup", "analyst_day", "pdufa", "product_launch")

# profile2.finnhubIndustry values that make a holding "bio" — the PDUFA/readout
# quick-add and Phase 3 discovery only apply here (docs/calendar.md §5 "Bio gate").
BIO_INDUSTRIES = ("biotechnology", "pharmaceuticals", "drug manufacturers")


def is_bio(industry: str | None) -> bool:
    if not industry:
        return False
    low = industry.lower()
    return any(term in low for term in BIO_INDUSTRIES)

# Human labels for the UI/digest. A new kind is a new row, not a new component.
KIND_LABELS = {
    "earnings": "Earnings",
    "ipo_lockup": "IPO lockup expiry",
    "analyst_day": "Analyst day",
    "pdufa": "PDUFA / readout",
    "product_launch": "Product launch",
    "manual": "Event",
    "fomc": "FOMC decision",
    "cpi": "CPI",
    "pce": "PCE (Personal Income & Outlays)",
    "jobs": "Jobs report",
    "ppi": "PPI",
    "gdp": "GDP",
    "fomc_minutes": "FOMC minutes",
    "retail_sales": "Retail sales",
    "ism": "ISM",
    "sentiment": "Consumer sentiment",
    "beige_book": "Beige Book",
    "fed_speak": "Fed speak",
    "jobless_claims": "Jobless claims",
}

_UPGRADEABLE_STATUS = ("confirmed", "tentative", "cancelled", "passed")


@dataclass
class Event:
    """One calendar row, pre-persistence. `dedup_key` is the merge identity."""

    kind: str
    title: str
    event_date: str  # YYYY-MM-DD
    source: str
    dedup_key: str
    ticker: str | None = None
    event_at: str | None = None
    tz: str | None = None
    is_timed: bool = False
    status: str = "scheduled"
    source_ref: str | None = None
    confidence: str | None = None
    fiscal_period: str | None = None
    meta: dict = field(default_factory=dict)


# -- dedup keys (docs/calendar.md §4) ---------------------------------------


def earnings_key(ticker: str, fiscal_period: str) -> str:
    return f"earnings:{ticker.upper()}:{fiscal_period}"


def lockup_key(ticker: str) -> str:
    return f"lockup:{ticker.upper()}"


def macro_key(kind: str, event_date: str) -> str:
    return f"macro:{kind}:{event_date}"


def discovered_key(kind: str, ticker: str, event_date: str) -> str:
    return f"disc:{kind}:{ticker.upper()}:{event_date}"


def manual_key() -> str:
    return f"manual:{uuid.uuid4().hex}"


# -- upsert -----------------------------------------------------------------


def upsert_event(conn: sqlite3.Connection, event: Event) -> bool:
    """Insert or merge `event` by dedup_key. Returns True if a new row was created.

    Merge rules (docs/calendar.md §4):
      * new row → INSERT everything, stamping created_at/updated_at.
      * existing manual row + non-manual incoming → preserved untouched (a
        hand-edit wins over any feed refresh).
      * otherwise UPDATE event_date/event_at/tz/is_timed/source_ref/confidence,
        merge meta_json, and set status — but never demote a confirmed/cancelled
        row back to scheduled.
    """
    now = utcnow()
    existing = conn.execute(
        "SELECT * FROM calendar_events WHERE dedup_key = ?", (event.dedup_key,)
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO calendar_events (
                ticker, kind, title, event_date, event_at, tz, is_timed, status,
                source, source_ref, confidence, fiscal_period, dedup_key,
                meta_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.ticker,
                event.kind,
                event.title,
                event.event_date,
                event.event_at,
                event.tz,
                1 if event.is_timed else 0,
                event.status,
                event.source,
                event.source_ref,
                event.confidence,
                event.fiscal_period,
                event.dedup_key,
                json.dumps(event.meta),
                now,
                now,
            ),
        )
        return True

    # A member's hand-edit is authoritative; a routine feed refresh leaves it be.
    if existing["source"] == "manual" and event.source != "manual":
        return False

    status = event.status
    if event.status == "scheduled" and existing["status"] in _UPGRADEABLE_STATUS:
        status = existing["status"]

    merged_meta = json.loads(existing["meta_json"] or "{}")
    merged_meta.update(event.meta)

    conn.execute(
        """
        UPDATE calendar_events SET
            ticker = ?, kind = ?, title = ?, event_date = ?, event_at = ?,
            tz = ?, is_timed = ?, status = ?, source = ?, source_ref = ?,
            confidence = ?, fiscal_period = ?, meta_json = ?, updated_at = ?
        WHERE dedup_key = ?
        """,
        (
            event.ticker,
            event.kind,
            event.title,
            event.event_date,
            event.event_at,
            event.tz,
            1 if event.is_timed else 0,
            status,
            event.source,
            event.source_ref,
            event.confidence,
            event.fiscal_period,
            json.dumps(merged_meta),
            now,
            event.dedup_key,
        ),
    )
    return False


def sweep_passed(conn: sqlite3.Connection, today: str) -> int:
    """Mark past events `passed` (kept, not deleted — retention, §9). `today` is
    YYYY-MM-DD. Confirmed/cancelled rows are swept too once the date is behind
    us; manual rows are left alone so a member's entry keeps its own status."""
    cur = conn.execute(
        "UPDATE calendar_events SET status = 'passed', updated_at = ? "
        "WHERE event_date < ? AND status NOT IN ('passed','cancelled') "
        "AND source != 'manual'",
        (utcnow(), today),
    )
    return cur.rowcount


def confirm_earnings(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    event_at: str | None = None,
    source_ref: str | None = None,
    within_days: int = 14,
) -> bool:
    """Upgrade the nearest `scheduled` earnings row for `ticker` to `confirmed`.

    Phase 2: called when the EDGAR collector ingests an 8-K item 2.02 for a
    holding. Matches the closest upcoming earnings row within `within_days` (the
    pre-earnings window) so a report filed the same day confirms the right
    quarter. Returns True if a row was upgraded.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(days=within_days)).date().isoformat()
    floor = (now - timedelta(days=within_days)).date().isoformat()
    row = conn.execute(
        "SELECT id FROM calendar_events WHERE ticker = ? AND kind = 'earnings' "
        "AND status = 'scheduled' AND event_date BETWEEN ? AND ? "
        "ORDER BY event_date LIMIT 1",
        (ticker.upper(), floor, horizon),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE calendar_events SET status = 'confirmed', "
        "event_at = COALESCE(?, event_at), is_timed = 1, "
        "source = 'edgar', source_ref = COALESCE(?, source_ref), "
        "confidence = 'high', updated_at = ? WHERE id = ?",
        (event_at, source_ref, utcnow(), row["id"]),
    )
    return True


def apply_manual_edit(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    title: str,
    event_date: str,
    event_at: str | None = None,
) -> bool:
    """A member's hand-edit. Converts the row to source='manual'/confidence='high'
    so a routine feed refresh will no longer overwrite it (docs/calendar.md §5:
    a computed lockup date corrected by hand is preserved across refreshes)."""
    cur = conn.execute(
        "UPDATE calendar_events SET title = ?, event_date = ?, event_at = ?, "
        "is_timed = ?, source = 'manual', confidence = 'high', updated_at = ? "
        "WHERE id = ?",
        (title, event_date, event_at, 1 if event_at else 0, utcnow(), event_id),
    )
    return cur.rowcount > 0


def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM calendar_events WHERE id = ?", (event_id,)
    ).fetchone()


def delete_event(conn: sqlite3.Connection, event_id: int) -> bool:
    """Delete one event. Intended for member-entered (manual) rows only; the
    caller enforces that so feed-owned rows can't be hand-deleted (they'd just
    reappear on the next refresh)."""
    cur = conn.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
    return cur.rowcount > 0
