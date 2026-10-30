"""Dates that live only in course text (exams, quizzes, presentations), and
timetable import from an .ics file.

Canvas deadlines come from assignments; an exam announced on a "Course
information" page never reaches them. The finder reads pages, syllabi,
announcements and course home text for sentences that name an assessment and
a date, and keeps them as *suggestions*. Nothing becomes a deadline until the
user confirms it, which creates a dated local to-do linked to the page (shown
in the Planner).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import dates as dates_mod
from ..db import Database
from ..htmltools import to_text
from ..util import InvalidInput, NotFound, now_iso, parse_iso

KEYWORDS = re.compile(r"\b(mid-?term(?:\s+exam)?|final\s+exam|exam(?:ination)?|quiz|test|presentation|deadline|"
                      r"due|submission|project\s+demo|demo|viva|oral)\b", re.I)
_MON = r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
DATE_MDY = re.compile(_MON + r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(?P<year>\d{4}))?", re.I)
DATE_DMY = re.compile(r"(?<!\d)(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?:,?\s+(?P<year>\d{4}))?", re.I)
TIME_24 = re.compile(r"(?<![\d:])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?:\s*(?:-|–|to)\s*(?P<h2>[01]?\d|2[0-3]):(?P<m2>[0-5]\d))?")
TIME_12 = re.compile(r"(?<![\d:])(?P<h>1[0-2]|0?[1-9])(?::(?P<m>[0-5]\d))?\s*(?P<ap>[ap])\.?m\.?"
                     r"(?:\s*(?:-|–|to)\s*(?P<h2>1[0-2]|0?[1-9])(?::(?P<m2>[0-5]\d))?\s*(?P<ap2>[ap])\.?m\.?)?"
                     r"|(?<![\d:])(?P<hb>1[0-2]|0?[1-9])\s*(?:-|–|to)\s*(?P<he>1[0-2]|0?[1-9])\s*(?P<apb>[ap])\.?m\.?", re.I)
ROOM = re.compile(r"\b(?:Rm\.?|Room|Lecture\s+Theat(?:er|re)|LT)\s*[A-Z]?\d{0,4}[A-Z]?(?:\s*[/&,]\s*\d{3,4})*", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
TEXT_FIELDS = ("body", "message", "description", "syllabus_body", "front_page_body", "content")


def _tz(name: str | None):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n+", text)
    return [" ".join(p.split()) for p in parts if 8 <= len(p.strip()) <= 600]


def _times(s: str):
    m12 = TIME_12.search(s)
    m = TIME_24.search(s)
    if m and not (m12 and m12.start() <= m.end()):      # "11:59pm" is a 12-hour time, not 11:59
        a = time(int(m["h"]), int(m["m"]))
        b = time(int(m["h2"]), int(m["m2"])) if m["h2"] else None
        return a, b
    m = m12
    if not m:
        return None, None

    def to24(h, mi, ap):
        h = int(h) % 12 + (12 if ap.lower() == "p" else 0)
        return time(h, int(mi or 0))
    if m["hb"]:
        return to24(m["hb"], 0, m["apb"]), to24(m["he"], 0, m["apb"])
    return to24(m["h"], m["m"], m["ap"]), (to24(m["h2"], m["m2"], m["ap2"]) if m["h2"] else None)


def find_in_text(text: str, *, time_zone: str | None, reference: datetime) -> list[dict]:
    """Assessment dates in free text. A missing year is taken as the one that
    puts the date nearest after the reference time (and flagged as inferred)."""
    tz = _tz(time_zone)
    out = []
    for s in _sentences(text):
        kw = KEYWORDS.search(s)
        if not kw:
            continue
        for rx in (DATE_MDY, DATE_DMY):
            for m in rx.finditer(s):
                mon = MONTHS[m["mon"][:3].lower()]
                day = int(m["day"])
                inferred = not m["year"]
                if m["year"]:
                    yr = int(m["year"])
                else:
                    ref = reference.astimezone(tz).date()
                    yr = ref.year
                    try:
                        if date(yr, mon, day) < ref - timedelta(days=60):
                            yr += 1
                    except ValueError:
                        continue
                try:
                    d = date(yr, mon, day)
                except ValueError:
                    continue
                t1, t2 = _times(s)
                start = datetime.combine(d, t1 or time(0, 0), tz)
                end = datetime.combine(d, t2, tz) if t2 else None
                room = ROOM.search(s)
                word = re.sub(r"\s+", " ", kw.group(1)).strip()
                out.append({"label": word[:1].upper() + word[1:].lower(), "starts_at": start.isoformat(),
                            "ends_at": end.isoformat() if end else None, "time_known": bool(t1),
                            "year_inferred": inferred, "location": room.group(0).strip() if room else None,
                            "snippet": s[:400]})
            if out and out[-1]["snippet"] == s[:400]:
                break       # one date per sentence form is enough
    uniq, seen = [], set()
    for c in out:
        k = (c["label"], c["starts_at"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


class DateFinder:
    def __init__(self, db: Database, query, annotations, settings):
        self.db = db
        self.query = query
        self.annotations = annotations
        self.settings = settings

    def scan(self, account_id: int, *, full: bool = False) -> dict:
        acct = self.db.one("SELECT time_zone FROM accounts WHERE id=?", (account_id,))
        tzname = acct["time_zone"] if acct else None
        state = self.settings.get(account_id, "datefinder")
        since = None if full else state.get("scanned_until")
        rows = self.db.query(
            f"""SELECT id, item_type, title, course_canvas_id, last_observed_at, first_observed_at FROM items
                WHERE account_id=? AND item_type IN ('page','syllabus','announcement','course','discussion')
                AND current_revision>0 {'AND last_observed_at>?' if since else ''}""",
            (account_id, since) if since else (account_id,))
        deadlines = {}
        for r in self.db.query("""SELECT i.course_canvas_id, f.value_json FROM items i JOIN item_fields f ON f.item_id=i.id
                                  WHERE i.account_id=? AND i.item_type IN ('assignment','quiz') AND f.field='due_at'""", (account_id,)):
            dt = dates_mod.instant(json.loads(r["value_json"]))
            if dt:
                deadlines.setdefault(r["course_canvas_id"], set()).add(dt.astimezone(_tz(tzname)).date())
        added = 0
        newest = since
        for r in rows:
            newest = max(newest or "", r["last_observed_at"] or "")
            f = self.query._fields(r["id"])
            text = " \n".join(to_text(f[k]) if isinstance(f.get(k), str) else "" for k in TEXT_FIELDS if f.get(k))
            if not text.strip():
                continue
            ref = parse_iso(r["first_observed_at"]) or parse_iso(r["last_observed_at"])
            for c in find_in_text(text, time_zone=tzname, reference=ref):
                day = datetime.fromisoformat(c["starts_at"]).date()
                near = {day + timedelta(days=k) for k in (-1, 0, 1)}
                if c["label"].lower() in ("due", "deadline", "submission") and near & deadlines.get(r["course_canvas_id"], set()):
                    continue          # a Canvas deadline (mismatches get the date-conflict warning instead)
                fp = hashlib.sha1(f"{r['id']}|{c['starts_at']}|{c['label']}".encode()).hexdigest()
                now = now_iso()
                label = f"{c['label']} — {r['title'] or 'course page'}"
                with self.db.tx() as cx:
                    n = cx.execute(
                        """INSERT OR IGNORE INTO date_candidates(account_id, item_id, course_canvas_id, label, starts_at, ends_at,
                           location, snippet, fingerprint, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (account_id, r["id"], r["course_canvas_id"], label[:200], c["starts_at"], c["ends_at"], c["location"],
                         c["snippet"] + (" [year assumed]" if c["year_inferred"] else "") + ("" if c["time_known"] else " [no time stated]"),
                         fp, now, now)).rowcount
                added += n
        if newest:
            self.settings.set(account_id, "datefinder", {"scanned_until": newest})
        return {"scanned_items": len(rows), "new_suggestions": added, **self.list(account_id)}

    def list(self, account_id: int, status: str | None = None) -> dict:
        sql = """SELECT d.*, i.title AS item_title, i.source_url,
                 (SELECT title FROM items c WHERE c.account_id=d.account_id AND c.item_type='course'
                    AND c.canvas_id=d.course_canvas_id) AS course_title
                 FROM date_candidates d JOIN items i ON i.id=d.item_id WHERE d.account_id=?"""
        params: list = [account_id]
        if status:
            sql += " AND d.status=?"
            params.append(status)
        rows = [dict(r) for r in self.db.query(sql + " ORDER BY d.starts_at", params)]
        return {"candidates": rows}

    def _get(self, account_id: int, cid: int) -> dict:
        r = self.db.one("SELECT * FROM date_candidates WHERE id=? AND account_id=?", (int(cid), account_id))
        if not r:
            raise NotFound("date suggestion not found", code="date_candidate_not_found")
        return dict(r)

    def confirm(self, account_id: int, cid: int, *, label: str | None = None, starts_at: str | None = None) -> dict:
        c = self._get(account_id, cid)
        if c["status"] == "confirmed":
            return c
        when = starts_at or c["starts_at"]
        if not parse_iso(when):
            raise InvalidInput("starts_at must be an ISO-8601 time with an offset")
        body = f"Found in: {c['snippet']}" + (f"\nWhere: {c['location']}" if c["location"] else "") + \
               (f"\nUntil: {c['ends_at'][11:16]}" if c["ends_at"] else "")
        a = self.annotations.create(account_id, kind="task", title=(label or c["label"])[:300], body=body,
                                    item_id=c["item_id"], local_due=when)
        with self.db.tx() as cx:
            cx.execute("UPDATE date_candidates SET status='confirmed', annotation_id=?, label=?, starts_at=?, updated_at=? WHERE id=?",
                       (a["id"], (label or c["label"])[:200], when, now_iso(), c["id"]))
        return self._get(account_id, cid)

    def dismiss(self, account_id: int, cid: int) -> dict:
        c = self._get(account_id, cid)
        with self.db.tx() as cx:
            cx.execute("UPDATE date_candidates SET status='dismissed', updated_at=? WHERE id=?", (now_iso(), c["id"]))
        return self._get(account_id, cid)


# ---- timetable import (.ics) ----------------------------------------------------------------
_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def parse_ics(text: str, time_zone: str | None) -> list[dict]:
    """Weekly recurring events from an iCalendar file -> class times."""
    lines = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    tz = _tz(time_zone)
    events, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            k, v = ln.split(":", 1)
            name, *params = k.split(";")
            cur[name.upper()] = (v, dict(p.split("=", 1) for p in params if "=" in p))

    def dt(prop):
        if not prop:
            return None
        v, params = prop
        try:
            if v.endswith("Z"):
                return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
            d = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
            return d.replace(tzinfo=_tz(params.get("TZID") or time_zone)).astimezone(tz)
        except ValueError:
            return None
    out = []
    for e in events:
        rr = dict(p.split("=", 1) for p in (e.get("RRULE", ("", {}))[0] or "").split(";") if "=" in p)
        if rr.get("FREQ") != "WEEKLY":
            continue
        s, en = dt(e.get("DTSTART")), dt(e.get("DTEND"))
        if not s or not en:
            continue
        until = None
        if rr.get("UNTIL"):
            u = rr["UNTIL"]
            try:
                until = (datetime.strptime(u[:8], "%Y%m%d")).date().isoformat()
            except ValueError:
                until = None
        # BYDAY names days in the event's own zone; moving the times to the account's zone can cross midnight
        # (23:00 UTC on Tuesday is Wednesday 07:00 in Hong Kong), so the weekdays move by the same number of days.
        try:
            v = e["DTSTART"][0]
            shift = (s.date() - date(int(v[:4]), int(v[4:6]), int(v[6:8]))).days
        except ValueError:
            shift = 0
        days = [(_DAYS[d[-2:]] + shift) % 7 for d in rr.get("BYDAY", "").split(",") if d[-2:] in _DAYS] or [s.weekday()]
        title = (e.get("SUMMARY", ("", {}))[0] or "Class").replace("\\,", ",").replace("\\;", ";")[:200]
        loc = (e.get("LOCATION", ("", {}))[0] or "").replace("\\,", ",")[:200] or None
        for wd in days:
            out.append({"title": title, "weekday": wd, "start": s.strftime("%H:%M"), "end": en.strftime("%H:%M"),
                        "location": loc, "valid_from": s.date().isoformat(), "valid_until": until})
    return out
