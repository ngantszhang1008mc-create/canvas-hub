"""Grade projection, hours tracking, score alerts, reminders, task board states and the read-only views."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from canvas_hub import projections, scoring
from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput

from .test_components import HMAW
from .test_components_ui import app_server  # noqa: F401  (fixture)

COURSE = {"title": "MATH1023", "scale": [{"letter": "A", "min": 85}, {"letter": "B", "min": 70}, {"letter": "C", "min": 55},
                                        {"letter": "F", "min": 0}],
          "categories": [
              {"id": "hw", "name": "Homework", "weight": 30, "items": [{"id": "h1", "label": "HW1", "max": 10, "score": 9},
                                                                    {"id": "h2", "label": "HW2", "max": 10, "score": 7},
                                                                    {"id": "h3", "label": "HW3", "max": 10}]},
              {"id": "mid", "name": "Midterm", "weight": 30, "items": [{"id": "m", "label": "Midterm", "max": 100, "score": 60}]},
              {"id": "fin", "name": "Final", "weight": 40, "items": [{"id": "f", "label": "Final", "max": 100}]}]}


def test_grade_projection_weights_letters_and_needed():
    spec = projections.normalize_projector(COURSE)
    p = projections.project(spec, {})
    # only graded categories count, weights re-normalised: (80% * 30 + 60% * 30) / 60 = 70
    assert p["percent"] == 70 and p["letter"] == "B" and p["remaining_items"] == 2
    need = {n["letter"]: n for n in p["needed_on_remaining"]}
    assert need["F"]["needs"] == 0
    # overall = 34 + 0.5x with x% on HW3 and the final: full marks give 84, so an A (85) is out of reach
    assert not need["A"]["reachable"] and abs(need["A"]["needs"] - 102) <= 0.1      # 34 + 0.5x = 85
    # B (70): 34 + 0.5x = 70 -> x = 72
    assert need["B"]["reachable"] and abs(need["B"]["needs"] - 72) <= 0.1
    p2 = projections.project(spec, {"hypothetical": {"h3": 10, "f": 90}})
    assert p2["percent"] == round(0.3 * (26 / 30 * 100) + 0.3 * 60 + 0.4 * 90, 2) and p2["letter"] == "B"
    unweighted = projections.normalize_projector({"categories": [{"name": "All", "items": [{"label": "a", "max": 10, "score": 5}]}]})
    assert projections.project(unweighted, {})["percent"] == 50
    drop = projections.normalize_projector({"categories": [{"name": "Q", "weight": 100, "drop_lowest": 1, "items": [
        {"label": "q1", "max": 10, "score": 2}, {"label": "q2", "max": 10, "score": 10}]}]})
    assert projections.project(drop, {})["percent"] == 100


def test_hours_rule():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    spec = projections.normalize_hours({"title": "SDE", "target": 20, "minimum": 10, "points": 20,
                                        "deadline": "2026-12-01T23:59:00+08:00", "source": "Engage"})
    st = projections.clean_hours_state({"entries": [{"date": "2026-10-03", "hours": 4}, {"date": "2026-10-10", "hours": 4.5}]})
    s = projections.hours_summary(spec, st, now)
    assert s["total"] == 8.5 and s["status"] == "below_minimum" and s["points"] == 0 and s["hours_to_minimum"] == 1.5
    assert s["needed_per_week"] and s["needed_per_week"] > 2
    st["entries"].append({"id": "x", "date": "2026-10-20", "hours": 6.5, "note": "", })
    s = projections.hours_summary(spec, st, now)
    assert s["total"] == 15 and s["status"] == "counting" and s["points"] == 15
    late = projections.hours_summary(spec, projections.clean_hours_state({"entries": [{"date": "2026-11-01", "hours": 5}]}),
                                     datetime(2026, 12, 5, tzinfo=timezone.utc))
    assert late["status"] == "lost" and late["points"] == 0           # reset: below minimum when the deadline passed
    with pytest.raises(InvalidInput):
        projections.normalize_hours({"target": 5, "minimum": 10})


def test_score_alerts():
    m = scoring.normalize_model({**HMAW, "buckets": [*HMAW["buckets"][:3],
                                                     {**HMAW["buckets"][3], "due": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()},
                                                     *HMAW["buckets"][4:]]})
    texts = [a["text"] for a in scoring.evaluate(m, {"values": {"sde": 5}})["alerts"]]
    assert any("counts 0 until 10 h" in t for t in texts)
    assert any("all-or-nothing and due within a week" in t for t in texts)
    assert any("Below the pass mark" in t for t in scoring.evaluate(m, {"include": {"nest": False, "fyes": False, "final": False}})["alerts"]
               for t in [a["text"] for a in scoring.evaluate(m, {"include": {"nest": False, "fyes": False, "final": False}})["alerts"]])
    lean = scoring.evaluate(m, {"include": {"nest": False, "fyes": False}})["alerts"]
    assert any("to spare" in a["text"] for a in lean)                # margin 0 is a warning


def test_reminders_fire_once(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    item = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1", (aid,))["id"]
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    hub.reminders.create(aid, title="Start proposal", remind_at=past, item_id=item)
    hub.reminders.create(aid, title="Later", remind_at=future)
    assert [r["title"] for r in hub.reminders.due(aid)] == ["Start proposal"]
    assert hub.reminders.due(aid) == []                               # shown once
    assert [r["title"] for r in hub.reminders.list(aid)] == ["Later"]
    with pytest.raises(InvalidInput):
        hub.reminders.create(aid, title="x", remind_at="tomorrow")


def test_task_states_and_board_tools(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    a = hub.annotations.create(aid, kind="task", title="Draft")
    hub.annotations.update(aid, a["id"], task_state="in_progress")
    b = hub.annotations.create(aid, kind="task", title="Wait for TA")
    hub.annotations.update(aid, b["id"], task_state="blocked")
    c = hub.annotations.create(aid, kind="task", title="Done one", task_state="done")
    assert {x["id"] for x in hub.annotations.list(aid, task_state="active")} == {a["id"], b["id"]}
    assert c["task_state"] == "done"
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    text, err, display = run_tool_display(ctx, "show_kanban", {}, {"ui"})
    assert not err and display["component"] == "kanban" and "1 in progress, 1 blocked, 1 done" in json.loads(text)["summary"]
    text, err, display = run_tool_display(ctx, "show_heatmap", {"weeks": 4}, {"ui"})
    assert not err and display["component"] == "heatmap" and display["weeks"] == 4
    text, err, display = run_tool_display(ctx, "show_grade_projector", {"course_id": "4242"}, {"ui"})
    assert not err and display["component"] == "grade_projector"
    assert display["view"]["spec"]["categories"] and "grade projection" in json.loads(text)["summary"]
    text, err, display = run_tool_display(ctx, "show_hours_tracker", {"title": "SDE", "target": 20, "minimum": 10, "points": 20,
                                                                      "entries": [{"date": "2026-10-03", "hours": 4}]}, {"ui"})
    assert not err and "4 of 20 h" in json.loads(text)["summary"] and "below_minimum" in json.loads(text)["summary"]
    back = json.loads(run_tool_display(ctx, "get_component", {"component_id": display["component_id"]}, {"read"})[0])
    assert back["summary"]["total"] == 4 and back["entries"][0]["hours"] == 4
    text, err, _ = run_tool_display(ctx, "set_reminder", {"title": "Check SDE", "remind_at": "2026-11-20T09:00:00+08:00"}, {"notes"})
    assert not err and json.loads(text)["reminder"]["status"] == "pending"


@pytest.mark.browser
def test_board_moves_and_reminders_in_the_browser(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    aid = account["id"]
    hub.annotations.create(aid, kind="task", title="Read chapter 4")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/notes")
        pg.locator(".remind button").first.click()
        pg.get_by_role("menuitem", name="In 1 hour").click()
        expect(pg.locator(".remind button").first).to_have_text("⏰✓")
        pg.get_by_role("button", name="Board").click()
        pg.get_by_role("button", name="Move Read chapter 4 to In progress").click()
        expect(pg.locator(".kanban .kb-card", has_text="Read chapter 4").get_by_role("button", name="Move Read chapter 4 to Blocked")).to_be_visible()
        b.close()
    assert not errors
    assert [r["title"] for r in hub.reminders.list(aid)] == ["Read chapter 4"]
    assert [a["task_state"] for a in hub.annotations.list(aid) if a["title"] == "Read chapter 4"] == ["in_progress"]
