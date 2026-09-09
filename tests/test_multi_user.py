"""Release-blocking multi-user isolation and rollout migration coverage."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from avalpha import accounts, db, watchlist
from avalpha.config import Config
from avalpha.web.app import create_app


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "multi.db",
        digest_dir=tmp_path / "digests",
        email_recipient="",
        email_sender="sender@example.com",
    )


def _client(cfg: Config, monkeypatch, email: str) -> TestClient:
    monkeypatch.setenv("AVALPHA_WEB_DEV_USER", email)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    return TestClient(create_app(cfg), follow_redirects=False)


def _add(conn, user, ticker: str, weight: float) -> None:
    watchlist.upsert(
        conn,
        ticker=ticker,
        cik=f"cik-{ticker}",
        legal_name=f"{ticker} Incorporated",
        aliases=[],
        products=[],
        executives=[],
        ir_feed_url=None,
        ir_feed_status="none",
        weight=weight,
        shares_outstanding=None,
        enrichment_confidence="high",
        portfolio_id=user.portfolio_id,
    )


@pytest.fixture
def users(cfg: Config):
    conn = db.connect(cfg.db_path)
    alice = accounts.resolve_login(conn, "Alice@Example.com")
    bob = accounts.resolve_login(conn, "bob@example.com")
    avi = accounts.resolve_login(conn, accounts.ADMIN_EMAIL)
    _add(conn, alice, "NVDA", 10)
    watchlist.add_existing(conn, bob.portfolio_id, "NVDA", 25)
    _add(conn, bob, "MSFT", 15)
    conn.close()
    return alice, bob, avi


def test_new_login_gets_one_empty_portfolio(cfg, monkeypatch):
    client = _client(cfg, monkeypatch, "  NEW.USER@Example.COM ")
    response = client.get("/")
    assert response.status_code == 200
    assert "No holdings yet" in response.text

    conn = db.connect(cfg.db_path)
    user = conn.execute(
        "SELECT * FROM users WHERE email = 'new.user@example.com'"
    ).fetchone()
    assert user is not None
    portfolio = conn.execute(
        "SELECT id FROM portfolios WHERE owner_user_id = ?", (user["id"],)
    ).fetchone()
    assert portfolio is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM portfolio_holdings WHERE portfolio_id = ?",
        (portfolio["id"],),
    ).fetchone()[0] == 0


def test_users_are_isolated_and_shared_ticker_weights_differ(
    cfg, users, monkeypatch
):
    alice, bob, _ = users
    client = _client(cfg, monkeypatch, alice.email)

    dashboard = client.get("/")
    assert "NVDA Incorporated" in dashboard.text
    assert "MSFT Incorporated" not in dashboard.text
    assert client.get("/holding/MSFT").status_code == 303
    assert client.get(
        f"/?portfolio_id={bob.portfolio_id}", headers={"accept": "text/html"}
    ).status_code == 403
    assert client.post(
        f"/holding/NVDA/weight?portfolio_id={bob.portfolio_id}",
        data={"weight": 99},
        headers={"accept": "text/html"},
    ).status_code == 403

    assert client.post("/holding/NVDA/weight", data={"weight": 7}).status_code == 303
    conn = db.connect(cfg.db_path)
    weights = {
        row["portfolio_id"]: row["weight"]
        for row in conn.execute(
            "SELECT portfolio_id, weight FROM portfolio_holdings WHERE ticker = 'NVDA'"
        )
    }
    assert weights[alice.portfolio_id] == 7
    assert weights[bob.portfolio_id] == 25


def test_add_holding_carries_requesting_portfolio_to_enrichment(
    cfg, users, monkeypatch
):
    from avalpha.web.jobs import TriggerResult

    alice, _, _ = users
    client = _client(cfg, monkeypatch, alice.email)
    seen = {}

    def capture(job_key, email, portfolio_id):
        seen.update(job_key=job_key, email=email, portfolio_id=portfolio_id)
        return TriggerResult(True, "queued")

    client.app.state.jobs.trigger = capture
    assert client.post("/holding/add", data={"ticker": "AAPL"}).status_code == 303
    assert seen == {
        "job_key": "enrich:AAPL",
        "email": alice.email,
        "portfolio_id": alice.portfolio_id,
    }


def test_admin_can_view_both_but_other_portfolios_are_read_only(
    cfg, users, monkeypatch
):
    alice, bob, avi = users
    client = _client(cfg, monkeypatch, avi.email)

    directory = client.get("/admin")
    assert directory.status_code == 200
    assert alice.email in directory.text and bob.email in directory.text

    view = client.get(f"/admin/portfolio/{bob.portfolio_id}")
    assert view.status_code == 200
    assert "Viewing bob@example.com's portfolio · read-only" in view.text
    assert "MSFT Incorporated" in view.text
    assert "Add holding" not in view.text
    assert "Pipeline health" not in view.text

    edit = client.post(
        f"/holding/NVDA/weight?portfolio_id={bob.portfolio_id}",
        data={"weight": 1},
        headers={"accept": "text/html"},
    )
    assert edit.status_code == 403
    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT weight FROM portfolio_holdings WHERE portfolio_id = ? AND ticker = 'NVDA'",
        (bob.portfolio_id,),
    ).fetchone()[0] == 25


def test_manual_events_jobs_and_digest_pdfs_are_isolated(
    cfg, users, monkeypatch, tmp_path
):
    alice, bob, _ = users
    bob_client = _client(cfg, monkeypatch, bob.email)
    event_date = "2026-12-18"
    assert bob_client.post(
        "/calendar/add",
        data={"title": "Bob only", "event_date": event_date, "kind": "manual"},
    ).status_code == 303

    conn = db.connect(cfg.db_path)
    event_id = conn.execute(
        "SELECT id FROM calendar_events WHERE portfolio_id = ?", (bob.portfolio_id,)
    ).fetchone()[0]
    bob_pdf = tmp_path / "bob.pdf"
    bob_pdf.write_bytes(b"%PDF-1.4 bob")
    conn.execute(
        "INSERT INTO digests (portfolio_id, date, built_at, pdf_path) VALUES (?, ?, ?, ?)",
        (bob.portfolio_id, "2026-12-17", db.utcnow(), str(bob_pdf)),
    )
    conn.commit()
    conn.close()

    alice_client = _client(cfg, monkeypatch, alice.email)
    assert "Bob only" not in alice_client.get("/calendar").text
    attempted_edit = alice_client.post(
        f"/calendar/{event_id}/edit",
        data={"title": "stolen", "event_date": event_date},
    )
    assert attempted_edit.status_code == 303 and "err=" in attempted_edit.headers["location"]
    assert alice_client.get("/digests/2026-12-17.pdf").status_code == 303
    assert alice_client.get(
        f"/digests/2026-12-17.pdf?portfolio_id={bob.portfolio_id}",
        headers={"accept": "text/html"},
    ).status_code == 403
    assert alice_client.post(
        "/jobs/scorer", headers={"accept": "text/html"}
    ).status_code == 403

    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT title FROM calendar_events WHERE id = ?", (event_id,)
    ).fetchone()[0] == "Bob only"


def test_collectors_use_distinct_union_of_active_tickers(cfg, users):
    alice, bob, _ = users
    conn = db.connect(cfg.db_path)
    assert [holding.ticker for holding in watchlist.active(conn)] == ["MSFT", "NVDA"]
    watchlist.deactivate(conn, "NVDA", bob.portfolio_id)
    assert [holding.ticker for holding in watchlist.active(conn)] == ["MSFT", "NVDA"]
    watchlist.deactivate(conn, "NVDA", alice.portfolio_id)
    assert [holding.ticker for holding in watchlist.active(conn)] == ["MSFT"]


def test_digest_timer_targets_each_portfolio_owner(cfg, users, monkeypatch):
    from avalpha.digest import build as digest_build
    from avalpha import mailer

    conn = db.connect(cfg.db_path)
    sent = []

    def fake_build(config, db_conn, date_str=None, portfolio_id=None):
        path = config.digest_dir / str(portfolio_id) / f"avalpha-{date_str}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF")
        db_conn.execute(
            "INSERT INTO digests (portfolio_id, date, built_at, pdf_path) VALUES (?, ?, ?, ?)",
            (portfolio_id, date_str, db.utcnow(), str(path)),
        )
        db_conn.commit()
        return path

    monkeypatch.setattr(digest_build, "build_digest", fake_build)
    monkeypatch.setattr(
        mailer,
        "send_digest_email",
        lambda config, path, date_str, recipient=None: sent.append(
            (recipient, path.parent.name)
        ),
    )
    digest_build.build_and_send(cfg, conn, date_str="2026-12-17")
    assert {recipient for recipient, _ in sent} == {
        accounts.ADMIN_EMAIL,
        "alice@example.com",
        "bob@example.com",
    }
    owner_ids = {
        str(row["id"])
        for row in conn.execute("SELECT id FROM portfolios")
    }
    assert {directory for _, directory in sent} == owner_ids


def test_v3_migration_assigns_shared_data_only_to_avi(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE watchlist (
            ticker TEXT PRIMARY KEY, cik TEXT NOT NULL, legal_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]', products_json TEXT NOT NULL DEFAULT '[]',
            executives_json TEXT NOT NULL DEFAULT '[]', ir_feed_url TEXT,
            ir_feed_status TEXT NOT NULL DEFAULT 'none', weight REAL NOT NULL DEFAULT 0,
            shares_outstanding INTEGER, enrichment_confidence TEXT, enriched_at TEXT,
            industry TEXT, active INTEGER NOT NULL DEFAULT 1, added_at TEXT NOT NULL,
            deactivated_at TEXT
        );
        INSERT INTO watchlist (ticker, cik, legal_name, weight, active, added_at)
        VALUES ('NVDA', '1', 'NVIDIA', 12, 1, '2026-01-01T00:00:00Z');
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY, ticker TEXT, kind TEXT NOT NULL, title TEXT NOT NULL,
            event_date TEXT NOT NULL, event_at TEXT, tz TEXT, is_timed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'scheduled', source TEXT NOT NULL, source_ref TEXT,
            confidence TEXT, fiscal_period TEXT, dedup_key TEXT NOT NULL UNIQUE,
            meta_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO calendar_events
            (kind, title, event_date, source, dedup_key, created_at, updated_at)
        VALUES ('manual', 'Legacy note', '2026-12-01', 'manual', 'manual:old',
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        CREATE TABLE web_jobs (
            id INTEGER PRIMARY KEY, job TEXT NOT NULL, status TEXT NOT NULL,
            triggered_by TEXT, started_at TEXT NOT NULL, finished_at TEXT, output TEXT
        );
        CREATE TABLE digests (
            date TEXT PRIMARY KEY, built_at TEXT NOT NULL, sent_at TEXT, pdf_path TEXT NOT NULL
        );
        INSERT INTO digests VALUES ('2026-01-02', '2026-01-03T00:00:00Z', NULL, 'old.pdf');
        PRAGMA user_version = 3;
        """
    )
    conn.close()

    migrated = db.connect(path)
    avi = accounts.resolve_login(migrated, accounts.ADMIN_EMAIL)
    newcomer = accounts.resolve_login(migrated, "new@example.com")
    assert [h.ticker for h in watchlist.active(migrated, avi.portfolio_id)] == ["NVDA"]
    assert watchlist.active(migrated, newcomer.portfolio_id) == []
    assert migrated.execute(
        "SELECT portfolio_id FROM calendar_events WHERE source = 'manual'"
    ).fetchone()[0] == avi.portfolio_id
    assert migrated.execute("SELECT portfolio_id FROM digests").fetchone()[0] == avi.portfolio_id
    assert [row["name"] for row in migrated.execute("PRAGMA table_info(watchlist)")] == [
        "ticker", "cik", "legal_name", "aliases_json", "products_json",
        "executives_json", "ir_feed_url", "ir_feed_status", "shares_outstanding",
        "enrichment_confidence", "enriched_at", "industry",
    ]
