"""The change bell: important changes since the user last looked.

Built on the change log (``change_events``). Only changes a student acts on
are surfaced: new announcements, assignments, quizzes, discussions and events;
edited due/lock dates, points and descriptions; newly posted grades; items
that disappeared from Canvas. The first baseline is never notified.
"""

from __future__ import annotations

import json

from .. import dates
from ..db import Database
from .settings import AccountSettings
from .deadlines import archived_sql

NEW_TYPES = ("announcement", "assignment", "quiz", "discussion", "calendar_event", "page", "file")
WATCHED_FIELDS = {
    "assignment": {"due_at", "lock_at", "unlock_at", "points_possible", "description", "submission_types"},
    "quiz": {"due_at", "lock_at", "unlock_at", "points_possible"},
    "announcement": {"message"},
    "discussion": {"message"},
    "calendar_event": {"start_at", "end_at", "location_name"},
    "submission": {"score", "grade", "workflow_state", "excused", "missing"},
}
LABEL = {"due_at": "due date", "lock_at": "close date", "unlock_at": "open date", "points_possible": "points",
         "description": "instructions", "message": "text", "score": "score", "grade": "grade",
         "workflow_state": "status", "start_at": "time", "end_at": "end time", "location_name": "place",
         "submission_types": "submission type", "excused": "excused", "missing": "missing flag"}


def _plain(v, tz: str | None = None):
    if isinstance(v, dict):
        return (dates.format_local(v, tz) if v.get("iso") else None) or v.get("raw") or json.dumps(v)[:80]
    return None if v is None else str(v)[:80]


class NotificationService:
    def __init__(self, db: Database, settings: AccountSettings):
        self.db = db
        self.settings = settings

    def _important(self, r, tz: str | None = None) -> dict | None:
        t = r["item_type"]
        if r["kind"] == "first_observed" and t in NEW_TYPES:
            return {"headline": f"New {t.replace('_', ' ')}", "detail": None}
        if r["kind"] == "field_changed" and r["field"] in WATCHED_FIELDS.get(t, ()):
            old, new = json.loads(r["old_json"]) if r["old_json"] else None, json.loads(r["new_json"]) if r["new_json"] else None
            if t == "submission" and r["field"] in ("score", "grade") and new is not None:
                return {"headline": "Grade posted" if old is None else "Grade changed", "detail": f"{_plain(old, tz) or '—'} → {_plain(new, tz)}"}
            if r["field"] in ("description", "message"):
                return {"headline": f"{t.title()} edited", "detail": "the text changed"}
            return {"headline": f"{LABEL.get(r['field'], r['field']).capitalize()} changed",
                    "detail": f"{_plain(old, tz) or '—'} → {_plain(new, tz) or '—'}"}
        if r["kind"] == "field_added" and t == "submission" and r["field"] in ("score", "grade"):
            return {"headline": "Grade posted", "detail": _plain(json.loads(r["new_json"])) if r["new_json"] else None}
        if r["kind"] in ("not_in_latest_index", "source_unavailable") and t in NEW_TYPES:
            return {"headline": "No longer listed in Canvas", "detail": None}
        return None

    def feed(self, account_id: int, limit: int = 30) -> dict:
        seen = int(self.settings.get(account_id, "notifications").get("last_seen_change_id") or 0)
        rows = self.db.query(
            f"""SELECT e.id, e.kind, e.field, e.old_json, e.new_json, e.observed_at, e.recorded_at, e.item_id,
                      i.item_type, i.title, i.course_canvas_id, i.source_url,
                      (SELECT title FROM items c WHERE c.account_id=e.account_id AND c.item_type='course'
                        AND c.canvas_id=i.course_canvas_id) AS course_title
               FROM change_events e JOIN items i ON i.id=e.item_id
               WHERE e.account_id=? AND e.baseline=0 AND NOT {archived_sql()} ORDER BY e.id DESC LIMIT 600""", (account_id,))
        acct = self.db.one("SELECT time_zone FROM accounts WHERE id=?", (account_id,))
        tz = acct["time_zone"] if acct else None
        mirrors = {r["item_id"] for r in self.db.query(
            """SELECT f.item_id FROM item_fields f JOIN items i ON i.id=f.item_id WHERE i.account_id=?
               AND i.item_type='calendar_event' AND f.field IN ('assignment_ref','assignment_id') AND f.value_json NOT IN ('null','')""",
            (account_id,))}
        items, unread, last_key = [], 0, None
        for r in rows:
            if r["item_id"] in mirrors:
                continue            # Canvas's calendar copy of an assignment; the assignment itself is reported
            imp = self._important(r, tz)
            if not imp:
                continue
            key = (r["item_id"], r["recorded_at"][:16])
            if key == last_key and items:      # several fields of one item changed together: one entry
                if imp["headline"] not in items[-1]["headline"]:
                    items[-1]["headline"] += f" · {imp['headline'].lower()}"
                continue
            last_key = key
            new = r["id"] > seen
            unread += 1 if new else 0
            if len(items) < limit:
                title = r["title"]
                if r["item_type"] == "submission":
                    a = self.db.one("""SELECT title FROM items WHERE account_id=? AND item_type='assignment' AND canvas_id=
                                       (SELECT source_key FROM items WHERE id=?)""", (account_id, r["item_id"]))
                    title = a["title"] if a else title
                items.append({"change_id": r["id"], "item_id": r["item_id"], "item_type": r["item_type"], "title": title,
                              "course": r["course_title"], "headline": imp["headline"], "detail": imp["detail"],
                              "when": r["recorded_at"], "unread": new, "source_url": r["source_url"]})
        top = rows[0]["id"] if rows else seen
        return {"unread": unread, "items": items, "last_seen_change_id": seen, "latest_change_id": max(top, seen)}

    def mark_seen(self, account_id: int, up_to: int | None = None) -> dict:
        if up_to is None:
            r = self.db.one("SELECT MAX(id) AS m FROM change_events WHERE account_id=?", (account_id,))
            up_to = r["m"] or 0
        cur = int(self.settings.get(account_id, "notifications").get("last_seen_change_id") or 0)
        self.settings.set(account_id, "notifications", {"last_seen_change_id": max(cur, int(up_to))})
        return self.feed(account_id)
