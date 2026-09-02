"""On-demand pipeline triggers for the ops console.

The collectors, matcher, scorer, and digest normally run on systemd timers.
This lets a signed-in member kick one immediately — e.g. right after adding a
holding — without shelling into the box. Jobs run in background threads (they do
blocking network/LLM work) each with their own SQLite connection, and every run
is recorded in the ``web_jobs`` table so the UI can show what happened.

Guardrails, because these spend API tokens:
  * the same exact job (e.g. ``enrich:NVDA``) never runs twice at once;
  * pipeline jobs (collector/matcher/scorer/digest) have a per-family cooldown
    so a member can't mash the button and rack up cost;
  * at most ``MAX_CONCURRENT`` jobs run at a time.
"""

import threading
import time
import traceback
from dataclasses import dataclass, field

from avalpha import db
from avalpha.config import Config

COOLDOWN_SECONDS = 30  # per pipeline family (not enrich)
MAX_CONCURRENT = 3
_OUTPUT_TAIL = 4000  # chars of job output persisted


@dataclass
class TriggerResult:
    accepted: bool
    message: str
    job_id: int | None = None


@dataclass
class _State:
    running: set[str] = field(default_factory=set)
    last_finished: dict[str, float] = field(default_factory=dict)  # family -> monotonic


class JobRunner:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._state = _State()

    # -- public API ---------------------------------------------------------

    def trigger(self, job_key: str, member_email: str) -> TriggerResult:
        """Validate guardrails and, if clear, start the job in a thread."""
        family = job_key.split(":", 1)[0]
        now = time.monotonic()
        with self._lock:
            if job_key in self._state.running:
                return TriggerResult(False, f"{job_key} is already running.")
            if len(self._state.running) >= MAX_CONCURRENT:
                return TriggerResult(
                    False, "Too many jobs running right now — try again shortly."
                )
            if family != "enrich":
                last = self._state.last_finished.get(family)
                if last is not None and (now - last) < COOLDOWN_SECONDS:
                    wait = int(COOLDOWN_SECONDS - (now - last))
                    return TriggerResult(
                        False, f"{family} just ran — wait {wait}s before re-running."
                    )
            self._state.running.add(job_key)

        job_id = self._record_start(job_key, member_email)
        thread = threading.Thread(
            target=self._run, args=(job_key, family, job_id), daemon=True
        )
        thread.start()
        return TriggerResult(True, f"Started {job_key}.", job_id=job_id)

    # -- execution ----------------------------------------------------------

    def _run(self, job_key: str, family: str, job_id: int) -> None:
        status, output = "ok", ""
        try:
            output = self._dispatch(job_key)
        except Exception:  # noqa: BLE001 - surface any failure to the UI
            status = "error"
            output = traceback.format_exc()
        finally:
            self._record_finish(job_id, status, output)
            with self._lock:
                self._state.running.discard(job_key)
                self._state.last_finished[family] = time.monotonic()

    def _dispatch(self, job_key: str) -> str:
        """Run one job with a fresh connection. Returns a short result string."""
        conn = db.connect(self.config.db_path)
        try:
            if job_key.startswith("collector:"):
                from avalpha.collectors import run_source

                source = job_key.split(":", 1)[1]
                return str(run_source(self.config, conn, source, force=True))
            if job_key == "matcher":
                from avalpha.matcher import match_pending

                return str(match_pending(self.config, conn, limit=500))
            if job_key == "scorer":
                from avalpha.scorer import drain

                scored, errors = drain(self.config, conn, limit=200)
                return f"scored={scored} errors={errors}"
            if job_key == "digest":
                from avalpha.digest.build import build_digest

                path = build_digest(self.config, conn)
                return f"built {path}"
            if job_key.startswith("enrich:"):
                return self._enrich(conn, job_key.split(":", 1)[1])
            raise ValueError(f"unknown job {job_key!r}")
        finally:
            conn.close()

    def _enrich(self, conn, ticker: str) -> str:
        """Add/refresh a holding: SEC + web-search enrichment, then upsert."""
        from avalpha import watchlist
        from avalpha.enrich import enrich

        ticker = ticker.upper()
        result = enrich(self.config, ticker)
        watchlist.upsert(
            conn,
            ticker=result.ticker,
            cik=result.cik,
            legal_name=result.legal_name,
            aliases=result.aliases,
            products=result.products,
            executives=result.executives,
            ir_feed_url=result.ir_feed_url,
            ir_feed_status=result.ir_feed_status,
            weight=0.0,
            shares_outstanding=result.shares_outstanding,
            enrichment_confidence=result.confidence,
            industry=result.industry,
        )
        return (
            f"added {result.ticker} — {result.legal_name} "
            f"(confidence {result.confidence})"
        )

    # -- web_jobs bookkeeping ----------------------------------------------

    def _record_start(self, job_key: str, member_email: str) -> int:
        conn = db.connect(self.config.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO web_jobs (job, status, triggered_by, started_at) "
                "VALUES (?, 'running', ?, ?)",
                (job_key, member_email, db.utcnow()),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _record_finish(self, job_id: int, status: str, output: str) -> None:
        conn = db.connect(self.config.db_path)
        try:
            conn.execute(
                "UPDATE web_jobs SET status = ?, finished_at = ?, output = ? WHERE id = ?",
                (status, db.utcnow(), output[-_OUTPUT_TAIL:], job_id),
            )
            conn.commit()
        finally:
            conn.close()
