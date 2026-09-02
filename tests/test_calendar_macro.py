from datetime import date

from avalpha import calendar_macro as cm


def test_fomc_minutes_are_decision_day_plus_21_and_wednesdays():
    minutes = cm.fomc_minutes_dates(cm.FOMC_DECISION_DAYS)
    assert minutes[0] == "2026-02-18"  # 2026-01-28 + 21d
    for iso in minutes:
        assert date.fromisoformat(iso).weekday() == 2  # Wednesday


def test_beige_book_is_two_weeks_before_meeting():
    assert cm.beige_book_dates(["2026-01-28"]) == ["2026-01-14"]


def test_ism_first_and_third_business_day():
    # Sep 2026: 1st = Tue Sep 1, 3rd business day = Thu Sep 3 (Labor Day is Sep 7).
    ism = cm.ism_dates(date(2026, 9, 1), date(2026, 9, 30))
    assert ("manufacturing", "2026-09-01") in ism
    assert ("services", "2026-09-03") in ism


def test_sentiment_conf_board_and_umich():
    sent = dict(
        (v, k) for k, v in cm.sentiment_dates(date(2026, 9, 1), date(2026, 9, 30))
    )
    # last Tuesday of Sep 2026 is the 29th; 2nd/4th Fridays are 11th/25th.
    assert sent["2026-09-29"] == "conference_board"
    assert sent["2026-09-11"] == "umich_prelim"
    assert sent["2026-09-25"] == "umich_final"


def test_et_instant_converts_wall_clock_to_utc():
    # 08:30 ET in September is EDT (UTC-4) → 12:30Z.
    assert cm.et_instant("2026-09-11", 8, 30) == "2026-09-11T12:30:00Z"
    # 14:00 ET in January is EST (UTC-5) → 19:00Z.
    assert cm.et_instant("2026-01-28", 14, 0) == "2026-01-28T19:00:00Z"


def test_fallback_dates_filtered_to_window():
    got = cm.fallback_dates("cpi", date(2026, 9, 1), date(2026, 10, 31))
    assert got == ["2026-09-11", "2026-10-14"]
    assert cm.fallback_dates("gdp", date(2026, 1, 1), date(2027, 1, 1)) == []  # no table
