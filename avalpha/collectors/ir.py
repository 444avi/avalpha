"""Company IR press-release feed collector."""

import sqlite3

import feedparser
import requests

from avalpha import watchlist
from avalpha.collectors.base import insert_item, text_from_html
from avalpha.config import Config


def _entry_time(entry) -> str | None:
    for key in ("published", "updated"):
        if entry.get(key):
            return entry[key]
    return None


def collect(config: Config, conn: sqlite3.Connection) -> tuple[int, int]:
    fetched = 0
    new = 0
    for holding in watchlist.active(conn):
        if holding.ir_feed_status != "ok" or not holding.ir_feed_url:
            continue
        try:
            resp = requests.get(
                holding.ir_feed_url,
                headers={"User-Agent": config.edgar_user_agent},
                timeout=30,
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except requests.RequestException:
            # One broken feed must not stop the others; the story will still
            # arrive via EDGAR/news, and status shows per-run error counts.
            continue
        for entry in feed.entries:
            url = entry.get("link")
            if not url:
                continue
            fetched += 1
            summary = entry.get("summary", "")
            if insert_item(
                conn,
                source="ir",
                source_id=entry.get("id") or url,
                url=url,
                title=entry.get("title", ""),
                raw_text=text_from_html(summary) if summary else "",
                published_at=_entry_time(entry),
                meta={"ticker_hint": holding.ticker, "feed": holding.ir_feed_url},
            ):
                new += 1
    conn.commit()
    return fetched, new
