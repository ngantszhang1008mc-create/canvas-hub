"""Dates found in course text, timetable import, course content states and shell links."""

import copy
import json
from datetime import datetime, timezone

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.services.datefinder import find_in_text, parse_ics
from canvas_hub.util import InvalidInput

REF = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_find_dates_in_text():
    t = ("Course information. The COMP2711H midterm will be held on Nov 20 in Rm 2503/2504, 19:00-22:00. "
         "Office hours are on Mondays. Quiz 2 takes place on 5 December at 2pm in LTA.")
    found = find_in_text(t, time_zone="Asia/Hong_Kong", reference=REF)
    mid = next(c for c in found if c["label"] == "Midterm")
    assert mid["starts_at"] == "2026-11-20T19:00:00+08:00" and mid["ends_at"] == "2026-11-20T22:00:00+08:00"
    assert mid["location"] == "Rm 2503/2504" and mid["year_inferred"]
    quiz = next(c for c in found if c["label"] == "Quiz")
    assert quiz["starts_at"] == "2026-12-05T14:00:00+08:00"
    assert find_in_text("Office hours on Nov 3 at 4pm.", time_zone="Asia/Hong_Kong", reference=REF) == []   # no assessment word
    pm = find_in_text("Problem Set 1 is due on Oct 30 at 11:59pm.", time_zone="Asia/Hong_Kong", reference=REF)[0]
    assert pm["starts_at"] == "2026-10-30T23:59:00+08:00"
    # a date in January after a September reference belongs to next year
    jan = find_in_text("Final exam: 12 January, 9:30.", time_zone="Asia/Hong_Kong", reference=REF)[0]
    assert jan["starts_at"].startswith("2027-01-12T09:30")


def test_date_suggestions_need_confirmation(hub, account, fixture_pkg):
    pkg = copy.deepcopy(fixture_pkg)
    page = next(i for i in pkg["items"] if i["type"] == "page")
    page["fields"]["body"] = {"value": "<p>The midterm exam is on Nov 20, 19:00-22:00 in Rm 2503.</p>"} \
        if isinstance(page["fields"]["body"], dict) and "value" in page["fields"]["body"] else \
        "<p>The midterm exam is on Nov 20, 19:00-22:00 in Rm 2503.</p>"
    hub.importer.import_package(pkg)
    aid = account["id"]
    r = hub.datefinder.scan(aid)
    sugg = [c for c in r["candidates"] if c["status"] == "suggested"]
    assert len(sugg) == 1 and sugg[0]["label"].startswith("Midterm") and sugg[0]["location"] == "Rm 2503"
    assert hub.datefinder.scan(aid)["new_suggestions"] == 0                     # incremental, no duplicates
    assert [a for a in hub.annotations.list(aid) if a["kind"] == "task"] == []    # nothing is a deadline yet
    c = hub.datefinder.confirm(aid, sugg[0]["id"])
    todo = hub.annotations.get(aid, c["annotation_id"])
    assert c["status"] == "confirmed" and todo["local_due"].startswith("2026-11-20T19:00") and "Rm 2503" in todo["body"]
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    out = json.loads(run_tool_display(ctx, "find_dates_in_pages", {"rescan": False}, {"read"})[0])
    assert out["suggestions"][0]["status"] == "confirmed"


ICS = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
SUMMARY:COMP2711H Lecture\r
DTSTART;TZID=Asia/Hong_Kong:20260901T103000\r
DTEND;TZID=Asia/Hong_Kong:20260901T115000\r
RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261130T000000Z\r
LOCATION:Rm 2465\\, Lift 25-26\r
END:VEVENT\r
BEGIN:VEVENT\r
SUMMARY:One-off talk\r
DTSTART:20261003T020000Z\r
DTEND:20261003T030000Z\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_timetable_import_from_ics(hub, account):
    rows = parse_ics(ICS, "Asia/Hong_Kong")
    assert [(r["weekday"], r["start"], r["end"]) for r in rows] == [(1, "10:30", "11:50"), (3, "10:30", "11:50")]
    assert rows[0]["location"] == "Rm 2465, Lift 25-26" and rows[0]["valid_until"] == "2026-11-30"
    api = HubAPI(hub)
    r = api.import_timetable(account["id"], ICS)
    assert r["found"] == 2 and r["added"] == 2
    assert api.import_timetable(account["id"], ICS)["added"] == 0              # no duplicates
    with pytest.raises(InvalidInput):
        api.import_timetable(account["id"], "BEGIN:VCALENDAR\nEND:VCALENDAR")


def test_course_states_and_shell_links(hub, account, fixture_pkg):
    pkg = copy.deepcopy(fixture_pkg)
    course = next(i for i in pkg["items"] if i["type"] == "course")
    twin = copy.deepcopy(course)
    twin["canvas_id"] = twin["course_id"] = "4243"
    for k in ("title", "name"):
        if isinstance(twin["fields"].get(k), str):
            twin["fields"][k] += " (copy)"
    twin["title"] = (twin.get("title") or "") + " (copy)"
    pkg["items"].append(twin)
    hub.importer.import_package(pkg)
    aid = account["id"]
    cs = {c["course_id"]: c for c in hub.query.list_courses(aid)}
    assert cs["4242"]["content_state"] == "has_content"
    assert cs["4243"]["content_state"] == "not_checked"           # no coverage recorded for the twin
    assert cs["4242"]["possible_duplicates"] == ["4243"]           # same course code
    hub.query.link_courses(aid, "4243", "4242", "different")
    cs = {c["course_id"]: c for c in hub.query.list_courses(aid)}
    assert cs["4242"]["possible_duplicates"] == [] and cs["4242"]["linked_shells"] == []
    hub.query.link_courses(aid, "4242", "4243", "same")
    cs = {c["course_id"]: c for c in hub.query.list_courses(aid)}
    assert cs["4243"]["linked_shells"] == ["4242"]
    with pytest.raises(InvalidInput):
        hub.query.link_courses(aid, "4242", "4242", "same")
