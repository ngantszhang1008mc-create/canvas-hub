"""Runner ownership survives long tasks and is lost safely, without a browser."""
import json
import threading
from types import SimpleNamespace

import pytest

from canvas_hub.collector import runner as runner_module
from canvas_hub.collector.runner import JobRunner, LOCK_KEY
from canvas_hub.util import Conflict


def job_for(hub, account, state="queued"):
    return hub.jobs.create(account_id=account["id"], instance_id=account["instance_id"],
                           kind="collect", mode="quick", scope={}, state=state)


def test_lease_renews_during_long_job_and_drain_shutdown(hub, account, monkeypatch):
    monkeypatch.setattr(runner_module, "LOCK_RENEW_SECONDS", 0.01)
    job_for(hub, account)
    runner = JobRunner(hub, SimpleNamespace(), poll_seconds=0.01)
    busy, draining, renewed_busy, renewed_draining, release = [threading.Event() for _ in range(5)]
    renew = runner._renew_lock
    def observe_renewal():
        result = renew()
        if busy.is_set():
            renewed_busy.set()
        if draining.is_set():
            renewed_draining.set()
        return result
    monkeypatch.setattr(runner, "_renew_lock", observe_renewal)
    def long_job(job):
        busy.set()
        assert runner._stop.wait(5)
        draining.set()
        assert release.wait(5)
    monkeypatch.setattr(runner, "_run_one", long_job)
    stopper = None
    runner.start()
    try:
        assert busy.wait(2) and renewed_busy.wait(2)
        # A distinct runner in the same process must not share an ownership ID.
        contender = JobRunner(hub, SimpleNamespace())
        assert contender._try_lock() is False
        stopper = threading.Thread(target=runner.stop)
        stopper.start()
        assert draining.wait(2) and renewed_draining.wait(2)
        assert stopper.is_alive() and runner._lease_thread.is_alive()
        assert contender._try_lock() is False
    finally:
        release.set()
        if stopper:
            stopper.join(5)
        runner.stop()
    assert not runner._thread.is_alive() and not runner._lease_thread.is_alive()
    assert not hub.db.one("SELECT 1 FROM app_settings WHERE key=?", (LOCK_KEY,))


def test_lost_lease_stops_active_job_without_releasing_other_owner(hub, account, monkeypatch):
    monkeypatch.setattr(runner_module, "LOCK_RENEW_SECONDS", 0.01)
    job_for(hub, account)
    runner = JobRunner(hub, SimpleNamespace(), poll_seconds=0.01)
    busy, stopped = threading.Event(), threading.Event()
    def long_job(job):
        busy.set()
        assert runner._stop.wait(5)
        stopped.set()
    monkeypatch.setattr(runner, "_run_one", long_job)
    runner.start()
    try:
        assert busy.wait(2)
        other = {"worker": "different-process", "lease": "different-lease", "heartbeat": "2099-01-01T00:00:00Z"}
        with hub.db.tx() as c:
            c.execute("UPDATE app_settings SET value=? WHERE key=?", (json.dumps(other), LOCK_KEY))
        assert stopped.wait(2)
        assert runner._stop.is_set() and not runner.has_lock
        assert runner._renew_lock() is False
    finally:
        runner.stop()
    assert json.loads(hub.db.one("SELECT value FROM app_settings WHERE key=?", (LOCK_KEY,))["value"]) == other
    runner._release()
    assert hub.db.one("SELECT 1 FROM app_settings WHERE key=?", (LOCK_KEY,))


def test_missing_or_failed_lease_renewal_never_reacquires(hub, monkeypatch):
    runner = JobRunner(hub, SimpleNamespace())
    assert runner._try_lock()
    with hub.db.tx() as c:
        c.execute("DELETE FROM app_settings WHERE key=?", (LOCK_KEY,))
    assert not runner._keep_lease() and runner._stop.is_set()
    assert not hub.db.one("SELECT 1 FROM app_settings WHERE key=?", (LOCK_KEY,))
    def fail():
        raise RuntimeError("synthetic database unavailable")
    monkeypatch.setattr(runner, "_renew_lock", fail)
    assert not runner._keep_lease()


def test_fresh_legacy_lease_is_not_shared_even_with_same_process_id(hub):
    runner = JobRunner(hub, SimpleNamespace())
    legacy = {"worker": runner_module.WORKER_ID, "heartbeat": "2099-01-01T00:00:00Z"}
    with hub.db.tx() as c:
        c.execute("INSERT INTO app_settings(key,value) VALUES (?,?)", (LOCK_KEY, json.dumps(legacy)))
    assert runner._try_lock() is False
    runner._release()
    assert json.loads(hub.db.one("SELECT value FROM app_settings WHERE key=?", (LOCK_KEY,))["value"]) == legacy


def test_resume_checks_and_queue_transition_share_transaction(hub, account, monkeypatch):
    first = job_for(hub, account, "failed")
    second = job_for(hub, account, "failed")
    hub.jobs.add_task(first["id"], "test", "download")
    task = hub.jobs.next_task(first["id"])
    hub.jobs.finish_task(task["id"], "failed", "synthetic")
    set_state = hub.jobs.set_state
    def checked_set_state(job_id, state, **kwargs):
        if state == "queued":
            assert hub.db.conn.in_transaction
        return set_state(job_id, state, **kwargs)
    monkeypatch.setattr(hub.jobs, "set_state", checked_set_state)
    barrier = threading.Barrier(3)
    results, errors = [], []
    def resume(job_id):
        barrier.wait()
        try:
            results.append(hub.jobs.resume(job_id, account["id"]))
        except Exception as error:
            errors.append(error)
    workers = [threading.Thread(target=resume, args=(job["id"],)) for job in (first, second)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert len(results) == 1 and len(errors) == 1 and isinstance(errors[0], Conflict)
    assert hub.db.one("SELECT COUNT(*) n FROM jobs WHERE account_id=? AND state='queued'", (account["id"],))["n"] == 1
    # Rejected resume cannot reset that job's failed tasks.
    if results[0]["id"] == second["id"]:
        assert hub.jobs.tasks(first["id"])[0]["state"] == "failed"


@pytest.mark.parametrize("kind,status,expected", [("assignments", "updated", None), ("pages", "gaps", None),
                                                  ("download", "updated", "updated"),
                                                  ("process_document", "gaps", "gaps")])
def test_resume_resets_core_notice_only_when_core_work_is_pending(hub, account, kind, status, expected):
    job = job_for(hub, account, "failed")
    hub.jobs.update_stats(job["id"], core_status=status)
    hub.jobs.add_task(job["id"], "retry", kind)
    task = hub.jobs.next_task(job["id"])
    hub.jobs.finish_task(task["id"], "failed", "synthetic")
    resumed = hub.jobs.resume(job["id"])
    assert resumed["state"] == "queued" and resumed["stats"]["core_status"] == expected
    assert hub.jobs.tasks(job["id"])[0]["state"] == "pending"
