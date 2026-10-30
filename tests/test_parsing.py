import pytest

from canvas_hub import dates
from canvas_hub.collector.discovery import parse_all_courses
from canvas_hub.htmltools import links, normalized_for_compare, sanitize, to_text
from canvas_hub.urls import canonical_url, classify_link, parse_canvas_address
from canvas_hub.util import InvalidInput


@pytest.mark.parametrize("raw,origin,course", [
    ("https://canvas.ust.hk/", "https://canvas.ust.hk", None),
    ("canvas.ust.hk", "https://canvas.ust.hk", None),
    ("https://canvas.ust.hk/courses/73682/assignments/456957", "https://canvas.ust.hk", "73682"),
    ("https://CANVAS.Example.EDU:443/courses/1", "https://canvas.example.edu", "1"),
    ("https://school.instructure.com/courses/9/pages/x", "https://school.instructure.com", "9"),
    ("http://127.0.0.1:8765/courses/101", "http://127.0.0.1:8765", "101"),
])
def test_parse_canvas_address(raw, origin, course):
    a = parse_canvas_address(raw)
    assert a.origin == origin
    assert a.course_id == course


@pytest.mark.parametrize("raw", ["", "ftp://canvas.ust.hk", "http://canvas.ust.hk", "https://user:pw@canvas.ust.hk",
                                 "https://10.0.0.1/", "https://localhostx/", "javascript:alert(1)"])
def test_parse_canvas_address_rejects(raw):
    with pytest.raises(InvalidInput):
        parse_canvas_address(raw)


def test_classify_links():
    o = "https://canvas.example.edu"
    assert classify_link("/courses/1/pages/lecture-notes", o).kind == "page"
    f = classify_link("/courses/1/files/55/download?verifier=abc&wrap=1", o)
    assert (f.kind, f.ref, f.course_id) == ("file", "55", "1")
    assert "verifier" not in f.url
    assert classify_link("/courses/1/assignments/7", o).kind == "assignment"
    assert classify_link("/courses/1/discussion_topics/7", o).kind == "discussion"
    assert classify_link("/courses/1/modules/items/9", o).kind == "module_item"
    assert classify_link("/courses/1/announcements", o).kind == "course_nav"
    assert classify_link("https://other.example.org/x", o).kind == "external"
    assert classify_link("mailto:a@b.c", o) is None
    assert classify_link("#top", o) is None
    assert canonical_url("https://A.example.edu/x/?b=2&a=1#frag") == "https://a.example.edu/x?a=1&b=2"


def test_dates_api_and_display():
    d = dates.from_api("2026-10-31T15:59:00Z")
    assert d["iso"] == "2026-10-31T15:59:00Z" and d["precision"] == "instant"
    assert dates.from_api(None) is None
    no_year = dates.from_display_text("Oct 30 at 11:59pm", time_zone="Asia/Hong_Kong")
    assert no_year["iso"] is None and "year" in no_year["note"]
    no_tz = dates.from_display_text("Oct 30, 2026 11:59pm")
    assert no_tz["iso"] is None and "time zone" in no_tz["note"]
    ok = dates.from_display_text("Oct 30, 2026 11:59pm", time_zone="Asia/Hong_Kong")
    assert ok["iso"] == "2026-10-30T15:59:00Z"
    rel = dates.from_display_text("2 days ago")
    assert rel["iso"] is None and rel["raw"] == "2 days ago"
    date_only = dates.from_display_text("30 Oct 2026")
    assert date_only["precision"] == "date_only" and date_only["iso"] is None
    m = dates.date_mentions("Problem Set 1 is due on Oct 30 at 11:59pm.")
    assert m[0]["month"] == 10 and m[0]["day"] == 30


def test_sanitize_and_normalize():
    dirty = '<p onclick="x()">Hi<script>alert(1)</script><iframe src="x"></iframe><a href="javascript:x">j</a></p>'
    clean = sanitize(dirty)
    assert "script" not in clean and "onclick" not in clean and "iframe" not in clean and "javascript" not in clean
    assert to_text("<p>A</p><ul><li>b</li></ul>") == "A\n\n- b"
    collapsed = '<details><summary>More</summary><a href="/courses/1/pages/x">x</a></details>'
    assert links(collapsed) == [("/courses/1/pages/x", "x")]
    assert normalized_for_compare("<p>Hello   world</p>") == normalized_for_compare("<div>Hello world</div>")
    assert normalized_for_compare('<a href="/f?verifier=1">f</a>') == normalized_for_compare('<a href="/f?verifier=2">f</a>')
    assert normalized_for_compare("<p>Due Friday</p>") != normalized_for_compare("<p>Due Monday</p>")


def test_all_courses_parser_handles_unlinked_and_sections():
    head = ["", "Course", "Nickname", "Term", "Enrolled as", "Published"]

    def row(name, href=None, pub="Yes"):
        links = [{"href": href, "text": name}] if href else []
        return {"id": "", "cls": "course-list-table-row", "data_course_ids": [], "cells": [
            {"text": ""}, {"text": name, "links": links}, {"text": ""}, {"text": "Fall"}, {"text": "Student"}, {"text": pub}]}
    snap = {"tables": [
        {"id": "my_courses_table", "headers": head, "rows": [row("A101", "/courses/1"), row("Hidden lab", None, "No")]},
        {"id": "", "heading": "Past Enrollments", "headers": head, "rows": [row("OLD1", "/courses/9")]},
        {"id": "other", "headers": ["Name", "Value"], "rows": []},
    ]}
    out = parse_all_courses(snap)
    assert out["tables_seen"] == 2 and not out["problems"]
    by = {r["name"]: r for r in out["rows"]}
    assert by["A101"]["course_id"] == "1" and by["A101"]["section"] == "current"
    assert by["Hidden lab"]["course_id"] is None and by["Hidden lab"]["unlinked_key"].startswith("unlinked-")
    assert by["OLD1"]["section"] == "past"
    assert parse_all_courses({"tables": []})["problems"]
