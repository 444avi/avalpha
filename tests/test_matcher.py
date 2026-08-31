from avalpha.matcher import _clean_company_name, build_patterns, cheap_pass
from avalpha.watchlist import Holding


def _holding(ticker, legal_name, aliases=(), products=(), executives=()):
    return Holding(
        ticker=ticker,
        cik="0000000001",
        legal_name=legal_name,
        aliases=list(aliases),
        products=list(products),
        executives=list(executives),
        ir_feed_url=None,
        ir_feed_status="none",
        weight=0,
        shares_outstanding=None,
        enrichment_confidence="high",
        active=True,
    )


def test_matches_company_name_and_alias():
    patterns = build_patterns(
        [_holding("NVDA", "NVIDIA CORP", aliases=["Nvidia"], products=["GeForce"])]
    )
    assert "NVDA" in cheap_pass("Nvidia beats earnings expectations", patterns)
    assert "NVDA" in cheap_pass("New GeForce cards announced", patterns)


def test_never_matches_ticker_symbol():
    # Ford ($F), Allstate ($ALL), Gartner ($IT), ON Semi, KeyCorp — the spec's
    # canonical false-positive generators.
    holdings = [
        _holding("F", "FORD MOTOR CO", aliases=["Ford", "F"]),
        _holding("ALL", "ALLSTATE CORP", aliases=["Allstate", "ALL"]),
        _holding("IT", "GARTNER INC", aliases=["Gartner", "IT"]),
        _holding("ON", "ON SEMICONDUCTOR CORP", aliases=["onsemi", "ON"]),
        _holding("KEY", "KEYCORP", aliases=["KeyBank", "KEY"]),
    ]
    patterns = build_patterns(holdings)
    text = (
        "ALL the news that matters: IT departments say the key trend is ON "
        "track, and f is a letter"
    )
    assert cheap_pass(text, patterns) == {}


def test_short_acronym_is_case_sensitive():
    patterns = build_patterns([_holding("NVDA", "NVIDIA CORP", products=["CUDA"])])
    assert "NVDA" in cheap_pass("New CUDA release improves throughput", patterns)
    # 'barracuda' must not match (word boundary) and 'cuda' lowercase must not
    # match (case-sensitive acronym rule).
    assert cheap_pass("barracuda networks earnings", patterns) == {}
    assert cheap_pass("the cuda word in lowercase", patterns) == {}


def test_executive_names_match():
    patterns = build_patterns(
        [_holding("NVDA", "NVIDIA CORP", executives=["Jensen Huang"])]
    )
    assert "NVDA" in cheap_pass("Jensen Huang announced today...", patterns)
    assert cheap_pass("someone named Huang, unrelated", patterns) == {}


def test_legal_suffix_stripped_for_matching():
    assert _clean_company_name("NVIDIA CORP") == "NVIDIA"
    assert _clean_company_name("Apple Inc.") == "Apple"
    assert _clean_company_name("ON SEMICONDUCTOR CORP") == "ON SEMICONDUCTOR"
    patterns = build_patterns([_holding("AAPL", "Apple Inc.")])
    assert "AAPL" in cheap_pass("Apple releases new products", patterns)


def test_multiple_tickers_in_one_item():
    patterns = build_patterns(
        [
            _holding("NVDA", "NVIDIA CORP", aliases=["Nvidia"]),
            _holding("AMD", "ADVANCED MICRO DEVICES INC", aliases=["AMD"]),
        ]
    )
    found = cheap_pass("Nvidia and Advanced Micro Devices both rallied", patterns)
    assert set(found) == {"NVDA", "AMD"}


def test_alias_equal_to_ticker_is_excluded():
    # "AMD" the alias equals the ticker symbol, so the never-match-tickers rule
    # drops it; coverage for such companies comes from the full name. This is
    # the spec's explicit tradeoff: a missed headline beats constant $F/$ALL
    # false positives.
    patterns = build_patterns(
        [_holding("AMD", "ADVANCED MICRO DEVICES INC", aliases=["AMD"])]
    )
    assert cheap_pass("AMD beats estimates", patterns) == {}
    assert "AMD" in cheap_pass("Advanced Micro Devices beats estimates", patterns)


def test_min_term_length_guard():
    # Two-character alias would be noise; it must be dropped at build time.
    patterns = build_patterns([_holding("GM", "GENERAL MOTORS CO", aliases=["GM"])])
    terms = {p.term for p in patterns}
    assert "GM" not in terms
    assert "GENERAL MOTORS" in {t.upper() for t in terms}
