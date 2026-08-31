"""Matcher: links items to zero or more tickers.

EDGAR items match exactly by CIK (both passes skipped). Everything else goes
through a cheap deterministic pass over watchlist metadata, then a small-model
confirm pass on the flagged candidates. Ticker symbols are never used as match
terms — $F/$ALL/$IT-style false positives are how systems like this die.

Rejected candidates stay in item_matches with confirmed=0, so a missed story
is diagnosable per stage.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field

from avalpha import watchlist
from avalpha.config import Config
from avalpha.db import utcnow

MIN_TERM_LEN = 3
SNIPPET_LEN = 1500

CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "about_company": {"type": "boolean"},
                },
                "required": ["ticker", "about_company"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}

CONFIRM_PROMPT = """\
You match news items to companies on a stock watchlist. A keyword scan flagged
this item as possibly being about the companies below. For each, decide whether
the item is substantively about that company — not a passing mention, a
different entity with a similar name, or an unrelated use of the word.

Item title: {title}
Item text (may be truncated): {snippet}

Candidates:
{candidates}

Answer for every candidate.
"""


@dataclass
class _Pattern:
    ticker: str
    term: str
    regex: re.Pattern


@dataclass
class MatchStats:
    processed: int = 0
    cik_matched: int = 0
    candidates: int = 0
    confirmed: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = (
            f"matcher: {self.processed} items processed, {self.cik_matched} CIK-matched, "
            f"{self.candidates} candidates -> {self.confirmed} confirmed, "
            f"{self.rejected} rejected"
        )
        if self.errors:
            s += f", {len(self.errors)} errors (first: {self.errors[0]})"
        return s


def _clean_company_name(name: str) -> str:
    """Strip legal suffixes so 'NVIDIA CORP' can match 'Nvidia' prose usage."""
    return re.sub(
        r"\b(corp(oration)?|inc|co|ltd|plc|llc|holdings?|group|company)\.?$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip(" ,.")


def build_patterns(holdings: list[watchlist.Holding]) -> list[_Pattern]:
    patterns = []
    for h in holdings:
        terms = set()
        terms.add(h.legal_name)
        cleaned = _clean_company_name(h.legal_name)
        if cleaned:
            terms.add(cleaned)
        terms.update(h.aliases)
        terms.update(h.products)
        terms.update(h.executives)
        for term in terms:
            term = term.strip()
            if len(term) < MIN_TERM_LEN:
                continue
            # Never match the bare ticker symbol, even if enrichment slipped
            # it into the alias list.
            if term.upper() == h.ticker.upper():
                continue
            escaped = re.escape(term)
            if term.isupper() and len(term) <= 5:
                # Short all-caps terms (acronyms) match case-sensitively:
                # "CUDA" yes, "cuda" no.
                regex = re.compile(rf"\b{escaped}\b")
            else:
                regex = re.compile(rf"\b{escaped}\b", re.IGNORECASE)
            patterns.append(_Pattern(h.ticker, term, regex))
    return patterns


def cheap_pass(text: str, patterns: list[_Pattern]) -> dict[str, str]:
    """Returns {ticker: first matched term} for candidate tickers."""
    found: dict[str, str] = {}
    for p in patterns:
        if p.ticker in found:
            continue
        if p.regex.search(text):
            found[p.ticker] = p.term
    return found


def _confirm(
    config: Config,
    title: str,
    snippet: str,
    candidates: dict[str, str],
    holdings_by_ticker: dict[str, watchlist.Holding],
) -> dict[str, bool]:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    lines = []
    for ticker, term in sorted(candidates.items()):
        h = holdings_by_ticker[ticker]
        lines.append(f"- {ticker}: {h.legal_name} (flagged on the term {term!r})")
    response = client.messages.create(
        model=config.model_confirm,
        max_tokens=1024,
        output_config={
            "format": {"type": "json_schema", "schema": CONFIRM_SCHEMA}
        },
        messages=[
            {
                "role": "user",
                "content": CONFIRM_PROMPT.format(
                    title=title, snippet=snippet, candidates="\n".join(lines)
                ),
            }
        ],
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return {
        m["ticker"].upper(): bool(m["about_company"])
        for m in data.get("matches", [])
    }


def _record(conn, item_id: int, ticker: str, method: str, confirmed: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO item_matches (item_id, ticker, method, confirmed, matched_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (item_id, ticker, method, int(confirmed), utcnow()),
    )


def _mark_done(conn, item_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO matcher_done (item_id, processed_at) VALUES (?, ?)",
        (item_id, utcnow()),
    )


def match_pending(config: Config, conn: sqlite3.Connection, limit: int = 500) -> MatchStats:
    holdings = watchlist.active(conn)
    by_ticker = {h.ticker: h for h in holdings}
    by_cik = {int(h.cik): h for h in holdings}
    patterns = build_patterns(holdings)
    stats = MatchStats()

    rows = conn.execute(
        "SELECT i.* FROM items i LEFT JOIN matcher_done d ON d.item_id = i.id "
        "WHERE d.item_id IS NULL ORDER BY i.id LIMIT ?",
        (limit,),
    ).fetchall()

    for row in rows:
        item_id = row["id"]
        meta = json.loads(row["meta_json"])

        # EDGAR: CIK is an exact match; both passes skipped.
        if row["source"] == "edgar":
            cik = meta.get("cik")
            holding = by_cik.get(int(cik)) if cik else None
            if holding:
                _record(conn, item_id, holding.ticker, "cik", True)
                stats.cik_matched += 1
            _mark_done(conn, item_id)
            stats.processed += 1
            conn.commit()
            continue

        text = f"{row['title']}\n{row['raw_text']}"
        candidates = cheap_pass(text, patterns)
        if not candidates:
            _mark_done(conn, item_id)
            stats.processed += 1
            conn.commit()
            continue

        stats.candidates += len(candidates)
        try:
            verdicts = _confirm(
                config,
                row["title"],
                row["raw_text"][:SNIPPET_LEN],
                candidates,
                by_ticker,
            )
        except Exception as e:
            # Leave the item unprocessed; it will be retried next run.
            stats.errors.append(f"item {item_id}: {type(e).__name__}: {e}")
            conn.commit()
            continue

        for ticker in candidates:
            confirmed = verdicts.get(ticker, False)
            _record(conn, item_id, ticker, "confirm", confirmed)
            if confirmed:
                stats.confirmed += 1
            else:
                stats.rejected += 1
        _mark_done(conn, item_id)
        stats.processed += 1
        conn.commit()

    return stats
