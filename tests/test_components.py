"""Score models, checklists and the saved component store."""

import json

import pytest

from canvas_hub import scoring
from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.util import Conflict, InvalidInput, NotFound

# A 105-point course tracker shaped like the one in the spec (HMAW1905B):
# 15 points booked, Community Meeting 1 lost (0/10), 80 points still open.
HMAW = {
    "title": "HMAW1905B tracker", "threshold": 80, "unit_label": "pts",
    "buckets": [
        {"id": "intro", "label": "Intro survey", "max": 5, "earned": 5, "status": "earned", "group": "Admin"},
        {"id": "plan", "label": "Study Plan", "max": 10, "earned": 10, "status": "earned", "group": "Admin"},
        {"id": "cm1", "label": "Community Meeting 1", "max": 10, "earned": 0, "status": "lost", "group": "Meetings"},
        {"id": "cm2", "label": "Community Meeting 2", "max": 10, "status": "pending", "group": "Meetings",
         "gating": "all_or_nothing"},
        {"id": "cm3", "label": "Community Meeting 3", "max": 10, "status": "pending", "group": "Meetings",
         "gating": "all_or_nothing"},
        {"id": "nest", "label": "Nest Posts", "max": 10, "status": "pending", "group": "Online"},
        {"id": "fyes", "label": "FYES", "max": 5, "status": "pending", "group": "Online"},
        {"id": "reflect", "label": "Reflection", "max": 15, "status": "pending", "group": "Writing"},
        {"id": "sde", "label": "SDE hours", "max": 20, "status": "pending", "group": "Service", "unit": "hours",
         "gating": "min_threshold", "min_value": 10, "full_at": 20},
        {"id": "final", "label": "Final check-in", "max": 10, "status": "pending", "group": "Admin"},
    ],
    "scenarios": [{"id": "min", "label": "Minimum", "include": {"nest": False, "fyes": False}}],
}


def test_score_model_acceptance_numbers():
    m = scoring.normalize_model(HMAW)
    r = scoring.evaluate(m, {})
    t = r["totals"]
    assert len(r["lines"]) == 10
    assert (t["booked"], t["ceiling"], t["projected"]) == (15, 95, 95)
    cm1 = next(x for x in r["lines"] if x["id"] == "cm1")
    assert cm1["points"] == 0 and cm1["lost"] == 10 and cm1["status"] == "lost"
    t = scoring.evaluate(m, {"include": {"nest": False}})["totals"]
    assert t["projected"] == 85 and t["passes"] and t["margin"] == 5
    t = scoring.evaluate(m, {"include": {"nest": False, "fyes": False}})["totals"]
    assert t["projected"] == 80 and t["margin"] == 0 and t["passes"] and t["skip_budget"] == 0
    assert t["skipped"] == 15 and t["ceiling"] == 95


def test_gating_and_hours_rules():
    m = scoring.normalize_model(HMAW)
    # all-or-nothing: 9/10 on a meeting counts nothing
    r = scoring.evaluate(m, {"values": {"cm2": 9}})
    cm2 = next(x for x in r["lines"] if x["id"] == "cm2")
    assert cm2["points"] == 0 and cm2["gated"] and r["totals"]["projected"] == 85
    # hours: zero until 10 h, then proportional up to 20 h
    def pts(hours):
        return next(x for x in scoring.evaluate(m, {"values": {"sde": hours}})["lines"] if x["id"] == "sde")["points"]
    assert pts(9.5) == 0 and pts(10) == 10 and pts(15) == 15 and pts(30) == 20
    # partial credit on an ungated line
    r = scoring.evaluate(m, {"values": {"reflect": 12}})
    assert r["totals"]["projected"] == 92 and r["totals"]["ceiling"] == 95
    with pytest.raises(InvalidInput):
        scoring.normalize_model({"buckets": [{"label": "x", "max": 5, "gating": "min_threshold"}]})
    with pytest.raises(InvalidInput):
        scoring.normalize_model({"buckets": [{"id": "a", "label": "x", "max": 5}, {"id": "a", "label": "y", "max": 1}]})


def test_component_store_state_undo_and_readback(hub, account):
    aid = account["id"]
    svc = hub.components
    v = svc.create(aid, "score_model", HMAW)
    assert v["version"] == 1 and v["computed"]["totals"]["projected"] == 95 and not v["can_undo"]
    v = svc.set_state(aid, v["id"], {"include": {"nest": False}}, expected_version=1)
    assert v["version"] == 2 and v["computed"]["totals"]["projected"] == 85 and v["can_undo"]
    with pytest.raises(Conflict):
        svc.set_state(aid, v["id"], {"include": {}}, expected_version=1)        # stale page
    v = svc.set_state(aid, v["id"], {"include": {"nest": False, "fyes": False}}, expected_version=2)
    d = svc.describe(aid, v["id"])
    assert d["totals"]["projected"] == 80 and d["totals"]["margin"] == 0
    assert {x["id"] for x in d["lines"] if not x["included"]} == {"nest", "fyes", "intro", "plan", "cm1"} - {"intro", "plan", "cm1"}
    v = svc.undo(aid, v["id"])
    assert v["computed"]["totals"]["projected"] == 85
    v = svc.undo(aid, v["id"])
    assert v["computed"]["totals"]["projected"] == 95
    # the assistant can revise the definition; state stays valid against the new buckets
    spec = json.loads(json.dumps(HMAW))
    spec["threshold"] = 90
    v = svc.update_spec(aid, v["id"], spec)
    assert v["computed"]["totals"]["passes"] and v["computed"]["totals"]["margin"] == 5
    assert svc.pin(aid, v["id"], True)["pinned"] and [x["id"] for x in svc.list(aid, pinned=True)] == [v["id"]]
    other = hub.identity.upsert_verified_account(account["instance_id"], {"canvas_user_id": "999"})
    with pytest.raises(NotFound):
        svc.get(other["id"], v["id"])                               # never across accounts


def test_checklist_links_to_local_todos(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    svc = hub.components
    item = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1", (aid,))
    v = svc.create(aid, "checklist", {"title": "Week 9", "sync_todos": True, "sections": [
        {"id": "a", "title": "Submit", "rows": [{"id": "r1", "label": "Proposal", "points": 10, "item_id": item["id"],
                                                  "due": "2026-11-03T23:59:00+08:00"},
                                                 {"id": "r2", "label": "Read chapter 4", "points": 0}]},
        {"id": "b", "title": "Prep", "rows": [{"id": "r3", "label": "Book a room", "done": True}]}]})
    assert v["computed"]["done_count"] == 1 and v["computed"]["next_due"]["id"] == "r1"
    v = svc.set_state(aid, v["id"], {"done": {"r1": True, "r3": True}})
    links = v["state"]["links"]
    assert set(links) == {"r1", "r2", "r3"}
    todo = hub.annotations.get(aid, links["r1"])
    assert todo["task_state"] == "done" and todo["item_id"] == item["id"] and todo["local_due"]
    assert hub.annotations.get(aid, links["r2"])["task_state"] == "open"
    # finishing the to-do elsewhere ticks the row; reopening it unticks
    hub.annotations.update(aid, links["r2"], task_state="done")
    hub.annotations.update(aid, links["r1"], task_state="open")
    v = svc.get(aid, v["id"])
    assert v["state"]["done"] == {"r2": True, "r3": True}
    assert v["computed"]["done_points"] == 0 and v["computed"]["done_count"] == 2


def test_tools_create_and_read_back(hub, account):
    ctx = ToolContext(api=HubAPI(hub), account_id=account["id"])
    text, err, display = run_tool_display(ctx, "show_score_model", {**HMAW, "pin": True}, {"read", "ui"})
    assert not err and display["component"] == "score_model"
    out = json.loads(text)
    cid = out["component_id"]
    assert "projected 95" in out["summary"] and "threshold 80" in out["summary"]
    hub.components.set_state(account["id"], cid, {"include": {"nest": False}})
    back = json.loads(run_tool_display(ctx, "get_component", {"component_id": cid}, {"read"})[0])
    assert back["totals"]["projected"] == 85 and next(x for x in back["lines"] if x["id"] == "nest")["included"] is False
    text, err, display = run_tool_display(ctx, "show_artifact", {"title": "Spec", "content": "# Spec\n\nline", "filename": "spec v1.md"},
                                          {"ui"})
    assert not err and display["filename"] == "spec v1.md" and "Copy and Download" in json.loads(text)["summary"]
    text, err, display = run_tool_display(ctx, "show_chart", {"labels": ["A", "B"], "kind": "stacked", "threshold": 10,
                                                              "series": [{"name": "Earned", "values": [3, 4]},
                                                                         {"name": "Lost", "values": [1, 0]}]}, {"ui"})
    assert not err and display["kind"] == "stacked" and len(display["series"]) == 2
    assert run_tool_display(ctx, "show_chart", {"labels": ["A", "B"], "series": [{"name": "x", "values": [1]}]}, {"ui"})[1]
    text, err, display = run_tool_display(ctx, "show_table", {"columns": ["Item", "Pts"], "rows": [["a", "3"], ["b", "4"]],
                                                              "totals": ["Pts"], "column_types": ["text", "number"]}, {"ui"})
    assert not err and display["totals"] == ["Pts"]


def test_course_score_lines_drafts_buckets(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    ctx = ToolContext(api=HubAPI(hub), account_id=account["id"])
    course = hub.db.one("SELECT course_canvas_id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1",
                        (account["id"],))["course_canvas_id"]
    out = json.loads(run_tool_display(ctx, "course_score_lines", {"course_id": course}, {"read"})[0])
    assert out["buckets"] and all(b["status"] in scoring.STATUSES and b["max"] > 0 for b in out["buckets"])
    scoring.normalize_model({"buckets": out["buckets"]})       # drafts are valid model input


def test_components_web_api(hub, account):
    from fastapi.testclient import TestClient

    from canvas_hub.web.app import create_app
    v = hub.components.create(account["id"], "score_model", HMAW)
    c = TestClient(create_app(HubAPI(hub), token="t" * 40, port=8730), base_url="http://127.0.0.1:8730")
    hdr = {"Authorization": "Bearer " + "t" * 40, "X-Canvas-Hub": "1"}
    base = f"/api/accounts/{account['id']}/components/{v['id']}"
    r = c.patch(base, json={"state": {"include": {"nest": False}}, "expected_version": 1}, headers=hdr)
    assert r.status_code == 200 and r.json()["computed"]["totals"]["projected"] == 85
    assert c.patch(base, json={"state": {}, "expected_version": 1}, headers=hdr).status_code == 409
    assert c.patch(base, json={"undo": True}, headers=hdr).json()["computed"]["totals"]["projected"] == 95
    assert c.patch(base, json={"pinned": True}, headers=hdr).json()["pinned"] is True
    assert [x["id"] for x in c.get(f"/api/accounts/{account['id']}/components?pinned=true", headers=hdr).json()["components"]] == [v["id"]]
    assert c.delete(base, headers=hdr).status_code == 200 and c.get(base, headers=hdr).status_code == 404


def test_concurrent_saves_conflict_instead_of_overwriting(hub, account):
    import threading
    aid = account["id"]
    v = hub.components.create(aid, "score_model", HMAW)
    results = []

    def save(bucket):
        try:
            hub.components.set_state(aid, v["id"], {"include": {bucket: False}}, expected_version=1)
            results.append("ok")
        except Conflict:
            results.append("conflict")
    ts = [threading.Thread(target=save, args=(b,)) for b in ("nest", "fyes")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == ["conflict", "ok"]
    assert hub.components.get(aid, v["id"])["version"] == 2
    # even a caller holding a stale row cannot write
    stale = hub.components._row(aid, v["id"]) | {"version": 1}
    with pytest.raises(Conflict):
        hub.components._save(aid, stale, json.loads(stale["spec_json"]), {}, "user")


def test_undo_puts_linked_todos_back(hub, account):
    aid = account["id"]
    svc = hub.components
    v = svc.create(aid, "checklist", {"title": "T", "sync_todos": True, "sections": [
        {"id": "s", "rows": [{"id": "r1", "label": "One"}, {"id": "r2", "label": "Two"}]}]})
    links = v["state"]["links"]                                       # created with the checklist
    v = svc.set_state(aid, v["id"], {"done": {"r1": True}})
    v = svc.set_state(aid, v["id"], {"done": {"r1": True, "r2": True}, "links": links})
    v = svc.undo(aid, v["id"])
    assert v["state"]["done"] == {"r1": True} and hub.annotations.get(aid, links["r2"])["task_state"] == "open"
    v = svc.undo(aid, v["id"])                                          # back to nothing ticked
    assert v["state"]["done"] == {} and hub.annotations.get(aid, links["r1"])["task_state"] == "open"
    svc.set_state(aid, v["id"], {"done": {"r2": True}, "links": v["state"]["links"]})
    tasks = [a for a in hub.annotations.list(aid) if a["kind"] == "task"]
    assert len(tasks) == 2                                              # no orphans, no duplicates


def test_saves_without_links_reuse_linked_todos(hub, account):
    # A page that sends a save while the first sync is in flight has no links yet; the server keeps its own.
    aid = account["id"]
    svc = hub.components
    v = svc.create(aid, "checklist", {"title": "T", "sync_todos": True, "sections": [
        {"id": "s", "rows": [{"id": "r1", "label": "One"}, {"id": "r2", "label": "Two"}]}]})
    first = svc.set_state(aid, v["id"], {"done": {"r1": True}})
    v = svc.set_state(aid, v["id"], {"done": {"r1": True, "r2": True}, "links": {}})
    assert v["state"]["links"] == first["state"]["links"]
    assert len([a for a in hub.annotations.list(aid) if a["kind"] == "task"]) == 2


def test_linked_checklists_sync_from_creation_and_stop_when_unlinked(hub, account):
    aid = account["id"]
    svc = hub.components
    spec = {"title": "T", "sync_todos": True, "sections": [{"id": "s", "rows": [{"id": "r1", "label": "One"},
                                                                                 {"id": "r2", "label": "Two"}]}]}
    v = svc.create(aid, "checklist", spec)
    links = v["state"]["links"]
    assert set(links) == {"r1", "r2"}                                   # in the to-do list before any tick
    assert {hub.annotations.get(aid, i)["task_state"] for i in links.values()} == {"open"}
    hub.annotations.update(aid, links["r1"], task_state="done")
    v = svc.update_spec(aid, v["id"], {**spec, "sync_todos": False})
    assert v["state"]["done"] == {"r1": True}                           # ticks kept as they were when unlinked
    v = svc.set_state(aid, v["id"], {"done": {}})
    assert v["state"]["done"] == {}                                      # no longer overridden by the to-do
    assert hub.annotations.get(aid, links["r1"])["task_state"] == "done"


def test_linked_checklist_tools_need_the_notes_permission(hub, account):
    ctx = ToolContext(api=HubAPI(hub), account_id=account["id"])
    spec = {"title": "T", "sync_todos": True, "sections": [{"rows": [{"label": "One"}]}]}
    text, err, _ = run_tool_display(ctx, "show_checklist", spec, {"read", "ui"})
    assert err and json.loads(text)["error"] == "notes_permission_required"
    assert not [a for a in hub.annotations.list(account["id"]) if a["kind"] == "task"]
    text, err, _ = run_tool_display(ctx, "show_checklist", spec, {"read", "ui", "notes"})
    cid = json.loads(text)["component_id"]
    text, err, _ = run_tool_display(ctx, "update_component", {"component_id": cid, "state": {"done": {"s.1": True}}},
                                    {"read", "ui"})
    assert err and json.loads(text)["error"] == "notes_permission_required"
    assert not run_tool_display(ctx, "update_component", {"component_id": cid, "pin": True}, {"read", "ui"})[1]
    assert not run_tool_display(ctx, "show_checklist", {**spec, "sync_todos": False}, {"read", "ui"})[1]


def test_undo_keeps_todos_finished_elsewhere(hub, account):
    aid = account["id"]
    svc = hub.components
    v = svc.create(aid, "checklist", {"title": "T", "sync_todos": True, "sections": [
        {"id": "s", "rows": [{"id": "r1", "label": "One"}, {"id": "r2", "label": "Two"}]}]})
    links = v["state"]["links"]
    hub.annotations.update(aid, links["r1"], task_state="done")         # finished under Notes & tasks
    v = svc.get(aid, v["id"])
    assert v["state"]["done"] == {"r1": True}
    v = svc.set_state(aid, v["id"], {**v["state"], "done": {"r1": True, "r2": True}})
    v = svc.undo(aid, v["id"])                                          # undoes only the r2 tick
    assert v["state"]["done"] == {"r1": True}
    assert hub.annotations.get(aid, links["r1"])["task_state"] == "done"
    assert hub.annotations.get(aid, links["r2"])["task_state"] == "open"


def test_stale_page_does_not_reopen_todos_finished_elsewhere(hub, account):
    aid = account["id"]
    svc = hub.components
    v = svc.create(aid, "checklist", {"title": "T", "sync_todos": True, "sections": [
        {"id": "s", "rows": [{"id": "r1", "label": "One"}, {"id": "r2", "label": "Two"}]}]})
    links, seen = v["state"]["links"], v["state"]
    hub.annotations.update(aid, links["r1"], task_state="done")         # finished under Notes & tasks
    v = svc.set_state(aid, v["id"], {**seen, "done": {"r2": True}}, base=seen)   # the page never saw that
    assert v["state"]["done"] == {"r1": True, "r2": True}
    assert hub.annotations.get(aid, links["r1"])["task_state"] == "done"
    # a row the page did change still wins
    v = svc.set_state(aid, v["id"], {**v["state"], "done": {"r2": True}}, base=v["state"])
    assert v["state"]["done"] == {"r2": True} and hub.annotations.get(aid, links["r1"])["task_state"] == "open"


def test_uncollected_submissions_stay_open(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    with hub.db.tx() as c:                                               # the submission was never collected
        c.execute("DELETE FROM items WHERE account_id=? AND item_type='submission'", (aid,))
        c.execute("""UPDATE item_fields SET value_json=? WHERE field='due_at' AND item_id IN
                     (SELECT id FROM items WHERE account_id=? AND item_type='assignment')""",
                  (json.dumps({"raw": "2026-01-01T00:00:00Z", "iso": "2026-01-01T00:00:00Z", "precision": "instant"}), aid))
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    b = json.loads(run_tool_display(ctx, "course_score_lines", {"course_id": "4242"}, {"read"})[0])["buckets"][0]
    assert b["status"] == "pending" and "not collected" in b["note"]


def test_hidden_grades_stay_open(hub, account, fixture_pkg):
    # Graded on paper, score not released, no online submission: unknown, not lost.
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    sub = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='submission'", (aid,))["id"]
    with hub.db.tx() as c:
        for field, value in (("workflow_state", "graded"), ("score", None), ("submitted_at", None), ("missing", False)):
            c.execute("UPDATE item_fields SET value_json=? WHERE item_id=? AND field=?", (json.dumps(value), sub, field))
        c.execute("""UPDATE item_fields SET value_json=? WHERE field='due_at' AND item_id IN
                     (SELECT id FROM items WHERE account_id=? AND item_type='assignment')""",
                  (json.dumps({"raw": "2026-01-01T00:00:00Z", "iso": "2026-01-01T00:00:00Z", "precision": "instant"}), aid))
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    b = json.loads(run_tool_display(ctx, "course_score_lines", {"course_id": "4242"}, {"read"})[0])["buckets"][0]
    assert b["status"] == "pending" and "not visible" in b["note"]


def test_renamed_or_rescheduled_rows_update_their_todos(hub, account):
    aid = account["id"]
    svc = hub.components
    spec = {"title": "T", "sync_todos": True, "sections": [{"id": "s", "rows": [
        {"id": "r1", "label": "Draft", "due": "2026-11-03T23:59:00+08:00"}, {"id": "r2", "label": "Read"}]}]}
    v = svc.create(aid, "checklist", spec)
    links = v["state"]["links"]
    hub.annotations.update(aid, links["r2"], title="Read ch. 4 (my words)")      # the user's own edit
    spec["sections"][0]["rows"][0].update(label="Final draft", due="2026-11-05T23:59:00+08:00")
    v = svc.update_spec(aid, v["id"], spec)
    t1, t2 = hub.annotations.get(aid, links["r1"]), hub.annotations.get(aid, links["r2"])
    assert t1["title"] == "Final draft" and t1["local_due"].startswith("2026-11-05")
    assert t2["title"] == "Read ch. 4 (my words)"                              # unchanged rows leave it alone
    svc.undo(aid, v["id"])                                                       # undo puts the old ones back
    t1 = hub.annotations.get(aid, links["r1"])
    assert t1["title"] == "Draft" and t1["local_due"].startswith("2026-11-03")
