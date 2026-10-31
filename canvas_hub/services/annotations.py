"""Local notes and personal tasks.

Stored separately from Canvas-reported fields; collection and import never
modify them. A local task marked done is only a personal marker - it is
never presented as, or turned into, a Canvas submission.
"""

from __future__ import annotations

from ..db import Database
from ..util import InvalidInput, NotFound, now_iso, parse_iso
from .deadlines import archived_sql

_KINDS = ("note", "task")
_TASK_STATES = ("open", "in_progress", "blocked", "done")


class AnnotationService:
    def __init__(self, db: Database):
        self.db = db

    def _check_item(self, account_id: int, item_id) -> dict | None:
        if item_id is None:
            return None
        row = self.db.one("SELECT id, item_type, source_key, course_canvas_id FROM items WHERE id=? AND account_id=?",
                          (int(item_id), account_id))
        if not row:
            raise NotFound("item not found for this account", code="item_not_found")
        return dict(row)

    def create(self, account_id: int, *, kind: str, body: str = "", title: str | None = None, item_id=None,
               course_id: str | None = None, local_due: str | None = None, task_state: str | None = None) -> dict:
        if kind not in _KINDS:
            raise InvalidInput("kind must be 'note' or 'task'")
        if local_due and not parse_iso(local_due):
            raise InvalidInput("local_due must be an ISO-8601 timestamp with an offset")
        if len(body or "") > 100_000 or len(title or "") > 500:
            raise InvalidInput("annotation too long")
        item = self._check_item(account_id, item_id)
        state = (task_state or "open") if kind == "task" else None
        if state and state not in _TASK_STATES:
            raise InvalidInput("task_state must be open, in_progress, blocked or done")
        now = now_iso()
        with self.db.tx() as c:
            aid = c.execute(
                """INSERT INTO annotations(account_id, item_id, item_type, item_source_key, course_canvas_id, kind, title,
                   body, task_state, local_due, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, item["id"] if item else None, item["item_type"] if item else None,
                 item["source_key"] if item else None, (item["course_canvas_id"] if item else None) or course_id,
                 kind, title, body or "", state, local_due, now, now),
            ).lastrowid
        return self.get(account_id, aid)

    def get(self, account_id: int, annotation_id: int) -> dict:
        row = self.db.one("SELECT * FROM annotations WHERE id=? AND account_id=?", (int(annotation_id), account_id))
        if not row:
            raise NotFound("annotation not found", code="annotation_not_found")
        return dict(row)

    def update(self, account_id: int, annotation_id: int, **changes) -> dict:
        cur = self.get(account_id, annotation_id)
        allowed = {"title", "body", "task_state", "local_due"}
        bad = set(changes) - allowed
        if bad:
            raise InvalidInput(f"cannot update {sorted(bad)}")
        if "task_state" in changes:
            if cur["kind"] != "task" or changes["task_state"] not in _TASK_STATES:
                raise InvalidInput("task_state applies to tasks and must be open, in_progress, blocked or done")
        if changes.get("local_due") and not parse_iso(changes["local_due"]):
            raise InvalidInput("local_due must be an ISO-8601 timestamp with an offset")
        if not changes:
            return cur
        sets = ", ".join(f"{k}=?" for k in changes)
        with self.db.tx() as c:
            c.execute(f"UPDATE annotations SET {sets}, updated_at=? WHERE id=? AND account_id=?",
                      (*changes.values(), now_iso(), cur["id"], account_id))
        return self.get(account_id, annotation_id)

    def delete(self, account_id: int, annotation_id: int) -> None:
        self.get(account_id, annotation_id)
        with self.db.tx() as c:
            c.execute("DELETE FROM annotations WHERE id=? AND account_id=?", (int(annotation_id), account_id))

    def list(self, account_id: int, *, item_id=None, course_id: str | None = None, kind: str | None = None,
             task_state: str | None = None) -> list[dict]:
        where, params = ["a.account_id=?"], [account_id]
        if item_id is None:
            where.append("(a.kind!='task' OR NOT " + archived_sql() + ")")
        if item_id is not None:
            where.append("a.item_id=?")
            params.append(int(item_id))
        if course_id:
            where.append("a.course_canvas_id=?")
            params.append(str(course_id))
        if kind:
            where.append("a.kind=?")
            params.append(kind)
        if task_state == "active":                 # anything not done yet
            where.append("a.task_state!='done'")
        elif task_state:
            where.append("a.task_state=?")
            params.append(task_state)
        return [dict(r) for r in self.db.query(
            f"""SELECT a.*, i.title AS item_title FROM annotations a LEFT JOIN items i ON i.id=a.item_id
                WHERE {' AND '.join(where)} ORDER BY a.task_state='done', COALESCE(a.local_due,'9999'), a.updated_at DESC""",
            params)]
