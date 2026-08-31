"""Morning digest builder.

Each digest covers everything fetched since the previous digest was built
(first run: trailing 48h), labeled with the prior trading day. One page per
active holding plus a cover page. Quiet pages say so explicitly — and cost no
LLM call.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from avalpha import watchlist
from avalpha.config import Config
from avalpha.db import utcnow
from avalpha.market_state import PACIFIC, prior_trading_day
from avalpha.scorer import PROMPT_VERSION

MAX_ITEMS_PER_PAGE = 8

NARRATIVE_PROMPT = """\
You write the "what mattered" section of a morning portfolio digest page for
{ticker} ({legal_name}), covering the previous trading day. Below are the
scored items. Write 2-3 plain sentences on what actually mattered and why it
could move the stock. No preamble, no bullet points, no hedging boilerplate.
If the items are all minor, say so plainly in one sentence.

Items:
{items}
"""

COVER_PROMPT = """\
You write the cover page of a morning portfolio digest. Below are per-holding
summaries of the previous trading day. Write 3-5 sentences on portfolio-level
themes and anything spanning multiple holdings. Mention only what is supported
by the items below. No preamble, no bullet points.

{sections}
"""


def _window(conn: sqlite3.Connection, now: datetime) -> tuple[str, str]:
    row = conn.execute(
        "SELECT built_at FROM digests ORDER BY built_at DESC LIMIT 1"
    ).fetchone()
    if row:
        start = row["built_at"]
    else:
        start = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _price_action(conn, ticker: str, label_date: str):
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE ticker = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 2",
        (ticker, label_date),
    ).fetchall()
    if not rows:
        return None, None
    close = rows[0]["close"]
    if len(rows) < 2 or not rows[1]["close"]:
        return close, None
    pct = (close - rows[1]["close"]) / rows[1]["close"] * 100
    return close, pct


def _scored_items(conn, ticker: str, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.materiality, s.direction, s.category, s.mechanism, s.summary,
               i.source, i.published_at, i.fetched_at
        FROM scores s
        JOIN items i ON i.id = s.item_id
        WHERE s.ticker = ? AND s.prompt_version = ?
          AND i.fetched_at > ? AND i.fetched_at <= ?
        ORDER BY s.materiality DESC, i.fetched_at DESC
        """,
        (ticker, PROMPT_VERSION, start, end),
    ).fetchall()
    items = []
    for r in rows[:MAX_ITEMS_PER_PAGE]:
        when = (r["published_at"] or r["fetched_at"] or "")[:16].replace("T", " ")
        items.append(
            {
                "materiality": r["materiality"],
                "direction": r["direction"],
                "category": r["category"],
                "mechanism": r["mechanism"],
                "summary": r["summary"],
                "source": r["source"],
                "when": when,
            }
        )
    return items


def _insider_filings(conn, cik: str, start: str, end: str) -> list[str]:
    rows = conn.execute(
        "SELECT title, meta_json FROM items WHERE source = 'edgar' "
        "AND fetched_at > ? AND fetched_at <= ?",
        (start, end),
    ).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["meta_json"])
        if meta.get("cik") == cik and meta.get("form", "").startswith("4"):
            out.append(r["title"])
    return out


def _reddit_stats(conn, ticker: str, start: str, end: str) -> tuple[int, float]:
    window = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM reddit_mentions "
        "WHERE ticker = ? AND window_start > ? AND window_start <= ?",
        (ticker, start, end),
    ).fetchone()[0]
    week_ago = (
        datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM reddit_mentions "
        "WHERE ticker = ? AND window_start > ? AND window_start <= ?",
        (ticker, week_ago, end),
    ).fetchone()[0]
    return int(window), total / 7.0


def _llm_text(config: Config, prompt: str, max_tokens: int = 512) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=config.model_narrative,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def build_digest(
    config: Config, conn: sqlite3.Connection, date_str: str | None = None
) -> Path:
    now = datetime.now(timezone.utc)
    label_date = date_str or prior_trading_day(now.astimezone(PACIFIC).date()).isoformat()
    start, end = _window(conn, now)

    holdings_data = []
    for h in watchlist.active(conn):
        close, pct = _price_action(conn, h.ticker, label_date)
        items = _scored_items(conn, h.ticker, start, end)
        insiders = _insider_filings(conn, h.cik, start, end)
        reddit_count, reddit_baseline = _reddit_stats(conn, h.ticker, start, end)

        narrative = ""
        if items:
            listing = "\n".join(
                f"- [{it['materiality']}/10 {it['category']}] {it['summary']} "
                f"(mechanism: {it['mechanism']})"
                for it in items
            )
            narrative = _llm_text(
                config,
                NARRATIVE_PROMPT.format(
                    ticker=h.ticker, legal_name=h.legal_name, items=listing
                ),
            )

        direction = "flat"
        if pct is not None:
            direction = "up" if pct > 0.05 else ("down" if pct < -0.05 else "flat")

        holdings_data.append(
            {
                "ticker": h.ticker,
                "name": h.legal_name,
                "close": close,
                "pct": pct,
                "direction": direction,
                "narrative": narrative,
                "bullets": items,
                "insider_filings": insiders,
                "reddit_count": reddit_count,
                "reddit_baseline": reddit_baseline,
            }
        )

    active_sections = [
        f"{d['ticker']}: {d['narrative']}" for d in holdings_data if d["narrative"]
    ]
    if active_sections:
        cover_text = _llm_text(
            config, COVER_PROMPT.format(sections="\n".join(active_sections))
        )
    else:
        cover_text = "Quiet day across the portfolio — nothing material at any holding."

    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parent), autoescape=True
    )
    html = env.get_template("template.html").render(
        label_date=label_date,
        built_at=end[:16].replace("T", " "),
        holdings=holdings_data,
        cover_text=cover_text,
    )

    config.digest_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = config.digest_dir / f"avalpha-{label_date}.pdf"
    # WeasyPrint needs native pango/cairo; import lazily so the rest of the
    # CLI works on a box where those aren't installed.
    from weasyprint import HTML

    HTML(string=html).write_pdf(pdf_path)

    conn.execute(
        "INSERT INTO digests (date, built_at, pdf_path) VALUES (?, ?, ?) "
        "ON CONFLICT (date) DO UPDATE SET built_at = excluded.built_at, "
        "pdf_path = excluded.pdf_path",
        (label_date, utcnow(), str(pdf_path)),
    )
    conn.commit()
    return pdf_path


def build_and_send(
    config: Config, conn: sqlite3.Connection, date_str: str | None = None
) -> None:
    from avalpha.mailer import send_digest_email

    now = datetime.now(timezone.utc)
    label_date = date_str or prior_trading_day(now.astimezone(PACIFIC).date()).isoformat()
    already = conn.execute(
        "SELECT sent_at FROM digests WHERE date = ? AND sent_at IS NOT NULL",
        (label_date,),
    ).fetchone()
    if already:
        print(f"digest for {label_date} already sent at {already['sent_at']}; skipping")
        return

    pdf_path = build_digest(config, conn, date_str=label_date)
    send_digest_email(config, pdf_path, label_date)
    conn.execute(
        "UPDATE digests SET sent_at = ? WHERE date = ?", (utcnow(), label_date)
    )
    conn.commit()
    print(f"digest for {label_date} sent to {config.email_recipient}")
