"""Swing alerter: threshold math, global dedup, opt-in scoping, debounce, gate.

The Finnhub client (`prices._quote`) and the mailer (`send_swing_alert`) are the
only I/O boundaries; both are mocked, so nothing here touches the network.
"""

from pathlib import Path

import pytest

from avalpha import accounts, db, mailer, swing, watchlist
from avalpha.collectors import prices
from avalpha.config import Config


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    # config.finnhub_api_key reads the env; _quote is mocked so the value is inert.
    monkeypatch.setenv("FINNHUB_API_KEY", "test-token")
    return Config(
        db_path=tmp_path / "swing.db",
        digest_dir=tmp_path / "digests",
        email_recipient="",
        email_sender="avalpha <avalpha@arboretuminvestments.net>",
    )


@pytest.fixture
def conn(cfg: Config):
    c = db.connect(cfg.db_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def market_open(monkeypatch):
    """Force the regular session so behaviour tests don't depend on wall-clock."""
    monkeypatch.setattr(swing, "market_state", lambda now: "regular")


@pytest.fixture
def sent(monkeypatch) -> list:
    """Capture swing emails instead of sending them."""
    captured: list[tuple[str, list]] = []
    monkeypatch.setattr(
        mailer,
        "send_swing_alert",
        lambda config, recipient, breaches: captured.append((recipient, breaches)),
    )
    return captured


def _catalog_add(conn, portfolio_id: int, ticker: str, weight: float = 1.0) -> None:
    """Enrich a ticker into the global catalog and add it to one portfolio."""
    watchlist.upsert(
        conn,
        ticker=ticker,
        cik=f"cik-{ticker}",
        legal_name=f"{ticker} Inc",
        aliases=[],
        products=[],
        executives=[],
        ir_feed_url=None,
        ir_feed_status="none",
        weight=weight,
        shares_outstanding=None,
        enrichment_confidence="high",
        portfolio_id=portfolio_id,
    )


def _enable(conn, portfolio_id: int) -> None:
    conn.execute(
        "UPDATE portfolios SET swing_alerts_enabled = 1 WHERE id = ?", (portfolio_id,)
    )
    conn.commit()


def _quotes(mapping: dict[str, tuple[float, float]]):
    """Build a fake _quote(symbol, token) plus a list recording each call.

    `mapping` is ticker -> (current, previous_close). Unknown tickers -> None.
    """
    calls: list[str] = []

    def fake(symbol: str, token: str):
        calls.append(symbol)
        if symbol not in mapping:
            return None
        c, pc = mapping[symbol]
        return {"c": c, "pc": pc, "t": 1}

    return fake, calls


def test_threshold_boundary_both_directions(cfg, conn, monkeypatch, sent):
    """Exactly ±10% fires; 9.99% does not; gains and losses both alert."""
    user = accounts.resolve_login(conn, "one@example.com")
    for t in ("AAA", "BBB", "CCC"):
        _catalog_add(conn, user.portfolio_id, t)
    _enable(conn, user.portfolio_id)

    # pc = 100: AAA +10.00 (fires), BBB -10.00 (fires), CCC +9.99 (does not).
    fake, calls = _quotes({"AAA": (110.0, 100.0), "BBB": (90.0, 100.0), "CCC": (109.99, 100.0)})
    monkeypatch.setattr(prices, "_quote", fake)

    out = swing.run_swing(cfg, conn)

    assert calls == ["AAA", "BBB", "CCC"]  # all three polled
    assert len(sent) == 1
    _recipient, breaches = sent[0]
    by_ticker = {t: pct for t, pct, _price in breaches}
    assert set(by_ticker) == {"AAA", "BBB"}  # CCC (9.99%) excluded
    assert by_ticker["AAA"] > 0 and by_ticker["BBB"] < 0
    assert out == "swing: 3 tickers polled, 2 breached, 1 email sent"


def test_global_dedup_polls_shared_ticker_once(cfg, conn, monkeypatch, sent):
    """A ticker active in two opted-in portfolios is quoted exactly once."""
    alice = accounts.resolve_login(conn, "alice@example.com")
    bob = accounts.resolve_login(conn, "bob@example.com")
    _catalog_add(conn, alice.portfolio_id, "AAA")
    watchlist.add_existing(conn, bob.portfolio_id, "AAA")
    _enable(conn, alice.portfolio_id)
    _enable(conn, bob.portfolio_id)

    fake, calls = _quotes({"AAA": (110.0, 100.0)})
    monkeypatch.setattr(prices, "_quote", fake)

    swing.run_swing(cfg, conn)

    assert calls == ["AAA"]  # one poll despite two holders


def test_opted_out_ticker_never_polled(cfg, conn, monkeypatch, sent):
    """A ticker held only by an opted-out portfolio is never polled or alerted."""
    on = accounts.resolve_login(conn, "on@example.com")
    off = accounts.resolve_login(conn, "off@example.com")
    _catalog_add(conn, on.portfolio_id, "AAA")   # opted-in, small move
    _catalog_add(conn, off.portfolio_id, "ZZZ")  # opted-out, big move
    _enable(conn, on.portfolio_id)  # `off` stays disabled

    fake, calls = _quotes({"AAA": (101.0, 100.0), "ZZZ": (200.0, 100.0)})
    monkeypatch.setattr(prices, "_quote", fake)

    swing.run_swing(cfg, conn)

    assert calls == ["AAA"]  # ZZZ never fetched
    assert sent == []  # AAA didn't breach; ZZZ can't alert an opted-out owner


def test_debounce_one_alert_per_ticker_per_day(cfg, conn, monkeypatch, sent):
    """A holding that stays ±10% is not re-emailed on the next cycle."""
    user = accounts.resolve_login(conn, "one@example.com")
    _catalog_add(conn, user.portfolio_id, "AAA")
    _enable(conn, user.portfolio_id)

    fake, _calls = _quotes({"AAA": (115.0, 100.0)})
    monkeypatch.setattr(prices, "_quote", fake)

    swing.run_swing(cfg, conn)
    assert len(sent) == 1
    swing.run_swing(cfg, conn)  # same session_date
    assert len(sent) == 1  # no second email

    rows = conn.execute("SELECT COUNT(*) FROM swing_alerts_sent").fetchone()[0]
    assert rows == 1


def test_market_gate_skips_when_closed(cfg, conn, monkeypatch, sent):
    """Outside the regular session: no poll, no send, explicit skip message."""
    monkeypatch.setattr(swing, "market_state", lambda now: "closed")
    user = accounts.resolve_login(conn, "one@example.com")
    _catalog_add(conn, user.portfolio_id, "AAA")
    _enable(conn, user.portfolio_id)

    fake, calls = _quotes({"AAA": (115.0, 100.0)})
    monkeypatch.setattr(prices, "_quote", fake)

    out = swing.run_swing(cfg, conn)

    assert out == "swing: skipped (market closed)"
    assert calls == []  # never polled
    assert sent == []


def test_fan_out_each_owner_gets_own_email_and_row(cfg, conn, monkeypatch, sent):
    """Two opted-in portfolios holding the same breached ticker each get their own."""
    alice = accounts.resolve_login(conn, "alice@example.com")
    bob = accounts.resolve_login(conn, "bob@example.com")
    _catalog_add(conn, alice.portfolio_id, "AAA")
    watchlist.add_existing(conn, bob.portfolio_id, "AAA")
    _enable(conn, alice.portfolio_id)
    _enable(conn, bob.portfolio_id)

    fake, _calls = _quotes({"AAA": (112.0, 100.0)})
    monkeypatch.setattr(prices, "_quote", fake)

    swing.run_swing(cfg, conn)

    assert {recipient for recipient, _breaches in sent} == {
        "alice@example.com",
        "bob@example.com",
    }
    rows = conn.execute(
        "SELECT portfolio_id FROM swing_alerts_sent WHERE ticker = 'AAA'"
    ).fetchall()
    assert {r["portfolio_id"] for r in rows} == {alice.portfolio_id, bob.portfolio_id}
