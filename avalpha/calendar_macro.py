"""Macro calendar date math — pure, network-free, unit-testable.

Live macro dates come from the FRED API + the Fed FOMC page (fetched in
collectors/calendar.py). This module owns two things that need no network:

  * the **derived** rules (docs/calendar.md §5c) — fomc_minutes, beige_book,
    ism, sentiment — computed from the FOMC anchors and fixed calendar rules,
    self-sustaining forever; and
  * the **fallback** anchor tables (docs/calendar.md §5 "Graceful fallback"),
    a verified safety net so the calendar is never empty when FRED/the Fed is
    unreachable. FRED overwrites these on the next successful run.
"""

import calendar as _calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from avalpha.market_state import is_trading_day

ET = ZoneInfo("America/New_York")

# Release times (ET wall clock). Approximate — presented honestly in the UI.
RELEASE_TIME_ET = {
    "fomc": (14, 0),
    "fomc_minutes": (14, 0),
    "beige_book": (14, 0),
    "cpi": (8, 30),
    "ppi": (8, 30),
    "jobs": (8, 30),
    "gdp": (8, 30),
    "pce": (8, 30),
    "retail_sales": (8, 30),
    "jobless_claims": (8, 30),
    "ism": (10, 0),
    "sentiment": (10, 0),
}

# --- FALLBACK ONLY. Live dates come from FRED + the Fed. ---
# Verified 2026-09-01 against federalreserve.gov, BLS, and the usinflationcalculator
# mirror of the BLS CPI schedule.

# FOMC decision days (2nd day of each meeting), 14:00 ET.
FOMC_DECISION_DAYS = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]
# CPI, 08:30 ET.
CPI_2026 = [
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]
# Employment Situation (jobs), 08:30 ET.
JOBS_2026 = [
    "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03",
    "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]

# What each fallback list feeds. PPI/GDP/PCE/retail have no fallback table —
# they come from FRED (their 2026 dates were disrupted by the 2025 shutdown,
# so a hand-copy would mislead).
FALLBACK_TABLES = {
    "fomc": FOMC_DECISION_DAYS,
    "cpi": CPI_2026,
    "jobs": JOBS_2026,
}


def et_instant(event_date: str, hour: int, minute: int) -> str:
    """UTC ISO instant for an ET wall-clock time on `event_date` (YYYY-MM-DD)."""
    y, m, d = (int(x) for x in event_date.split("-"))
    aware = datetime(y, m, d, hour, minute, tzinfo=ET)
    return aware.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- calendar primitives ----------------------------------------------------


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-based) `weekday` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last `weekday` (Mon=0) of a month."""
    last_day = _calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _nth_business_day(year: int, month: int, n: int) -> date:
    """The nth (1-based) trading day of a month (weekends + market holidays skipped)."""
    d = date(year, month, 1)
    count = 0
    while True:
        if is_trading_day(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def _months(start: date, end: date):
    """Yield (year, month) for each month touched by [start, end]."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


# -- derived rules (docs/calendar.md §5c) -----------------------------------


def fomc_minutes_dates(decision_days: list[str]) -> list[str]:
    """FOMC decision day + 21 days (always a Wednesday), 14:00 ET."""
    out = []
    for iso in decision_days:
        d = date.fromisoformat(iso) + timedelta(days=21)
        out.append(d.isoformat())
    return out


def beige_book_dates(decision_days: list[str]) -> list[str]:
    """The Wednesday two weeks before each FOMC decision day."""
    return [
        (date.fromisoformat(iso) - timedelta(days=14)).isoformat()
        for iso in decision_days
    ]


def ism_dates(start: date, end: date) -> list[tuple[str, str]]:
    """(subtype, date) for ISM mfg (1st business day) and services (3rd) per month."""
    out = []
    for y, m in _months(start, end):
        mfg = _nth_business_day(y, m, 1)
        svc = _nth_business_day(y, m, 3)
        if start <= mfg <= end:
            out.append(("manufacturing", mfg.isoformat()))
        if start <= svc <= end:
            out.append(("services", svc.isoformat()))
    return out


def sentiment_dates(start: date, end: date) -> list[tuple[str, str]]:
    """(subtype, date) for Conf. Board (last Tue), UMich prelim (2nd Fri) & final
    (4th Fri) per month."""
    out = []
    for y, m in _months(start, end):
        confboard = _last_weekday(y, m, 1)       # Tuesday
        umich_prelim = _nth_weekday(y, m, 4, 2)  # 2nd Friday
        umich_final = _nth_weekday(y, m, 4, 4)   # 4th Friday
        for subtype, d in (
            ("conference_board", confboard),
            ("umich_prelim", umich_prelim),
            ("umich_final", umich_final),
        ):
            if start <= d <= end:
                out.append((subtype, d.isoformat()))
    return out


def fallback_dates(kind: str, start: date, end: date) -> list[str]:
    """Anchor-table dates for `kind` within [start, end]; empty if no table."""
    return [
        iso
        for iso in FALLBACK_TABLES.get(kind, [])
        if start <= date.fromisoformat(iso) <= end
    ]
