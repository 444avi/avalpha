"""Collector run wrapper: cadence due-check, run logging, item dedup.

A collector is a plain function `collect(config, conn) -> (fetched, new)`.
It fetches, normalizes, writes rows, and returns. No LLM calls, no judgment.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser

from avalpha.config import Config
from avalpha.db import utcnow
from avalpha.market_state import poll_interval


class RunOutcome:
    def __init__(self, source: str, status: str, fetched: int = 0, new: int = 0, error: str = ""):
        self.source = source
        self.status = status  # ran | skipped | failed
        self.fetched = fetched
        self.new = new
        self.error = error

    def __str__(self) -> str:
        if self.status == "skipped":
            return f"{self.source}: skipped (not due)"
        if self.status == "failed":
            return f"{self.source}: FAILED — {self.error}"
        return f"{self.source}: ok, {self.fetched} fetched, {self.new} new"


def _is_due(conn: sqlite3.Connection, source: str, now: datetime) -> bool:
    row = conn.execute(
        "SELECT started_at FROM collector_runs WHERE source = ? AND ok = 1 "
        "ORDER BY started_at DESC LIMIT 1",
        (source,),
    ).fetchone()
    if row is None:
        return True
    last = datetime.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (now - last).total_seconds() >= poll_interval(source, now)


def run(config: Config, conn: sqlite3.Connection, source: str, collect, force: bool = False) -> RunOutcome:
    now = datetime.now(timezone.utc)
    if not force and not _is_due(conn, source, now):
        return RunOutcome(source, "skipped")

    cur = conn.execute(
        "INSERT INTO collector_runs (source, started_at) VALUES (?, ?)",
        (source, utcnow()),
    )
    run_id = cur.lastrowid
    conn.commit()
    try:
        fetched, new = collect(config, conn)
    except Exception as e:  # any failure is isolated to this collector
        conn.execute(
            "UPDATE collector_runs SET finished_at = ?, ok = 0, error = ? WHERE id = ?",
            (utcnow(), f"{type(e).__name__}: {e}", run_id),
        )
        conn.commit()
        return RunOutcome(source, "failed", error=f"{type(e).__name__}: {e}")

    conn.execute(
        "UPDATE collector_runs SET finished_at = ?, ok = 1, items_fetched = ?, "
        "items_new = ? WHERE id = ?",
        (utcnow(), fetched, new, run_id),
    )
    conn.commit()
    return RunOutcome(source, "ran", fetched=fetched, new=new)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def insert_item(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_id: str | None,
    url: str,
    title: str,
    raw_text: str = "",
    published_at: str | None = None,
    meta: dict | None = None,
) -> bool:
    """Insert one item; returns True if new (dedup on url_hash)."""
    import json

    cur = conn.execute(
        "INSERT OR IGNORE INTO items (source, source_id, url, url_hash, title, "
        "raw_text, published_at, fetched_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            source,
            source_id,
            url,
            url_hash(url),
            title,
            raw_text,
            published_at,
            utcnow(),
            json.dumps(meta or {}),
        ),
    )
    return cur.rowcount > 0


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def text_from_html(html: str, limit: int = 50_000) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return " ".join(parser.parts)[:limit]
