"""Global security catalog and portfolio-scoped holding store."""

import json
import sqlite3
from dataclasses import dataclass

from avalpha.db import utcnow


@dataclass
class Holding:
    ticker: str
    cik: str
    legal_name: str
    aliases: list[str]
    products: list[str]
    executives: list[str]
    ir_feed_url: str | None
    ir_feed_status: str
    weight: float
    shares_outstanding: int | None
    enrichment_confidence: str | None
    active: bool
    industry: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Holding":
        keys = row.keys()
        return cls(
            ticker=row["ticker"],
            cik=row["cik"],
            legal_name=row["legal_name"],
            aliases=json.loads(row["aliases_json"]),
            products=json.loads(row["products_json"]),
            executives=json.loads(row["executives_json"]),
            ir_feed_url=row["ir_feed_url"],
            ir_feed_status=row["ir_feed_status"],
            weight=float(row["weight"]) if "weight" in keys and row["weight"] is not None else 0.0,
            shares_outstanding=row["shares_outstanding"],
            enrichment_confidence=row["enrichment_confidence"],
            active=bool(row["active"]) if "active" in keys and row["active"] is not None else False,
            industry=row["industry"] if "industry" in keys else None,
        )


def _default_portfolio_id(conn: sqlite3.Connection) -> int:
    from avalpha.accounts import default_portfolio_id

    return default_portfolio_id(conn)


def get(
    conn: sqlite3.Connection, ticker: str, portfolio_id: int | None = None
) -> Holding | None:
    """Get global metadata plus position state for one portfolio."""
    portfolio_id = portfolio_id or _default_portfolio_id(conn)
    row = conn.execute(
        """
        SELECT w.*, ph.weight, ph.active
        FROM watchlist w
        LEFT JOIN portfolio_holdings ph
          ON ph.ticker = w.ticker AND ph.portfolio_id = ?
        WHERE w.ticker = ?
        """,
        (portfolio_id, ticker.upper()),
    ).fetchone()
    return Holding.from_row(row) if row else None


def active(
    conn: sqlite3.Connection, portfolio_id: int | None = None
) -> list[Holding]:
    """Active holdings for a portfolio, or the distinct global collector universe."""
    if portfolio_id is None:
        rows = conn.execute(
            """
            SELECT w.*, 0.0 AS weight, 1 AS active
            FROM watchlist w
            WHERE EXISTS (
                SELECT 1 FROM portfolio_holdings ph
                WHERE ph.ticker = w.ticker AND ph.active = 1
            )
            ORDER BY w.ticker
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT w.*, ph.weight, ph.active
            FROM portfolio_holdings ph JOIN watchlist w ON w.ticker = ph.ticker
            WHERE ph.portfolio_id = ? AND ph.active = 1
            ORDER BY w.ticker
            """,
            (portfolio_id,),
        ).fetchall()
    return [Holding.from_row(r) for r in rows]


def alert_enabled_tickers(conn: sqlite3.Connection) -> list[str]:
    """Distinct active tickers across portfolios opted in to swing alerts.

    The swing alerter's dedup primitive: one Finnhub poll per unique ticker,
    scoped to holders who actually want alerts (opted-out portfolios' tickers
    are never polled)."""
    rows = conn.execute(
        """
        SELECT DISTINCT ph.ticker
        FROM portfolio_holdings ph JOIN portfolios p ON p.id = ph.portfolio_id
        WHERE ph.active = 1 AND p.swing_alerts_enabled = 1
        ORDER BY ph.ticker
        """
    ).fetchall()
    return [r["ticker"] for r in rows]


def alert_enabled_portfolios(conn: sqlite3.Connection) -> list[int]:
    """Portfolio ids opted in to swing alerts, for per-owner fan-out."""
    rows = conn.execute(
        "SELECT id FROM portfolios WHERE swing_alerts_enabled = 1 ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows]


def all_holdings(
    conn: sqlite3.Connection, portfolio_id: int | None = None
) -> list[Holding]:
    portfolio_id = portfolio_id or _default_portfolio_id(conn)
    rows = conn.execute(
        """
        SELECT w.*, ph.weight, ph.active
        FROM portfolio_holdings ph JOIN watchlist w ON w.ticker = ph.ticker
        WHERE ph.portfolio_id = ?
        ORDER BY ph.active DESC, ph.weight DESC, w.ticker
        """,
        (portfolio_id,),
    ).fetchall()
    return [Holding.from_row(r) for r in rows]


def upsert(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cik: str,
    legal_name: str,
    aliases: list[str],
    products: list[str],
    executives: list[str],
    ir_feed_url: str | None,
    ir_feed_status: str,
    weight: float = 0.0,
    shares_outstanding: int | None,
    enrichment_confidence: str,
    industry: str | None = None,
    portfolio_id: int | None = None,
) -> None:
    """Upsert catalog metadata and add/reactivate one portfolio position."""
    now = utcnow()
    ticker = ticker.upper()
    conn.execute(
        """
        INSERT INTO watchlist (ticker, cik, legal_name, aliases_json, products_json,
            executives_json, ir_feed_url, ir_feed_status, shares_outstanding,
            enrichment_confidence, industry, enriched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker) DO UPDATE SET
            cik = excluded.cik,
            legal_name = excluded.legal_name,
            aliases_json = excluded.aliases_json,
            products_json = excluded.products_json,
            executives_json = excluded.executives_json,
            ir_feed_url = excluded.ir_feed_url,
            ir_feed_status = excluded.ir_feed_status,
            shares_outstanding = excluded.shares_outstanding,
            enrichment_confidence = excluded.enrichment_confidence,
            industry = COALESCE(excluded.industry, watchlist.industry),
            enriched_at = excluded.enriched_at
        """,
        (
            ticker,
            cik,
            legal_name,
            json.dumps(aliases),
            json.dumps(products),
            json.dumps(executives),
            ir_feed_url,
            ir_feed_status,
            shares_outstanding,
            enrichment_confidence,
            industry,
            now,
        ),
    )
    portfolio_id = portfolio_id or _default_portfolio_id(conn)
    conn.execute(
        """
        INSERT INTO portfolio_holdings
            (portfolio_id, ticker, weight, active, added_at, deactivated_at)
        VALUES (?, ?, ?, 1, ?, NULL)
        ON CONFLICT (portfolio_id, ticker) DO UPDATE SET
            weight = excluded.weight, active = 1, deactivated_at = NULL
        """,
        (portfolio_id, ticker, weight, now),
    )
    conn.commit()


def add_existing(
    conn: sqlite3.Connection, portfolio_id: int, ticker: str, weight: float = 0.0
) -> bool:
    """Add an already-enriched catalog security to a portfolio."""
    if conn.execute(
        "SELECT 1 FROM watchlist WHERE ticker = ?", (ticker.upper(),)
    ).fetchone() is None:
        return False
    now = utcnow()
    conn.execute(
        """
        INSERT INTO portfolio_holdings
            (portfolio_id, ticker, weight, active, added_at, deactivated_at)
        VALUES (?, ?, ?, 1, ?, NULL)
        ON CONFLICT (portfolio_id, ticker) DO UPDATE SET
            active = 1, deactivated_at = NULL
        """,
        (portfolio_id, ticker.upper(), weight, now),
    )
    conn.commit()
    return True


def set_industry(conn: sqlite3.Connection, ticker: str, industry: str | None) -> bool:
    if not industry:
        return False
    cur = conn.execute(
        "UPDATE watchlist SET industry = ? WHERE ticker = ?", (industry, ticker.upper())
    )
    conn.commit()
    return cur.rowcount > 0


def deactivate(
    conn: sqlite3.Connection, ticker: str, portfolio_id: int | None = None
) -> bool:
    portfolio_id = portfolio_id or _default_portfolio_id(conn)
    cur = conn.execute(
        """
        UPDATE portfolio_holdings SET active = 0, deactivated_at = ?
        WHERE portfolio_id = ? AND ticker = ? AND active = 1
        """,
        (utcnow(), portfolio_id, ticker.upper()),
    )
    conn.commit()
    return cur.rowcount > 0


def activate(
    conn: sqlite3.Connection, ticker: str, portfolio_id: int | None = None
) -> bool:
    portfolio_id = portfolio_id or _default_portfolio_id(conn)
    cur = conn.execute(
        """
        UPDATE portfolio_holdings SET active = 1, deactivated_at = NULL
        WHERE portfolio_id = ? AND ticker = ?
        """,
        (portfolio_id, ticker.upper()),
    )
    conn.commit()
    return cur.rowcount > 0


def set_weight(
    conn: sqlite3.Connection,
    ticker: str,
    weight: float,
    portfolio_id: int | None = None,
) -> bool:
    portfolio_id = portfolio_id or _default_portfolio_id(conn)
    cur = conn.execute(
        "UPDATE portfolio_holdings SET weight = ? "
        "WHERE portfolio_id = ? AND ticker = ?",
        (weight, portfolio_id, ticker.upper()),
    )
    conn.commit()
    return cur.rowcount > 0
