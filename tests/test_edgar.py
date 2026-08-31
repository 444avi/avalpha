from avalpha.collectors.edgar import _ACC_RE, _TITLE_RE


def test_title_regex_parses_getcurrent_entries():
    m = _TITLE_RE.match("8-K - NVIDIA CORP (0001045810) (Filer)")
    assert m
    assert m.group("form") == "8-K"
    assert m.group("company") == "NVIDIA CORP"
    assert m.group("cik") == "0001045810"


def test_title_regex_handles_hyphenated_company_names():
    m = _TITLE_RE.match("SC 13D/A - ACME-WIDGETS - CO (0000123456) (Subject)")
    assert m
    # Non-greedy form match takes the first " - " split; company keeps the rest.
    assert m.group("form") == "SC 13D/A"
    assert m.group("cik") == "0000123456"


def test_accession_regex():
    link = (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000029/"
        "0001045810-26-000029-index.htm"
    )
    m = _ACC_RE.search(link)
    assert m and m.group(1) == "0001045810-26-000029"
