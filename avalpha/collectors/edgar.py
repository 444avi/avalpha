"""SEC EDGAR collector.

One request to the global `getcurrent` Atom feed covers all recent filings;
entries are filtered to watchlist CIKs. For each match we pull the filing's
item codes and primary-document text via the per-company submissions JSON.
"""

import re
import sqlite3

import feedparser
import requests

from avalpha import watchlist
from avalpha.calendar_store import confirm_earnings
from avalpha.collectors.base import insert_item, text_from_html
from avalpha.config import Config

GETCURRENT_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=&company=&owner=include&count=100&output=atom"
)
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"

_TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<company>.+) \((?P<cik>\d{10})\)")
_ACC_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def _session(config: Config) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.edgar_user_agent
    return s


def _filed_at_utc(entry) -> str | None:
    """The Atom entry's timestamp as a UTC "…Z" instant, or None. feedparser's
    *_parsed struct is already UTC, so this avoids the tz-offset string the raw
    `updated` field carries (which would break UTC string comparisons)."""
    import calendar as _calendar
    from datetime import datetime, timezone

    parsed = entry.get("updated_parsed") or entry.get("published_parsed")
    if not parsed:
        return None
    ts = _calendar.timegm(parsed)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filing_details(
    session: requests.Session, cik: str, accession: str
) -> tuple[str, str]:
    """(item codes, primary document text) for one filing, best-effort."""
    try:
        resp = session.get(SUBMISSIONS_URL.format(cik=cik), timeout=30)
        resp.raise_for_status()
        recent = resp.json()["filings"]["recent"]
    except Exception:
        return "", ""
    try:
        idx = recent["accessionNumber"].index(accession)
    except ValueError:
        return "", ""
    items = (recent.get("items") or [""] * len(recent["accessionNumber"]))[idx]
    primary = (recent.get("primaryDocument") or [""] * len(recent["accessionNumber"]))[idx]
    text = ""
    if primary:
        url = DOC_URL.format(
            cik_int=int(cik), acc_nodash=accession.replace("-", ""), doc=primary
        )
        try:
            doc = session.get(url, timeout=30)
            if doc.status_code == 200:
                text = text_from_html(doc.text)
        except requests.RequestException:
            pass
    return items or "", text


def collect(config: Config, conn: sqlite3.Connection) -> tuple[int, int]:
    ciks = {int(h.cik): h for h in watchlist.active(conn)}
    if not ciks:
        return 0, 0

    session = _session(config)
    resp = session.get(GETCURRENT_URL, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    fetched = 0
    new = 0
    for entry in feed.entries:
        m = _TITLE_RE.match(entry.get("title", ""))
        if not m or int(m.group("cik")) not in ciks:
            continue
        fetched += 1
        cik10 = m.group("cik")
        link = entry.get("link", "")
        acc_match = _ACC_RE.search(link) or _ACC_RE.search(entry.get("id", ""))
        accession = acc_match.group(1) if acc_match else None

        # Skip the detail fetches if we already have this filing.
        if accession:
            exists = conn.execute(
                "SELECT 1 FROM items WHERE source = 'edgar' AND source_id = ?",
                (accession,),
            ).fetchone()
            if exists:
                continue

        items_codes, raw_text = ("", "")
        if accession:
            items_codes, raw_text = _filing_details(session, cik10, accession)

        was_new = insert_item(
            conn,
            source="edgar",
            source_id=accession,
            url=link,
            title=entry.get("title", ""),
            raw_text=raw_text,
            published_at=entry.get("updated"),
            meta={
                "form": m.group("form").strip(),
                "items": items_codes,
                "cik": cik10,
                "accession": accession,
                "company": m.group("company").strip(),
            },
        )
        if was_new:
            new += 1

        # Phase 2 (docs/calendar.md §6.2): an 8-K item 2.02 (Results of
        # Operations) is the company confirming its earnings — upgrade the
        # matching scheduled calendar row to 'confirmed'. Reuses an item the
        # pipeline already collects; no new feed.
        form = m.group("form").strip()
        if form.startswith("8-K") and "2.02" in (items_codes or ""):
            holding = ciks.get(int(cik10))
            if holding is not None:
                confirm_earnings(
                    conn,
                    holding.ticker,
                    event_at=_filed_at_utc(entry),
                    source_ref=accession,
                )
    conn.commit()
    return fetched, new
