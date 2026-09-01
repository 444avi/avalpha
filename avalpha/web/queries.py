"""Read helpers for the web console.

Every function takes a live sqlite3 connection and returns plain dicts/lists so
the Jinja templates stay dumb. Nothing here writes; portfolio edits and job
triggers live in the route handlers and jobs module.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from avalpha.collectors import SOURCES
from avalpha.scorer import PROMPT_VERSION
from avalpha.watchlist import Holding

DIRECTION_CLASS = {"positive": "pos", "negative": "neg", "neutral": "neu", "mixed": "neu"}


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
