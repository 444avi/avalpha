"""One-shot entity enrichment, run by `avalpha add`.

Deterministic facts (CIK, legal name, shares outstanding) come from SEC EDGAR.
Aliases, products, executives, and the IR feed URL come from one Sonnet call
with web search, then the IR feed is verified by actually fetching it. The
matcher depends entirely on this metadata; failures are reported, never
silently swallowed.
"""

import json
import re
from dataclasses import dataclass, field

import feedparser
import requests

from avalpha.config import Config

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
PROFILE2_URL = "https://finnhub.io/api/v1/stock/profile2"

ENRICH_PROMPT = """\
You are building watchlist metadata for a stock monitoring system. The system
matches news articles and press releases to companies by name — the quality of
your answer directly determines whether stories about this company are caught.

Company: {legal_name} (ticker {ticker}, SEC CIK {cik})

Use web search to verify current facts, then answer with a single JSON object
in a ```json fenced block, exactly this shape:

{{
  "aliases": ["common and informal names for the company, including the short name the press uses; exclude the ticker symbol itself"],
  "products": ["principal product, brand, and service names the press would mention instead of the company name"],
  "ceo": "current CEO full name",
  "cfo": "current CFO full name",
  "ir_feed_candidates": ["up to 3 candidate URLs for the company's investor-relations press-release RSS/Atom feed, most likely first; empty list if none exists"],
  "confidence": "high | medium | low — your confidence in this metadata overall",
  "notes": "one sentence on anything uncertain"
}}

Keep aliases and products to names distinctive enough to identify the company —
skip generic words that would false-positive on unrelated text.
"""


@dataclass
class Enrichment:
    ticker: str
    cik: str
    legal_name: str
    shares_outstanding: int | None
    aliases: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    executives: list[str] = field(default_factory=list)
    ir_feed_url: str | None = None
    ir_feed_status: str = "none"
    confidence: str = "low"
    notes: str = ""
    industry: str | None = None


class EnrichmentError(Exception):
    pass


def resolve_cik(ticker: str, user_agent: str) -> tuple[str, str]:
    """Ticker -> (zero-padded CIK, legal name) via SEC's official mapping."""
    resp = requests.get(
        COMPANY_TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30
    )
    resp.raise_for_status()
    for entry in resp.json().values():
        if entry["ticker"].upper() == ticker.upper():
            return f"{entry['cik_str']:010d}", entry["title"]
    raise EnrichmentError(
        f"{ticker} not found in SEC company_tickers.json — not a US-listed "
        "SEC registrant, or the ticker is wrong"
    )


def fetch_shares_outstanding(cik: str, user_agent: str) -> int | None:
    resp = requests.get(
        COMPANYFACTS_URL.format(cik=cik),
        headers={"User-Agent": user_agent},
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    facts = resp.json().get("facts", {}).get("dei", {})
    concept = facts.get("EntityCommonStockSharesOutstanding", {})
    values = concept.get("units", {}).get("shares", [])
    if not values:
        return None
    latest = max(values, key=lambda v: v.get("end", ""))
    return int(latest["val"])


def fetch_industry(config: Config, ticker: str) -> str | None:
    """profile2.finnhubIndustry, for the calendar's bio (PDUFA) gate. Best-effort:
    the matcher/digest don't depend on it, so any failure just yields None."""
    try:
        resp = requests.get(
            PROFILE2_URL,
            params={"symbol": ticker.upper(), "token": config.finnhub_api_key},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("finnhubIndustry") or None
    except (requests.RequestException, RuntimeError, ValueError):
        return None


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        # Fall back to the last {...} span in the text.
        spans = re.findall(r"\{.*\}", text, re.DOTALL)
        if not spans:
            raise EnrichmentError("model returned no JSON object")
        candidate = spans[-1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise EnrichmentError(f"model JSON did not parse: {e}") from e


def llm_enrich(config: Config, ticker: str, cik: str, legal_name: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    messages = [
        {
            "role": "user",
            "content": ENRICH_PROMPT.format(
                legal_name=legal_name, ticker=ticker, cik=cik
            ),
        }
    ]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]

    for _ in range(4):
        response = client.messages.create(
            model=config.model_scorer,
            max_tokens=4096,
            tools=tools,
            messages=messages,
        )
        if response.stop_reason == "pause_turn":
            messages = messages[:1] + [
                {"role": "assistant", "content": response.content}
            ]
            continue
        text = "".join(b.text for b in response.content if b.type == "text")
        return _extract_json(text)
    raise EnrichmentError("enrichment model kept pausing; giving up")


def verify_ir_feed(candidates: list[str], user_agent: str) -> str | None:
    """Return the first candidate that fetches and parses as a non-empty feed."""
    for url in candidates[:3]:
        try:
            resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
            if resp.status_code != 200:
                continue
            parsed = feedparser.parse(resp.content)
            if parsed.entries:
                return url
        except requests.RequestException:
            continue
    return None


def enrich(config: Config, ticker: str) -> Enrichment:
    ua = config.edgar_user_agent
    cik, legal_name = resolve_cik(ticker, ua)
    shares = fetch_shares_outstanding(cik, ua)

    data = llm_enrich(config, ticker, cik, legal_name)

    executives = [n for n in (data.get("ceo"), data.get("cfo")) if n]
    feed_url = verify_ir_feed(data.get("ir_feed_candidates", []), ua)
    confidence = data.get("confidence", "low")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    industry = fetch_industry(config, ticker)

    return Enrichment(
        ticker=ticker.upper(),
        cik=cik,
        legal_name=legal_name,
        shares_outstanding=shares,
        aliases=[a for a in data.get("aliases", []) if isinstance(a, str)],
        products=[p for p in data.get("products", []) if isinstance(p, str)],
        executives=executives,
        ir_feed_url=feed_url,
        ir_feed_status="ok" if feed_url else "none",
        confidence=confidence,
        notes=data.get("notes", ""),
        industry=industry,
    )
