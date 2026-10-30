"""Read-side services: courses, items, history, deadlines, changes, coverage.

All methods require an account id and never return another account's rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import dates
from ..db import Database
from ..htmltools import absolutize
from ..model import CATEGORIES, ITEM_TYPES, spec_for
from ..util import InvalidInput, NotFound, clamp_limit, decode_cursor, encode_cursor, now_iso, parse_iso, to_utc_iso


def _tz(name: str | None, fallback: str | None) -> tuple[ZoneInfo, str]:
    tzname = name or fallback or "UTC"
    try:
        return ZoneInfo(tzname), tzname
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise InvalidInput(f"unknown time zone {tzname!r}; use an IANA name such as Asia/Hong_Kong", code="invalid_time_zone") from e


def _local(value, tz: ZoneInfo) -> str | None:
    dt = dates.instant(value)
    return dt.astimezone(tz).isoformat() if dt else None


class QueryService:
    def __init__(self, db: Database):
        self.db = db

    # ---- helpers --------------------------------------------------------
    def _fields(self, item_id: int) -> dict:
        return {r["field"]: json.loads(r["value_json"]) for r in
                self.db.query("SELECT field, value_json FROM item_fields WHERE item_id=?", (item_id,))}

    def _field_rows(self, item_id: int) -> list[dict]:
        out = []
        for r in self.db.query("SELECT * FROM item_fields WHERE item_id=? ORDER BY field", (item_id,)):
            d = dict(r)
            d["value"] = json.loads(d.pop("value_json"))
            d.pop("compare_hash", None)
            d["volatile"] = None  # filled below
            out.append(d)
        return out

    def _item_row(self, account_id: int, item_id: int) -> dict:
        row = self.db.one("SELECT * FROM items WHERE id=? AND account_id=?", (int(item_id), account_id))
        if not row:
            raise NotFound(f"item {item_id} not found", code="item_not_found")
        return dict(row)

    def _course_titles(self, account_id: int) -> dict[str, str]:
        return {r["source_key"]: r["title"] for r in self.db.query(
            "SELECT source_key, title FROM items WHERE account_id=? AND item_type='course'", (account_id,))}

    # ---- courses ---------------------------------------------------------
    def list_courses(self, account_id: int) -> list[dict]:
        out = []
        for r in self.db.query(
            "SELECT * FROM items WHERE account_id=? AND item_type='course' AND current_revision>0 ORDER BY title", (account_id,)
        ):
            f = self._fields(r["id"])
            counts = {x["item_type"]: x["n"] for x in self.db.query(
                """SELECT item_type, COUNT(*) n FROM items WHERE account_id=? AND course_canvas_id=? AND current_revision>0
                   GROUP BY item_type""", (account_id, r["source_key"]))}
            out.append({
                "item_id": r["id"], "course_id": r["source_key"], "title": r["title"],
                "course_code": f.get("course_code"), "term": f.get("term"), "enrollment_role": f.get("enrollment_role"),
                "listing_section": f.get("listing_section"), "published": f.get("published"),
                "access": f.get("access"), "navigation": f.get("navigation"), "source_url": r["source_url"],
                "presence": r["presence"], "first_observed_at": r["first_observed_at"], "last_observed_at": r["last_observed_at"],
                "item_counts": counts,
            })
        self._annotate_shells(account_id, out)
        return out

    def _annotate_shells(self, account_id: int, courses: list[dict]) -> None:
        """content_state tells empty, hidden and inaccessible apart; possible
        duplicates are shells with the same course code (the user decides)."""
        for c in courses:
            cov = {}
            for r in self.db.query("""SELECT category, status, collected_count FROM coverage WHERE account_id=?
                                      AND course_canvas_id=? ORDER BY id DESC""", (account_id, c["course_id"])):
                cov.setdefault(r["category"], dict(r))
            cats = {k: v for k, v in cov.items() if k not in ("courses", "home")}
            content = sum(n for t, n in c["item_counts"].items() if t not in ("course", "syllabus"))
            if c["access"] not in (None, "accessible"):
                state, why = "inaccessible", "Canvas does not let you open this course (unpublished, concluded or not available)."
            elif not cov:
                state, why = "not_checked", "Not collected yet."
            elif cats and all(v["status"] == "not_exposed" for v in cats.values()):
                state, why = "hidden", "The course menu shows none of the collected sections; content may exist but is hidden from students."
            elif content == 0 and all(v["status"] in ("complete", "not_exposed") for v in cats.values()):
                state, why = "empty", "Sections are shown but contain nothing yet."
            else:
                state, why = "has_content", None
            c["content_state"] = state
            c["content_state_reason"] = why
            c["blocked_sections"] = sorted(k for k, v in cats.items() if v["status"] == "blocked")
        links = {(r["course_canvas_id"], r["other_canvas_id"]): r["relation"] for r in self.db.query(
            "SELECT course_canvas_id, other_canvas_id, relation FROM course_links WHERE account_id=?", (account_id,))}

        def key(c):
            code = (c.get("course_code") or "").strip().upper()
            return code or None
        for c in courses:
            k = key(c)
            same, maybe = [], []
            for o in courses:
                if o is c:
                    continue
                rel = links.get((c["course_id"], o["course_id"])) or links.get((o["course_id"], c["course_id"]))
                if rel == "same":
                    same.append(o["course_id"])
                elif rel != "different" and k and key(o) == k:
                    maybe.append(o["course_id"])
            c["linked_shells"] = same
            c["possible_duplicates"] = maybe

    def link_courses(self, account_id: int, course_id: str, other_id: str, relation: str) -> None:
        if relation not in ("same", "different", "none"):
            raise InvalidInput("relation must be same, different or none")
        a, b = sorted((str(course_id), str(other_id)))
        if a == b:
            raise InvalidInput("pick two different courses")
        known = {r["source_key"] for r in self.db.query(
            "SELECT source_key FROM items WHERE account_id=? AND item_type='course' AND source_key IN (?,?)", (account_id, a, b))}
        if known != {a, b}:
            raise NotFound("both courses must be among the collected courses", code="course_not_found")
        with self.db.tx() as c:
            c.execute("DELETE FROM course_links WHERE account_id=? AND course_canvas_id=? AND other_canvas_id=?", (account_id, a, b))
            if relation != "none":
                c.execute("INSERT INTO course_links(account_id, course_canvas_id, other_canvas_id, relation, created_at) "
                          "VALUES (?,?,?,?,?)", (account_id, a, b, relation, now_iso()))

    # ---- items -----------------------------------------------------------
    def list_items(self, account_id: int, *, course_id: str | None = None, item_type: str | None = None,
                   presence: str | None = None, limit: int | None = None, cursor: str | None = None,
                   include_stubs: bool = False) -> dict:
        lim = clamp_limit(limit, 50, 200)
        if item_type and item_type not in ITEM_TYPES:
            raise InvalidInput(f"unknown item type {item_type!r}")
        where, params = ["account_id=?"], [account_id]
        if course_id:
            where.append("course_canvas_id=?")
            params.append(str(course_id))
        if item_type:
            where.append("item_type=?")
            params.append(item_type)
        if presence:
            where.append("presence=?")
            params.append(presence)
        if not include_stubs:
            where.append("current_revision>0")
        cur = decode_cursor(cursor)
        if cur:
            where.append("(COALESCE(title,''), id) > (?, ?)")
            params += [cur["t"], cur["id"]]
        rows = self.db.query(
            f"""SELECT id, item_type, course_canvas_id, canvas_id, title, source_url, presence, first_observed_at,
                last_observed_at, current_revision FROM items WHERE {' AND '.join(where)}
                ORDER BY COALESCE(title,''), id LIMIT ?""", (*params, lim + 1))
        items = [dict(r) for r in rows[:lim]]
        nxt = encode_cursor({"t": items[-1]["title"] or "", "id": items[-1]["id"]}) if len(rows) > lim else None
        return {"items": items, "next_cursor": nxt}

    def get_item(self, account_id: int, item_id: int) -> dict:
        item = self._item_row(account_id, item_id)
        acct = self.db.one("SELECT time_zone FROM accounts WHERE id=?", (account_id,))
        tzname = acct["time_zone"] if acct else None
        fields = self._field_rows(item["id"])
        for f in fields:
            spec = spec_for(item["item_type"], f["field"])
            f["volatile"] = spec.volatile
            f["kind"] = spec.kind
            if spec.kind == "date" and f["value"] is not None:
                f["display"] = dates.format_local(f["value"], tzname)
        origin = self.db.one("SELECT i.origin FROM accounts a JOIN instances i ON i.id=a.instance_id WHERE a.id=?",
                             (account_id,))["origin"]
        for f in fields:
            if f["kind"] == "html" and isinstance(f["value"], str):
                f["value"] = absolutize(f["value"], origin)
        item["fields"] = fields
        item["links_out"] = [dict(r) for r in self.db.query(
            """SELECT l.relation, l.label, l.position, l.attrs_json, l.last_observed_at, i.id AS item_id, i.item_type, i.title,
               i.current_revision>0 AS collected FROM item_links l JOIN items i ON i.id=l.to_item_id
               WHERE l.from_item_id=? ORDER BY l.relation, COALESCE(l.position, 999999), l.id""", (item["id"],))]
        item["links_in"] = [dict(r) for r in self.db.query(
            """SELECT l.relation, l.label, l.position, l.last_observed_at, i.id AS item_id, i.item_type, i.title
               FROM item_links l JOIN items i ON i.id=l.from_item_id WHERE l.to_item_id=? ORDER BY l.relation, l.id""",
            (item["id"],))]
        for ln in item["links_out"]:
            ln["attrs"] = json.loads(ln.pop("attrs_json") or "{}")
            ln["collected"] = bool(ln["collected"])
        item["resources"] = [dict(r) for r in self.db.query(
            """SELECT ir.resource_id, ir.filename, ir.revision, ir.observed_at, r.size, r.content_type,
               r.extraction_status, r.extraction_detail, r.extracted_chars, r.ocr_confidence
               FROM item_resources ir JOIN resources r ON r.id=ir.resource_id WHERE ir.item_id=? ORDER BY ir.observed_at DESC""",
            (item["id"],))]
        item["discrepancies"] = [
            {**dict(r), "detail": json.loads(r["detail_json"])} for r in self.db.query(
                """SELECT d.id, d.kind, d.detail_json, d.first_detected_at, d.last_detected_at, d.item_id, d.other_item_id,
                   a.title AS item_title, b.title AS other_title FROM discrepancies d
                   JOIN items a ON a.id=d.item_id JOIN items b ON b.id=d.other_item_id
                   WHERE d.account_id=? AND (d.item_id=? OR d.other_item_id=?)""", (account_id, item["id"], item["id"]))]
        for d in item["discrepancies"]:
            d.pop("detail_json", None)
        item["annotations"] = [dict(r) for r in self.db.query(
            "SELECT * FROM annotations WHERE account_id=? AND item_id=? ORDER BY id", (account_id, item["id"]))]
        if item["item_type"] == "assignment" and item["canvas_id"]:
            sub = self.db.one("SELECT id FROM items WHERE account_id=? AND item_type='submission' AND source_key=?",
                              (account_id, item["canvas_id"]))
            item["submission_item_id"] = sub["id"] if sub else None
        if item["course_canvas_id"]:
            crs = self.db.one("SELECT title FROM items WHERE account_id=? AND item_type='course' AND source_key=?",
                              (account_id, item["course_canvas_id"]))
            item["course_title"] = crs["title"] if crs else None
        item["freshness"] = self._freshness(fields, item)
        item["time_zone"] = tzname
        return item

    def _freshness(self, fields: list[dict], item: dict) -> dict:
        if not fields:
            return {"state": "not_collected", "note": "Only a reference to this item has been seen; its content was not read."}
        times = [f["last_observed_at"] for f in fields if not f.get("volatile")]
        if not times:
            return {"state": "volatile_only"}
        oldest, newest = min(times), max(times)
        return {
            "state": "uniform" if oldest == newest else "mixed",
            "oldest_field_observed_at": oldest,
            "newest_field_observed_at": newest,
            "note": None if oldest == newest else "Fields were read at different times; check each field's observed time.",
        }

    def history(self, account_id: int, item_id: int) -> dict:
        item = self._item_row(account_id, item_id)
        revs = []
        for r in self.db.query("SELECT * FROM item_revisions WHERE item_id=? ORDER BY revision DESC", (item["id"],)):
            d = dict(r)
            d["fields"] = json.loads(d.pop("fields_json"))
            d["changed_fields"] = json.loads(d.pop("changed_fields_json"))
            d["added_fields"] = json.loads(d.pop("added_fields_json"))
            revs.append(d)
        events = [self._event_dict(r) for r in self.db.query(
            "SELECT * FROM change_events WHERE item_id=? AND account_id=? ORDER BY id DESC", (item["id"], account_id))]
        observations = [dict(r) for r in self.db.query(
            "SELECT job_id, observed_at, method, depth, fields_json FROM item_observations WHERE item_id=? ORDER BY observed_at DESC LIMIT 100",
            (item["id"],))]
        for o in observations:
            o["fields"] = json.loads(o.pop("fields_json"))
        return {"item_id": item["id"], "title": item["title"], "item_type": item["item_type"], "revisions": revs,
                "events": events, "observations": observations}

    # ---- deadlines -------------------------------------------------------
    def deadlines(self, account_id: int, *, start: str, end: str, time_zone: str | None = None,
                  course_id: str | None = None, include_calendar: bool = True, limit: int | None = None,
                  cursor: str | None = None) -> dict:
        s, e = parse_iso(start), parse_iso(end)
        if not s or not e:
            raise InvalidInput("start and end must be ISO-8601 timestamps with an offset, e.g. 2026-10-01T00:00:00+08:00")
        if e <= s:
            raise InvalidInput("end must be after start")
        acct = self.db.one("SELECT time_zone FROM accounts WHERE id=?", (account_id,))
        tz, tzname = _tz(time_zone, acct["time_zone"] if acct else None)
        lim = clamp_limit(limit, 100, 500)
        s_iso, e_iso = to_utc_iso(s), to_utc_iso(e)
        courses = self._course_titles(account_id)
        types = ["assignment", "quiz"] + (["calendar_event"] if include_calendar else [])
        rows = self.db.query(
            f"""SELECT i.id, i.item_type, i.title, i.course_canvas_id, i.canvas_id, i.source_url, i.presence,
                f.value_json, f.last_observed_at AS due_observed_at, f.field
                FROM items i JOIN item_fields f ON f.item_id=i.id
                WHERE i.account_id=? AND i.item_type IN ({','.join('?'*len(types))})
                  AND ((i.item_type IN ('assignment','quiz') AND f.field='due_at') OR (i.item_type='calendar_event' AND f.field='start_at'))
                  {'AND i.course_canvas_id=?' if course_id else ''}""",
            (account_id, *types, *([str(course_id)] if course_id else [])),
        )
        dated, unresolved = [], []
        for r in rows:
            val = json.loads(r["value_json"])
            if val is None:
                continue
            inst = dates.instant(val)
            entry = {"item_id": r["id"], "item_type": r["item_type"], "title": r["title"],
                     "course_id": r["course_canvas_id"], "course_title": courses.get(r["course_canvas_id"]),
                     "source_url": r["source_url"], "presence": r["presence"], "date_field": r["field"],
                     "date": val, "date_local": _local(val, tz), "date_observed_at": r["due_observed_at"]}
            if inst is None:
                unresolved.append(entry)
                continue
            iso = to_utc_iso(inst)
            if not (s_iso <= iso < e_iso):
                continue
            entry["sort_key"] = iso
            f = self._fields(r["id"])
            for k in ("unlock_at", "lock_at", "end_at"):
                if k in f:
                    entry[k] = f[k]
                    entry[k + "_local"] = _local(f[k], tz)
            entry["points_possible"] = f.get("points_possible")
            entry["submission_types"] = f.get("submission_types")
            if r["item_type"] == "quiz" and f.get("assignment_id"):
                entry["_assignment_id"] = str(f["assignment_id"])
            if r["item_type"] == "calendar_event":
                entry["location_name"] = f.get("location_name")
                entry["assignment_ref"] = f.get("assignment_id")
            if r["item_type"] in ("assignment", "quiz"):
                entry["submission"] = self._submission(account_id, f.get("assignment_id") if r["item_type"] == "quiz" else r["canvas_id"])
            entry["local_tasks"] = [dict(x) for x in self.db.query(
                "SELECT id, title, task_state, local_due FROM annotations WHERE account_id=? AND item_id=? AND kind='task'",
                (account_id, r["id"]))]
            # Only an undated linked to-do ("Mark done locally") stands for the
            # deadline itself; dated ones (study blocks, "start on Saturday")
            # are steps towards it.
            entry["done_locally"] = any(t["task_state"] == "done" and not t["local_due"] for t in entry["local_tasks"])
            dated.append(entry)
        # A classic quiz is also an assignment; list it once (as the assignment,
        # which carries the submission state).
        listed_assignments = {str(self._canvas_id(d["item_id"])) for d in dated if d["item_type"] == "assignment"}
        dated = [d for d in dated if not (d["item_type"] == "quiz" and d.get("_assignment_id") in listed_assignments)]
        # Calendar entries that merely mirror an assignment's due date are not listed twice.
        dated = [d for d in dated if not (d["item_type"] == "calendar_event" and d.get("assignment_ref") in listed_assignments)]
        for d in dated:
            d.pop("_assignment_id", None)
        dated.sort(key=lambda d: (d["sort_key"], d["item_id"]))
        cur = decode_cursor(cursor)
        if cur:
            dated = [d for d in dated if (d["sort_key"], d["item_id"]) > (cur["k"], cur["id"])]
        page = dated[:lim]
        nxt = encode_cursor({"k": page[-1]["sort_key"], "id": page[-1]["item_id"]}) if len(dated) > lim else None
        tasks = [dict(x) for x in self.db.query(
            """SELECT id, title, body, task_state, local_due, item_id, course_canvas_id FROM annotations
               WHERE account_id=? AND kind='task' AND local_due IS NOT NULL ORDER BY local_due""", (account_id,))]
        return {
            "range": {"start": s_iso, "end": e_iso}, "time_zone": tzname,
            "deadlines": page, "next_cursor": nxt,
            "unresolved_dates": unresolved if not cur else [],
            "local_tasks_with_dates": tasks if not cur else [],
            "notes": [
                "Dates come from Canvas as collected; date_observed_at says when each was last read.",
                "Submission states are Canvas-reported; local task states are your own and never claim a Canvas submission.",
            ],
        }

    def _canvas_id(self, item_id: int):
        r = self.db.one("SELECT canvas_id FROM items WHERE id=?", (item_id,))
        return r["canvas_id"] if r else None

    def _submission(self, account_id: int, assignment_canvas_id) -> dict | None:
        if not assignment_canvas_id:
            return None
        row = self.db.one("SELECT id, last_observed_at FROM items WHERE account_id=? AND item_type='submission' AND source_key=?",
                          (account_id, str(assignment_canvas_id)))
        if not row:
            return {"state": "not_collected"}
        f = self._fields(row["id"])
        obs = {r["field"]: r["last_observed_at"] for r in self.db.query(
            "SELECT field, last_observed_at FROM item_fields WHERE item_id=?", (row["id"],))}
        return {
            "item_id": row["id"],
            "workflow_state": f.get("workflow_state"), "submitted_at": f.get("submitted_at"),
            "late": f.get("late"), "missing": f.get("missing"), "excused": f.get("excused"),
            "score": f.get("score"), "grade": f.get("grade"), "grade_visible": f.get("grade_visible"),
            "attempt": f.get("attempt"), "observed_at": obs.get("workflow_state") or row["last_observed_at"],
        }

    # ---- changes ---------------------------------------------------------
    def _event_dict(self, r) -> dict:
        d = dict(r)
        old, new = d.pop("old_json", None), d.pop("new_json", None)
        d["old"] = json.loads(old) if old is not None else None
        d["new"] = json.loads(new) if new is not None else None
        d["baseline"] = bool(d["baseline"])
        return d

    def changes(self, account_id: int, *, cursor: str | None = None, since: str | None = None,
                include_baseline: bool = False, include_first_observed: bool = True, kinds: list[str] | None = None,
                course_id: str | None = None, limit: int | None = None) -> dict:
        """Ordered by event id ascending (stable, reproducible). Use the
        returned next_cursor to continue; when no more events exist the
        cursor still advances so polling can resume from it later."""
        lim = clamp_limit(limit, 50, 500)
        where, params = ["e.account_id=?"], [account_id]
        after = 0
        if cursor:
            try:
                after = int(cursor)
            except ValueError as ex:
                raise InvalidInput("cursor must be the next_cursor from a previous call", code="invalid_cursor") from ex
        where.append("e.id > ?")
        params.append(after)
        if since:
            if not parse_iso(since):
                raise InvalidInput("since must be an ISO-8601 timestamp with an offset")
            where.append("e.recorded_at >= ?")
            params.append(to_utc_iso(parse_iso(since)))
        if not include_baseline:
            where.append("e.baseline = 0")
        if not include_first_observed:
            where.append("e.kind != 'first_observed'")
        if kinds:
            where.append(f"e.kind IN ({','.join('?'*len(kinds))})")
            params += kinds
        if course_id:
            where.append("i.course_canvas_id = ?")
            params.append(str(course_id))
        rows = self.db.query(
            f"""SELECT e.*, i.title, i.item_type, i.course_canvas_id, i.source_url FROM change_events e
                JOIN items i ON i.id=e.item_id WHERE {' AND '.join(where)} ORDER BY e.id LIMIT ?""", (*params, lim + 1))
        events = [self._event_dict(r) for r in rows[:lim]]
        courses = self._course_titles(account_id)
        for ev in events:
            ev["course_title"] = courses.get(ev["course_canvas_id"])
        if events:
            nxt = str(events[-1]["id"])
        else:
            mx = self.db.one("SELECT MAX(id) m FROM change_events WHERE account_id=?", (account_id,))["m"]
            nxt = str(max(after, mx or 0))
        return {"events": events, "next_cursor": nxt, "has_more": len(rows) > lim}

    # ---- coverage --------------------------------------------------------
    def coverage(self, account_id: int, *, course_id: str | None = None) -> dict:
        acct = self.db.one("SELECT scope_json FROM accounts WHERE id=?", (account_id,))
        if not acct:
            raise NotFound("account not found", code="account_not_found")
        scope = json.loads(acct["scope_json"] or "{}")
        params: list = [account_id]
        sql = "SELECT * FROM coverage WHERE account_id=?"
        if course_id is not None:
            sql += " AND course_canvas_id=?"
            params.append(str(course_id))
        rows = self.db.query(sql + " ORDER BY id", params)
        latest: dict[tuple, dict] = {}
        thorough: dict[tuple, dict] = {}
        success: dict[tuple, str] = {}
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            key = (d["course_canvas_id"], d["category"])
            latest[key] = d
            if d["mode"] in ("baseline", "thorough"):
                thorough[key] = d
            if d["status"] == "complete":
                success[key] = d["observed_at"]
        courses = self._course_titles(account_id)
        out = []
        for key, d in sorted(latest.items(), key=lambda kv: (kv[0][0], list(CATEGORIES).index(kv[0][1]) if kv[0][1] in CATEGORIES else 99)):
            t = thorough.get(key)
            out.append({
                "course_id": key[0] or None, "course_title": courses.get(key[0]), "category": key[1],
                "latest": d,
                "last_thorough": {k: t[k] for k in ("job_id", "status", "index_depth", "body_depth", "attachment_depth",
                                                    "observed_at", "reason")} if t else None,
                "last_complete_at": success.get(key),
            })
        unfinished = [dict(r) for r in self.db.query(
            """SELECT id, mode, state, message, created_at, updated_at FROM jobs WHERE account_id=? AND kind='collect'
               AND state IN ('queued','running','needs_login','paused','interrupted','cancel_requested') ORDER BY id""",
            (account_id,))]
        for j in unfinished:
            j["pending_tasks"] = [dict(r) for r in self.db.query(
                """SELECT task_key, kind, course_canvas_id, category, state, attempts, last_error FROM job_tasks
                   WHERE job_id=? AND state IN ('pending','failed','blocked') ORDER BY id LIMIT 100""", (j["id"],))]
        failures = [dict(r) for r in self.db.query(
            """SELECT t.job_id, t.task_key, t.kind, t.course_canvas_id, t.category, t.state, t.last_error, t.updated_at
               FROM job_tasks t JOIN jobs j ON j.id=t.job_id WHERE j.account_id=? AND t.state IN ('failed','blocked')
               AND j.id = (SELECT MAX(id) FROM jobs WHERE account_id=? AND kind='collect') ORDER BY t.id LIMIT 200""",
            (account_id, account_id))]
        return {
            "scope": scope, "records": out, "unfinished_jobs": unfinished, "latest_job_failures": failures,
            "legend": {
                "complete": "Exhaustively read at the stated depth",
                "partial": "Some content read; see reason/detail for what is missing",
                "blocked": "Access or login prevented reading",
                "not_exposed": "Course navigation does not expose this category; linked content may still be collected",
                "not_checked": "Not attempted in that run (out of scope or quick mode)",
            },
        }

    def freshness(self, account_id: int) -> dict:
        last_jobs = [dict(r) for r in self.db.query(
            """SELECT id, kind, mode, state, started_at, finished_at FROM jobs WHERE account_id=?
               ORDER BY id DESC LIMIT 5""", (account_id,))]
        last_thorough = self.db.one(
            """SELECT id, mode, state, finished_at FROM jobs WHERE account_id=? AND kind='collect' AND mode IN ('baseline','thorough')
               AND state IN ('completed','completed_with_gaps') ORDER BY id DESC LIMIT 1""", (account_id,))
        counts = self.db.one(
            """SELECT COUNT(*) n, SUM(presence!='observed') gone, MIN(last_observed_at) oldest, MAX(last_observed_at) newest
               FROM items WHERE account_id=? AND current_revision>0""", (account_id,))
        return {
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "items": counts["n"], "items_not_currently_observed": counts["gone"] or 0,
            "oldest_item_observation": counts["oldest"], "newest_item_observation": counts["newest"],
            "last_thorough_job": dict(last_thorough) if last_thorough else None,
            "recent_jobs": last_jobs,
        }

    def discrepancies(self, account_id: int) -> list[dict]:
        out = []
        for r in self.db.query(
            """SELECT d.*, a.title AS item_title, a.item_type AS item_type, b.title AS other_title, a.course_canvas_id
               FROM discrepancies d JOIN items a ON a.id=d.item_id JOIN items b ON b.id=d.other_item_id
               WHERE d.account_id=? ORDER BY d.last_detected_at DESC""", (account_id,)):
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
