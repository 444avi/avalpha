from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from avalpha.market_state import (
    PACIFIC,
    is_trading_day,
    market_state,
    poll_interval,
    prior_trading_day,
)


def pt(y, m, d, hh, mm) -> datetime:
    """Build an aware UTC datetime from a Pacific wall-clock time."""
    return datetime(y, m, d, hh, mm, tzinfo=PACIFIC).astimezone(timezone.utc)


def test_regular_hours():
    assert market_state(pt(2026, 8, 4, 6, 30)) == "regular"  # Tuesday open
    assert market_state(pt(2026, 8, 4, 12, 59)) == "regular"


def test_after_hours_window():
    assert market_state(pt(2026, 8, 4, 13, 0)) == "after_hours"
    assert market_state(pt(2026, 8, 4, 15, 29)) == "after_hours"
    assert market_state(pt(2026, 8, 4, 15, 30)) == "closed"


def test_overnight_and_premarket():
    assert market_state(pt(2026, 8, 4, 6, 29)) == "closed"
    assert market_state(pt(2026, 8, 4, 3, 0)) == "closed"


def test_weekend():
    assert market_state(pt(2026, 8, 1, 10, 0)) == "closed"  # Saturday
    assert market_state(pt(2026, 8, 2, 10, 0)) == "closed"  # Sunday


def test_holiday():
    # Thanksgiving 2026 falls on a Thursday.
    assert is_trading_day(date(2026, 11, 26)) is False
    assert market_state(pt(2026, 11, 26, 10, 0)) == "closed"


def test_dst_boundary_uses_wall_clock():
    # 10am PT is regular hours whether in PDT (summer) or PST (winter).
    assert market_state(pt(2026, 7, 6, 10, 0)) == "regular"
    assert market_state(pt(2026, 12, 7, 10, 0)) == "regular"


def test_naive_datetime_rejected():
    import pytest

    with pytest.raises(ValueError):
        market_state(datetime(2026, 8, 4, 10, 0))


def test_poll_intervals_follow_spec():
    regular = pt(2026, 8, 4, 10, 0)
    after = pt(2026, 8, 4, 14, 0)
    night = pt(2026, 8, 4, 20, 0)
    assert poll_interval("edgar", regular) == 60
    assert poll_interval("edgar", after) == 30
    assert poll_interval("edgar", night) == 600
    assert poll_interval("ir", regular) == 180
    assert poll_interval("ir", after) == 60
    assert poll_interval("gnews", night) == 1800
    assert poll_interval("reddit", night) == 3600


def test_earnings_escalation_hook():
    regular = pt(2026, 8, 4, 10, 0)
    assert poll_interval("edgar", regular, escalated=True) == 15
    assert poll_interval("ir", regular, escalated=True) == 30
    # Escalation never touches news/reddit.
    assert poll_interval("gnews", regular, escalated=True) == 600


def test_prior_trading_day_skips_weekend_and_holiday():
    assert prior_trading_day(date(2026, 8, 3)) == date(2026, 7, 31)  # Mon -> Fri
    # Friday after Thanksgiving 2026 -> Wednesday (Thu is a holiday).
    assert prior_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)
