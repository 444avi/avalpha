"""Google News RSS collector. Coverage insurance, not a primary source."""

import sqlite3
import urllib.parse

import feedparser
import requests

from avalpha import watchlist
from avalpha.collectors.base import insert_item, text_from_html
from avalpha.config import Config

SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def collect(config: Config, conn: sqlite3.Connection) -> tuple[int, int]:
    fetched = 0
    new = 0
    for holding in watchlist.active(conn):
        query = urllib.parse.quote(f'"{holding.legal_name}"')
        try:
            resp = requests.get(
                SEARCH_URL.format(query=query),
                headers={"User-Agent": config.edgar_user_agent},
                timeout=30,
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except requests.RequestException:
            continue
        for entry in feed.entries:
            url = entry.get("link")
            if not url:
                continue
            fetched += 1
            summary = entry.get("summary", "")
            if insert_item(
                conn,
                source="gnews",
                source_id=entry.get("id") or url,
                url=url,
                title=entry.get("title", ""),
                raw_text=text_from_html(summary) if summary else "",
                published_at=entry.get("published"),
                meta={"ticker_hint": holding.ticker},
            ):
                new += 1
    conn.commit()
    return fetched, new
