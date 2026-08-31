"""End-of-day price collector via Finnhub.

Finnhub's free-tier /quote endpoint returns the latest session's OHLC plus the
previous close for one symbol. Run once daily (after the close, or before the
next open), it gives the prior-day price action the digest header needs and the
latest close the scorer uses for market-cap normalization. We store one row per
(ticker, session date) in the prices table; the digest computes the % move by
comparing consecutive stored closes.

Needs FINNHUB_API_KEY. Volume is not part of /quote, so that column stays null.
"""

import sqlite3
from datetime import datetime, timezone

import requests

from avalpha import watchlist
from avalpha.config import Config

QUOTE_URL = "https://finnhub.io/api/v1/quote"


class PriceError(Exception):
    pass


def _quote(symbol: str, token: str) -> dict | None:
    resp = requests.get(
        QUOTE_URL, params={"symbol": symbol, "token": token}, timeout=30
    )
    if resp.status_code == 401 or resp.status_code == 403:
        raise PriceError(
            f"Finnhub rejected the key ({resp.status_code}); check FINNHUB_API_KEY"
        )
    if resp.status_code == 429:
        raise PriceError("Finnhub rate limit hit (free tier is 60 calls/min)")
    resp.raise_for_status()
    data = resp.json()
    # A valid quote has a nonzero close and a trade timestamp; unknown symbols
    # come back all-zero.
    if not data.get("c") or not data.get("t"):
        return None
    return data


def collect(config: Config, conn: sqlite3.Connection) -> tuple[int, int]:
    token = config.finnhub_api_key
    fetched = 0
    new = 0
    for holding in watchlist.active(conn):
        quote = _quote(holding.ticker.upper(), token)
        if quote is None:
            continue
        session_date = (
            datetime.fromtimestamp(quote["t"], tz=timezone.utc).date().isoformat()
        )
        fetched += 1
        cur = conn.execute(
            "INSERT OR IGNORE INTO prices (ticker, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                holding.ticker,
                session_date,
                quote.get("o"),
                quote.get("h"),
                quote.get("l"),
                quote.get("c"),
            ),
        )
        if cur.rowcount > 0:
            new += 1
    conn.commit()
    return fetched, new
