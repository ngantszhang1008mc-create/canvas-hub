"""Local deadline archives and saved views, shared by the UI and assistant."""
from __future__ import annotations

import json
import uuid

from ..util import InvalidInput, NotFound, now_iso
from .settings import AccountSettings


def deadline_key_sql(alias="i", schema=""):
    # Assignment, classic quiz and calendar copies share one archive state,
    # including copies first discovered after the user archived the deadline.
    ref = (f"(SELECT CAST(json_extract(df.value_json, '$') AS TEXT) FROM {schema}item_fields df "
           f"WHERE df.item_id={alias}.id AND df.field='assignment_id')")
    return (f"CASE WHEN {alias}.item_type='assignment' THEN 'assignment:' || COALESCE({alias}.canvas_id,{alias}.source_key) "
            f"WHEN {alias}.item_type='submission' THEN 'assignment:' || {alias}.source_key "
            f"WHEN {alias}.item_type IN ('quiz','calendar_event') AND {ref} IS NOT NULL "
            f"THEN 'assignment:' || {ref} ELSE {alias}.item_type || ':' || COALESCE({alias}.canvas_id,{alias}.source_key) END")


def archived_sql(alias="i", schema=""):
    return (f"EXISTS (SELECT 1 FROM {schema}deadline_archives da WHERE da.account_id={alias}.account_id "
            f"AND da.deadline_key=({deadline_key_sql(alias, schema)}))")


DEFAULT_TABS = [{"id": key, "name": name, "filters": {"range": key, "include_calendar": True,
                "course_id": "", "item_type": "", "status": "all", "archive": "active"}}
                for key, name in [("7", "7 days"), ("14", "14 days"), ("30", "30 days"),
                                  ("120", "Term"), ("past", "Past 30 days")]]
FILTER_DEFAULTS = DEFAULT_TABS[1]["filters"]


class DeadlineService:
    def __init__(self, db):
        self.db = db
        self.settings = AccountSettings(db)

    def archived_ids(self, account_id):
        return {r["id"] for r in self.db.query(
            f"SELECT i.id FROM items i WHERE i.account_id=? AND {archived_sql()}", (account_id,))}

    def set_archived(self, account_id, item_id, archived):
        if not isinstance(archived, bool):
            raise InvalidInput("archived must be true or false")
        row = self.db.one(f"SELECT i.*, ({deadline_key_sql()}) AS deadline_key FROM items i "
                          "WHERE i.account_id=? AND i.id=?", (account_id, item_id))
        if not row:
            raise NotFound("deadline not found", code="item_not_found")
        if row["item_type"] not in ("assignment", "quiz", "calendar_event"):
            raise InvalidInput("only assignment, quiz and calendar deadlines can be archived")
        with self.db.tx() as c:
            if archived:
                c.execute("INSERT INTO deadline_archives VALUES (?,?,?) ON CONFLICT DO NOTHING",
                          (account_id, row["deadline_key"], now_iso()))
            else:
                c.execute("DELETE FROM deadline_archives WHERE account_id=? AND deadline_key=?",
                          (account_id, row["deadline_key"]))
        return {"item_id": item_id, "archived": archived, "deadline_key": row["deadline_key"]}

    def archive_list(self, account_id):
        rows = self.db.query(f"""SELECT i.id AS item_id, i.item_type, i.title, i.course_canvas_id AS course_id,
            i.source_url, ({deadline_key_sql()}) AS deadline_key,
            (SELECT value_json FROM item_fields f WHERE f.item_id=i.id AND
             f.field=CASE WHEN i.item_type='calendar_event' THEN 'start_at' ELSE 'due_at' END) AS date_json,
            (SELECT title FROM items c WHERE c.account_id=i.account_id AND c.item_type='course'
             AND c.canvas_id=i.course_canvas_id) AS course_title
            FROM items i WHERE i.account_id=? AND i.item_type IN ('assignment','quiz','calendar_event')
            AND {archived_sql()} ORDER BY i.item_type, i.id""", (account_id,))
        # Prefer the assignment to its quiz/calendar mirror, with no date window.
        entries = {}
        for r in rows:
            d = dict(r)
            raw = d.pop("date_json")
            d["date"] = json.loads(raw) if raw else None
            entries.setdefault(d["deadline_key"], d)
        return {"deadlines": list(entries.values())}

    def views(self, account_id):
        value = self.settings.get(account_id, "deadline_views")
        return {"tabs": value.get("tabs", json.loads(json.dumps(DEFAULT_TABS))),
                "active_tab": value.get("active_tab", "14")}

    def save_tab(self, account_id, data):
        if not isinstance(data, dict) or set(data) - {"id", "name", "filters"}:
            raise InvalidInput("give id, name and filters")
        with self.db.tx():
            views = self.views(account_id)
            tabs = views["tabs"]
            tab_id = data.get("id")
            old = next((t for t in tabs if t["id"] == tab_id), None)
            if tab_id is not None and old is None:
                raise NotFound("deadline tab not found")
            if old is None and len(tabs) >= 20:
                raise InvalidInput("at most 20 deadline tabs")
            name = data.get("name", old["name"] if old else "New view")
            if not isinstance(name, str) or not name.strip() or len(name) > 60:
                raise InvalidInput("tab name must have 1–60 characters")
            supplied = data.get("filters", {})
            if not isinstance(supplied, dict) or set(supplied) - set(FILTER_DEFAULTS):
                raise InvalidInput("unknown deadline filter")
            filters = {**FILTER_DEFAULTS, **(old["filters"] if old else {}), **supplied}
            enums = {"range": ("7", "14", "30", "120", "365", "past", "all"),
                     "item_type": ("", "assignment", "quiz", "calendar_event"),
                     "status": ("all", "unfinished", "completed"), "archive": ("active", "all", "archived")}
            if any(filters[k] not in opts for k, opts in enums.items()):
                raise InvalidInput("invalid deadline filter value")
            if not isinstance(filters["include_calendar"], bool) or not isinstance(filters["course_id"], str):
                raise InvalidInput("invalid calendar or course filter")
            if filters["course_id"] and not self.db.one("SELECT 1 FROM items WHERE account_id=? AND item_type='course' "
                                                       "AND canvas_id=?", (account_id, filters["course_id"])):
                raise InvalidInput("course is not collected for this account")
            tab = {"id": tab_id or uuid.uuid4().hex[:12], "name": name.strip(), "filters": filters}
            tabs = [tab if t["id"] == tab["id"] else t for t in tabs] if old else [*tabs, tab]
            return self.settings.set(account_id, "deadline_views", {"tabs": tabs, "active_tab": tab["id"]})

    def select_tab(self, account_id, tab_id):
        with self.db.tx():
            if not any(t["id"] == tab_id for t in self.views(account_id)["tabs"]):
                raise NotFound("deadline tab not found")
            return self.settings.set(account_id, "deadline_views", {"active_tab": tab_id})

    def delete_tab(self, account_id, tab_id):
        with self.db.tx():
            views = self.views(account_id)
            tabs = [t for t in views["tabs"] if t["id"] != tab_id]
            if len(tabs) == len(views["tabs"]):
                raise NotFound("deadline tab not found")
            if not tabs:
                raise InvalidInput("keep at least one deadline tab")
            return self.settings.set(account_id, "deadline_views", {"tabs": tabs,
                "active_tab": views["active_tab"] if views["active_tab"] != tab_id else tabs[0]["id"]})
