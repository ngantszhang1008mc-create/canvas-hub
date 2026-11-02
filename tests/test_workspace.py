"""Persistent active work and provider handoff, using only in-process fake models."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.providers import ToolCall, Turn
from canvas_hub.boundary import HubAPI
from canvas_hub.services.workspace import WorkspaceService
from canvas_hub.util import InvalidInput, NotFound

CHECKLIST = {"title": "Proposal steps", "sections": [{"id": "steps", "rows": [
    {"id": "draft", "label": "Draft proposal"}, {"id": "review", "label": "Review rubric"}]}]}


class FakeProvider:
    def __init__(self, *, calls=None, entered=None, release=None):
        self.calls = list(calls or [])
        self.entered, self.release = entered, release
        self.requests = []
        self.closed = False

    def complete(self, system, transcript, tools, on_text=None):
        self.requests.append({"system": system, "transcript": transcript, "tools": tools})
        if self.entered:
            self.entered.set()
        if self.release:
            assert self.release.wait(5), "test did not release fake model"
        if on_text:
            on_text("Saved local answer")
        if self.calls:
            name, args = self.calls.pop(0)
            return Turn("Checking stored sources", [ToolCall("fake-call", name, args)], "tool_use")
        return Turn("Saved local answer", model="fake-model")

    def close(self):
        self.closed = True


@pytest.fixture
def workspace(hub, account, fixture_pkg, monkeypatch):
    hub.importer.import_package(fixture_pkg)
    api = HubAPI(hub)
    svc = WorkspaceService(hub)
    assistant = AssistantService(hub, api)
    configured = assistant.update_config({"preset": "custom", "name": "Test provider", "base_url": "http://localhost:1234/v1",
                                          "model": "fake-model", "api_key": "fake-key"})
    item = hub.db.one("SELECT id,course_canvas_id FROM items WHERE item_type='assignment' AND canvas_id='7001'")
    other_item = hub.db.one("SELECT id FROM items WHERE item_type IN ('assignment','quiz','calendar_event') AND id<>?", (item["id"],))
    if other_item is None:
        with hub.db.tx() as c:
            c.execute("""INSERT INTO items(account_id,item_type,source_key,canvas_id,course_canvas_id,title,source_url,
                         first_observed_at,last_observed_at) SELECT account_id,item_type,'workspace-second','workspace-second',
                         course_canvas_id,'Second assignment',source_url,first_observed_at,last_observed_at FROM items WHERE id=?""", (item["id"],))
        other_item = hub.db.one("SELECT id FROM items WHERE source_key='workspace-second'")
    fake = FakeProvider()
    factories = []
    def make(cfg):
        factories.append(json.loads(json.dumps(cfg)))
        return fake
    monkeypatch.setattr("canvas_hub.assistant.make_provider", make)
    state = {"hub": hub, "api": api, "svc": svc, "assistant": assistant, "aid": account["id"], "iid": item["id"],
             "course": item["course_canvas_id"], "other_iid": other_item["id"], "profile": configured["saved_profile"],
             "fake": fake, "factories": factories}
    yield state
    if fake.release:
        fake.release.set()
    for cid in list(assistant._threads):
        assistant.stop(account["id"], cid)
        assistant.wait(cid, 5)


def prepare(w, **data):
    return w["svc"].prepare(w["api"], w["assistant"], w["aid"], {"action": "breakdown", "item_id": w["iid"], **data})


def start(w, **data):
    payload = {"action": "breakdown", "item_id": w["iid"], **data}
    prepared = w["svc"].prepare(w["api"], w["assistant"], w["aid"], payload)
    result = w["svc"].start(w["api"], w["assistant"], w["aid"], {
        **payload, "disclosure_fingerprint": prepared["disclosure"]["fingerprint"], "acknowledge": True})
    return result


def test_active_work_restores_selection_and_explicit_switch(workspace):
    w = workspace
    svc, aid = w["svc"], w["aid"]
    assert svc.get(aid) == {"active": None}
    checklist = w["hub"].components.create(aid, "checklist", CHECKLIST, item_id=w["iid"])
    saved = svc.save(aid, {"item_id": w["iid"], "component_id": checklist["id"], "step_id": "draft",
                           "next_action": "Write the first paragraph", "view": "assistant"})["active"]
    assert saved["item"]["id"] == w["iid"] and saved["component"]["id"] == checklist["id"]
    restored = WorkspaceService(w["hub"]).get(aid)["active"]
    assert restored["step_id"] == "draft" and restored["next_action"] == "Write the first paragraph"
    assert svc.save(aid, {"view": "requirements"})["active"]["step_id"] == "draft"
    switched = svc.save(aid, {"item_id": w["other_iid"]})["active"]
    assert switched["component_id"] is None and switched["step_id"] is None and switched["next_action"] == ""
    assert switched["conversation_id"] != saved["conversation_id"]
    assert svc.save(aid, {"item_id": w["iid"]})["active"]["conversation_id"] == saved["conversation_id"]
    assert svc.save(aid, {"clear": True}) == {"active": None}
    assert svc.get(aid) == {"active": None}


def test_active_component_switch_and_delete_clear_row(workspace):
    w = workspace
    a = w["hub"].components.create(w["aid"], "checklist", CHECKLIST, item_id=w["iid"])
    b = w["hub"].components.create(w["aid"], "checklist", CHECKLIST, item_id=w["iid"])
    w["svc"].save(w["aid"], {"item_id": w["iid"], "component_id": a["id"], "step_id": "draft"})
    assert w["svc"].save(w["aid"], {"component_id": b["id"]})["active"]["step_id"] is None
    w["svc"].save(w["aid"], {"step_id": "review"})
    with w["hub"].db.tx() as c:
        c.execute("DELETE FROM components WHERE id=?", (b["id"],))
    assert w["svc"].get(w["aid"])["active"]["step_id"] is None
    assert w["svc"].save(w["aid"], {"next_action": "Continue"})["active"]["step_id"] is None


def test_active_row_removed_by_spec_edit_can_still_resume(workspace):
    w = workspace
    component = w["hub"].components.create(w["aid"], "checklist", CHECKLIST, item_id=w["iid"])
    w["svc"].save(w["aid"], {"item_id": w["iid"], "component_id": component["id"], "step_id": "draft"})
    w["hub"].components.update_spec(w["aid"], component["id"], {"title": "Changed checklist", "sections": [{"id": "steps", "rows": [
        {"id": "review", "label": "Review rubric"}]}]})
    assert w["svc"].get(w["aid"])["active"]["step_id"] is None
    assert w["svc"].save(w["aid"], {"next_action": "Review"})["active"]["step_id"] is None


def test_active_work_validates_component_row_conversation_and_fields(workspace):
    w = workspace
    other = w["hub"].components.create(w["aid"], "checklist", CHECKLIST, item_id=w["other_iid"])
    conversation = w["svc"].conversation(w["aid"], item_id=w["other_iid"])
    for data in ({"component_id": other["id"]}, {"step_id": "draft"}, {"conversation_id": conversation["id"]},
                 {"view": "arbitrary"}, {"next_action": 12}, {"item_id": True}, {"clear": "yes"}, {"injected": True}):
        with pytest.raises(InvalidInput):
            w["svc"].save(w["aid"], {"item_id": w["iid"], **data})
    own = w["hub"].components.create(w["aid"], "checklist", CHECKLIST, item_id=w["iid"])
    with pytest.raises(InvalidInput):
        w["svc"].save(w["aid"], {"item_id": w["iid"], "component_id": own["id"], "step_id": "missing"})


def test_scopes_reuse_separate_conversations_and_are_immutable(workspace):
    w = workspace
    assignment = w["svc"].conversation(w["aid"], item_id=w["iid"])
    course = w["svc"].conversation(w["aid"], course_id=w["course"])
    general = w["svc"].conversation(w["aid"])
    assert len({assignment["id"], course["id"], general["id"]}) == 3
    assert w["svc"].conversation(w["aid"], item_id=w["iid"])["id"] == assignment["id"]
    assert w["svc"].conversation(w["aid"], course_id=w["course"])["id"] == course["id"]
    assert w["svc"].conversation(w["aid"])["id"] == general["id"]
    assert assignment["scope_course_id"] == course["scope_course_id"] == w["course"]
    assert course["scope_item_id"] is None and general["scope_course_id"] is None
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), w["hub"].db.tx() as c:
        c.execute("UPDATE assistant_conversations SET scope_item_id=? WHERE id=?", (w["other_iid"], assignment["id"]))
    listed = {c["id"]: c for c in w["assistant"].list_conversations(w["aid"])}
    assert listed[assignment["id"]]["scope_item_id"] == w["iid"]


def test_scope_creation_concurrent_reuses_one_row(workspace):
    w = workspace
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: w["svc"].conversation(w["aid"], item_id=w["iid"])["id"], range(8)))
    assert len(set(ids)) == 1


def test_account_isolation_rejects_scope_active_work_and_conversation_access(workspace):
    w = workspace
    acct = w["hub"].identity.get_account(w["aid"])
    other = w["hub"].identity.upsert_verified_account(acct["instance_id"], {"canvas_user_id": "workspace-other"})["id"]
    conv = w["svc"].conversation(w["aid"], item_id=w["iid"])
    assert w["svc"].get(other) == {"active": None}
    for operation in (lambda: w["svc"].conversation(other, item_id=w["iid"]),
                      lambda: w["svc"].conversation(other, course_id=w["course"]),
                      lambda: w["svc"].save(other, {"item_id": w["iid"]}),
                      lambda: w["assistant"].get_conversation(other, conv["id"]),
                      lambda: w["assistant"].stop(other, conv["id"]),
                      lambda: w["assistant"].send(other, "Question", conversation_id=conv["id"])):
        with pytest.raises(NotFound):
            operation()
    with pytest.raises(sqlite3.IntegrityError, match="belong"), w["hub"].db.tx() as c:
        c.execute("""INSERT INTO assistant_conversations(account_id,title,created_at,updated_at,scope_course_id,scope_item_id)
                     VALUES (?,'bad','now','now',?,?)""", (other, w["course"], w["iid"]))
    assert not w["factories"]


def test_prepare_has_reviewable_sources_and_never_calls_provider(workspace):
    w = workspace
    prepared = prepare(w)
    assert prepared["scope"] == {"course_id": w["course"], "item_id": w["iid"]}
    assert prepared["context"]["sources"] and "show_checklist" in prepared["prompt"]
    assert "purpose_key" in prepared["prompt"] and "sources.review_sources" in prepared["prompt"]
    assert not prepared["disclosure"]["acknowledged"]
    assert prepared["disclosure"]["endpoint"] == "http://localhost:1234/v1"
    assert prepared["disclosure"]["profile_id"] == w["profile"]
    assert "fake-key" not in json.dumps(prepared)
    assert not w["factories"]
    ask = prepare(w, action="ask", text="What should I do first?")
    assert ask["conversation_id"] == prepared["conversation_id"]
    assert ask["prompt"].startswith("What should I do first?") and "Stored source excerpts" in ask["prompt"]
    grading = prepare(w, action="grading", item_id=None, course_id=w["course"])
    assert "show_score_model" in grading["prompt"] and grading["conversation_id"] != ask["conversation_id"]


def test_unconfigured_and_invalid_requests_do_not_send(workspace):
    w = workspace
    w["assistant"].delete_profile(w["profile"])
    with pytest.raises(InvalidInput) as error:
        prepare(w)
    assert error.value.code == "assistant_not_configured"
    assert not w["factories"]
    assert w["hub"].db.one("SELECT COUNT(*) n FROM assistant_messages")["n"] == 0


@pytest.mark.parametrize("data", [[], {"text": 123}, {"action": "bad"}, {"acknowledge": 1}, {"model": []},
                                  {"reasoning": "illegal"}, {"request_id": "bad / key"}, {"item_id": True},
                                  {"action": "grading"}, {"unknown": "injected"}])
def test_invalid_handoff_input(workspace, data):
    payload = {"item_id": workspace["iid"], **data} if isinstance(data, dict) else data
    with pytest.raises(InvalidInput):
        workspace["svc"].prepare(workspace["api"], workspace["assistant"], workspace["aid"], payload)
    assert not workspace["factories"]


def test_first_action_requires_ack_and_later_action_is_direct(workspace):
    w = workspace
    payload = {"action": "breakdown", "item_id": w["iid"]}
    prepared = prepare(w)
    for extra in ({}, {"acknowledge": True}, {"disclosure_fingerprint": prepared["disclosure"]["fingerprint"]}):
        with pytest.raises(InvalidInput) as error:
            w["svc"].start(w["api"], w["assistant"], w["aid"], {**payload, **extra})
        assert error.value.code == "disclosure_required"
    assert not w["factories"]
    first = start(w)
    w["assistant"].wait(first["conversation_id"], 5)
    assert prepare(w)["disclosure"]["acknowledged"]
    second = w["svc"].start(w["api"], w["assistant"], w["aid"], payload)
    w["assistant"].wait(second["conversation_id"], 5)
    assert first["conversation_id"] == second["conversation_id"]
    assert len(w["factories"]) == 2


def test_changed_model_endpoint_profile_or_scope_requires_disclosure(workspace):
    w = workspace
    first = start(w)
    w["assistant"].wait(first["conversation_id"], 5)
    old = prepare(w)["disclosure"]["fingerprint"]
    w["assistant"].update_config({"profile_id": w["profile"], "model": "new-fake-model"})
    changed = prepare(w)
    assert not changed["disclosure"]["acknowledged"] and changed["disclosure"]["fingerprint"] != old
    assert changed["disclosure"]["model"] == "new-fake-model"
    with pytest.raises(InvalidInput) as error:
        w["svc"].start(w["api"], w["assistant"], w["aid"], {"item_id": w["iid"], "action": "breakdown",
                     "acknowledge": True, "disclosure_fingerprint": old})
    assert error.value.code == "disclosure_required"
    assert not prepare(w, item_id=w["other_iid"])["disclosure"]["acknowledged"]
    w["assistant"].update_config({"profile_id": w["profile"], "model": "fake-model", "base_url": "https://other.example/v1"})
    assert prepare(w)["disclosure"]["fingerprint"] != old
    created = w["assistant"].update_config({"profile_id": "new", "preset": "custom", "model": "fake-model",
                                          "base_url": "http://localhost:1234/v1"})
    assert prepare(w, profile_id=created["saved_profile"])["disclosure"]["fingerprint"] != old
    assert len(w["factories"]) == 1


def test_disclosure_masks_endpoint_secrets_and_pins_effective_codex_model(workspace):
    w = workspace
    w["assistant"].update_config({"profile_id": w["profile"], "base_url": "https://user:password@provider.example/v1?key=secret#fragment"})
    disclosure = prepare(w)["disclosure"]
    assert disclosure["endpoint"] == "https://provider.example/v1"
    assert not any(secret in json.dumps(disclosure) for secret in ("password", "secret", "fake-key"))
    pid = w["assistant"].update_config({"profile_id": "new", "preset": "codex"})["saved_profile"]
    prepared = prepare(w, profile_id=pid, model="test-codex")
    result = start(w, profile_id=pid, model="test-codex")
    w["assistant"].wait(result["conversation_id"], 5)
    assert prepared["disclosure"]["model"] == w["factories"][-1]["model"] == "test-codex"


def test_idempotent_action_retry_reuses_run_and_rejects_changed_payload(workspace):
    w = workspace
    first = start(w, request_id="click:1")
    w["assistant"].wait(first["conversation_id"], 5)
    second = start(w, request_id="click:1")
    assert second["reused_request"] and second["conversation_id"] == first["conversation_id"]
    assert len(w["factories"]) == 1
    with pytest.raises(InvalidInput, match="different action"):
        start(w, request_id="click:1", text="changed")


def test_concurrent_identical_actions_cannot_duplicate_run_or_message(workspace):
    w = workspace
    fake = w["fake"]
    fake.entered, fake.release = threading.Event(), threading.Event()
    prepared = prepare(w)
    payload = {"item_id": w["iid"], "action": "breakdown", "acknowledge": True,
               "disclosure_fingerprint": prepared["disclosure"]["fingerprint"]}
    def launch(_):
        try:
            return w["svc"].start(w["api"], w["assistant"], w["aid"], payload)
        except InvalidInput as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(launch, range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1 and "assistant_busy" in results
    assert fake.entered.wait(2)
    assert w["hub"].db.one("SELECT COUNT(*) n FROM assistant_messages WHERE role='user'")["n"] == 1
    fake.release.set()
    w["assistant"].wait(prepared["conversation_id"], 5)
    assert len(w["factories"]) == 1


def test_run_scope_is_pinned_across_active_switch_and_stop(workspace):
    w = workspace
    fake = w["fake"]
    fake.entered, fake.release = threading.Event(), threading.Event()
    result = start(w)
    cid = result["conversation_id"]
    assert fake.entered.wait(2)
    w["svc"].save(w["aid"], {"item_id": w["other_iid"]})
    stopped = w["assistant"].stop(w["aid"], cid)
    assert stopped["run_state"] == "cancelling"
    with pytest.raises(InvalidInput) as error:
        w["assistant"].send(w["aid"], "Again", conversation_id=cid)
    assert error.value.code == "assistant_busy"
    fake.release.set()
    w["assistant"].wait(cid, 5)
    conv = w["assistant"].get_conversation(w["aid"], cid)
    assert conv["conversation"]["scope_item_id"] == w["iid"]
    assert conv["conversation"]["run_state"] == "idle" and fake.closed
    assert f"item_id={w['iid']}" in fake.requests[0]["system"]
    assert w["svc"].get(w["aid"])["active"]["item_id"] == w["other_iid"]


def test_tool_creation_stays_with_original_assignment_after_switch(workspace):
    w = workspace
    fake = w["fake"]
    fake.entered, fake.release = threading.Event(), threading.Event()
    fake.calls = [("show_checklist", CHECKLIST)]
    result = start(w)
    assert fake.entered.wait(2)
    w["svc"].save(w["aid"], {"item_id": w["other_iid"]})
    fake.release.set()
    w["assistant"].wait(result["conversation_id"], 5)
    assert w["assistant"].get_conversation(w["aid"], result["conversation_id"])["conversation"]["run_state"] == "idle"
    assert len(w["hub"].components.list(w["aid"], item_id=w["iid"])) == 1
    assert w["hub"].components.list(w["aid"], item_id=w["other_iid"]) == []


def test_tool_context_uses_conversation_scope_and_repeated_creation_reuses_component(workspace):
    w = workspace
    fake = w["fake"]
    fake.calls = [("show_checklist", CHECKLIST)]
    result = start(w)
    w["assistant"].wait(result["conversation_id"], 5)
    conv = w["assistant"].get_conversation(w["aid"], result["conversation_id"])
    assert conv["conversation"]["run_state"] == "idle", conv
    component = w["hub"].components.list(w["aid"], item_id=w["iid"])[0]
    assert component["conversation_id"] == result["conversation_id"]
    assert component["course_id"] == w["course"]
    w["hub"].components.set_state(w["aid"], component["id"], {"done": {"draft": True}})
    fake.calls = [("show_checklist", {**CHECKLIST, "title": "Another generated title"})]
    again = start(w)
    w["assistant"].wait(again["conversation_id"], 5)
    linked = w["hub"].components.list(w["aid"], item_id=w["iid"])
    assert len(linked) == 1 and linked[0]["id"] == component["id"]
    assert linked[0]["title"] == "Proposal steps" and linked[0]["state"]["done"]["draft"]


def test_generic_send_cannot_bypass_scoped_disclosure(workspace):
    from fastapi.testclient import TestClient
    from canvas_hub.web.app import create_app
    w = workspace
    w["api"].assistant = w["assistant"]
    cid = w["svc"].conversation(w["aid"], item_id=w["iid"])["id"]
    client = TestClient(create_app(w["api"], token="t" * 40, port=8730), base_url="http://127.0.0.1:8730")
    headers = {"Authorization": "Bearer " + "t" * 40, "X-Canvas-Hub": "1"}
    path = f"/api/accounts/{w['aid']}/assistant/messages"
    body = {"text": "Question through the general chat route", "conversation_id": cid, "profile_id": w["profile"]}
    rejected = client.post(path, json=body, headers=headers)
    assert rejected.status_code == 400 and rejected.json()["error"] == "disclosure_required"
    assert not w["factories"]
    initial = start(w)
    w["assistant"].wait(initial["conversation_id"], 5)
    accepted = client.post(path, json=body, headers=headers)
    assert accepted.status_code == 200
    w["assistant"].wait(cid, 5)
    w["assistant"].update_config({"profile_id": w["profile"], "model": "changed-model"})
    changed = client.post(path, json=body, headers=headers)
    assert changed.status_code == 400 and changed.json()["error"] == "disclosure_required"
    assert len(w["factories"]) == 2
    general = w["assistant"].send(w["aid"], "General question", profile_id=w["profile"])
    w["assistant"].wait(general["conversation_id"], 5)
    assert general["conversation_id"] != cid and len(w["factories"]) == 3
