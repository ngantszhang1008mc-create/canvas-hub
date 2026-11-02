"""Upcoming views retain unfinished overdue work without changing saved filters."""

import copy
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect, sync_playwright

from .test_components_ui import serve
from .test_ui_layout_regressions import screenshot


@pytest.mark.browser
def test_upcoming_backlog_preserves_archive_completion_and_saved_filters(hub, account, fixture_pkg, monkeypatch, tmp_path):
    package = copy.deepcopy(fixture_pkg)
    course = next(i for i in package["items"] if i["type"] == "course")
    assignment = next(i for i in package["items"] if i["type"] == "assignment")
    submission = next(i for i in package["items"] if i["type"] == "submission")
    other_course = copy.deepcopy(course)
    other_course.update(course_id="5252", canvas_id="5252", title="OTHER101 Synthetic course")
    other_course["fields"].update(title=other_course["title"], name=other_course["title"])
    package["items"] = [course, other_course]
    package["coverage"] = []
    package["presence_checks"] = []
    now = datetime.now(timezone.utc)
    cases = [
        ("Old open", -2, "assignment", "4242", False),
        ("Older open", -35, "assignment", "4242", False),
        ("Oldest open", -200, "assignment", "4242", False),
        ("Archived open", -4, "assignment", "4242", False),
        ("Canvas completed", -5, "assignment", "4242", True),
        ("Locally completed", -6, "assignment", "4242", False),
        ("Other course open", -3, "assignment", "5252", False),
        ("Old quiz", -40, "quiz", "4242", False),
        ("Past calendar", -10, "calendar_event", "4242", False),
        ("Future calendar", 1, "calendar_event", "4242", False),
        ("Future assignment", 2, "assignment", "4242", False),
        ("Recent completed", -0.5, "assignment", "4242", True),
    ]
    for number, (title, days, kind, course_id, completed) in enumerate(cases, 8000):
        item = copy.deepcopy(assignment)
        item.update(type=kind, canvas_id=str(number), course_id=course_id, title=title)
        item["fields"]["title"] = title
        item["fields"]["due_at"]["iso"] = (now + timedelta(days=days)).isoformat()
        if kind == "calendar_event":
            item["fields"]["start_at"] = item["fields"].pop("due_at")
        package["items"].append(item)
        if completed:
            sub = copy.deepcopy(submission)
            sub.update(canvas_id=str(number), course_id=course_id)
            sub["links"][0]["to"].update(canvas_id=str(number), course_id=course_id)
            sub["fields"].update(workflow_state="graded", submitted_at=now.isoformat(), score=0, grade="0")
            package["items"].append(sub)
    hub.importer.import_package(package)
    aid = account["id"]
    ids = {r["title"]: r["id"] for r in hub.db.query("SELECT id,title FROM items WHERE account_id=?", (aid,))}
    with serve(hub, []) as (base, token, api), sync_playwright() as playwright:
        api.archive_deadline(aid, ids["Archived open"], True)
        api.set_deadline_local_completion(aid, ids["Locally completed"], True)
        api.save_deadline_tab(aid, {"id": "7", "name": "My saved week", "filters": {"course_id": "4242"}})
        saved = copy.deepcopy(api.deadline_views(aid))
        archives = api.archived_deadlines(aid)
        # Exercise pagination with a small fixture rather than hundreds of rows.
        original_deadlines = api.deadlines
        queries = []
        def small_pages(account_id, **options):
            queries.append(dict(options))
            return original_deadlines(account_id, **{**options, "limit": 2})
        monkeypatch.setattr(api, "deadlines", small_pages)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1120, "height": 960})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/?token={token}#/deadlines")
        titles = page.locator(".deadline a.strong")
        backlog = ["Oldest open", "Old quiz", "Older open", "Old open", "Recent completed", "Future calendar", "Future assignment"]
        expect(titles).to_have_text(backlog)
        expect(page.locator(".deadline-carryover")).to_contain_text("4 unfinished overdue")
        expect(page.get_by_label("Date range", exact=True)).to_have_value("7")
        assert api.deadline_views(aid) == saved
        assert api.archived_deadlines(aid) == archives
        assert any(q.get("cursor") and q["start"].startswith("1900") for q in queries)
        page.reload()
        expect(titles).to_have_text(backlog)
        assert api.deadline_views(aid) == saved
        screenshot(page, tmp_path, "overdue-backlog.png")

        page.get_by_label("Deadline status", exact=True).select_option("completed")
        expect(titles).to_have_text(["Recent completed"])
        expect(page.locator(".deadline-carryover")).to_have_count(0)
        page.get_by_label("Deadline status", exact=True).select_option("unfinished")
        expect(titles).to_have_text([t for t in backlog if t != "Recent completed"])
        page.get_by_label("Show archived", exact=True).check()
        expect(titles).to_have_text(["Oldest open", "Old quiz", "Older open", "Archived open", "Old open", "Future calendar", "Future assignment"])
        page.get_by_label("Show archived", exact=True).uncheck()
        expect(titles).to_have_text([t for t in backlog if t != "Recent completed"])
        page.get_by_label("Deadline type", exact=True).select_option("quiz")
        expect(titles).to_have_text(["Old quiz"])
        page.get_by_label("Deadline type", exact=True).select_option("calendar_event")
        expect(titles).to_have_text(["Future calendar"])
        expect(page.locator(".deadline-carryover")).to_have_count(0)
        page.get_by_label("Deadline type", exact=True).select_option("")
        expect(titles).to_have_text([t for t in backlog if t != "Recent completed"])
        page.get_by_label("Deadline course", exact=True).select_option("5252")
        expect(titles).to_have_text(["Other course open"])
        page.get_by_label("Deadline course", exact=True).select_option("4242")
        expect(titles).to_have_text([t for t in backlog if t != "Recent completed"])
        page.get_by_label("Deadline status", exact=True).select_option("all")
        expect(titles).to_have_text(backlog)
        page.get_by_label("Date range", exact=True).select_option("past")
        expect(titles).to_have_text(["Past calendar", "Locally completed", "Canvas completed", "Old open", "Recent completed"])
        expect(page.locator(".deadline-carryover")).to_have_count(0)
        page.get_by_label("Date range", exact=True).select_option("all")
        expect(titles).to_have_count(10)
        expect(page.locator(".deadline-carryover")).to_have_count(0)
        assert api.archived_deadlines(aid) == archives
        assert not errors, errors
        browser.close()
