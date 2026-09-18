"""Morning digest builder.

Each digest covers everything fetched since the previous digest was *sent*
(first run: trailing 48h), labeled with the calendar day it is built. One page
per active holding plus a cover page. Quiet pages say so explicitly — and cost
no LLM call.

The digest runs every day, weekends included: market-moving news (filings, IR,
macro) lands on Saturdays and Sundays too, and each day gets its own edition.
That is why the identity/dedup key is the calendar day rather than the prior
trading day — see ``_digest_date``.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from avalpha import watchlist
from avalpha.config import Config
from avalpha.db import utcnow
from avalpha.market_state import PACIFIC
from avalpha.scorer import PROMPT_VERSION

MAX_ITEMS_PER_PAGE = 8

NARRATIVE_PROMPT = """\
You write the "what mattered" section of a morning portfolio digest page for
{ticker} ({legal_name}), covering news since the last digest (which may span a
weekend). Below are the scored items. Write 2-3 plain sentences on what actually
mattered and why it could move the stock. No preamble, no bullet points, no
hedging boilerplate. If the items are all minor, say so plainly in one sentence.

Items:
{items}
"""

COVER_PROMPT = """\
You write the cover page of a morning portfolio digest. Below are per-holding
summaries covering news since the last digest (which may span a weekend). Write
3-5 sentences on portfolio-level themes and anything spanning multiple holdings.
Mention only what is supported by the items below. No preamble, no bullet points.

{sections}
"""

MACRO_ANALYSIS_PROMPT = """\
You write the "What happened" macro section of a morning portfolio digest,
covering economic releases since the last digest. Below are the released figures
(actual vs the prior period). Write 2-4 plain sentences on what the prints say
about the economy and what they mean for this portfolio — tie to the holdings
only where there is a real connection, don't force it. No preamble, no bullet
points, no hedging boilerplate. Report only what the figures support; do not
invent a consensus or "expected" number unless one is given below.

Holdings: {holdings}

Releases:
{releases}
"""


def _window(
    conn: sqlite3.Connection,
    now: datetime,
    portfolio_id: int | None = None,
    label_date: str | None = None,
) -> tuple[str, str]:
    if portfolio_id is None:
        from avalpha.accounts import default_portfolio_id

        portfolio_id = default_portfolio_id(conn)
    # Anchor on the last *sent* digest from an *earlier* edition (date < today's
    # label). Two independent reasons, each learned from a real dropped section:
    #
    #   * *sent*, not merely built: built_at is advanced by every build_digest
    #     call, including unsent preview rebuilds (the web-console "digest" job,
    #     `avalpha build-digest`). Anchoring on any build let a mid-morning
    #     preview shrink the real send's window to minutes, dropping scored items
    #     and macro releases that fell before it.
    #   * an *earlier* edition (date < label_date): once today's digest is sent it
    #     would otherwise become its own anchor, so any rebuild of today — the
    #     console "digest" button, a manual re-run — recomputed a near-empty
    #     window and silently dropped the whole "what happened — macro" section
    #     from the regenerated PDF (which overwrites the delivered one on disk).
    #     Excluding today's own edition makes a rebuild idempotent: it reproduces
    #     the window the send actually used, macro and all.
    #
    # Only a sent, earlier digest is the true high-water mark for "what has this
    # portfolio already covered". label_date is None only in low-level unit tests.
    row = conn.execute(
        "SELECT built_at FROM digests WHERE portfolio_id = ? AND sent_at IS NOT NULL "
        "AND (? IS NULL OR date < ?) ORDER BY date DESC, built_at DESC LIMIT 1",
        (portfolio_id, label_date, label_date),
    ).fetchone()
    if row:
        start = row["built_at"]
    else:
        start = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _macro_window(
    conn: sqlite3.Connection, now: datetime, label_date: str | None
) -> tuple[str, str]:
    """Fund-wide window for the *macro* block — deliberately not per-portfolio.

    Macro releases are market-wide: every recipient should see the same figures
    in the same edition. Anchoring macro on each portfolio's own last-sent digest
    (as the holdings window in ``_window`` must, to catch each member up on their
    own news) made macro coverage depend on that one portfolio's send timing — so
    one member got the FOMC print and another didn't, edition after edition. This
    anchors instead on the most recent *sent* edition across the whole fund
    (date < label_date), so the macro window is identical for everyone in a run
    and a rebuild reproduces it. First run ever: trailing 48h.
    """
    row = conn.execute(
        "SELECT built_at FROM digests WHERE sent_at IS NOT NULL "
        "AND (? IS NULL OR date < ?) ORDER BY date DESC, built_at DESC LIMIT 1",
        (label_date, label_date),
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


def _catalysts(
    conn: sqlite3.Connection, now: datetime, portfolio_id: int, days: int = 7
) -> list[dict]:
    """"Catalysts — next `days` days": company events for active holdings + Tier A
    macro only (no Tier B, docs/calendar.md §7). One rolling heads-up block."""
    from datetime import date

    from avalpha.calendar_store import KIND_LABELS, TIER_A_MACRO

    today = now.date()
    horizon = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT c.* FROM calendar_events c WHERE ("
        "c.portfolio_id = ? OR (c.portfolio_id IS NULL AND ("
        "c.ticker IS NULL OR EXISTS (SELECT 1 FROM portfolio_holdings ph "
        "WHERE ph.portfolio_id = ? AND ph.ticker = c.ticker AND ph.active = 1)))) "
        "AND c.status IN ('scheduled','confirmed','tentative') "
        "AND c.event_date >= ? AND c.event_date <= ? "
        "ORDER BY c.event_date, c.is_timed DESC, c.ticker IS NULL, c.ticker",
        (portfolio_id, portfolio_id, today.isoformat(), horizon),
    ).fetchall()
    out = []
    for r in rows:
        kind = r["kind"]
        if r["ticker"] is None and kind not in TIER_A_MACRO:
            continue  # digest carries Tier A macro only
        meta = json.loads(r["meta_json"] or "{}")
        when = {"amc": "after close", "bmo": "before open"}.get(meta.get("hour"), "")
        out.append(
            {
                "date": date.fromisoformat(r["event_date"]).strftime("%a %b %-d"),
                "ticker": r["ticker"],
                "label": KIND_LABELS.get(kind, kind),
                "title": r["title"],
                "when": when,
                "confirmed": r["status"] == "confirmed",
            }
        )
    return out


def _macro_events(config: Config, conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """Tier A macro events whose release fell in the digest window, each enriched
    with the actual figures from FRED (and consensus, if the FMP seam is active).
    Returns [] if the FRED key is unset — the outcome block is then simply omitted."""
    from avalpha import calendar_outcomes
    from avalpha.calendar_store import TIER_A_MACRO

    try:
        fred_key = config.fred_api_key
    except RuntimeError:
        return []  # no key configured → skip the block, still build the digest

    placeholders = ",".join("?" * len(TIER_A_MACRO))
    rows = conn.execute(
        f"SELECT * FROM calendar_events WHERE ticker IS NULL AND kind IN ({placeholders}) "
        "AND status != 'cancelled' AND ("
        "  (event_at IS NOT NULL AND event_at > ? AND event_at <= ?) OR "
        "  (event_at IS NULL AND event_date >= ? AND event_date <= ?)) "
        "ORDER BY event_date, kind",
        (*TIER_A_MACRO, start, end, start[:10], end[:10]),
    ).fetchall()

    out = []
    for r in rows:
        # Pass event_date so macro_outcome can suppress a line whose FRED data has
        # not caught up to the release yet (else we'd print the prior period).
        outcome = calendar_outcomes.macro_outcome(r["kind"], fred_key, r["event_date"])
        if not outcome:
            continue  # data short, stale, or fetch failed — omit rather than mislead
        consensus = calendar_outcomes.macro_consensus(r["kind"], r["event_date"], config)
        cons_str = None
        if consensus:
            verdict = "beat" if consensus["beat"] else "miss"
            cons_str = f"vs est {consensus['estimate']} — {verdict}"
        out.append({"label": outcome["label"], "lines": outcome["lines"], "consensus": cons_str})
    return out


def _macro_block(
    config: Config, conn: sqlite3.Connection, now: datetime, label_date: str | None
) -> list[dict]:
    """The shared macro figures for one digest run: computed once over the
    fund-wide window (``_macro_window``) and handed to every portfolio's build.
    Every recipient then renders the same releases, and FRED is queried once per
    run rather than once per portfolio — which also means a transient FRED error
    no longer drops the macro section for just the subset of members built after
    it. Returns ``[]`` when nothing released (or no FRED key), same as before."""
    return _macro_events(config, conn, *_macro_window(conn, now, label_date))


def _earnings_in_window(config: Config, conn, ticker: str, start: str, end: str) -> dict | None:
    """EPS beat/miss for a holding whose earnings date fell in the digest window,
    or None. The scheduled date is on the calendar; the actuals come from Finnhub."""
    from avalpha import calendar_outcomes

    ev = conn.execute(
        "SELECT fiscal_period FROM calendar_events WHERE ticker = ? AND kind = 'earnings' "
        "AND status != 'cancelled' AND ("
        "  (event_at IS NOT NULL AND event_at > ? AND event_at <= ?) OR "
        "  (event_at IS NULL AND event_date >= ? AND event_date <= ?)) "
        "ORDER BY event_date DESC LIMIT 1",
        (ticker, start, end, start[:10], end[:10]),
    ).fetchone()
    if ev is None:
        return None
    try:
        finnhub_key = config.finnhub_api_key
    except RuntimeError:
        return None
    return calendar_outcomes.earnings_outcome(ticker, finnhub_key, ev["fiscal_period"])


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


def _llm_text_safe(
    config: Config, prompt: str, *, fallback: str, label: str, max_tokens: int = 512
) -> str:
    """``_llm_text`` that returns ``fallback`` instead of raising. One section's
    LLM error — a holding narrative, the cover blurb, the macro analysis — must
    not sink the whole digest and deliver nothing; every other section (and every
    other recipient) still ships. Callers pass a non-empty ``fallback`` wherever
    an empty string would change the page's meaning: a build *with* items keeps a
    non-empty narrative so the template never falls back to its "quiet day" line."""
    try:
        return _llm_text(config, prompt, max_tokens=max_tokens)
    except Exception as e:  # noqa: BLE001 — degrade this section, deliver the rest
        print(f"digest LLM call failed ({label}): {type(e).__name__}: {e}")
        return fallback


def _digest_date(now: datetime, date_str: str | None) -> str:
    """Identity date for a digest: the Pacific calendar day it is built.

    This is both the dedup key (one digest per portfolio per day) and the label.
    It is deliberately the *calendar* day, not the prior trading day: the digest
    ships every day, and weekends carry market-moving news worth analyzing, so
    Saturday and Sunday each earn their own edition. Keying on the prior trading
    day instead collapsed Sat/Sun/Mon onto the same Friday key, so only the
    first weekend run sent and Monday's digest was silently deduped away. An
    explicit ``date_str`` (manual/backfill builds) is honored as-is.

    Price action still reflects the last completed session regardless of which
    calendar day this is: ``_price_action`` selects the most recent close at or
    before this date, and today's close is not in the book at send time.
    """
    return date_str or now.astimezone(PACIFIC).date().isoformat()


def build_digest(
    config: Config,
    conn: sqlite3.Connection,
    date_str: str | None = None,
    portfolio_id: int | None = None,
    macro: list[dict] | None = None,
) -> Path:
    # `macro` is the shared, fund-wide macro block computed once per run (see
    # _macro_block). The batch callers (build_and_send, build_all_digests) pass it
    # so every recipient renders identical releases; a standalone build (the CLI,
    # a single-portfolio rebuild) leaves it None and computes its own.
    if portfolio_id is None:
        from avalpha.accounts import default_portfolio_id

        portfolio_id = default_portfolio_id(conn)
    now = datetime.now(timezone.utc)
    label_date = _digest_date(now, date_str)
    start, end = _window(conn, now, portfolio_id, label_date)

    holdings_data = []
    for h in watchlist.active(conn, portfolio_id):
        close, pct = _price_action(conn, h.ticker, label_date)
        items = _scored_items(conn, h.ticker, start, end)
        insiders = _insider_filings(conn, h.cik, start, end)
        earnings = _earnings_in_window(config, conn, h.ticker, start, end)
        reddit_count, reddit_baseline = _reddit_stats(conn, h.ticker, start, end)

        narrative = ""
        if items:
            listing = "\n".join(
                f"- [{it['materiality']}/10 {it['category']}] {it['summary']} "
                f"(mechanism: {it['mechanism']})"
                for it in items
            )
            narrative = _llm_text_safe(
                config,
                NARRATIVE_PROMPT.format(
                    ticker=h.ticker, legal_name=h.legal_name, items=listing
                ),
                fallback="Automated summary unavailable this edition — see the items below.",
                label=f"narrative {h.ticker}",
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
                "earnings": earnings,
                "insider_filings": insiders,
                "reddit_count": reddit_count,
                "reddit_baseline": reddit_baseline,
            }
        )

    active_sections = [
        f"{d['ticker']}: {d['narrative']}" for d in holdings_data if d["narrative"]
    ]
    if active_sections:
        cover_text = _llm_text_safe(
            config,
            COVER_PROMPT.format(sections="\n".join(active_sections)),
            fallback="Automated portfolio summary unavailable this edition — see the per-holding pages.",
            label="cover",
        )
    else:
        cover_text = "Quiet day across the portfolio — nothing material at any holding."

    macro_events = (
        macro if macro is not None else _macro_block(config, conn, now, label_date)
    )
    macro_analysis = ""
    if macro_events:
        releases = "\n".join(
            f"- {m['label']}: {'; '.join(m['lines'])}"
            + (f" [consensus: {m['consensus']}]" if m["consensus"] else "")
            for m in macro_events
        )
        holdings_str = (
            ", ".join(f"{d['ticker']} ({d['name']})" for d in holdings_data) or "none active"
        )
        # Best-effort: if the narrative LLM call fails, still ship the digest with
        # the released figures (the macro_events list renders on its own) rather
        # than raising and delivering no digest at all. For an empty-holdings
        # member this is the only LLM call in the build, so this keeps their
        # digest going out even during an Anthropic hiccup.
        macro_analysis = _llm_text_safe(
            config,
            MACRO_ANALYSIS_PROMPT.format(holdings=holdings_str, releases=releases),
            fallback="",
            label=f"macro analysis p{portfolio_id}",
        )

    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parent), autoescape=True
    )
    html = env.get_template("template.html").render(
        label_date=label_date,
        built_at=end[:16].replace("T", " "),
        holdings=holdings_data,
        cover_text=cover_text,
        macro_events=macro_events,
        macro_analysis=macro_analysis,
        catalysts=_catalysts(conn, now, portfolio_id),
    )

    portfolio_dir = config.digest_dir / str(portfolio_id)
    portfolio_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = portfolio_dir / f"avalpha-{label_date}.pdf"
    # WeasyPrint needs native pango/cairo; import lazily so the rest of the
    # CLI works on a box where those aren't installed.
    from weasyprint import HTML

    HTML(string=html).write_pdf(pdf_path)

    # Record built_at = end (the window's right edge, captured at build start),
    # not utcnow() at build end. This stored value is the *next* digest's window
    # left edge, so it must equal the boundary this digest actually covered; the
    # LLM + PDF work between the two instants would otherwise be an interval no
    # digest covers, and a macro release timestamped there would silently vanish.
    # Once a digest is sent, freeze its built_at and pdf_path (WHERE sent_at IS
    # NULL) so a later preview rebuild can neither move the high-water mark nor
    # repoint the row away from the PDF that was actually delivered.
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, pdf_path) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (portfolio_id, date) DO UPDATE SET built_at = excluded.built_at, "
        "pdf_path = excluded.pdf_path WHERE digests.sent_at IS NULL",
        (portfolio_id, label_date, end, str(pdf_path)),
    )
    conn.commit()
    return pdf_path


def build_and_send(
    config: Config,
    conn: sqlite3.Connection,
    date_str: str | None = None,
    portfolio_id: int | None = None,
) -> None:
    """Build and send one portfolio, or every portfolio for the timer job.

    Each portfolio is sent independently: a failure (e.g. a rejected recipient)
    is logged and does not stop the others — one bad address must not silently
    skip everyone queued after it. That portfolio's ``sent_at`` stays NULL so it
    retries next cycle, and the run still raises at the end so the timer reports
    failure rather than exiting clean on a partial send."""
    portfolio_ids = (
        [portfolio_id]
        if portfolio_id is not None
        else [r["id"] for r in conn.execute("SELECT id FROM portfolios ORDER BY id")]
    )
    # Compute the market-wide macro block once and hand the same figures to every
    # portfolio, so all recipients get identical macro analysis in this edition
    # (and FRED is hit once for the run, not once per portfolio).
    now = datetime.now(timezone.utc)
    macro = _macro_block(config, conn, now, _digest_date(now, date_str))
    failures = []
    for selected_id in portfolio_ids:
        try:
            _build_and_send_one(config, conn, selected_id, date_str, macro)
        except Exception as e:  # isolate one portfolio's failure from the rest
            failures.append((selected_id, e))
            print(f"digest FAILED for portfolio {selected_id}: {type(e).__name__}: {e}")
    if failures:
        raise RuntimeError(
            f"{len(failures)} of {len(portfolio_ids)} portfolio digests failed: "
            + ", ".join(f"p{pid} ({type(e).__name__})" for pid, e in failures)
        )


def _build_and_send_one(
    config: Config,
    conn: sqlite3.Connection,
    portfolio_id: int,
    date_str: str | None,
    macro: list[dict] | None = None,
) -> None:
    from avalpha.mailer import send_digest_email
    from avalpha.accounts import portfolio_owner_email

    now = datetime.now(timezone.utc)
    label_date = _digest_date(now, date_str)
    already = conn.execute(
        "SELECT sent_at FROM digests WHERE portfolio_id = ? AND date = ? "
        "AND sent_at IS NOT NULL",
        (portfolio_id, label_date),
    ).fetchone()
    if already:
        print(f"digest for {label_date} already sent at {already['sent_at']}; skipping")
        return

    recipient = portfolio_owner_email(conn, portfolio_id)
    if not recipient:
        raise RuntimeError(f"portfolio {portfolio_id} has no owner")
    pdf_path = build_digest(
        config, conn, date_str=label_date, portfolio_id=portfolio_id, macro=macro
    )
    send_digest_email(config, pdf_path, label_date, recipient=recipient)
    conn.execute(
        "UPDATE digests SET sent_at = ? WHERE portfolio_id = ? AND date = ?",
        (utcnow(), portfolio_id, label_date),
    )
    conn.commit()
    print(f"digest for {label_date} sent to {recipient}")


def build_all_digests(
    config: Config, conn: sqlite3.Connection, date_str: str | None = None
) -> list[Path]:
    """Build independent content and PDFs for every provisioned portfolio, all
    sharing one fund-wide macro block so the preview matches what is delivered."""
    now = datetime.now(timezone.utc)
    macro = _macro_block(config, conn, now, _digest_date(now, date_str))
    return [
        build_digest(config, conn, date_str=date_str, portfolio_id=row["id"], macro=macro)
        for row in conn.execute("SELECT id FROM portfolios ORDER BY id")
    ]
