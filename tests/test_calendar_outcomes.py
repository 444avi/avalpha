"""Post-event outcomes: FRED macro math, earnings beat/miss, the consensus seam.

FRED/Finnhub/FMP are stubbed — the point is the shaping math (YoY/MoM, payroll
deltas, the fed-funds step, surprise), not the network."""

from avalpha import calendar_outcomes as co
from avalpha.config import Config


def _series(vals):
    """Wrap bare floats as (date, value) observations, newest first."""
    return [(f"2026-{i:02d}-01", v) for i, v in enumerate(vals)]


def _cfg(tmp_path) -> Config:
    return Config(db_path=tmp_path / "t.db", digest_dir=tmp_path,
                  email_recipient="", email_sender="")


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


# -- macro: price indexes (CPI/PCE/PPI) -------------------------------------


def test_index_outcome_core_and_headline(monkeypatch):
    data = {
        # yoy = (110-100)/100 = 10%; mom = (110-109)/109 = +0.9%; prior yoy 9%.
        "CPILFESL": _series([110, 109, 108, 107, 106, 105, 104, 103, 102, 101,
                             100.5, 100.2, 100, 100, 100]),
        "CPIAUCSL": _series([102.8, 102.6, 102, 101.8, 101.5, 101.3, 101, 100.8,
                             100.6, 100.4, 100.2, 100.1, 100, 100, 100]),
    }
    monkeypatch.setattr(co, "_fred_series", lambda s, k, limit=15: data[s])
    out = co.macro_outcome("cpi", "key")
    assert out["label"] == "CPI"
    assert out["lines"][0].startswith("Core 10.0% YoY")
    assert "prior 9.0%, +1.0pp" in out["lines"][0]
    assert "+0.9% MoM" in out["lines"][0]
    assert out["lines"][1].startswith("Headline 2.8% YoY")


def test_index_outcome_single_series_has_no_prefix(monkeypatch):
    data = {"PPIFIS": _series([104, 103.7, 103, 102.5, 102, 101.5, 101, 100.8,
                               100.6, 100.4, 100.2, 100.1, 100, 100])}
    monkeypatch.setattr(co, "_fred_series", lambda s, k, limit=15: data[s])
    out = co.macro_outcome("ppi", "key")
    assert out["lines"] == [out["lines"][0]]  # exactly one line
    assert out["lines"][0].startswith("4.0% YoY")  # no "Headline" prefix


def test_index_outcome_short_series_returns_none(monkeypatch):
    monkeypatch.setattr(co, "_fred_series", lambda s, k, limit=15: _series([100, 99]))
    assert co.macro_outcome("ppi", "key") is None


# -- macro: fed funds target range ------------------------------------------


def test_fomc_cut(monkeypatch):
    data = {
        "DFEDTARU": _series([3.75, 3.75, 3.75, 4.00, 4.00]),
        "DFEDTARL": _series([3.50, 3.50, 3.50, 3.75, 3.75]),
    }
    monkeypatch.setattr(co, "_fred_series", lambda s, k, limit=15: data[s])
    out = co.macro_outcome("fomc", "key")
    assert out["label"] == "FOMC decision"
    line = out["lines"][0]
    assert "cut 25 bps to 3.50" in line and "3.75%" in line
    assert "from 3.75" in line and "4.00%" in line


def test_fomc_hold(monkeypatch):
    data = {"DFEDTARU": _series([3.75, 3.75, 3.75]), "DFEDTARL": _series([3.50, 3.50, 3.50])}
    monkeypatch.setattr(co, "_fred_series", lambda s, k, limit=15: data[s])
    out = co.macro_outcome("fomc", "key")
    assert out["lines"][0] == "Fed funds target held at 3.50–3.75%"


# -- macro: jobs + GDP ------------------------------------------------------


def test_jobs_outcome(monkeypatch):
    data = {"PAYEMS": _series([1000, 800, 750]), "UNRATE": _series([4.1, 4.2])}
    monkeypatch.setattr(co, "_fred_series", lambda s, k, limit=15: data[s])
    out = co.macro_outcome("jobs", "key")
    assert out["lines"][0] == "Nonfarm payrolls +200k (prior +50k)"
    assert out["lines"][1] == "Unemployment 4.1% (prior 4.2%)"


def test_gdp_outcome(monkeypatch):
    monkeypatch.setattr(co, "_fred_series", lambda s, k, limit=15: _series([1.5, 2.1]))
    out = co.macro_outcome("gdp", "key")
    assert out["lines"][0] == "Real GDP 1.5% annualized (prior 2.1%)"


def test_unmapped_kind_returns_none():
    assert co.macro_outcome("beige_book", "key") is None


# -- earnings beat/miss -----------------------------------------------------


def test_earnings_outcome_beat(monkeypatch):
    rows = [{"symbol": "NVDA", "estimate": 2.1384, "actual": 2.22,
             "surprisePercent": 3.82, "year": 2027, "quarter": 2, "period": "2026-09-30"}]
    monkeypatch.setattr(co.requests, "get", lambda *a, **k: _Resp(200, rows))
    out = co.earnings_outcome("NVDA", "tok")
    assert out["beat"] is True
    assert out["eps_actual"] == 2.22 and out["eps_estimate"] == 2.1384
    assert round(out["surprise_pct"], 2) == 3.82


def test_earnings_outcome_matches_fiscal_period(monkeypatch):
    rows = [
        {"estimate": 1.0, "actual": 0.9, "year": 2026, "quarter": 4, "period": "2026-12-31"},
        {"estimate": 1.1, "actual": 1.3, "year": 2026, "quarter": 3, "period": "2026-09-30"},
    ]
    monkeypatch.setattr(co.requests, "get", lambda *a, **k: _Resp(200, rows))
    out = co.earnings_outcome("NVDA", "tok", "2026Q3")
    assert out["eps_actual"] == 1.3 and out["beat"] is True  # picked Q3, not the newest row


def test_earnings_outcome_handles_403(monkeypatch):
    monkeypatch.setattr(co.requests, "get", lambda *a, **k: _Resp(403, {}))
    assert co.earnings_outcome("NVDA", "tok") is None


# -- consensus seam (inert until an FMP key exists) -------------------------


def test_consensus_from_rows_computes_surprise():
    rows = [
        {"country": "JP", "event": "Consumer Price Index", "actual": 9, "estimate": 1},
        {"country": "US", "event": "Consumer Price Index (YoY)", "actual": 3.1, "estimate": 2.9},
    ]
    out = co.consensus_from_rows(rows, "consumer price index")
    assert out["beat"] is True and round(out["surprise"], 2) == 0.2
    assert out["event"] == "Consumer Price Index (YoY)"  # skipped the non-US row


def test_macro_consensus_inert_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    # No key → returns None and makes no network call (get would raise if hit).
    monkeypatch.setattr(co.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert co.macro_consensus("cpi", "2026-08-12", _cfg(tmp_path)) is None


def test_macro_consensus_active_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fmpkey")
    rows = [{"country": "US", "event": "CPI Consumer Price Index (YoY)",
             "actual": 3.1, "estimate": 2.9}]
    monkeypatch.setattr(co.requests, "get", lambda *a, **k: _Resp(200, rows))
    out = co.macro_consensus("cpi", "2026-08-12", _cfg(tmp_path))
    assert out["beat"] is True
