"""Watchlist store. Removal deactivates; rows are never deleted."""

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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Holding":
        return cls(
            ticker=row["ticker"],
            cik=row["cik"],
            legal_name=row["legal_name"],
            aliases=json.loads(row["aliases_json"]),
            products=json.loads(row["products_json"]),
            executives=json.loads(row["executives_json"]),
            ir_feed_url=row["ir_feed_url"],
            ir_feed_status=row["ir_feed_status"],
            weight=row["weight"],
            shares_outstanding=row["shares_outstanding"],
            enrichment_confidence=row["enrichment_confidence"],
            active=bool(row["active"]),
        )


def get(conn: sqlite3.Connection, ticker: str) -> Holding | None:
    row = conn.execute("SELECT * FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
    return Holding.from_row(row) if row else None


def active(conn: sqlite3.Connection) -> list[Holding]:
    rows = conn.execute(
        "SELECT * FROM watchlist WHERE active = 1 ORDER BY ticker"
    ).fetchall()
    return [Holding.from_row(r) for r in rows]


def all_holdings(conn: sqlite3.Connection) -> list[Holding]:
    rows = conn.execute("SELECT * FROM watchlist ORDER BY ticker").fetchall()
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
    weight: float,
    shares_outstanding: int | None,
    enrichment_confidence: str,
) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO watchlist (ticker, cik, legal_name, aliases_json, products_json,
            executives_json, ir_feed_url, ir_feed_status, weight, shares_outstanding,
            enrichment_confidence, enriched_at, active, added_at, deactivated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL)
        ON CONFLICT (ticker) DO UPDATE SET
            cik = excluded.cik,
            legal_name = excluded.legal_name,
            aliases_json = excluded.aliases_json,
            products_json = excluded.products_json,
            executives_json = excluded.executives_json,
            ir_feed_url = excluded.ir_feed_url,
            ir_feed_status = excluded.ir_feed_status,
            weight = excluded.weight,
            shares_outstanding = excluded.shares_outstanding,
            enrichment_confidence = excluded.enrichment_confidence,
            enriched_at = excluded.enriched_at,
            active = 1,
            deactivated_at = NULL
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
            weight,
            shares_outstanding,
            enrichment_confidence,
            now,
            now,
        ),
    )
    conn.commit()


def deactivate(conn: sqlite3.Connection, ticker: str) -> bool:
    cur = conn.execute(
        "UPDATE watchlist SET active = 0, deactivated_at = ? WHERE ticker = ? AND active = 1",
        (utcnow(), ticker),
    )
    conn.commit()
    return cur.rowcount > 0


def set_weight(conn: sqlite3.Connection, ticker: str, weight: float) -> bool:
    cur = conn.execute(
        "UPDATE watchlist SET weight = ? WHERE ticker = ?", (weight, ticker)
    )
    conn.commit()
    return cur.rowcount > 0
