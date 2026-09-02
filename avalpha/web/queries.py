"""Read helpers for the web console.

Every function takes a live sqlite3 connection and returns plain dicts/lists so
the Jinja templates stay dumb. Nothing here writes; portfolio edits and job
triggers live in the route handlers and jobs module.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from avalpha.calendar_store import (
    KIND_LABELS,
    TIER_A_MACRO,
    TIER_B_MACRO,
)
from avalpha.collectors import SOURCES
from avalpha.scorer import PROMPT_VERSION
from avalpha.watchlist import Holding

DIRECTION_CLASS = {"positive": "pos", "negative": "neg", "neutral": "neu", "mixed": "neu"}

_ET = ZoneInfo("America/New_York")
_ACTIVE_STATUSES = ("scheduled", "confirmed", "tentative")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_prices(conn: sqlite3.Connection, ticker: str) -> dict:
    """Most recent close and the prior close, for a day-change figure."""
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 2",
        (ticker,),
    ).fetchall()
    if not rows:
        return {"close": None, "prev": None, "date": None, "change_pct": None}
    close = rows[0]["close"]
    prev = rows[1]["close"] if len(rows) > 1 else None
    change = None
    if close is not None and prev:
        change = (close - prev) / prev * 100
    return {"close": close, "prev": prev, "date": rows[0]["date"], "change_pct": change}


def portfolio(conn: sqlite3.Connection, include_inactive: bool = True) -> list[dict]:
    """Holdings with target weight, latest price, day change, market cap, and a
    7-day scored-item count — the dashboard's main table."""
    week_ago = _iso(_utcnow() - timedelta(days=7))
    rows = conn.execute(
        "SELECT * FROM watchlist ORDER BY active DESC, weight DESC, ticker"
    ).fetchall()
    out = []
    for row in rows:
        h = Holding.from_row(row)
        if not include_inactive and not h.active:
            continue
        price = latest_prices(conn, h.ticker)
        market_cap = None
        if h.shares_outstanding and price["close"] is not None:
            market_cap = h.shares_outstanding * price["close"]
        scored_7d = conn.execute(
            "SELECT COUNT(*) FROM scores WHERE ticker = ? AND prompt_version = ? "
            "AND scored_at > ?",
            (h.ticker, PROMPT_VERSION, week_ago),
        ).fetchone()[0]
        out.append(
            {
                "ticker": h.ticker,
                "legal_name": h.legal_name,
                "weight": h.weight,
                "active": h.active,
                "confidence": h.enrichment_confidence,
                "ir_feed": h.ir_feed_status == "ok",
                "price": price,
                "market_cap": market_cap,
                "scored_7d": scored_7d,
            }
        )
    return out


def total_weight(holdings: list[dict]) -> float:
    return sum(h["weight"] for h in holdings if h["active"])


def recent_scores(
    conn: sqlite3.Connection, ticker: str | None = None, limit: int = 40
) -> list[dict]:
    """Latest scored news items (current prompt version), newest first."""
    params: list = [PROMPT_VERSION]
    where = "s.prompt_version = ?"
    if ticker:
        where += " AND s.ticker = ?"
        params.append(ticker)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT s.ticker, s.materiality, s.direction, s.category, s.mechanism,
               s.summary, s.scored_at, i.title, i.url, i.source, i.published_at
        FROM scores s JOIN items i ON i.id = s.item_id
        WHERE {where}
        ORDER BY s.scored_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        {
            "ticker": r["ticker"],
            "materiality": r["materiality"],
            "direction": r["direction"],
            "direction_class": DIRECTION_CLASS.get(r["direction"], "neu"),
            "category": r["category"],
            "mechanism": r["mechanism"],
            "summary": r["summary"],
            "scored_at": r["scored_at"],
            "title": r["title"],
            "url": r["url"],
            "source": r["source"],
            "published_at": r["published_at"],
        }
        for r in rows
    ]


def holding_detail(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute("SELECT * FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
    if not row:
        return None
    h = Holding.from_row(row)
    price = latest_prices(conn, ticker)
    spark = conn.execute(
        "SELECT date, close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 30",
        (ticker,),
    ).fetchall()
    return {
        "holding": h,
        "price": price,
        "market_cap": (
            h.shares_outstanding * price["close"]
            if h.shares_outstanding and price["close"] is not None
            else None
        ),
        "spark": [{"date": r["date"], "close": r["close"]} for r in reversed(spark)],
        "scores": recent_scores(conn, ticker=ticker, limit=60),
    }


def health(conn: sqlite3.Connection) -> dict:
    """Collector freshness, queue depths, latest digest — the ops tiles."""
    now = _utcnow()
    day_ago = _iso(now - timedelta(hours=24))
    collectors = []
    for source in SOURCES:
        last_ok = conn.execute(
            "SELECT started_at FROM collector_runs WHERE source = ? AND ok = 1 "
            "ORDER BY started_at DESC LIMIT 1",
            (source,),
        ).fetchone()
        errors = conn.execute(
            "SELECT COUNT(*) FROM collector_runs WHERE source = ? AND ok = 0 "
            "AND started_at > ?",
            (source, day_ago),
        ).fetchone()[0]
        new_24h = conn.execute(
            "SELECT COALESCE(SUM(items_new), 0) FROM collector_runs "
            "WHERE source = ? AND ok = 1 AND started_at > ?",
            (source, day_ago),
        ).fetchone()[0]
        collectors.append(
            {
                "source": source,
                "last_ok": last_ok["started_at"] if last_ok else None,
                "errors_24h": errors,
                "new_24h": new_24h,
                "ok": errors == 0,
            }
        )

    unmatched = conn.execute(
        "SELECT COUNT(*) FROM items i LEFT JOIN matcher_done d ON d.item_id = i.id "
        "WHERE d.item_id IS NULL"
    ).fetchone()[0]
    unscored = conn.execute(
        """
        SELECT COUNT(*) FROM item_matches m
        WHERE m.confirmed = 1 AND NOT EXISTS (
            SELECT 1 FROM scores s WHERE s.item_id = m.item_id
              AND s.ticker = m.ticker AND s.prompt_version = ?
        )
        """,
        (PROMPT_VERSION,),
    ).fetchone()[0]
    total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    last_digest = conn.execute(
        "SELECT date, built_at, sent_at FROM digests ORDER BY date DESC LIMIT 1"
    ).fetchone()

    return {
        "collectors": collectors,
        "queues": {
            "total_items": total_items,
            "awaiting_matcher": unmatched,
            "awaiting_scorer": unscored,
        },
        "digest": (
            {
                "date": last_digest["date"],
                "built_at": last_digest["built_at"],
                "sent_at": last_digest["sent_at"],
            }
            if last_digest
            else None
        ),
    }


def digests(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT date, built_at, sent_at, pdf_path FROM digests ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_jobs(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    rows = conn.execute(
        "SELECT id, job, status, triggered_by, started_at, finished_at, output "
        "FROM web_jobs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# -- calendar ---------------------------------------------------------------


def _et_clock(event_at: str | None) -> str:
    """UTC "…Z" instant → an ET wall-clock label like "8:30a ET"."""
    if not event_at:
        return ""
    try:
        utc = datetime.strptime(event_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    local = utc.astimezone(_ET)
    hour = local.hour % 12 or 12
    ampm = "a" if local.hour < 12 else "p"
    mins = f":{local.minute:02d}" if local.minute else ""
    return f"{hour}{mins}{ampm} ET"


def _tier(kind: str) -> str:
    if kind in TIER_A_MACRO:
        return "A"
    if kind in TIER_B_MACRO:
        return "B"
    return "company"


def _shape_event(row: sqlite3.Row, today: date) -> dict:
    """One calendar row → a display dict. Presents timed events honestly (§9)."""
    kind = row["kind"]
    ev_date = date.fromisoformat(row["event_date"])
    days_until = (ev_date - today).days
    meta = json.loads(row["meta_json"] or "{}")
    is_macro = row["ticker"] is None

    # When-of-day, honestly. Earnings uses the after-close/before-open phrasing;
    # other timed events show their ET release clock.
    if kind == "earnings":
        when = {"amc": "after close", "bmo": "before open"}.get(meta.get("hour"), "")
    elif row["is_timed"]:
        when = _et_clock(row["event_at"])
    else:
        when = ""

    if row["status"] == "confirmed":
        chip, chip_cls = "confirmed", "chip-ok"
    elif row["status"] == "tentative":
        chip, chip_cls = "tentative", "chip-muted"
    elif row["confidence"] in ("low", "medium") and not is_macro:
        chip, chip_cls = "est.", "chip-muted"
    else:
        chip, chip_cls = None, ""

    if days_until < 0:
        rel = f"{-days_until}d ago"
    elif days_until == 0:
        rel = "today"
    elif days_until == 1:
        rel = "tomorrow"
    else:
        rel = f"in {days_until}d"

    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "kind": kind,
        "label": KIND_LABELS.get(kind, kind),
        "title": row["title"],
        "event_date": row["event_date"],
        "date_label": ev_date.strftime("%a %b %-d"),
        "when": when,
        "status": row["status"],
        "chip": chip,
        "chip_class": chip_cls,
        "source": row["source"],
        "confidence": row["confidence"],
        "tier": _tier(kind),
        "is_macro": is_macro,
        "days_until": days_until,
        "rel": rel,
        "near": 0 <= days_until <= 7,  # gold-accent high-signal window
        "editable": row["source"] == "manual",
        "meta": meta,
    }


def _visible_rows(
    conn: sqlite3.Connection, today: date, include_passed: bool
) -> list[sqlite3.Row]:
    """Company events for *active* holdings + all macro. Passed/cancelled hidden
    by default (relevance rules, §7)."""
    sql = (
        "SELECT c.* FROM calendar_events c "
        "LEFT JOIN watchlist w ON w.ticker = c.ticker "
        "WHERE (c.ticker IS NULL OR w.active = 1) "
    )
    params: list = []
    if not include_passed:
        sql += (
            "AND c.status IN ('scheduled','confirmed','tentative') "
            "AND c.event_date >= ? "
        )
        params.append(today.isoformat())
    sql += "ORDER BY c.event_date, c.is_timed DESC, c.ticker IS NULL, c.ticker"
    return conn.execute(sql, params).fetchall()


def calendar_agenda(
    conn: sqlite3.Connection, include_passed: bool = False, horizon_days: int = 120
) -> list[dict]:
    """Agenda grouped by week (docs/calendar.md §7). Each group carries `events`
    (company + Tier A macro, shown inline) and `tier_b` (collapsed "More macro")."""
    today = _utcnow().date()
    horizon = today + timedelta(days=horizon_days)
    groups: dict[str, dict] = {}
    for row in _visible_rows(conn, today, include_passed):
        ev_date = date.fromisoformat(row["event_date"])
        if not include_passed and ev_date > horizon:
            continue
        monday = ev_date - timedelta(days=ev_date.weekday())
        key = monday.isoformat()
        if key not in groups:
            weeks_out = (monday - (today - timedelta(days=today.weekday()))).days // 7
            if weeks_out <= 0:
                label = "This week"
            elif weeks_out == 1:
                label = "Next week"
            else:
                label = f"Week of {monday.strftime('%b %-d')}"
            groups[key] = {
                "week_start": key,
                "label": label,
                "sub": f"{monday.strftime('%b %-d')} – "
                f"{(monday + timedelta(days=6)).strftime('%b %-d')}",
                "events": [],
                "tier_b": [],
            }
        shaped = _shape_event(row, today)
        if shaped["tier"] == "B":
            groups[key]["tier_b"].append(shaped)
        else:
            groups[key]["events"].append(shaped)
    # Drop weeks that ended up with only hidden Tier B and nothing else? Keep them
    # — the "More macro" toggle still surfaces them. Order weeks chronologically.
    return [groups[k] for k in sorted(groups)]


def upcoming_events(
    conn: sqlite3.Connection, days: int = 7, include_tier_b: bool = False
) -> list[dict]:
    """Flat next-`days` list for the dashboard strip and the digest. Tier B macro
    excluded unless asked (never in the digest, §7)."""
    today = _utcnow().date()
    horizon = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT c.* FROM calendar_events c "
        "LEFT JOIN watchlist w ON w.ticker = c.ticker "
        "WHERE (c.ticker IS NULL OR w.active = 1) "
        "AND c.status IN ('scheduled','confirmed','tentative') "
        "AND c.event_date >= ? AND c.event_date <= ? "
        "ORDER BY c.event_date, c.is_timed DESC, c.ticker IS NULL, c.ticker",
        (today.isoformat(), horizon),
    ).fetchall()
    out = [_shape_event(r, today) for r in rows]
    if not include_tier_b:
        out = [e for e in out if e["tier"] != "B"]
    return out


def events_for_ticker(conn: sqlite3.Connection, ticker: str, limit: int = 12) -> list[dict]:
    """Upcoming events for one holding — the detail page's catalyst list."""
    today = _utcnow().date()
    rows = conn.execute(
        "SELECT * FROM calendar_events WHERE ticker = ? "
        "AND status IN ('scheduled','confirmed','tentative') AND event_date >= ? "
        "ORDER BY event_date LIMIT ?",
        (ticker.upper(), today.isoformat(), limit),
    ).fetchall()
    return [_shape_event(r, today) for r in rows]


def ticker_badges(conn: sqlite3.Connection, days: int = 21) -> dict[str, dict]:
    """Nearest upcoming company event per active ticker within `days` — the
    dashboard per-row badge ("Earnings in 3d")."""
    today = _utcnow().date()
    horizon = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT c.* FROM calendar_events c "
        "JOIN watchlist w ON w.ticker = c.ticker AND w.active = 1 "
        "WHERE c.status IN ('scheduled','confirmed','tentative') "
        "AND c.event_date >= ? AND c.event_date <= ? "
        "ORDER BY c.event_date, c.is_timed DESC",
        (today.isoformat(), horizon),
    ).fetchall()
    badges: dict[str, dict] = {}
    for r in rows:
        if r["ticker"] not in badges:  # first (soonest) wins
            badges[r["ticker"]] = _shape_event(r, today)
    return badges
