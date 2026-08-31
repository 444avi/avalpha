"""`avalpha status` — how the system is debugged from a terminal at 6am."""

import sqlite3
from datetime import datetime, timedelta, timezone

from avalpha.collectors import SOURCES
from avalpha.scorer import PROMPT_VERSION


def print_status(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("collectors (last 24h):")
    for source in SOURCES:
        last_ok = conn.execute(
            "SELECT started_at, items_new FROM collector_runs "
            "WHERE source = ? AND ok = 1 ORDER BY started_at DESC LIMIT 1",
            (source,),
        ).fetchone()
        errors = conn.execute(
            "SELECT COUNT(*) FROM collector_runs "
            "WHERE source = ? AND ok = 0 AND started_at > ?",
            (source, day_ago),
        ).fetchone()[0]
        new_24h = conn.execute(
            "SELECT COALESCE(SUM(items_new), 0) FROM collector_runs "
            "WHERE source = ? AND ok = 1 AND started_at > ?",
            (source, day_ago),
        ).fetchone()[0]
        last = last_ok["started_at"] if last_ok else "never"
        line = f"  {source:<8} last ok: {last:<22} new items 24h: {new_24h:<5} errors 24h: {errors}"
        if errors:
            last_err = conn.execute(
                "SELECT error FROM collector_runs WHERE source = ? AND ok = 0 "
                "ORDER BY started_at DESC LIMIT 1",
                (source,),
            ).fetchone()
            line += f"\n           last error: {last_err['error']}"
        print(line)

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
    print("\nqueues:")
    print(f"  items total: {total_items}, awaiting matcher: {unmatched}, awaiting scorer: {unscored}")

    last_digest = conn.execute(
        "SELECT date, built_at, sent_at FROM digests ORDER BY date DESC LIMIT 1"
    ).fetchone()
    print("\ndigest:")
    if last_digest:
        sent = last_digest["sent_at"] or "not sent"
        print(f"  latest: {last_digest['date']} built {last_digest['built_at']} sent {sent}")
    else:
        print("  none built yet")
