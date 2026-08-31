"""avalpha CLI. Phase 1 interface, runs over SSH."""

import sys

import click

from avalpha import db, watchlist
from avalpha.config import load_config


def _conn():
    config = load_config()
    return config, db.connect(config.db_path)


@click.group()
def main():
    """Portfolio monitoring: watchlist, collectors, scorer, morning digest."""


@main.command()
@click.argument("ticker")
@click.option("--weight", type=float, default=0.0, help="Rough % of portfolio.")
@click.option(
    "--add-anyway",
    is_flag=True,
    help="Add even if enrichment confidence is low (the matcher may miss stories).",
)
def add(ticker: str, weight: float, add_anyway: bool):
    """Add TICKER to the watchlist and enrich its entity metadata."""
    from avalpha.enrich import EnrichmentError, enrich

    config, conn = _conn()
    ticker = ticker.upper()
    click.echo(f"Enriching {ticker} (SEC lookup + web search)...")
    try:
        result = enrich(config, ticker)
    except EnrichmentError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"  legal name:  {result.legal_name}")
    click.echo(f"  CIK:         {result.cik}")
    click.echo(f"  aliases:     {', '.join(result.aliases) or '(none)'}")
    click.echo(f"  products:    {', '.join(result.products) or '(none)'}")
    click.echo(f"  executives:  {', '.join(result.executives) or '(none)'}")
    click.echo(f"  IR feed:     {result.ir_feed_url or 'none found'}")
    shares = f"{result.shares_outstanding:,}" if result.shares_outstanding else "unknown"
    click.echo(f"  shares out:  {shares}")
    click.echo(f"  confidence:  {result.confidence}")
    if result.notes:
        click.echo(f"  notes:       {result.notes}")

    if result.confidence == "low" and not add_anyway:
        click.echo(
            f"\nNot added: enrichment confidence is low, so the matcher would "
            f"likely miss stories about {ticker}. Re-run with --add-anyway to "
            "override, or fix the metadata by hand.",
            err=True,
        )
        sys.exit(1)

    watchlist.upsert(
        conn,
        ticker=result.ticker,
        cik=result.cik,
        legal_name=result.legal_name,
        aliases=result.aliases,
        products=result.products,
        executives=result.executives,
        ir_feed_url=result.ir_feed_url,
        ir_feed_status=result.ir_feed_status,
        weight=weight,
        shares_outstanding=result.shares_outstanding,
        enrichment_confidence=result.confidence,
    )
    click.echo(f"\nAdded {ticker} (weight {weight}%).")
    if result.ir_feed_status == "none":
        click.echo("Note: no usable IR feed — coverage relies on EDGAR and news.")


@main.command()
@click.argument("ticker")
def remove(ticker: str):
    """Deactivate TICKER. Historical items stay queryable."""
    _, conn = _conn()
    if watchlist.deactivate(conn, ticker.upper()):
        click.echo(f"Deactivated {ticker.upper()}.")
    else:
        click.echo(f"{ticker.upper()} is not an active holding.", err=True)
        sys.exit(1)


@main.command(name="list")
def list_():
    """List watchlist holdings."""
    _, conn = _conn()
    holdings = watchlist.all_holdings(conn)
    if not holdings:
        click.echo("Watchlist is empty. Add a holding with: avalpha add TICKER")
        return
    for h in holdings:
        state = "active" if h.active else "inactive"
        feed = "ir-feed" if h.ir_feed_status == "ok" else "no-ir-feed"
        click.echo(
            f"{h.ticker:<6} {h.legal_name:<40.40} weight={h.weight:<5g} "
            f"{feed:<10} confidence={h.enrichment_confidence or '-':<7} {state}"
        )


@main.command(name="run-collector")
@click.argument(
    "source", type=click.Choice(["edgar", "ir", "gnews", "reddit", "prices"])
)
@click.option("--force", is_flag=True, help="Ignore the cadence due-check.")
def run_collector(source: str, force: bool):
    """Run one collector cycle (debug helper; systemd timers call this too)."""
    from avalpha.collectors import run_source

    config, conn = _conn()
    outcome = run_source(config, conn, source, force=force)
    click.echo(outcome)


@main.command(name="run-matcher")
@click.option("--limit", type=int, default=500)
def run_matcher(limit: int):
    """Match unprocessed items to tickers (debug helper)."""
    from avalpha.matcher import match_pending

    config, conn = _conn()
    stats = match_pending(config, conn, limit=limit)
    click.echo(stats)


@main.command(name="run-scorer")
@click.option("--once", is_flag=True, help="Drain the queue once, then exit.")
def run_scorer(once: bool):
    """Run the scoring worker (systemd runs this without --once)."""
    from avalpha.scorer import run_worker

    config, conn = _conn()
    run_worker(config, conn, once=once)


@main.command()
def status():
    """Last successful run per collector, queue depth, error counts."""
    from avalpha.status import print_status

    _, conn = _conn()
    print_status(conn)


@main.command(name="test-digest")
@click.option("--date", "date_str", default=None, help="Trading day (YYYY-MM-DD).")
def test_digest(date_str: str | None):
    """Build today's PDF now; don't email it."""
    from avalpha.digest.build import build_digest

    config, conn = _conn()
    path = build_digest(config, conn, date_str=date_str)
    click.echo(f"Wrote {path}")


@main.command(name="send-digest")
@click.option("--date", "date_str", default=None, help="Trading day (YYYY-MM-DD).")
def send_digest(date_str: str | None):
    """Build and email the morning digest (called by the 6am timer)."""
    from avalpha.digest.build import build_and_send

    config, conn = _conn()
    build_and_send(config, conn, date_str=date_str)


@main.command()
@click.argument("date_range")
@click.option("--ticker", default=None, help="Limit replay to one ticker.")
def replay(date_range: str, ticker: str | None):
    """Re-score stored items against the current prompt.

    DATE_RANGE is YYYY-MM-DD:YYYY-MM-DD (inclusive).
    """
    from avalpha.scorer import replay_range

    config, conn = _conn()
    try:
        start, end = date_range.split(":")
    except ValueError:
        click.echo("date range must be YYYY-MM-DD:YYYY-MM-DD", err=True)
        sys.exit(1)
    stats = replay_range(config, conn, start=start, end=end, ticker=ticker)
    click.echo(stats)


if __name__ == "__main__":
    main()
