"""Calendar collector: anticipate catalyst dates for holdings and the market.

A scheduling layer, not a content collector (docs/calendar.md §1). Each daily
run does three things and upserts everything by ``dedup_key``:

  1. **Earnings** — Finnhub ``/calendar/earnings`` per active holding: the next
     upcoming report, refreshed in place as the date drifts.
  2. **IPO lockup + bio gate** — Finnhub ``/stock/profile2``: persist
     ``finnhubIndustry`` to ``watchlist.industry``, and for recent IPOs create a
     computed lockup-expiry estimate (ipo + 180d).
  3. **Macro** — statistical releases from the FRED API, FOMC meeting dates from
     the Fed's page, and the derived rules (fomc_minutes / beige_book / ism /
     sentiment). If FRED or the Fed is unreachable, a verified anchor table keeps
     the calendar populated and the run is reported as degraded (red health tile).

Mirrors prices.py for Finnhub error handling (401/403/429). Runs under
collectors/base.run(), so any outage is isolated and logged.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import requests

from avalpha import calendar_macro as cm
from avalpha import watchlist
from avalpha.calendar_store import (
    KIND_LABELS,
    Event,
    earnings_key,
    lockup_key,
    macro_key,
    sweep_passed,
    upsert_event,
)
from avalpha.config import Config

EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
PROFILE2_URL = "https://finnhub.io/api/v1/stock/profile2"
FRED_RELEASES_URL = "https://api.stlouisfed.org/fred/releases"
FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
FOMC_PAGE_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

EARNINGS_HORIZON_DAYS = 100     # next report is within a quarter
MACRO_HORIZON_DAYS = 180        # how far ahead to publish macro dates
LOCKUP_WINDOW_DAYS = 183        # only recent IPOs get a lockup estimate

# FRED release names (resolved to ids by name — more robust than hard-coded ids,
# docs/calendar.md §5). jobless_claims is deliberately excluded: Tier B, off by
# default (docs/calendar.md §3).
FRED_RELEASES = {
    "cpi": "Consumer Price Index",
    "ppi": "Producer Price Index",
    "jobs": "Employment Situation",
    "gdp": "Gross Domestic Product",
    "pce": "Personal Income and Outlays",
    "retail_sales": "Advance Monthly Sales for Retail and Food Services",
}


class CalendarError(Exception):
    pass


# -- Finnhub: earnings ------------------------------------------------------


def _finnhub_get(url: str, params: dict) -> dict:
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code in (401, 403):
        raise CalendarError(
            f"Finnhub rejected the key ({resp.status_code}); check FINNHUB_API_KEY"
        )
    if resp.status_code == 429:
        raise CalendarError("Finnhub rate limit hit (free tier is 60 calls/min)")
    resp.raise_for_status()
    return resp.json()


def _amc_bmo_instant(event_date: str, hour: str) -> tuple[str | None, bool]:
    """Approximate ET instant for an earnings `hour` code. Approximate — never
    presented as exact (docs/calendar.md §5)."""
    if hour == "amc":
        return cm.et_instant(event_date, 16, 5), True   # after close ~16:05 ET
    if hour == "bmo":
        return cm.et_instant(event_date, 7, 0), True     # before open ~07:00 ET
    return None, False


def _collect_earnings(
    conn: sqlite3.Connection, h, token: str, today: date, horizon: date
) -> int:
    data = _finnhub_get(
        EARNINGS_URL,
        {
            "symbol": h.ticker.upper(),
            "from": today.isoformat(),
            "to": horizon.isoformat(),
            "token": token,
        },
    )
    rows = data.get("earningsCalendar") or []
    upcoming = sorted(
        (r for r in rows if r.get("date") and r["date"] >= today.isoformat()),
        key=lambda r: r["date"],
    )
    if not upcoming:
        return 0
    r = upcoming[0]
    hour = r.get("hour") or ""
    year, quarter = r.get("year"), r.get("quarter")
    fiscal = f"{year}Q{quarter}" if year and quarter else r["date"]
    event_at, is_timed = _amc_bmo_instant(r["date"], hour)
    upsert_event(
        conn,
        Event(
            kind="earnings",
            ticker=h.ticker,
            title=f"{h.ticker} earnings — {fiscal}",
            event_date=r["date"],
            event_at=event_at,
            tz="America/New_York" if is_timed else None,
            is_timed=is_timed,
            status="scheduled",
            source="finnhub",
            source_ref="/calendar/earnings",
            confidence="medium",
            fiscal_period=fiscal,
            dedup_key=earnings_key(h.ticker, fiscal),
            meta={
                "hour": hour,
                "epsEstimate": r.get("epsEstimate"),
                "revenueEstimate": r.get("revenueEstimate"),
            },
        ),
    )
    return 1


# -- Finnhub: profile2 (industry + IPO lockup) ------------------------------


def _collect_profile(
    conn: sqlite3.Connection, h, token: str, today: date
) -> int:
    data = _finnhub_get(PROFILE2_URL, {"symbol": h.ticker.upper(), "token": token})
    if not data:  # unknown symbol comes back as {}
        return 0
    watchlist.set_industry(conn, h.ticker, data.get("finnhubIndustry"))

    ipo = data.get("ipo")
    if not ipo:
        return 0
    try:
        ipo_date = date.fromisoformat(ipo)
    except ValueError:
        return 0
    if (today - ipo_date).days > LOCKUP_WINDOW_DAYS:
        return 0  # only recent IPOs get a lockup estimate
    expiry = (ipo_date + timedelta(days=180)).isoformat()
    upsert_event(
        conn,
        Event(
            kind="ipo_lockup",
            ticker=h.ticker,
            title=f"{h.ticker} IPO lockup expiry (est.)",
            event_date=expiry,
            status="scheduled",
            source="computed",
            source_ref="profile2.ipo + 180d",
            confidence="low",  # lockup terms vary 90–180d; labeled an estimate
            dedup_key=lockup_key(h.ticker),
            meta={"ipo": ipo, "basis": "ipo+180d"},
        ),
    )
    return 1


# -- Macro: FRED + Fed + derived --------------------------------------------


def _fred_release_ids(fred_key: str) -> dict[str, int]:
    """Resolve FRED release ids by name once per run (docs/calendar.md §5)."""
    resp = requests.get(
        FRED_RELEASES_URL,
        params={"api_key": fred_key, "file_type": "json", "limit": 1000},
        timeout=30,
    )
    resp.raise_for_status()
    by_name = {r["name"]: r["id"] for r in resp.json().get("releases", [])}
    return {
        kind: by_name[name]
        for kind, name in FRED_RELEASES.items()
        if name in by_name
    }


def _fred_dates(fred_key: str, release_id: int, today: date, horizon: date) -> list[str]:
    data = requests.get(
        FRED_RELEASE_DATES_URL,
        params={
            "release_id": release_id,
            "include_release_dates_with_no_data": "true",
            "api_key": fred_key,
            "file_type": "json",
        },
        timeout=30,
    )
    data.raise_for_status()
    out = []
    for r in data.json().get("release_dates", []):
        d = r.get("date")
        if d and today.isoformat() <= d <= horizon.isoformat():
            out.append(d)
    return out


def _parse_fomc_page(html: str) -> list[str]:
    """Best-effort parse of the Fed FOMC calendar page → every decision-day ISO
    date (the last/decision day of each meeting), across all years shown. Returns
    [] if the layout can't be read, in which case the caller falls back to the
    anchor table. The Fed publishes ~2 years ahead, so a successful parse lets the
    calendar (and the derived minutes/beige-book rules) self-sustain."""
    import re

    months = {m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], start=1)}
    out: list[str] = []
    year = None
    for token in re.split(r"(?i)(\d{4})\s+FOMC\s+Meetings", html):
        if re.fullmatch(r"\d{4}", token or ""):
            year = int(token)
            continue
        if year is None:
            continue
        # Month & day sit in separate divs, each wrapped in optional tags
        # (e.g. <div class="fomc-meeting__month"><strong>January</strong></div>
        #  … <div class="fomc-meeting__date">27-28</div>). Take the last day of
        # the span as the decision day.
        for mo, span in re.findall(
            r"(?is)fomc-meeting__month[^>]*>(?:\s*<[^>]+>)*\s*([A-Za-z]+)"
            r".*?fomc-meeting__date[^>]*>(?:\s*<[^>]+>)*\s*"
            r"([0-9]{1,2}(?:\s*[-–/]\s*(?:[A-Za-z]+\s*)?[0-9]{1,2})?)",
            token,
        ):
            mnum = months.get(mo.strip().lower())
            if not mnum:
                continue
            last_day = re.findall(r"[0-9]{1,2}", span)[-1]
            try:
                out.append(date(year, mnum, int(last_day)).isoformat())
            except ValueError:
                continue
    return sorted(set(out))


def _upsert_macro(conn: sqlite3.Connection, kind: str, iso: str, source: str,
                  title: str | None = None, meta: dict | None = None) -> None:
    hh, mm = cm.RELEASE_TIME_ET.get(kind, (8, 30))
    upsert_event(
        conn,
        Event(
            kind=kind,
            ticker=None,
            title=title or KIND_LABELS.get(kind, kind),
            event_date=iso,
            event_at=cm.et_instant(iso, hh, mm),
            tz="America/New_York",
            is_timed=True,
            status="scheduled",
            source=source,
            confidence="high",
            dedup_key=macro_key(kind, iso),
            meta=meta or {},
        ),
    )


def _collect_macro(
    conn: sqlite3.Connection, config: Config, today: date, horizon: date
) -> tuple[int, list[str]]:
    errors: list[str] = []
    count = 0

    # (a) FRED statistical releases -----------------------------------------
    try:
        fred_key = config.fred_api_key
    except RuntimeError as e:
        fred_key = None
        errors.append(str(e))

    resolved: dict[str, int] = {}
    if fred_key:
        try:
            resolved = _fred_release_ids(fred_key)
        except Exception as e:  # noqa: BLE001
            errors.append(f"FRED releases: {type(e).__name__}: {e}")

    for kind in FRED_RELEASES:
        dates: list[str] = []
        rid = resolved.get(kind)
        if fred_key and rid is not None:
            try:
                dates = _fred_dates(fred_key, rid, today, horizon)
            except Exception as e:  # noqa: BLE001
                errors.append(f"FRED {kind}: {type(e).__name__}: {e}")
        if not dates:  # fallback anchor table (cpi/jobs only), never empty
            dates = cm.fallback_dates(kind, today, horizon)
        src = "fred" if (fred_key and rid is not None and dates) else "derived"
        for iso in dates:
            _upsert_macro(conn, kind, iso, src)
            count += 1

    # (b) FOMC meeting dates from the Fed page (fallback: anchor table) ------
    fomc_all: list[str] = []
    try:
        resp = requests.get(
            FOMC_PAGE_URL, headers={"User-Agent": config.edgar_user_agent}, timeout=30
        )
        resp.raise_for_status()
        fomc_all = _parse_fomc_page(resp.text)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Fed FOMC page: {type(e).__name__}: {e}")
    fomc_source = "fed"
    if not fomc_all:  # fetch failed or layout changed → verified anchor table
        fomc_all = list(cm.FOMC_DECISION_DAYS)
        fomc_source = "derived"
    for iso in fomc_all:
        if today.isoformat() <= iso <= horizon.isoformat():
            _upsert_macro(conn, "fomc", iso, fomc_source)
            count += 1

    # (c) Derived rules keyed off the FOMC anchor set (live-parsed or fallback),
    # so minutes/beige self-sustain with the meeting calendar. The full set (not
    # just in-window) is used because a minutes date (meeting + 21d) can land in
    # the window while its meeting already passed.
    for iso in cm.fomc_minutes_dates(fomc_all):
        if today.isoformat() <= iso <= horizon.isoformat():
            _upsert_macro(conn, "fomc_minutes", iso, "derived")
            count += 1
    for iso in cm.beige_book_dates(fomc_all):
        if today.isoformat() <= iso <= horizon.isoformat():
            _upsert_macro(conn, "beige_book", iso, "derived")
            count += 1
    for subtype, iso in cm.ism_dates(today, horizon):
        label = f"ISM {subtype.replace('_', ' ')} PMI"
        _upsert_macro(conn, "ism", iso, "derived", title=label, meta={"subtype": subtype})
        count += 1
    for subtype, iso in cm.sentiment_dates(today, horizon):
        label = {
            "conference_board": "Consumer Confidence (Conf. Board)",
            "umich_prelim": "Consumer Sentiment (UMich, prelim)",
            "umich_final": "Consumer Sentiment (UMich, final)",
        }[subtype]
        _upsert_macro(conn, "sentiment", iso, "derived", title=label, meta={"subtype": subtype})
        count += 1

    return count, errors


# -- entry point ------------------------------------------------------------


def collect(config: Config, conn: sqlite3.Connection) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    today = now.date()
    earnings_horizon = today + timedelta(days=EARNINGS_HORIZON_DAYS)
    macro_horizon = today + timedelta(days=MACRO_HORIZON_DAYS)

    token = config.finnhub_api_key  # real misconfig -> whole run fails (isolated)
    fetched = 0
    new = 0
    for h in watchlist.active(conn):
        fetched += _collect_earnings(conn, h, token, today, earnings_horizon)
        fetched += _collect_profile(conn, h, token, today)

    macro_count, macro_errors = _collect_macro(conn, config, today, macro_horizon)
    fetched += macro_count

    # Retention sweep: past events become 'passed' (kept, not deleted — §9).
    sweep_passed(conn, today.isoformat())
    conn.commit()

    # `new` is unused here (upserts, not inserts); everything worth counting is in
    # `fetched`. Surface macro degradation so the health tile goes red while the
    # fallback data we just committed keeps the calendar populated (§5).
    if macro_errors:
        raise CalendarError("macro degraded — " + "; ".join(macro_errors))
    return fetched, new
