"""Collector dispatch. One module per source; collectors never judge."""

import sqlite3

from avalpha.collectors.base import RunOutcome, run
from avalpha.config import Config

SOURCES = ("edgar", "ir", "gnews", "reddit", "prices", "calendar")


def run_source(
    config: Config, conn: sqlite3.Connection, source: str, force: bool = False
) -> RunOutcome:
    if source == "edgar":
        from avalpha.collectors.edgar import collect
    elif source == "ir":
        from avalpha.collectors.ir import collect
    elif source == "gnews":
        from avalpha.collectors.gnews import collect
    elif source == "reddit":
        from avalpha.collectors.reddit import collect
    elif source == "prices":
        from avalpha.collectors.prices import collect
    elif source == "calendar":
        from avalpha.collectors.calendar import collect
    else:
        raise ValueError(f"unknown source {source!r}")
    return run(config, conn, source, collect, force=force)
