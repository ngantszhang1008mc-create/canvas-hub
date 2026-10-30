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


def test_ics_weekdays_follow_the_time_zone_conversion():
    # 23:00 UTC on Tuesdays and Thursdays is 07:00 on Wednesdays and Fridays in Hong Kong.
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Early lab\nDTSTART:20260901T230000Z\nDTEND:20260902T000000Z\n"
           "RRULE:FREQ=WEEKLY;BYDAY=TU,TH\nEND:VEVENT\nEND:VCALENDAR\n")
    rows = parse_ics(ics, "Asia/Hong_Kong")
    assert sorted((r["weekday"], r["start"], r["end"]) for r in rows) == [(2, "07:00", "08:00"), (4, "07:00", "08:00")]
    # going west: 01:00 Hong Kong time on a Monday is Sunday 17:00 UTC
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Late\nDTSTART;TZID=Asia/Hong_Kong:20260907T010000\n"
           "DTEND;TZID=Asia/Hong_Kong:20260907T020000\nRRULE:FREQ=WEEKLY;BYDAY=MO\nEND:VEVENT\nEND:VCALENDAR\n")
    assert [(r["weekday"], r["start"]) for r in parse_ics(ics, "UTC")] == [(6, "17:00")]


FORTNIGHTLY_EVENT = """BEGIN:VEVENT
SUMMARY:Fortnightly lab
DTSTART;TZID=Asia/Hong_Kong:20260901T140000
DTEND;TZID=Asia/Hong_Kong:20260901T150000
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU
END:VEVENT
"""


@pytest.mark.parametrize("interval", ["2", "0", "-1", "invalid"])
def test_ics_refuses_unsupported_weekly_intervals(hub, account, interval):
    ics = "BEGIN:VCALENDAR\n" + FORTNIGHTLY_EVENT.replace("INTERVAL=2", f"INTERVAL={interval}") + "END:VCALENDAR\n"
    assert parse_ics(ics, "Asia/Hong_Kong") == []
    with pytest.raises(InvalidInput) as exc:
        HubAPI(hub).import_timetable(account["id"], ics)
    assert "every week" in str(exc.value)
    assert exc.value.detail["skipped"][0]["title"] == "Fortnightly lab"
    assert hub.planner.list_classes(account["id"]) == []


def test_ics_import_reports_skipped_fortnightly_events(hub, account):
    ics = ICS.replace("END:VCALENDAR", FORTNIGHTLY_EVENT + "END:VCALENDAR")
    r = HubAPI(hub).import_timetable(account["id"], ics)
    assert r["found"] == r["added"] == 2
    assert len(r["skipped"]) == 1 and r["skipped"][0]["title"] == "Fortnightly lab"
    assert "every 2 weeks" in r["skipped"][0]["reason"]
    assert {c["title"] for c in r["class_times"]} == {"COMP2711H Lecture"}
    assert HubAPI(hub).import_timetable(account["id"], ics)["added"] == 0


def test_ics_accepts_an_explicit_weekly_interval():
    assert parse_ics(ICS.replace("FREQ=WEEKLY", "FREQ=WEEKLY;INTERVAL=1"), "Asia/Hong_Kong") == parse_ics(
        ICS, "Asia/Hong_Kong")


def test_clashes_use_class_validity_dates_in_the_account_time_zone(hub, account, fixture_pkg, monkeypatch):
    from canvas_hub.services.querylayer import find_clashes

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 1, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("canvas_hub.services.querylayer.datetime", FixedNow)
    pkg = copy.deepcopy(fixture_pkg)
    # Monday in UTC, Tuesday in the account's Hong Kong time zone.
    pkg["items"].append({
        "type": "calendar_event", "course_id": "4242", "canvas_id": "9001", "title": "Midterm",
        "url": "https://canvas.example.edu/calendar?event_id=9001", "observed_at": "2026-10-30T01:58:10Z",
        "method": "canvas_api", "depth": "detail", "fields": {
            k: {"raw": v, "iso": v, "precision": "instant", "source": "canvas_api_iso8601"}
            for k, v in (("start_at", "2026-11-02T16:30:00Z"), ("end_at", "2026-11-02T17:30:00Z"))
        },
    })
    hub.importer.import_package(pkg)
    aid = account["id"]
    for title, valid_from, valid_until in [
        ("Old term", None, "2026-11-02"),
        ("Next term", "2026-11-04", None),
        ("First day", "2026-11-03", None),
        ("Last day", None, "2026-11-03"),
        ("No bounds", None, None),
    ]:
        hub.planner.add_class(aid, title=title, weekday=1, start="00:00", end="02:00",
                              valid_from=valid_from, valid_until=valid_until)
    clashes = find_clashes(hub, aid)["events_overlapping_classes"]
    expected = {"First day", "Last day", "No bounds"}
    assert {c["class"] for c in clashes} == expected
    planner = hub.planner.week(aid, week_of="2026-11-03", suggestions=False)
    classes = {e["title"] for d in planner["days"] for e in d["events"] if e["kind"] == "class"}
    assert classes == expected
