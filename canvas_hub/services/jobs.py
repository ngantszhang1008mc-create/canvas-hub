"""Collection/import jobs and the resumable traversal queue.

State machine (collect jobs):

    queued -> running -> completed | completed_with_gaps | failed
    running -> needs_login        (session expired; resumable after sign-in)
    running -> cancel_requested -> cancelled
    running -> interrupted        (app stopped/crashed; set on next startup)
    needs_login | paused | interrupted | failed -> queued   (resume)
    queued | needs_login | paused | interrupted -> cancelled

Only one unfinished collect job may exist per account, so workers never
compete for the same account/browser state.
"""

from __future__ import annotations

import json
import os
import socket

from ..db import Database
from ..util import Conflict, InvalidInput, NotFound, clamp_limit, now_iso

ACTIVE_STATES = ("queued", "running", "cancel_requested")
RESUMABLE_STATES = ("needs_login", "paused", "interrupted", "failed")
UNFINISHED_STATES = ACTIVE_STATES + ("needs_login", "paused", "interrupted")
TERMINAL_STATES = ("completed", "completed_with_gaps", "cancelled", "failed")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class JobService:
    def __init__(self, db: Database):
        self.db = db

    def create(self, *, account_id: int | None, instance_id: int, kind: str, mode: str, scope: dict,
               package_id: str | None = None, state: str = "queued", stats: dict | None = None) -> dict:
        now = now_iso()
        with self.db.tx() as c:
            if kind == "collect" and account_id is not None:
                row = c.execute(
                    f"SELECT id, state FROM jobs WHERE account_id=? AND kind='collect' AND state IN ({','.join('?'*len(UNFINISHED_STATES))})",
                    (account_id, *UNFINISHED_STATES),
                ).fetchone()
                if row:
                    raise Conflict(
                        f"Job {row['id']} is still {row['state']}; resume or cancel it before starting another collection",
                        code="job_already_active", detail={"job_id": row["id"], "state": row["state"]},
                    )
            jid = c.execute(
                """INSERT INTO jobs(account_id, instance_id, kind, mode, scope_json, state, stats_json, package_id,
                   created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (account_id, instance_id, kind, mode, json.dumps(scope), state, json.dumps(stats or {}), package_id, now, now),
            ).lastrowid
        return self.get(jid)

    def get(self, job_id: int, account_id: int | None = None) -> dict:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (int(job_id),))
        if not row or (account_id is not None and row["account_id"] != int(account_id)):
            raise NotFound(f"job {job_id} not found", code="job_not_found")
        d = dict(row)
        d["scope"] = json.loads(d.pop("scope_json"))
        d["stats"] = json.loads(d.pop("stats_json"))
        d["tasks"] = {r["state"]: r["n"] for r in self.db.query(
            "SELECT state, COUNT(*) n FROM job_tasks WHERE job_id=? GROUP BY state", (job_id,))}
        d["resumable"] = d["state"] in RESUMABLE_STATES
        d["cancellable"] = d["state"] in ("queued", "running", "needs_login", "paused", "interrupted")
        return d

    def list(self, account_id: int, *, limit: int | None = None, cursor: int | None = None) -> dict:
        lim = clamp_limit(limit, 20, 100)
        params: list = [account_id]
        sql = "SELECT id FROM jobs WHERE account_id=?"
        if cursor:
            sql += " AND id < ?"
            params.append(int(cursor))
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self.db.query(sql, (*params, lim + 1))
        jobs = [self.get(r["id"]) for r in rows[:lim]]
        return {"jobs": jobs, "next_cursor": str(rows[lim - 1]["id"]) if len(rows) > lim else None}

    def by_package(self, account_id: int, package_id: str) -> dict | None:
        row = self.db.one("SELECT id FROM jobs WHERE account_id=? AND package_id=?", (account_id, package_id))
        return self.get(row["id"]) if row else None

    def set_state(self, job_id: int, state: str, *, message: str | None = None, error: str | None = None) -> None:
        now = now_iso()
        with self.db.tx() as c:
            sets = ["state=?", "updated_at=?"]
            params: list = [state, now]
            if message is not None:
                sets.append("message=?")
                params.append(message)
            if error is not None:
                sets.append("error=?")
                params.append(error)
            if state == "running":
                sets += ["started_at=COALESCE(started_at, ?)", "worker_id=?", "heartbeat_at=?"]
                params += [now, WORKER_ID, now]
            if state in TERMINAL_STATES:
                sets.append("finished_at=?")
                params.append(now)
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*params, job_id))

    def update_stats(self, job_id: int, **delta) -> None:
        with self.db.tx() as c:
            row = c.execute("SELECT stats_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            stats = json.loads(row["stats_json"])
            for k, v in delta.items():
                if isinstance(v, (int, float)) and isinstance(stats.get(k, 0), (int, float)):
                    stats[k] = stats.get(k, 0) + v
                else:
                    stats[k] = v
            c.execute("UPDATE jobs SET stats_json=?, heartbeat_at=?, updated_at=? WHERE id=?",
                      (json.dumps(stats), now_iso(), now_iso(), job_id))

    def heartbeat(self, job_id: int) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now_iso(), job_id))

    def request_cancel(self, job_id: int, account_id: int | None = None) -> dict:
        job = self.get(job_id, account_id)
        if job["state"] == "running":
            self.set_state(job_id, "cancel_requested", message="Cancellation requested; stopping after the current step")
        elif job["state"] in ("queued", "needs_login", "paused", "interrupted"):
            self.set_state(job_id, "cancelled", message="Cancelled before completion; verified observations were kept")
        elif job["state"] == "cancel_requested":
            pass
        else:
            raise InvalidInput(f"job is {job['state']} and cannot be cancelled", code="job_not_cancellable")
        return self.get(job_id)

    def resume(self, job_id: int, account_id: int | None = None) -> dict:
        job = self.get(job_id, account_id)
        if job["state"] not in RESUMABLE_STATES:
            raise InvalidInput(f"job is {job['state']} and cannot be resumed", code="job_not_resumable")
        with self.db.tx() as c:
            # Failed tasks get another bounded attempt on explicit resume.
            c.execute("UPDATE job_tasks SET state='pending', updated_at=? WHERE job_id=? AND state IN ('failed','blocked') AND attempts < 3",
                      (now_iso(), job_id))
        self.set_state(job_id, "queued", message="Resumed; continuing from the saved checkpoint")
        return self.get(job_id)

    def recover_after_restart(self) -> list[int]:
        """Mark jobs that were running when the app stopped as interrupted."""
        rows = self.db.query("SELECT id FROM jobs WHERE state IN ('running','cancel_requested')")
        for r in rows:
            self.set_state(r["id"], "interrupted",
                           message="The app stopped while this job was running. Verified results were kept; resume to continue.")
        return [r["id"] for r in rows]

    def next_queued(self) -> dict | None:
        row = self.db.one("SELECT id FROM jobs WHERE state='queued' AND kind='collect' ORDER BY id LIMIT 1")
        return self.get(row["id"]) if row else None

    # ---- traversal queue ------------------------------------------------
    def add_task(self, job_id: int, key: str, kind: str, *, course_id: str | None = None,
                 category: str | None = None, payload: dict | None = None) -> bool:
        now = now_iso()
        with self.db.tx() as c:
            cur = c.execute(
                """INSERT INTO job_tasks(job_id, task_key, kind, course_canvas_id, category, payload_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id, task_key) DO NOTHING""",
                (job_id, key, kind, course_id, category, json.dumps(payload or {}), now, now),
            )
            return cur.rowcount > 0

    def next_task(self, job_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM job_tasks WHERE job_id=? AND state='pending' ORDER BY id LIMIT 1", (job_id,))
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def finish_task(self, task_id: int, state: str, error: str | None = None) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE job_tasks SET state=?, last_error=?, attempts=attempts+1, updated_at=? WHERE id=?",
                      (state, error, now_iso(), task_id))

    def defer_task(self, task_id: int, error: str) -> None:
        """Leave the task pending (e.g. login expired mid-task) without
        consuming an attempt."""
        with self.db.tx() as c:
            c.execute("UPDATE job_tasks SET last_error=?, updated_at=? WHERE id=?", (error, now_iso(), task_id))

    def tasks(self, job_id: int, *, state: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT id, task_key, kind, course_canvas_id, category, state, attempts, last_error, updated_at FROM job_tasks WHERE job_id=?"
        params: list = [job_id]
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY id LIMIT ?"
        return [dict(r) for r in self.db.query(sql, (*params, limit))]
