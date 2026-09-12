"""Portfolio swing alerter: email a holder when one of their holdings makes a
±10% day move (vs the previous close), checked every 15 minutes during the
regular session.

This is a sidecar, not a collector: it *judges* (is this a ≥10% move?) and sends
email, which collectors never do. It reuses the price collector's Finnhub
``/quote`` client (one poll per unique opted-in ticker per cycle) and the
mailer's send path. Opt-in is per portfolio (default off); at most one alert per
portfolio per ticker per Pacific trading day (the ``swing_alerts_sent`` debounce
has no direction component, so a same-day reversal fires only once).

Out of scope for v1 (see the build plan): per-user thresholds, per-holding
toggles, and after-hours alerts.
"""

import sqlite3
from datetime import datetime, timezone

from avalpha import accounts, mailer, watchlist
from avalpha.collectors import prices
from avalpha.config import Config
from avalpha.db import utcnow
from avalpha.market_state import PACIFIC, market_state

SWING_THRESHOLD_PCT = 10.0  # fixed; not user-configurable


def _already_sent(
    conn: sqlite3.Connection, portfolio_id: int, ticker: str, session_date: str
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM swing_alerts_sent "
            "WHERE portfolio_id = ? AND ticker = ? AND session_date = ?",
            (portfolio_id, ticker, session_date),
        ).fetchone()
        is not None
    )


def run_swing(config: Config, conn: sqlite3.Connection) -> str:
    """One swing-alert cycle. Returns a short outcome string for the CLI/logs."""
    now = datetime.now(timezone.utc)
    if market_state(now) != "regular":
        return "swing: skipped (market closed)"

    tickers = watchlist.alert_enabled_tickers(conn)
    if not tickers:
        return "swing: no opted-in tickers"

    session_date = now.astimezone(PACIFIC).date().isoformat()
    token = config.finnhub_api_key

    # One poll per unique ticker; keep only the ±10% breaches as {ticker: (pct, price)}.
    breaches: dict[str, tuple[float, float]] = {}
    for ticker in tickers:
        quote = prices._quote(ticker, token)  # PriceError propagates (bad key / 429)
        if quote is None:
            continue
        pc = quote.get("pc")  # previous close — the day-move reference
        c = quote.get("c")  # current price
        if not pc or not c or pc <= 0:
            continue
        pct = (c - pc) / pc * 100
        if abs(pct) >= SWING_THRESHOLD_PCT:
            breaches[ticker] = (pct, c)

    # Fan out per opted-in portfolio: each owner gets one consolidated email
    # listing their own breached holdings not already alerted today.
    emails_sent = 0
    if breaches:
        for portfolio_id in watchlist.alert_enabled_portfolios(conn):
            batch = [
                (h.ticker, breaches[h.ticker][0], breaches[h.ticker][1])
                for h in watchlist.active(conn, portfolio_id)
                if h.ticker in breaches
                and not _already_sent(conn, portfolio_id, h.ticker, session_date)
            ]
            if not batch:
                continue
            recipient = accounts.portfolio_owner_email(conn, portfolio_id)
            if not recipient:
                continue
            # Record only after a successful send so a send failure retries next
            # cycle. At-least-once: a crash between send and insert could
            # double-send once, which is acceptable for v1.
            mailer.send_swing_alert(config, recipient, batch)
            sent_at = utcnow()
            for ticker, pct, price in batch:
                conn.execute(
                    "INSERT INTO swing_alerts_sent "
                    "(portfolio_id, ticker, session_date, pct, price, sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (portfolio_id, ticker, session_date, pct, price, sent_at),
                )
            conn.commit()
            emails_sent += 1

    return (
        f"swing: {len(tickers)} tickers polled, {len(breaches)} breached, "
        f"{emails_sent} email{'s' if emails_sent != 1 else ''} sent"
    )
