"""Timetable and study planner (local only).

A week view combines:
* class times the user (or the assistant, when allowed) added locally;
* Canvas calendar events;
* assignment/quiz deadlines with the Canvas-reported submission state;
* the user's local to-dos that have a due time;
* optional suggested study blocks before unfinished deadlines.

Nothing here is written to Canvas. Suggestions are not stored until the user
accepts one (it then becomes an ordinary local to-do).
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import dates
from ..db import Database
from ..util import InvalidInput, NotFound, now_iso, parse_iso

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DONE_STATES = {"submitted", "graded", "pending_review"}
# Preferred study start times (local), tried in order each day.
_STUDY_STARTS = [time(19, 0), time(14, 0), time(10, 0), time(16, 30), time(20, 30)]


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _iso_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).isoformat(timespec="minutes")


class PlannerService:
    def __init__(self, db: Database, query):
        self.db = db
        self.query = query

    # ---- class times -------------------------------------------------------------
    def add_class(self, account_id: int, *, title: str, weekday: int, start: str, end: str,
                  course_id: str | None = None, location: str | None = None,
                  valid_from: str | None = None, valid_until: str | None = None) -> dict:
        title = (title or "").strip()
        if not title or len(title) > 200:
            raise InvalidInput("class title is required (max 200 characters)")
        if not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise InvalidInput("weekday must be 0 (Monday) to 6 (Sunday)")
        for v, n in ((start, "start"), (end, "end")):
            if not isinstance(v, str) or not _HHMM.match(v):
                raise InvalidInput(f"{n} must be HH:MM (24-hour)")
        if end <= start:
            raise InvalidInput("end must be after start")
        for v, n in ((valid_from, "valid_from"), (valid_until, "valid_until")):
            if v:
                try:
                    date.fromisoformat(v)
                except ValueError as e:
                    raise InvalidInput(f"{n} must be YYYY-MM-DD") from e
        now = now_iso()
        with self.db.tx() as c:
            cid = c.execute(
                """INSERT INTO class_times(account_id, course_canvas_id, title, weekday, start_time, end_time, location,
                   valid_from, valid_until, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, course_id, title, weekday, start, end, (location or "").strip()[:200] or None,
                 valid_from, valid_until, now, now)).lastrowid
        return self.get_class(account_id, cid)

    def get_class(self, account_id: int, class_id: int) -> dict:
        row = self.db.one("SELECT * FROM class_times WHERE id=? AND account_id=?", (int(class_id), account_id))
        if not row:
            raise NotFound("class time not found", code="class_time_not_found")
        d = dict(row)
        d["weekday_name"] = WEEKDAYS[d["weekday"]]
        return d

    def list_classes(self, account_id: int) -> list[dict]:
        return [self.get_class(account_id, r["id"]) for r in self.db.query(
            "SELECT id FROM class_times WHERE account_id=? ORDER BY weekday, start_time", (account_id,))]

    def delete_class(self, account_id: int, class_id: int) -> dict:
        self.get_class(account_id, class_id)
        with self.db.tx() as c:
            c.execute("DELETE FROM class_times WHERE id=? AND account_id=?", (int(class_id), account_id))
        return {"deleted": int(class_id)}

    # ---- week view ------------------------------------------------------------------
    def _account_tz(self, account_id: int, override: str | None) -> tuple[ZoneInfo, str]:
        acct = self.db.one("SELECT time_zone FROM accounts WHERE id=?", (account_id,))
        name = override or (acct["time_zone"] if acct else None) or "UTC"
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise InvalidInput(f"unknown time zone {name!r}") from e

    def week(self, account_id: int, *, week_of: str | None = None, time_zone: str | None = None,
             suggestions: bool = False, study_hours: float = 3.0) -> dict:
        tz, tzname = self._account_tz(account_id, time_zone)
        if week_of:
            try:
                anchor = date.fromisoformat(week_of[:10])
            except ValueError as e:
                raise InvalidInput("week_of must be YYYY-MM-DD") from e
        else:
            anchor = datetime.now(timezone.utc).astimezone(tz).date()
        monday = anchor - timedelta(days=anchor.weekday())
        start = datetime.combine(monday, time(0, 0), tz)
        end = start + timedelta(days=7)
        events: list[dict] = []

        for ct in self.list_classes(account_id):
            d = monday + timedelta(days=ct["weekday"])
            if (ct["valid_from"] and d.isoformat() < ct["valid_from"]) or (ct["valid_until"] and d.isoformat() > ct["valid_until"]):
                continue
            s = datetime.combine(d, time.fromisoformat(ct["start_time"]), tz)
            e = datetime.combine(d, time.fromisoformat(ct["end_time"]), tz)
            events.append({"kind": "class", "title": ct["title"], "start": _iso_local(s, tz), "end": _iso_local(e, tz),
                           "location": ct["location"], "course_id": ct["course_canvas_id"], "class_id": ct["id"]})

        dl = self.query.deadlines(account_id, start=start.isoformat(), end=(end + timedelta(days=7)).isoformat(),
                                  time_zone=tzname, include_calendar=True, limit=500)
        upcoming = []
        for d in dl["deadlines"]:
            when = dates.instant(d["date"])
            if when is None:
                continue
            sub = d.get("submission") or {}
            if d["item_type"] == "calendar_event":
                if when >= end:
                    continue
                f = self.query._fields(d["item_id"])
                fin = dates.instant(f.get("end_at")) or when + timedelta(hours=1)
                events.append({"kind": "calendar", "title": d["title"], "start": _iso_local(when, tz),
                               "end": _iso_local(fin, tz), "location": d.get("location_name"),
                               "course": d["course_title"], "item_id": d["item_id"], "source_url": d["source_url"]})
                continue
            done = (sub.get("workflow_state") in _DONE_STATES or sub.get("excused") or sub.get("submitted_at")
                    or d.get("done_locally"))
            entry = {"kind": "deadline", "title": d["title"], "start": _iso_local(when, tz), "end": _iso_local(when, tz),
                     "course": d["course_title"], "item_id": d["item_id"], "source_url": d["source_url"],
                     "submission_state": "excused" if sub.get("excused") else sub.get("workflow_state"),
                     "late": sub.get("late"), "missing": sub.get("missing"), "done": bool(done),
                     "points": d.get("points_possible")}
            if when < end:
                events.append(entry)
            if not done:
                # Dated linked to-dos (accepted study blocks, "start on Saturday")
                # already plan time for this deadline.
                planned = [t for t in (parse_iso(x["local_due"]) for x in d.get("local_tasks") or [] if x["local_due"]) if t]
                upcoming.append((when, entry, planned))

        for t in self.db.query(
                """SELECT a.id, a.title, a.body, a.task_state, a.local_due, a.item_id FROM annotations a
                   WHERE a.account_id=? AND a.kind='task' AND a.local_due IS NOT NULL""", (account_id,)):
            due = parse_iso(t["local_due"])
            if due and start <= due < end:
                events.append({"kind": "task", "title": t["title"] or (t["body"] or "")[:60] or "To-do",
                               "start": _iso_local(due, tz), "end": _iso_local(due + timedelta(minutes=30), tz),
                               "annotation_id": t["id"], "item_id": t["item_id"], "done": t["task_state"] == "done"})

        suggested = self._suggest(events, upcoming, start, end, tz, study_hours) if suggestions else []
        events += suggested
        events.sort(key=lambda e: (e["start"], e["kind"]))
        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            days.append({"date": d.isoformat(), "weekday": WEEKDAYS[i],
                         "events": [e for e in events if e["start"][:10] == d.isoformat()]})
        return {"time_zone": tzname, "week_start": monday.isoformat(), "week_end": (monday + timedelta(days=6)).isoformat(),
                "days": days, "classes_defined": bool(self.list_classes(account_id)),
                "notes": ["Class times come from your own timetable entries and the Canvas calendar.",
                          "Study blocks are suggestions; they are not saved until you add one as a to-do."]}

    def _suggest(self, events, upcoming, start, end, tz, study_hours) -> list[dict]:
        busy = []
        for e in events:
            if e["kind"] in ("class", "calendar", "task"):
                busy.append((datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])))
        now = datetime.now(timezone.utc).astimezone(tz)
        out = []
        block = timedelta(minutes=90)
        for due, entry, planned in sorted(upcoming, key=lambda x: x[0]):
            need = max(1, math.ceil(study_hours * 60 / 90))
            placed = len(planned)
            used_days = {p.astimezone(tz).date() for p in planned}
            busy += [(p.astimezone(tz), p.astimezone(tz) + block) for p in planned]
            due_local = due.astimezone(tz)
            # Start up to a week before the deadline (never in the past) and
            # spread blocks one per day, so work begins early rather than the night before.
            first_day = max(now.date(), (due_local - timedelta(days=7)).date())
            last_day = (due_local - timedelta(hours=2)).date()
            candidates = []
            d = first_day
            while d <= last_day:
                candidates.append(d)
                d += timedelta(days=1)
            for d in candidates:
                if placed >= need:
                    break
                if d in used_days:
                    continue
                for t in _STUDY_STARTS:
                    s = datetime.combine(d, t, tz)
                    e = s + block
                    if s < now or e > due.astimezone(tz) or any(s < b2 and b1 < e for b1, b2 in busy):
                        continue
                    busy.append((s, e))
                    placed += 1
                    if start <= s < end:
                        out.append({"kind": "study", "title": f"Study: {entry['title']}", "start": _iso_local(s, tz),
                                    "end": _iso_local(e, tz), "for_item_id": entry["item_id"], "course": entry["course"],
                                    "suggested": True})
                    break
        return out

    # ---- calendar export --------------------------------------------------------------
    def ics(self, account_id: int, weeks: int = 8) -> str:
        tz, tzname = self._account_tz(account_id, None)
        weeks = max(1, min(int(weeks), 26))
        today = datetime.now(timezone.utc).astimezone(tz).date()
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Canvas Hub//Planner//EN", "CALSCALE:GREGORIAN",
                 "X-WR-CALNAME:Canvas Hub"]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        def esc(s: str | None) -> str:
            return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

        def utc(dt: datetime) -> str:
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        for ct in self.list_classes(account_id):
            first = today + timedelta(days=(ct["weekday"] - today.weekday()) % 7)
            if ct["valid_from"] and first.isoformat() < ct["valid_from"]:
                vf = date.fromisoformat(ct["valid_from"])
                first = vf + timedelta(days=(ct["weekday"] - vf.weekday()) % 7)
            s = datetime.combine(first, time.fromisoformat(ct["start_time"]), tz)
            e = datetime.combine(first, time.fromisoformat(ct["end_time"]), tz)
            until = (date.fromisoformat(ct["valid_until"]) if ct["valid_until"] else today + timedelta(weeks=weeks))
            lines += ["BEGIN:VEVENT", f"UID:canvas-hub-class-{ct['id']}@local", f"DTSTAMP:{stamp}",
                      f"DTSTART;TZID={tzname}:{s.strftime('%Y%m%dT%H%M%S')}", f"DTEND;TZID={tzname}:{e.strftime('%Y%m%dT%H%M%S')}",
                      f"RRULE:FREQ=WEEKLY;UNTIL={until.strftime('%Y%m%d')}T235959Z", f"SUMMARY:{esc(ct['title'])}"]
            if ct["location"]:
                lines.append(f"LOCATION:{esc(ct['location'])}")
            lines.append("END:VEVENT")
        start = datetime.combine(today, time(0, 0), tz)
        dl = self.query.deadlines(account_id, start=start.isoformat(), end=(start + timedelta(weeks=weeks)).isoformat(),
                                  include_calendar=False, limit=500)
        for d in dl["deadlines"]:
            when = dates.instant(d["date"])
            if when is None:
                continue
            sub = d.get("submission") or {}
            state = "excused" if sub.get("excused") else (sub.get("workflow_state") or "")
            lines += ["BEGIN:VEVENT", f"UID:canvas-hub-deadline-{d['item_id']}@local", f"DTSTAMP:{stamp}",
                      f"DTSTART:{utc(when - timedelta(minutes=30))}", f"DTEND:{utc(when)}",
                      f"SUMMARY:{esc('Due: ' + (d['title'] or ''))}",
                      f"DESCRIPTION:{esc((d['course_title'] or '') + (' — Canvas status: ' + state if state else ''))}"]
            if d.get("source_url"):
                lines.append(f"URL:{d['source_url']}")
            lines.append("END:VEVENT")
        for t in self.db.query("SELECT id, title, body, local_due, task_state FROM annotations WHERE account_id=? AND kind='task' "
                               "AND local_due IS NOT NULL AND task_state!='done'", (account_id,)):
            due = parse_iso(t["local_due"])
            if not due or due.date() < today:
                continue
            lines += ["BEGIN:VEVENT", f"UID:canvas-hub-task-{t['id']}@local", f"DTSTAMP:{stamp}",
                      f"DTSTART:{utc(due)}", f"DTEND:{utc(due + timedelta(minutes=30))}",
                      f"SUMMARY:{esc(t['title'] or (t['body'] or 'To-do')[:60])}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def _fold(line: str) -> str:
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        e = ch.encode("utf-8")
        if len(cur) + len(e) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += e
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)

