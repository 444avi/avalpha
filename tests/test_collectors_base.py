from avalpha.collectors.base import insert_item, run, text_from_html
from avalpha.config import Config
from avalpha.db import connect


def _config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "t.db",
        digest_dir=tmp_path,
        email_recipient="x@example.com",
        email_sender="y@example.com",
    )


def test_run_logs_success_and_due_check(tmp_path):
    conn = connect(tmp_path / "t.db")
    config = _config(tmp_path)

    calls = []

    def collect(config, conn):
        calls.append(1)
        return 5, 2

    out = run(config, conn, "edgar", collect)
    assert out.status == "ran" and out.fetched == 5 and out.new == 2
    # Immediately re-running is not due (interval >= 30s in every state).
    out2 = run(config, conn, "edgar", collect)
    assert out2.status == "skipped"
    # force bypasses the due-check.
    out3 = run(config, conn, "edgar", collect, force=True)
    assert out3.status == "ran"
    assert len(calls) == 2

    row = conn.execute(
        "SELECT ok, items_new FROM collector_runs ORDER BY id LIMIT 1"
    ).fetchone()
    assert row["ok"] == 1 and row["items_new"] == 2


def test_run_isolates_failure(tmp_path):
    conn = connect(tmp_path / "t.db")
    config = _config(tmp_path)

    def broken(config, conn):
        raise ValueError("boom")

    out = run(config, conn, "reddit", broken, force=True)
    assert out.status == "failed" and "boom" in out.error
    row = conn.execute("SELECT ok, error FROM collector_runs").fetchone()
    assert row["ok"] == 0 and "ValueError" in row["error"]


def test_failed_run_does_not_reset_cadence(tmp_path):
    conn = connect(tmp_path / "t.db")
    config = _config(tmp_path)

    def broken(config, conn):
        raise ValueError("boom")

    run(config, conn, "edgar", broken, force=True)

    # A failure never counts as a successful run, so the next attempt is due.
    def works(config, conn):
        return 1, 1

    out = run(config, conn, "edgar", works)
    assert out.status == "ran"


def test_insert_item_dedups_on_url(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert insert_item(conn, source="gnews", source_id="a", url="https://x/1", title="t")
    assert not insert_item(
        conn, source="gnews", source_id="b", url="https://x/1", title="t2"
    )
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_text_from_html_strips_tags_and_scripts():
    html = "<html><head><script>var x=1;</script></head><body><p>Hello <b>world</b></p></body></html>"
    text = text_from_html(html)
    assert "Hello" in text and "world" in text
    assert "var x" not in text
