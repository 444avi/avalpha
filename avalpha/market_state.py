"""Market-state cadence. All boundaries Pacific per spec; timestamps stay UTC.

States:
  regular      6:30a–1:00p PT on trading days
  after_hours  1:00p–3:30p PT on trading days (where the material filings land)
  closed       everything else: nights, weekends, market holidays
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

REGULAR_OPEN = time(6, 30)
REGULAR_CLOSE = time(13, 0)
AFTER_HOURS_CLOSE = time(15, 30)

# NYSE full-closure holidays. Extend annually; verify against the NYSE
# calendar when adding a year. Half-days are treated as normal trading days.
MARKET_HOLIDAYS: frozenset[date] = frozenset(
    [
        # 2026
        date(2026, 1, 1),    # New Year's Day
        date(2026, 1, 19),   # Martin Luther King Jr. Day
        date(2026, 2, 16),   # Washington's Birthday
        date(2026, 4, 3),    # Good Friday
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day (observed)
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        # 2027
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # Martin Luther King Jr. Day
        date(2027, 2, 15),   # Washington's Birthday
        date(2027, 3, 26),   # Good Friday
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 18),   # Juneteenth (observed)
        date(2027, 7, 5),    # Independence Day (observed)
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas (observed)
    ]
)

# Poll intervals in seconds, per spec cadence table.
INTERVALS: dict[str, dict[str, int]] = {
    "edgar":  {"regular": 60,  "after_hours": 30, "closed": 600},
    "ir":     {"regular": 180, "after_hours": 60, "closed": 900},
    "gnews":  {"regular": 600, "after_hours": 600, "closed": 1800},
    "reddit": {"regular": 900, "after_hours": 900, "closed": 3600},
    "prices": {"regular": 21600, "after_hours": 21600, "closed": 21600},
    "calendar": {"regular": 43200, "after_hours": 43200, "closed": 43200},
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in MARKET_HOLIDAYS


def market_state(now_utc: datetime) -> str:
    if now_utc.tzinfo is None:
        raise ValueError("market_state requires an aware UTC datetime")
    local = now_utc.astimezone(PACIFIC)
    if not is_trading_day(local.date()):
        return "closed"
    t = local.time()
    if REGULAR_OPEN <= t < REGULAR_CLOSE:
        return "regular"
    if REGULAR_CLOSE <= t < AFTER_HOURS_CLOSE:
        return "after_hours"
    return "closed"


def poll_interval(source: str, now_utc: datetime, escalated: bool = False) -> int:
    """Seconds between polls for `source` at this moment.

    `escalated` is the Phase 2 earnings-window hook: within 48h of a scheduled
    earnings report, EDGAR/IR drop to 15-30s regardless of clock time. Phase 1
    never sets it.
    """
    if escalated and source in ("edgar", "ir"):
        return 15 if source == "edgar" else 30
    return INTERVALS[source][market_state(now_utc)]


def earnings_escalated(conn, now_utc: datetime) -> bool:
    """True when any active holding has a near-term binary catalyst.

    Escalation is *global*, not per-ticker, because the collectors poll globally
    (docs/calendar.md §6.3): if any active holding has a **confirmed** earnings
    report — or any PDUFA/readout — landing within the next 48h, EDGAR/IR ramp
    their polling. Confirmed-only for earnings so an unverified Finnhub estimate
    that later slips doesn't churn the whole pipeline; PDUFA dates are curated so
    any non-terminal status counts.
    """
    from datetime import timedelta

    now = now_utc.astimezone(timezone.utc)
    horizon = now + timedelta(hours=48)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    horizon_iso = horizon.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_iso = now.date().isoformat()
    horizon_date = horizon.date().isoformat()

    row = conn.execute(
        """
        SELECT 1
        FROM calendar_events c
        JOIN watchlist w ON w.ticker = c.ticker AND w.active = 1
        WHERE c.status NOT IN ('passed', 'cancelled')
          AND (
                (c.kind = 'earnings' AND c.status = 'confirmed')
                OR c.kind = 'pdufa'
              )
          AND (
                (c.is_timed = 1 AND c.event_at IS NOT NULL
                    AND c.event_at BETWEEN ? AND ?)
                OR (c.event_at IS NULL AND c.event_date BETWEEN ? AND ?)
              )
        LIMIT 1
        """,
        (now_iso, horizon_iso, today_iso, horizon_date),
    ).fetchone()
    return row is not None


def prior_trading_day(d: date) -> date:
    """Most recent trading day strictly before `d`."""
    from datetime import timedelta

    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur
