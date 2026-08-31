"""Reddit collector. A volume signal, not a content source.

Posts are stored as items (they rarely matter individually) and, more
importantly, rolling per-ticker mention counts land in reddit_mentions so
Phase 2 has a baseline for spike detection.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone

from avalpha import watchlist
from avalpha.collectors.base import insert_item
from avalpha.config import Config


def _configured() -> bool:
    return bool(
        os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")
    )


def _reddit(config: Config):
    import praw

    return praw.Reddit(
        client_id=config.reddit_client_id,
        client_secret=config.reddit_client_secret,
        user_agent=config.edgar_user_agent,
        check_for_updates=False,
    )


def _hour_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:00:00Z")


def collect(config: Config, conn: sqlite3.Connection) -> tuple[int, int]:
    # Reddit is an optional volume signal. When credentials aren't set, skip
    # cleanly rather than erroring every run — the other sources carry the
    # content and this one does nothing in Phase 1 anyway.
    if not _configured():
        print(
            "reddit: REDDIT_CLIENT_ID/SECRET not set — source disabled, skipping",
            file=sys.stderr,
        )
        return 0, 0

    holdings = watchlist.active(conn)
    if not holdings or not config.subreddits:
        return 0, 0

    reddit = _reddit(config)
    subreddit = reddit.subreddit("+".join(config.subreddits))
    now = datetime.now(timezone.utc)
    bucket = _hour_bucket(now)

    fetched = 0
    new = 0
    for holding in holdings:
        # Ticker-as-query is fine here (search, not matching); the matcher
        # still decides whether a post is actually about the company.
        query = f'"{holding.legal_name}" OR "{holding.ticker}"'
        mentions = 0
        try:
            results = list(
                subreddit.search(query, sort="new", time_filter="day", limit=25)
            )
        except Exception:
            continue
        for post in results:
            mentions += 1
            fetched += 1
            if insert_item(
                conn,
                source="reddit",
                source_id=post.id,
                url=f"https://www.reddit.com{post.permalink}",
                title=post.title or "",
                raw_text=(post.selftext or "")[:10_000],
                published_at=datetime.fromtimestamp(
                    post.created_utc, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                meta={
                    "ticker_hint": holding.ticker,
                    "subreddit": str(post.subreddit),
                    "score": post.score,
                    "num_comments": post.num_comments,
                },
            ):
                new += 1
        conn.execute(
            "INSERT INTO reddit_mentions (ticker, window_start, count) VALUES (?, ?, ?) "
            "ON CONFLICT (ticker, window_start) DO UPDATE SET count = excluded.count",
            (holding.ticker, bucket, mentions),
        )
    conn.commit()
    return fetched, new
