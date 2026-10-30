"""Background job runner.

Runs queued collection jobs one at a time on the browser thread. A
database-level runner lock prevents two app processes from driving the same
data/browser profiles concurrently. Jobs that need sign-in are parked in
``needs_login`` and are re-queued automatically once that account's sign-in
is verified again.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from ..services.jobs import WORKER_ID
from ..util import Conflict, now_iso, parse_iso
from .browser import BrowserWorker
from .traversal import CollectionRun

log = logging.getLogger("canvas_hub.runner")
LOCK_KEY = "runner_lock"
LOCK_STALE_SECONDS = 90


class JobRunner:
    def __init__(self, hub, worker: BrowserWorker, *, poll_seconds: float = 1.0):
        self.hub = hub
        self.worker = worker
        self.poll = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="canvas-hub-runner", daemon=True)
        self.has_lock = False
        self.current_job: int | None = None
        self.worker.on_signed_in = self._on_signed_in

    # ---- lock -----------------------------------------------------------
    def _try_lock(self) -> bool:
        now = now_iso()
        with self.hub.db.tx() as c:
            row = c.execute("SELECT value FROM app_settings WHERE key=?", (LOCK_KEY,)).fetchone()
            if row:
                cur = json.loads(row["value"])
                hb = parse_iso(cur.get("heartbeat"))
                fresh = hb and (datetime.now(timezone.utc) - hb).total_seconds() < LOCK_STALE_SECONDS
                if cur.get("worker") != WORKER_ID and fresh:
                    return False
            c.execute("INSERT INTO app_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (LOCK_KEY, json.dumps({"worker": WORKER_ID, "heartbeat": now})))
        return True

    def _release(self) -> None:
        with self.hub.db.tx() as c:
            row = c.execute("SELECT value FROM app_settings WHERE key=?", (LOCK_KEY,)).fetchone()
            if row and json.loads(row["value"]).get("worker") == WORKER_ID:
                c.execute("DELETE FROM app_settings WHERE key=?", (LOCK_KEY,))

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.has_lock = self._try_lock()
        if not self.has_lock:
            log.warning("another Canvas Hub process holds the runner lock; this process will not run collection jobs")
            return
        interrupted = self.hub.jobs.recover_after_restart()
        if interrupted:
            log.info("marked interrupted jobs: %s", interrupted)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)
        if self.has_lock:
            self._release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._try_lock()
                job = self.hub.jobs.next_queued()
                if job:
                    self._run_one(job)
                    continue
            except Exception:  # noqa: BLE001
                log.exception("runner loop error")
            self._stop.wait(self.poll)

    def _run_one(self, job: dict) -> None:
        inst = job["instance_id"]
        state = self.worker.call(self.worker.check_session, inst, timeout=120)
        if state.get("status") != "signed_in":
            self.hub.jobs.set_state(job["id"], "needs_login",
                                    message="Sign in to Canvas to run this job; it starts automatically after sign-in.")
            return
        if state.get("account_id") != job["account_id"]:
            acct = self.hub.identity.get_account(job["account_id"])
            self.hub.jobs.set_state(
                job["id"], "needs_login",
                message=f"The browser is signed in as a different Canvas user. Sign in as "
                        f"{acct.get('display_name') or acct['canvas_user_id']} to run this job (data is never mixed).")
            return
        self.current_job = job["id"]

        def work():
            self.worker.busy_with = f"job {job['id']}"
            try:
                run = CollectionRun(self.hub, None, job["id"])
                run.client = self.worker.client_for(inst, should_stop=run.should_stop)
                return run.run()
            finally:
                self.worker.busy_with = None

        try:
            result = self.worker.call(work, timeout=None)
            log.info("job %s finished: %s", job["id"], result)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed", job["id"])
            self.hub.jobs.set_state(job["id"], "failed", error=f"{type(e).__name__}: {e}",
                                    message="The job stopped unexpectedly; verified observations were kept. Resume to retry.")
        finally:
            self.current_job = None
            if state.get("status") == "signed_in":
                # Refresh cached session state after a job (it may have expired).
                try:
                    self.worker.call(self.worker.check_session, inst, timeout=60)
                except Exception:  # noqa: BLE001
                    pass

    def _on_signed_in(self, instance_id: int, account_id: int) -> None:
        rows = self.hub.db.query("SELECT id FROM jobs WHERE account_id=? AND state='needs_login'", (account_id,))
        for r in rows:
            try:
                self.hub.jobs.resume(r["id"])
            except Exception:  # noqa: BLE001
                pass

    # ---- API used by the web layer ---------------------------------------------
    def start_collection(self, account_id: int, mode: str, scope_override: dict | None = None) -> dict:
        if mode not in ("baseline", "thorough", "quick"):
            raise Conflict("mode must be baseline, thorough or quick", code="invalid_mode", status=400)
        acct = self.hub.identity.get_account(account_id)
        scope = acct["scope"]
        if scope_override:
            from ..services.identity import normalize_scope
            scope = normalize_scope(scope_override, scope)
        if mode == "quick" and not self.hub.db.one(
                "SELECT 1 FROM jobs WHERE account_id=? AND kind='collect' AND mode IN ('baseline','thorough') "
                "AND state IN ('completed','completed_with_gaps')", (account_id,)):
            mode_note = "No thorough baseline exists yet; a quick check cannot establish one."
        else:
            mode_note = None
        job = self.hub.jobs.create(account_id=account_id, instance_id=acct["instance_id"], kind="collect", mode=mode,
                                   scope=scope)
        if mode_note:
            self.hub.jobs.update_stats(job["id"], note=mode_note)
        return self.hub.jobs.get(job["id"])

    def status(self) -> dict:
        return {"has_lock": self.has_lock, "current_job": self.current_job, "worker": WORKER_ID,
                "running": self._thread.is_alive()}

