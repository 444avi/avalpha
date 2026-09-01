"""Web console: auth gating, portfolio edits, job guardrails, read routes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from avalpha import db
from avalpha.config import Config
from avalpha.web.app import create_app


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "test.db",
        digest_dir=tmp_path / "digests",
        email_recipient="",
        email_sender="",
        web_fund_name="The Silo Fund",
    )


@pytest.fixture
def seeded(cfg: Config) -> Config:
    conn = db.connect(cfg.db_path)  # runs migrations to current version
    conn.execute(
        "INSERT INTO watchlist (ticker, cik, legal_name, weight, active, added_at) "
        "VALUES ('NVDA', '0001045810', 'NVIDIA CORP', 12, 1, ?)",
        (db.utcnow(),),
    )
    conn.execute(
        "INSERT INTO items (id, source, url, url_hash, title, fetched_at) "
        "VALUES (1, 'gnews', 'https://x/1', 'h1', 'NVDA does a thing', ?)",
        (db.utcnow(),),
    )
    conn.execute(
        "INSERT INTO scores (item_id, ticker, prompt_version, model, materiality, "
        "direction, category, mechanism, summary, raw_json, scored_at) "
        "VALUES (1, 'NVDA', ?, 'm', 4, 'positive', 'product', 'moves revenue', "
        "'up', '{}', ?)",
        (__import__("avalpha.scorer", fromlist=["PROMPT_VERSION"]).PROMPT_VERSION, db.utcnow()),
    )
    conn.commit()
    conn.close()
    return cfg


def client(cfg: Config, monkeypatch, *, dev_user="member@thesilofund.com",
           team=None, aud=None) -> TestClient:
    for var, val in (
        ("AVALPHA_WEB_DEV_USER", dev_user),
        ("CF_ACCESS_TEAM_DOMAIN", team),
        ("CF_ACCESS_AUD", aud),
    ):
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)
    return TestClient(create_app(cfg), follow_redirects=False)


# -- auth -------------------------------------------------------------------

def test_healthz_open_without_auth(cfg, monkeypatch):
    c = client(cfg, monkeypatch, dev_user=None)
    assert c.get("/healthz").status_code == 200


def test_no_identity_is_forbidden(cfg, monkeypatch):
    c = client(cfg, monkeypatch, dev_user=None)  # no dev bypass, no CF config
    r = c.get("/", headers={"accept": "text/html"})
    assert r.status_code == 403
    assert "Members only" in r.text
    assert c.get("/", headers={"accept": "application/json"}).status_code == 403


def test_dev_bypass_ignored_when_access_configured(cfg, monkeypatch):
    # If real Access is configured, the dev bypass must not grant entry.
    c = client(cfg, monkeypatch, dev_user="sneaky@x.com",
               team="fund.cloudflareaccess.com", aud="deadbeef")
    assert c.get("/").status_code == 403  # no valid JWT on the request


def test_dev_bypass_allows_when_no_access(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    r = c.get("/")
    assert r.status_code == 200
    assert "NVDA" in r.text
    assert c.get("/me").json()["email"] == "member@thesilofund.com"


# -- reads ------------------------------------------------------------------

def test_dashboard_shows_holding_and_signal(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    body = c.get("/").text
    assert "NVIDIA CORP" in body
    assert "moves revenue" in body  # the scored mechanism


def test_holding_detail(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    assert c.get("/holding/NVDA").status_code == 200
    assert c.get("/holding/ZZZZ").status_code == 303  # unknown -> redirect


# -- portfolio edits --------------------------------------------------------

def test_set_weight(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    r = c.post("/holding/NVDA/weight", data={"weight": "7.5"})
    assert r.status_code == 303
    conn = db.connect(seeded.db_path)
    assert conn.execute("SELECT weight FROM watchlist WHERE ticker='NVDA'").fetchone()[0] == 7.5


def test_weight_out_of_range_rejected(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    r = c.post("/holding/NVDA/weight", data={"weight": "250"})
    assert "err=" in r.headers["location"]


def test_deactivate_and_reactivate(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    assert c.post("/holding/NVDA/deactivate").status_code == 303
    conn = db.connect(seeded.db_path)
    assert conn.execute("SELECT active FROM watchlist WHERE ticker='NVDA'").fetchone()[0] == 0
    c.post("/holding/NVDA/activate")
    assert conn.execute("SELECT active FROM watchlist WHERE ticker='NVDA'").fetchone()[0] == 1


def test_bad_ticker_add_rejected(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    r = c.post("/holding/add", data={"ticker": "!!bad"})
    assert "err=" in r.headers["location"]


# -- job guardrails ---------------------------------------------------------

def test_unknown_job_rejected(seeded, monkeypatch):
    c = client(seeded, monkeypatch)
    r = c.post("/jobs/collector:bogus")
    assert "err=" in r.headers["location"]


def test_job_dedup_and_cooldown(seeded, monkeypatch):
    from avalpha.web.jobs import JobRunner

    runner = JobRunner(seeded)
    # Pretend a scorer run is in flight: a second identical trigger is refused.
    runner._state.running.add("scorer")
    assert not runner.trigger("scorer", "m@x.com").accepted
    runner._state.running.discard("scorer")
    # After a run finishes, the family cooldown blocks an immediate re-run.
    import time
    runner._state.last_finished["scorer"] = time.monotonic()
    assert not runner.trigger("scorer", "m@x.com").accepted
