"""Post-event outcomes for the morning digest — the released *numbers*.

The calendar is a scheduling layer: it knows *when* an event is due, never what
it says (docs/calendar.md §1). This module is the deliberate complement on the
digest side — it fetches *what happened* (the actual figures) at digest-build
time and hands them to the digest's analysis layer, the same place that already
owns "what mattered". Nothing here is persisted to `calendar_events`; the
calendar keeps its "when only" charter and this stays a read-time enrichment.

Two providers, both already used elsewhere in the app:

  * **FRED** for macro actuals — the fed funds target range, CPI/PCE/PPI, the
    jobs report, GDP. Actuals + prior only: FRED carries no consensus.
  * **Finnhub** ``/stock/earnings`` (free tier, verified) for EPS actual vs
    estimate + the surprise — a real beat/miss for a holding that just reported.

**Consensus (beat/miss vs expectations) has no production-grade free source.**
Finnhub ``/calendar/economic`` is 403 on the free tier; Trading Economics'
guest key was discontinued (HTTP 410); FMP's economic calendar needs a key and
is effectively paywalled. ``macro_consensus()`` is the drop-in seam: it stays
inert (returns None) unless ``FMP_API_KEY`` is set, and its surprise math
(``consensus_from_rows``) is unit-tested, so a working key lights it up with no
further plumbing.

Every fetch is best-effort: any failure returns None and the digest simply omits
that line rather than failing to build.
"""

import requests

from avalpha.calendar_store import KIND_LABELS
from avalpha.config import Config

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/stock/earnings"
# Legacy v3 path; the FMP seam is inert until a key exists (see module docstring).
FMP_ECON_URL = "https://financialmodelingprep.com/api/v3/economic_calendar"

# kind -> FRED series + how to shape the reported figure. Series ids verified
# 2026-09-07 against the live key (all return recent observations).
MACRO_SERIES: dict[str, dict] = {
    "cpi": {"shape": "index", "headline": "CPIAUCSL", "core": "CPILFESL"},
    "pce": {"shape": "index", "headline": "PCEPI", "core": "PCEPILFE"},
    "ppi": {"shape": "index", "headline": "PPIFIS"},
    "jobs": {"shape": "jobs", "payrolls": "PAYEMS", "unrate": "UNRATE"},
    "gdp": {"shape": "rate", "series": "A191RL1Q225SBEA"},
    "fomc": {"shape": "fed_range", "upper": "DFEDTARU", "lower": "DFEDTARL"},
}

# FMP economic-calendar event-name keywords per kind (lower-cased substring).
# Only exercised when the FMP seam is active; unvalidated against a live payload.
FMP_EVENT_KEYS = {
    "cpi": "consumer price index",
    "ppi": "producer price index",
    "pce": "pce price index",
    "jobs": "nonfarm payrolls",
    "gdp": "gdp",
    "fomc": "fed interest rate decision",
}


# -- FRED fetch + math ------------------------------------------------------


def _fred_series(series: str, key: str, limit: int = 15) -> list[tuple[str, float | None]]:
    """Latest `limit` observations for `series`, newest first. Non-numeric FRED
    values (the "." placeholder) come back as None for the caller to skip."""
    resp = requests.get(
        FRED_OBS_URL,
        params={
            "series_id": series,
            "api_key": key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        },
        timeout=30,
    )
    resp.raise_for_status()
    out: list[tuple[str, float | None]] = []
    for o in resp.json().get("observations", []):
        try:
            out.append((o["date"], float(o["value"])))
        except (TypeError, ValueError, KeyError):
            out.append((o.get("date", ""), None))
    return out


def _pct_change(cur: float | None, prev: float | None) -> float | None:
    if cur is None or not prev:
        return None
    return (cur - prev) / prev * 100.0


def _signed(x: float, dp: int = 1) -> str:
    """Signed format that never prints a negative zero ("-0.0" → "+0.0")."""
    if round(x, dp) == 0:
        x = 0.0
    return f"{x:+.{dp}f}"


def _index_line(prefix: str, series: str, key: str) -> str | None:
    """One YoY/MoM line for a price-index series (needs ≥13 monthly points for
    YoY, 14 to show the prior YoY). `prefix` labels the cut ("Core"/"Headline"),
    or "" for a single-series release like PPI."""
    obs = [v for _, v in _fred_series(series, key, 15) if v is not None]
    if len(obs) < 13:
        return None
    yoy = _pct_change(obs[0], obs[12])
    mom = _pct_change(obs[0], obs[1])
    prior_yoy = _pct_change(obs[1], obs[13]) if len(obs) >= 14 else None
    if yoy is None:
        return None
    head = f"{prefix} {yoy:.1f}% YoY" if prefix else f"{yoy:.1f}% YoY"
    if prior_yoy is not None:
        head += f" (prior {prior_yoy:.1f}%, {_signed(yoy - prior_yoy)}pp)"
    if mom is not None:
        head += f", {_signed(mom)}% MoM"
    return head


def _index_lines(cfg: dict, key: str) -> list[str] | None:
    lines: list[str] = []
    if cfg.get("core"):
        for prefix, series in (("Core", cfg["core"]), ("Headline", cfg["headline"])):
            line = _index_line(prefix, series, key)
            if line:
                lines.append(line)
    else:
        line = _index_line("", cfg["headline"], key)
        if line:
            lines.append(line)
    return lines or None


def _jobs_lines(cfg: dict, key: str) -> list[str] | None:
    pay = [v for _, v in _fred_series(cfg["payrolls"], key, 4) if v is not None]
    un = [v for _, v in _fred_series(cfg["unrate"], key, 3) if v is not None]
    lines: list[str] = []
    if len(pay) >= 2:  # PAYEMS is in thousands; the month-over-month diff is "k jobs"
        line = f"Nonfarm payrolls {_signed(pay[0] - pay[1], 0)}k"
        if len(pay) >= 3:
            line += f" (prior {_signed(pay[1] - pay[2], 0)}k)"
        lines.append(line)
    if un:
        line = f"Unemployment {un[0]:.1f}%"
        if len(un) >= 2:
            line += f" (prior {un[1]:.1f}%)"
        lines.append(line)
    return lines or None


def _rate_lines(cfg: dict, key: str) -> list[str] | None:
    obs = [v for _, v in _fred_series(cfg["series"], key, 3) if v is not None]
    if not obs:
        return None
    line = f"Real GDP {obs[0]:.1f}% annualized"
    if len(obs) >= 2:
        line += f" (prior {obs[1]:.1f}%)"
    return [line]


def _fed_range_lines(cfg: dict, key: str) -> list[str] | None:
    """Fed funds target range + the decision. The target series are step
    functions (flat between meetings), so the most recent *distinct* prior value
    is the pre-meeting regime; identical throughout the window means a hold."""
    up = [v for _, v in _fred_series(cfg["upper"], key, 90) if v is not None]
    lo = [v for _, v in _fred_series(cfg["lower"], key, 90) if v is not None]
    if not up or not lo:
        return None
    cur_up, cur_lo = up[0], lo[0]
    rng = f"{cur_lo:.2f}–{cur_up:.2f}%"
    prior_up = next((v for v in up if v != cur_up), None)
    prior_lo = next((v for v in lo if v != cur_lo), None)
    if prior_up is None:
        return [f"Fed funds target held at {rng}"]
    delta_bps = (cur_up - prior_up) * 100
    verb = "cut" if delta_bps < 0 else "raised"
    tail = f" from {prior_lo:.2f}–{prior_up:.2f}%" if prior_lo is not None else ""
    return [f"Fed funds target {verb} {abs(delta_bps):.0f} bps to {rng}{tail}"]


def macro_outcome(kind: str, key: str) -> dict | None:
    """Released figures for a Tier-A macro `kind` from FRED, or None if the kind
    isn't mapped, the data is short, or the fetch fails. Returns
    ``{"kind", "label", "lines"}`` where ``lines`` are ready-to-render strings."""
    cfg = MACRO_SERIES.get(kind)
    if cfg is None:
        return None
    builders = {
        "index": _index_lines,
        "jobs": _jobs_lines,
        "rate": _rate_lines,
        "fed_range": _fed_range_lines,
    }
    try:
        lines = builders[cfg["shape"]](cfg, key)
    except Exception:  # noqa: BLE001 — best-effort: a bad fetch omits the block
        return None
    if not lines:
        return None
    return {"kind": kind, "label": KIND_LABELS.get(kind, kind), "lines": lines}


# -- Finnhub: earnings beat/miss --------------------------------------------


def earnings_outcome(ticker: str, key: str, fiscal_period: str | None = None) -> dict | None:
    """EPS actual vs estimate + surprise for `ticker`'s most recent report (or the
    row matching `fiscal_period`, e.g. "2026Q3"). None if unavailable. Uses the
    free ``/stock/earnings`` endpoint (docs/calendar.md §2)."""
    try:
        resp = requests.get(
            FINNHUB_EARNINGS_URL, params={"symbol": ticker.upper(), "token": key}, timeout=30
        )
        if resp.status_code != 200:
            return None
        rows = resp.json() or []
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    row = None
    if fiscal_period and "Q" in fiscal_period:
        try:
            year, quarter = (int(x) for x in fiscal_period.split("Q"))
            row = next(
                (r for r in rows if r.get("year") == year and r.get("quarter") == quarter),
                None,
            )
        except ValueError:
            row = None
    if row is None:
        row = rows[0]  # /stock/earnings is newest-first; the top row just reported
    est, act = row.get("estimate"), row.get("actual")
    if est is None or act is None:
        return None
    surprise_pct = row.get("surprisePercent")
    if surprise_pct is None and est:
        surprise_pct = (act - est) / abs(est) * 100.0
    return {
        "eps_actual": act,
        "eps_estimate": est,
        "surprise_pct": surprise_pct,
        "beat": act >= est,
        "period": row.get("period"),
    }


# -- consensus seam (inert until an FMP key exists) -------------------------


def consensus_from_rows(rows: list[dict], keyword: str) -> dict | None:
    """Pick the US row whose event name contains `keyword` and compute the
    surprise. Split out from the fetch so the beat/miss math is unit-tested even
    though the live endpoint is paywalled."""
    for r in rows:
        if str(r.get("country", "")).upper() not in ("US", "USA", "UNITED STATES"):
            continue
        if keyword not in str(r.get("event", "")).lower():
            continue
        est, act = r.get("estimate"), r.get("actual")
        if est is None or act is None:
            continue
        return {
            "event": r.get("event"),
            "estimate": est,
            "actual": act,
            "surprise": act - est,
            "beat": act >= est,
        }
    return None


def macro_consensus(kind: str, event_date: str, config: Config) -> dict | None:
    """Consensus/expectations for a macro `kind` on `event_date`, or None.

    The drop-in seam for macro beat/miss. Inert unless ``FMP_API_KEY`` is set;
    the FMP economic-calendar endpoint is effectively paywalled (see the module
    docstring), so in practice this returns None today. Best-effort: any error or
    non-200 also returns None so the digest is never blocked on it."""
    key = config.fmp_api_key
    keyword = FMP_EVENT_KEYS.get(kind)
    if not key or not keyword:
        return None
    try:
        resp = requests.get(
            FMP_ECON_URL,
            params={"from": event_date, "to": event_date, "apikey": key},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        rows = resp.json() or []
    except Exception:  # noqa: BLE001
        return None
    return consensus_from_rows(rows, keyword)
