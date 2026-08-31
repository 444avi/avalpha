"""Scorer: the only real LLM stage.

Input is an item plus one matched ticker; output is structured JSON and
nothing else. The scorer does not know what email is — it writes verdicts to
the scores table and delivery decides what to do with them.

Scores are append-only per prompt_version: bump PROMPT_VERSION whenever the
prompt text changes, and `avalpha replay` re-scores history at the new version
while keeping old verdicts.
"""

import json
import sqlite3
import sys
import time

from avalpha import watchlist
from avalpha.config import Config
from avalpha.db import utcnow

PROMPT_VERSION = "v1"

CATEGORIES = [
    "earnings",
    "guidance",
    "M&A",
    "legal",
    "regulatory",
    "product",
    "personnel",
    "capital_structure",
    "other",
]

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string"},
        "materiality": {"type": "integer", "enum": list(range(11))},
        "direction": {"type": "string", "enum": ["positive", "negative", "unclear"]},
        "category": {"type": "string", "enum": CATEGORIES},
        "mechanism": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["ticker", "materiality", "direction", "category", "mechanism", "summary"],
    "additionalProperties": False,
}

SCORE_PROMPT = """\
You score public information items for a portfolio monitoring system. Your
verdicts are logged and audited; they do not trigger trades.

The question is narrow. Not "is this interesting?" but: would a reasonable
investor holding {ticker} change their position size based on this item alone?
Score materiality 0-10 on that question. 0-2: noise, routine coverage, or
already-priced information. 3-5: worth knowing, would not alone change a
position. 6-8: could plausibly change a position. 9-10: demands immediate
attention (restatements, CEO exits, transformative M&A).

Company: {legal_name} ({ticker})
Approximate market cap: {market_cap}
{filing_context}
Judge magnitude relative to this company's size — a $500M contract is
transformative for a $3B company and rounding error for a $3T one.

The mechanism field must name the concrete causal path from this event to the
stock price in one clause (e.g. "guidance cut implies datacenter demand is
softening"). If you cannot name a concrete mechanism, the materiality score
should be low.

Item source: {source}
Item published: {published_at}
Item title: {title}
Item text (may be truncated):
{text}
"""


def _market_cap_str(conn: sqlite3.Connection, holding: watchlist.Holding) -> str:
    if not holding.shares_outstanding:
        return "unknown"
    row = conn.execute(
        "SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        (holding.ticker,),
    ).fetchone()
    if not row or not row["close"]:
        return "unknown"
    cap = row["close"] * holding.shares_outstanding
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if cap >= threshold:
            return f"${cap / threshold:.1f}{suffix}"
    return f"${cap:,.0f}"


def _pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.item_id, m.ticker, i.source, i.title, i.raw_text, i.published_at,
               i.meta_json
        FROM item_matches m
        JOIN items i ON i.id = m.item_id
        WHERE m.confirmed = 1
          AND NOT EXISTS (
            SELECT 1 FROM scores s
            WHERE s.item_id = m.item_id AND s.ticker = m.ticker
              AND s.prompt_version = ?
          )
        ORDER BY m.item_id
        LIMIT ?
        """,
        (PROMPT_VERSION, limit),
    ).fetchall()


def score_one(config: Config, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    import anthropic

    holding = watchlist.get(conn, row["ticker"])
    if holding is None:
        raise RuntimeError(f"no watchlist row for {row['ticker']}")

    meta = json.loads(row["meta_json"])
    filing_context = ""
    if row["source"] == "edgar":
        form = meta.get("form", "")
        items = meta.get("items", "")
        filing_context = f"This is an SEC filing: form {form}"
        if items:
            filing_context += f", 8-K items {items}"
        filing_context += ".\n"

    prompt = SCORE_PROMPT.format(
        ticker=holding.ticker,
        legal_name=holding.legal_name,
        market_cap=_market_cap_str(conn, holding),
        filing_context=filing_context,
        source=row["source"],
        published_at=row["published_at"] or "unknown",
        title=row["title"],
        text=row["raw_text"][:6000] or "(no body text; judge from the title)",
    )

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=config.model_scorer,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": SCORE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    verdict = json.loads(text)

    conn.execute(
        """
        INSERT OR IGNORE INTO scores (item_id, ticker, prompt_version, model,
            materiality, direction, category, mechanism, summary, raw_json, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["item_id"],
            holding.ticker,
            PROMPT_VERSION,
            config.model_scorer,
            int(verdict["materiality"]),
            verdict["direction"],
            verdict["category"],
            verdict["mechanism"],
            verdict["summary"],
            text,
            utcnow(),
        ),
    )
    conn.commit()
    return verdict


def drain(config: Config, conn: sqlite3.Connection, limit: int = 200) -> tuple[int, int]:
    """Score everything pending. Returns (scored, failed)."""
    scored = 0
    failed = 0
    for row in _pending(conn, limit):
        try:
            score_one(config, conn, row)
            scored += 1
        except Exception as e:
            failed += 1
            print(
                f"scorer: item {row['item_id']}/{row['ticker']} failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            # Back off briefly; the item stays queued and will be retried.
            time.sleep(5)
    return scored, failed


def run_worker(config: Config, conn: sqlite3.Connection, once: bool = False) -> None:
    while True:
        scored, failed = drain(config, conn)
        if scored or failed:
            print(f"scorer: {scored} scored, {failed} failed")
        if once:
            return
        time.sleep(30)


def replay_range(
    config: Config,
    conn: sqlite3.Connection,
    start: str,
    end: str,
    ticker: str | None = None,
) -> str:
    """Re-score stored items fetched in [start, end] at the current prompt.

    Uses the same scoring path; the unique (item, ticker, prompt_version)
    index means already-current verdicts are skipped automatically.
    """
    params: list = [f"{start}T00:00:00Z", f"{end}T23:59:59Z", PROMPT_VERSION]
    ticker_clause = ""
    if ticker:
        ticker_clause = "AND m.ticker = ?"
        params.append(ticker.upper())
    rows = conn.execute(
        f"""
        SELECT m.item_id, m.ticker, i.source, i.title, i.raw_text, i.published_at,
               i.meta_json
        FROM item_matches m
        JOIN items i ON i.id = m.item_id
        WHERE m.confirmed = 1
          AND i.fetched_at BETWEEN ? AND ?
          AND NOT EXISTS (
            SELECT 1 FROM scores s
            WHERE s.item_id = m.item_id AND s.ticker = m.ticker
              AND s.prompt_version = ?
          )
          {ticker_clause}
        ORDER BY m.item_id
        """,
        params,
    ).fetchall()

    scored = 0
    failed = 0
    for row in rows:
        try:
            score_one(config, conn, row)
            scored += 1
        except Exception as e:
            failed += 1
            print(
                f"replay: item {row['item_id']}/{row['ticker']} failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
    return (
        f"replay {start}..{end} at {PROMPT_VERSION}: {len(rows)} pending, "
        f"{scored} scored, {failed} failed"
    )
