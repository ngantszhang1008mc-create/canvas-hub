"""Archive visibility, shared saved views, permissions and browser workflows."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from canvas_hub.agent_tools import ToolContext, run_tool, tools_for, course_dashboard
from canvas_hub.assistant import AssistantService
from canvas_hub.boundary import HubAPI
from canvas_hub.services import backup
from canvas_hub.util import InvalidInput, NotFound
from canvas_hub.web.app import create_app

from .conftest import variant
from .fake_llm import FakeLLM
from .test_planner_requests import _future_fixture


@pytest.fixture
def deadlines(hub, account, fixture_pkg):
    pkg, due = _future_fixture(fixture_pkg)
    hub.importer.import_package(pkg)
    api = HubAPI(hub)
    iid = hub.db.one("SELECT id FROM items WHERE item_type='assignment' AND canvas_id='7001'")["id"]
    return api, account["id"], iid, due, pkg


def test_archive_hides_every_active_deadline_surface_and_restores(deadlines):
    api, aid, iid, due, pkg = deadlines
    hub = api.hub
    window = {"start": (due-timedelta(days=7)).isoformat(), "end": (due+timedelta(days=7)).isoformat()}
    task = hub.annotations.create(aid, kind="task", title="Prepare proposal", item_id=iid, local_due=due.isoformat())
    hub.reminders.create(aid, title="Proposal reminder", item_id=iid,
                         remind_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
    assert iid in [d["item_id"] for d in api.deadlines(aid, **window)["deadlines"]]
    before = course_dashboard(ToolContext(api=api, account_id=aid), aid)["courses"]
    api.archive_deadline(aid, iid, True)
    api.archive_deadline(aid, iid, True)  # idempotent
    assert not any(d["item_id"] == iid for d in api.deadlines(aid, **window)["deadlines"])
    archived = api.deadlines(aid, **window, archive="archived")["deadlines"]
    assert [d["item_id"] for d in archived] == [iid] and archived[0]["archived"]
    assert api.archived_deadlines(aid)["deadlines"][0]["item_id"] == iid
    assert api.search(aid, "proposal")["results"] == []
    assert iid not in [i["id"] for i in api.list_items(aid)["items"]]
    assert hub.annotations.list(aid, kind="task") == []
    assert api.deadlines(aid, **window)["local_tasks_with_dates"] == []
    assert hub.reminders.list(aid) == [] and hub.reminders.due(aid) == []
    week = hub.planner.week(aid, week_of=due.date().isoformat(), suggestions=True)
    assert not any(e.get("item_id") == iid for d in week["days"] for e in d["events"])
    assert "Project proposal" not in hub.planner.ics(aid)
    after = course_dashboard(ToolContext(api=api, account_id=aid), aid)["courses"]
    assert [(c["assignments"], c["grade_percent"]) for c in before] == [(c["assignments"], c["grade_percent"]) for c in after]
    assert all(c["next_due"] is None or c["next_due"]["item_id"] != iid for c in after)
    assert api.read_item(aid, iid)["deadline_archived"]
    hub.importer.import_package(variant(pkg, "after-archive", "2026-11-01T00:00:00Z"))
    assert api.deadlines(aid, **window, archive="archived")["deadlines"][0]["item_id"] == iid
    hub.reopen()
    assert api.archived_deadlines(aid)["deadlines"]
    api.archive_deadline(aid, iid, False)
    assert iid in [d["item_id"] for d in api.deadlines(aid, **window)["deadlines"]]
    assert hub.annotations.list(aid, kind="task")[0]["id"] == task["id"]
    assert hub.reminders.due(aid)[0]["title"] == "Proposal reminder"


def test_archive_covers_future_quiz_calendar_mirrors_and_account_isolation(deadlines):
    api, aid, iid, due, pkg = deadlines
    hub = api.hub
    api.archive_deadline(aid, iid, True)
    newer = variant(pkg, "mirrors", "2026-11-01T00:00:00Z")
    original = next(i for i in newer["items"] if i["type"] == "assignment" and i["canvas_id"] == "7001")
    for kind, cid in [("quiz", "8001"), ("calendar_event", "9001")]:
        mirror = copy.deepcopy(original)
        mirror.update(type=kind, canvas_id=cid, title="Mirrored proposal", observation_id="mirror-"+cid)
        mirror["fields"]["assignment_id"] = "7001"
        if kind == "calendar_event":
            mirror["fields"]["start_at"] = mirror["fields"].pop("due_at")
        newer["items"].append(mirror)
    hub.importer.import_package(newer)
    window = {"start": (due-timedelta(days=1)).isoformat(), "end": (due+timedelta(days=1)).isoformat()}
    assert api.deadlines(aid, **window)["deadlines"] == []
    assert len(api.archived_deadlines(aid)["deadlines"]) == 1
    other = hub.identity.upsert_verified_account(1, {"canvas_user_id": "999"})["id"]
    with pytest.raises(NotFound):
        api.archive_deadline(other, iid, False)
    assert api.archived_deadlines(other)["deadlines"] == []
    mirror_id = hub.db.one("SELECT id FROM items WHERE item_type='quiz'")["id"]
    api.archive_deadline(aid, mirror_id, False)
    assert len(api.deadlines(aid, **window)["deadlines"]) == 1
    hub.annotations.create(aid, kind="task", item_id=iid, title="Done locally", task_state="done")
    assert api.deadlines(aid, **window, status="unfinished")["deadlines"] == []
    assert [d["item_id"] for d in api.deadlines(aid, **window, status="completed")["deadlines"]] == [iid]
    assert api.deadlines(aid, **window, status="completed")["unresolved_dates"] == []


def test_views_and_archive_share_ui_and_assistant_boundary(deadlines):
    api, aid, iid, _, _ = deadlines
    svc = AssistantService(api.hub, api)
    assert svc.public_config()["permissions"]["deadlines"] is True
    ctx = ToolContext(api=api, account_id=aid)
    perms = svc.permissions(svc._load())
    out, err = run_tool(ctx, "save_deadline_tab", {"name": "Unfinished", "filters": {"range": "all", "status": "unfinished"}}, perms)
    assert not err
    tid = json.loads(out)["active_tab"]
    client = TestClient(create_app(api, token="t"*40, port=8730), base_url="http://127.0.0.1:8730")
    hdr = {"Authorization": "Bearer " + "t"*40, "X-Canvas-Hub": "1"}
    path = f"/api/accounts/{aid}"
    assert client.get(path+"/deadline-views", headers=hdr).json()["active_tab"] == tid
    assert client.post(path+"/deadline-views", headers=hdr, json={"id": tid, "name": "My work", "filters": {"course_id": "4242"}}).status_code == 200
    assert api.deadline_views(aid)["tabs"][-1]["filters"]["status"] == "unfinished"
    svc.update_config({"permissions": {"deadlines": False}})
    denied = svc.permissions(svc._load())
    assert "archive_deadline" not in {t.name for t in tools_for(denied)}
    assert run_tool(ctx, "archive_deadline", {"item_id": iid, "archived": True}, denied)[1]
    assert api.archived_deadlines(aid)["deadlines"] == []
    # The user's own controls remain available when assistant access is off.
    assert client.patch(path+f"/deadlines/{iid}/archive", headers=hdr, json={"archived": True}).status_code == 200
    assert client.get(path+"/deadlines/archived", headers=hdr).json()["deadlines"][0]["item_id"] == iid
    assert client.patch(path+f"/deadlines/{iid}/archive", headers=hdr, json={"archived": "false"}).status_code == 400
    for bad in [{"filters": {"range": "nope"}}, {"filters": {"extra": True}}, {"filters": {"include_calendar": "yes"}}, {"filters": {"course_id": "other"}}]:
        with pytest.raises(InvalidInput):
            api.save_deadline_tab(aid, {"id": tid, **bad})
    api.select_deadline_tab(aid, "7")
    api.delete_deadline_tab(aid, tid)
    assert api.deadline_views(aid)["active_tab"] == "7"
    for t in api.deadline_views(aid)["tabs"][1:]:
        api.delete_deadline_tab(aid, t["id"])
    with pytest.raises(InvalidInput):
        api.delete_deadline_tab(aid, "7")


def test_in_app_assistant_manages_deadlines_by_default(deadlines):
    api, aid, iid, _, _ = deadlines
    fake = FakeLLM([("archive_deadline", {"item_id": iid, "archived": True}),
                    ("save_deadline_tab", {"id": "14", "name": "My deadlines", "filters": {"status": "unfinished"}})])
    try:
        svc = AssistantService(api.hub, api)
        svc.update_config({"preset": "custom", "base_url": fake.url+"/v1", "model": "m"})
        r = svc.send(aid, "Archive the proposal and filter my deadlines to unfinished")
        svc.wait(r["conversation_id"], 30)
        assert svc.get_conversation(aid, r["conversation_id"])["conversation"]["run_state"] == "idle"
        assert api.archived_deadlines(aid)["deadlines"][0]["item_id"] == iid
        tab = next(t for t in api.deadline_views(aid)["tabs"] if t["id"] == "14")
        assert tab["name"] == "My deadlines" and tab["filters"]["status"] == "unfinished"
    finally:
        fake.stop()


def test_revoking_permission_during_a_run_blocks_the_next_mutation(deadlines, monkeypatch):
    from canvas_hub.assistant.providers import ToolCall, Turn
    api, aid, iid, _, _ = deadlines
    svc = AssistantService(api.hub, api)
    svc.update_config({"preset": "custom", "base_url": "http://127.0.0.1:1/v1", "model": "m"})

    class Provider:
        def __init__(self):
            self.calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                svc.update_config({"permissions": {"deadlines": False}})
                return Turn("", [ToolCall("archive", "archive_deadline", {"item_id": iid, "archived": True})], stop="tool_use")
            return Turn("Done")

    monkeypatch.setattr("canvas_hub.assistant.make_provider", lambda cfg: Provider())
    r = svc.send(aid, "Archive this deadline")
    svc.wait(r["conversation_id"], 10)
    conv = svc.get_conversation(aid, r["conversation_id"])
    assert conv["conversation"]["run_state"] == "idle"
    assert conv["messages"][2]["results"][0]["is_error"]
    assert api.archived_deadlines(aid)["deadlines"] == []


def test_archives_and_views_survive_backup_restore_and_query_views_hide_archives(deadlines, tmp_path):
    from canvas_hub.services.querylayer import run_query
    api, aid, iid, _, _ = deadlines
    api.archive_deadline(aid, iid, True)
    api.save_deadline_tab(aid, {"id": "14", "name": "Saved", "filters": {"status": "unfinished"}})
    result = run_query(api.hub.db.path, aid, f"SELECT id FROM items WHERE id={iid}")
    assert result["rows"] == []
    path = backup.create_backup(api.hub, tmp_path/"before.zip")
    api.archive_deadline(aid, iid, False)
    api.save_deadline_tab(aid, {"id": "14", "name": "Changed"})
    backup.restore_backup(api.hub, path)
    assert api.archived_deadlines(aid)["deadlines"][0]["item_id"] == iid
    assert next(t for t in api.deadline_views(aid)["tabs"] if t["id"] == "14")["name"] == "Saved"


def test_saved_assistant_deadline_cards_hide_archived_items(deadlines):
    api, aid, iid, _, _ = deadlines
    svc = AssistantService(api.hub, api)
    with api.hub.db.tx() as c:
        cid = c.execute("INSERT INTO assistant_conversations(account_id,created_at,updated_at) VALUES (?,?,?)",
                        (aid, "2026-10-31T00:00:00Z", "2026-10-31T00:00:00Z")).lastrowid
    display = {"component": "deadlines", "deadlines": [{"item_id": iid, "title": "Project proposal"}]}
    svc._append(cid, "tool", {"results": [{"id": "show", "name": "show_deadlines", "content": "{}", "display": display}]})
    assert svc.get_conversation(aid, cid)["messages"][0]["results"][0]["display"]["deadlines"]
    api.archive_deadline(aid, iid, True)
    assert svc.get_conversation(aid, cid)["messages"][0]["results"][0]["display"]["deadlines"] == []
    api.archive_deadline(aid, iid, False)
    assert svc.get_conversation(aid, cid)["messages"][0]["results"][0]["display"]["deadlines"]


@pytest.mark.browser
def test_browser_archives_restores_and_remembers_edited_tabs(deadlines):
    from playwright.sync_api import sync_playwright, expect
    from .test_components_ui import serve
    api, aid, iid, _, _ = deadlines
    with serve(api.hub, []) as (base, tok, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={tok}#/deadlines")
        card = page.locator(".deadline", has_text="Project proposal")
        expect(card).to_be_visible()
        card.get_by_role("button", name="Archive", exact=True).click()
        expect(card).to_have_count(0)
        page.get_by_label("Show archived", exact=True).check()
        expect(card).to_be_visible()
        expect(card.get_by_text("Archived", exact=True)).to_be_visible()
        card.get_by_role("button", name="Restore", exact=True).click()
        expect(card.get_by_role("button", name="Archive", exact=True)).to_be_visible()
        page.get_by_text("Edit deadline tabs", exact=True).click()
        page.get_by_label("Deadline tab name").fill("My saved view")
        page.get_by_role("button", name="Save as new tab").click()
        expect(page.get_by_role("tab", name="My saved view", exact=True)).to_have_attribute("aria-selected", "true")
        with page.expect_response(lambda r: r.url.endswith("/deadline-views") and r.request.method == "POST"):
            page.get_by_label("Deadline status", exact=True).select_option("unfinished")
        page.reload()
        expect(page.get_by_label("Deadline status", exact=True)).to_have_value("unfinished")
        expect(page.get_by_role("tab", name="My saved view", exact=True)).to_have_attribute("aria-selected", "true")
        with page.expect_response(lambda r: r.url.endswith("/deadline-views") and r.request.method == "POST"):
            page.get_by_label("Show archived", exact=True).uncheck()
        card.get_by_role("button", name="Archive", exact=True).click()
        expect(card).to_have_count(0)
        page.get_by_role("link", name="Archive list", exact=True).click()
        expect(page.get_by_role("heading", name="Archived deadlines")).to_be_visible()
        page.get_by_role("button", name="Restore", exact=True).click()
        expect(page.get_by_text("No archived deadlines", exact=True)).to_be_visible()
        assert errors == []
        browser.close()
