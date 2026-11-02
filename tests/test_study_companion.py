"""Linked components and explicit, source-based assistant handoffs."""
import re

import pytest
from fastapi.testclient import TestClient

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput, NotFound
from canvas_hub.web.app import create_app
from .test_components import HMAW

CHECKLIST = {"title": "Proposal steps", "sections": [{"id": "steps", "rows": [{"id": "draft", "label": "Draft proposal"}]}]}


@pytest.fixture
def study(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    item = hub.db.one("SELECT id, course_canvas_id FROM items WHERE item_type='assignment' AND canvas_id='7001'")
    return HubAPI(hub), account["id"], item["id"], item["course_canvas_id"]


@pytest.mark.parametrize("kind,spec", [
    ("checklist", CHECKLIST), ("score_model", HMAW),
    ("grade_projector", {"title": "Grade", "categories": [{"id": "a", "name": "Work", "weight": 100,
                                                          "items": [{"id": "x", "label": "Essay", "max": 10}]}]}),
    ("hours_tracker", {"title": "Service", "target": 20}),
])
def test_all_component_kinds_link_and_filter(study, kind, spec):
    api, aid, iid, course = study
    svc = api.hub.components
    old = svc.create(aid, kind, spec)
    linked = svc.create(aid, kind, spec, item_id=iid)
    course_only = svc.create(aid, kind, spec, course_id=course)
    assert linked["course_id"] == course and linked["item_id"] == iid
    assert old["course_id"] is None and old["item_id"] is None
    assert [v["id"] for v in api.list_components(aid, item_id=iid)["components"]] == [linked["id"]]
    assert {v["id"] for v in api.list_components(aid, course_id=course)["components"]} == {linked["id"], course_only["id"]}
    svc.update_spec(aid, linked["id"], {**spec, "title": "Updated"})
    restored = svc.undo(aid, linked["id"])
    assert restored["item_id"] == iid and restored["course_id"] == course


def test_links_reject_other_account_missing_and_unrelated_course(study):
    api, aid, iid, course = study
    other = api.hub.identity.upsert_verified_account(api.get_account(aid)["instance_id"],
        {"canvas_user_id": "other", "display_name": "Other", "time_zone": "UTC"})["id"]
    svc = api.hub.components
    for kwargs in [{"item_id": iid}, {"course_id": course}]:
        with pytest.raises(NotFound):
            svc.create(other, "checklist", CHECKLIST, **kwargs)
        with pytest.raises(NotFound):
            api.list_components(other, **kwargs)
        with pytest.raises(NotFound):
            api.study_context(other, **kwargs)
    with pytest.raises(InvalidInput):
        svc.create(aid, "checklist", CHECKLIST, item_id=iid, course_id="unrelated")
    with pytest.raises(NotFound):
        svc.create(aid, "checklist", CHECKLIST, item_id=999999)
    with pytest.raises(NotFound):
        svc.create(aid, "checklist", CHECKLIST, conversation_id=999999)
    assert not svc.list(other)


def test_tool_links_and_http_filter(study):
    api, aid, iid, course = study
    result, error, display = run_tool_display(ToolContext(api=api, account_id=aid), "show_checklist",
                                      {**CHECKLIST, "item_id": iid}, {"read", "ui"})
    assert not error, result
    assert display["view"]["item_id"] == iid
    client = TestClient(create_app(api, token="t" * 40, port=8730), base_url="http://127.0.0.1:8730")
    headers = {"Authorization": "Bearer " + "t" * 40}
    response = client.get(f"/api/accounts/{aid}/components?item_id={iid}", headers=headers)
    assert response.status_code == 200 and response.json()["components"][0]["course_id"] == course
    assert client.get(f"/api/accounts/{aid}/study-context?item_id={iid}", headers=headers).status_code == 200
    assert client.get(f"/api/accounts/{aid}/study-context?item_id=999999", headers=headers).status_code == 404


def test_context_includes_evidence_and_limits_without_ai(study, monkeypatch):
    api, aid, iid, course = study
    original = api.read_item
    def with_attachment(a, i):
        item = original(a, i)
        if i == iid:
            item["resources"] = [{"resource_id": "a" * 64, "filename": "rubric.pdf", "observed_at": "2026-01-01",
                                  "extraction_status": "ok"}]
        return item
    monkeypatch.setattr(api, "read_item", with_attachment)
    monkeypatch.setattr(api, "read_resource_text", lambda *a, **k: {"text": "Include three citations", "next_offset": 3500})
    before = api.hub.db.one("SELECT COUNT(*) n FROM assistant_conversations")["n"]
    request = api.study_context(aid, item_id=iid)
    assert "Include three citations" in request["prompt"]
    assert f"item_id={iid}" in request["prompt"] and "show_checklist" in request["prompt"]
    assert "truncated" in " ".join(request["warnings"])
    assert len(request["prompt"]) < 20000
    assert api.hub.db.one("SELECT COUNT(*) n FROM assistant_conversations")["n"] == before
    course_request = api.study_context(aid, course_id=course)
    assert "show_score_model" in course_request["prompt"]
    assert "stale" in " ".join(course_request["warnings"])


@pytest.mark.browser
@pytest.mark.parametrize("width", [1280, 380])
def test_linked_checklist_persists_across_resume_and_submission_stays_separate(study, width):
    from playwright.sync_api import expect, sync_playwright
    from .test_components_ui import serve
    api, aid, iid, course = study
    v = api.hub.components.create(aid, "checklist", CHECKLIST, item_id=iid)
    with serve(api.hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 844})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={token}#/item/{iid}")
        component = page.locator(".saved-cmp").filter(has_text="Proposal steps")
        expect(component.get_by_text("Proposal steps", exact=True)).to_be_visible()
        component.get_by_role("checkbox").first.check()
        expect(component.locator(".save-state")).to_contain_text("Saved")
        page.get_by_role("button", name="Keep working here", exact=True).click()
        expect(page.get_by_text("Saved for Continue", exact=True)).to_be_visible()
        page.goto(f"{base}/#/overview")
        expect(page.get_by_role("heading",name="Overall",exact=True)).to_be_visible()
        page.locator(".workspace-resume").click()
        expect(page.locator(".saved-cmp").get_by_role("checkbox").first).to_be_checked()
        assert api.hub.workspace.get(aid)["active"]["item_id"] == iid
        assert api.hub.components.get(aid,v["id"])["state"]["done"]["draft"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()


def test_conversation_ownership_and_deletion_preserve_links(study):
    api, aid, iid, course = study
    with api.hub.db.tx() as c:
        cid = c.execute("INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?, 'Study', 'now', 'now')", (aid,)).lastrowid
    v = api.hub.components.create(aid, "checklist", CHECKLIST, item_id=iid, conversation_id=cid)
    other = api.hub.identity.upsert_verified_account(api.get_account(aid)["instance_id"],
        {"canvas_user_id": "different", "display_name": "Other", "time_zone": "UTC"})["id"]
    with pytest.raises(NotFound):
        api.hub.components.create(other, "checklist", CHECKLIST, conversation_id=cid)
    with api.hub.db.tx() as c:
        c.execute("DELETE FROM assistant_conversations WHERE id=?", (cid,))
    retained = api.hub.components.get(aid, v["id"])
    assert retained["conversation_id"] is None and retained["item_id"] == iid


def test_migration_preserves_existing_unlinked_component(tmp_path, monkeypatch):
    import json
    from canvas_hub import db
    from canvas_hub.paths import get_paths
    from canvas_hub.services import Hub
    files = db._migration_files()
    with monkeypatch.context() as m:
        m.setattr(db, "_migration_files", lambda: [f for f in files if f[0] < 9])
        old = Hub(get_paths(tmp_path / "old"))
    try:
        inst = old.identity.add_instance("https://canvas.example.edu")
        aid = old.identity.upsert_verified_account(inst["id"], {"canvas_user_id": "legacy", "display_name": "Legacy", "time_zone": "UTC"})["id"]
        with old.db.tx() as c:
            cid = c.execute("""INSERT INTO components(account_id,kind,title,spec_json,state_json,created_at,updated_at)
                               VALUES (?, 'checklist', 'Proposal steps', ?, ?, 'now', 'now')""",
                            (aid, json.dumps(old.components.normalize("checklist", CHECKLIST)), json.dumps({"done": {"draft": True}}))).lastrowid
        assert db.migrate(old.db.conn) == [f[0] for f in files if f[0] >= 9]
        result = old.components.get(aid, cid)
        assert result["item_id"] is None and result["course_id"] is None
        assert result["state"]["done"]["draft"] is True
        assert db.migrate(old.db.conn) == []
    finally:
        old.close()
