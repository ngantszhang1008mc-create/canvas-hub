"""Reminders for to-dos and Canvas items, shown by the app while it runs."""

from __future__ import annotations

from datetime import datetime, timezone

from ..db import Database
from ..util import InvalidInput, NotFound, now_iso, parse_iso, to_utc_iso


class ReminderService:
    def __init__(self, db: Database):
        self.db = db

    def create(self, account_id: int, *, title: str, remind_at: str, item_id: int | None = None,
               annotation_id: int | None = None, note: str | None = None, created_by: str = "user") -> dict:
        when = parse_iso(remind_at or "")
        if not when:
            raise InvalidInput("remind_at must be an ISO-8601 time with an offset")
        if not (title or "").strip():
            raise InvalidInput("a reminder needs a title")
        for table, ref in (("items", item_id), ("annotations", annotation_id)):
            if ref is not None and not self.db.one(f"SELECT 1 FROM {table} WHERE id=? AND account_id=?", (int(ref), account_id)):
                raise NotFound(f"{table[:-1]} {ref} not found")
        with self.db.tx() as c:
            rid = c.execute("""INSERT INTO reminders(account_id, title, note, remind_at, item_id, annotation_id, created_by,
                               created_at) VALUES (?,?,?,?,?,?,?,?)""",
                            (account_id, title.strip()[:300], (note or "")[:1000] or None, to_utc_iso(when), item_id,
                             annotation_id, created_by, now_iso())).lastrowid
        return self.get(account_id, rid)

    def get(self, account_id: int, rid: int) -> dict:
        r = self.db.one("SELECT * FROM reminders WHERE id=? AND account_id=?", (int(rid), account_id))
        if not r:
            raise NotFound("reminder not found", code="reminder_not_found")
        return dict(r)

    def list(self, account_id: int, status: str | None = "pending") -> list[dict]:
        sql, p = "SELECT * FROM reminders WHERE account_id=?", [account_id]
        if status:
            sql += " AND status=?"
            p.append(status)
        return [dict(r) for r in self.db.query(sql + " ORDER BY remind_at LIMIT 300", p)]

    def due(self, account_id: int) -> list[dict]:
        """Pending reminders whose time has come; they are marked fired (shown once)."""
        now = to_utc_iso(datetime.now(timezone.utc))
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM reminders WHERE account_id=? AND status='pending' AND remind_at<=? ORDER BY remind_at", (account_id, now))]
        if rows:
            with self.db.tx() as c:
                c.executemany("UPDATE reminders SET status='fired', fired_at=? WHERE id=?", [(now_iso(), r["id"]) for r in rows])
        return rows

    def dismiss(self, account_id: int, rid: int) -> dict:
        self.get(account_id, rid)
        with self.db.tx() as c:
            c.execute("UPDATE reminders SET status='dismissed' WHERE id=?", (int(rid),))
        return self.get(account_id, rid)
