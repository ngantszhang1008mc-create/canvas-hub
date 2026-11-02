"""Focused tool-discovery/selection checks, using synthetic local providers."""
import json
import os
from pathlib import Path

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import Policy, estimate, valid_groups
from canvas_hub.assistant.providers import ToolCall, Turn
from canvas_hub.assistant.tool_catalog import MAX_SCHEMA_TOKENS, ToolCatalog, ToolSession
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput

from .test_codex import fake_codex


def test_catalog_selection_is_permission_filtered_bounded_and_stably_ordered():
    read = ToolCatalog({"read", "ui"})
    assert "create_note" not in read.tools and "prepare_message" not in read.tools
    with pytest.raises(InvalidInput, match="no longer permitted"):
        read.parse_message("/create_note hello", [])
    with pytest.raises(InvalidInput, match="Add a question"):
        read.parse_message("/tracker", [])
    prompt, chosen = read.parse_message("/tracker /documents compare these", ["tool:calculate"])
    assert prompt == "compare these" and "group:tracker" in chosen
    assert len(read.advertised([])) < 10 and estimate(read.advertised([])) < 2500
    session = ToolSession({"read", "ui"})
    before = session.advertised()
    metadata = json.loads(session.discover({"query": "tracker"}))
    assert not metadata["executed"] and before == session.advertised()
    session.discover({"select": ["tool:show_artifact", "tool:read_context_source"]})
    first = session.advertised()
    session.discover({"select": ["tool:read_context_source", "tool:show_artifact"]})
    assert first == session.advertised()
    assert estimate(first) + len(first)*32 <= MAX_SCHEMA_TOKENS
    with pytest.raises(InvalidInput, match="budget"):
        session.discover({"select": ["group:"+g["id"] for g in read.groups]})
    assert first == session.advertised()  # no partial selection on a budget error
    session.discover({"select": []})
    with pytest.raises(InvalidInput, match="not currently loaded"):
        session.dispatch({"name": "show_artifact", "arguments": {}})
    assert not Policy.for_config({"provider": "anthropic"}).auto_compact
    # The Codex bridge must not move document text into the ordinary tool budget.
    cfg = {"provider": "codex"}
    measured = Policy.for_config(cfg).measure(cfg, "", [{"role": "tool", "results": [
        {"id": "doc", "name": "call_discovered_tool", "executed_tool": "read_context_source", "content": "source " * 100}
    ]}], [])
    assert measured["used"]["documents"] > 0 and measured["used"]["tools"] == 0


def test_reviewed_gateway_catalog_never_adds_local_discovery_or_unapproved_tools():
    cfg = {"provider": "allowance_gateway", "gateway_tools": ["list_courses", "read_document", "calculate", "create_note"]}
    session = ToolSession({"read", "ui"}, **AssistantService._tool_catalog_options(cfg))
    public = session.catalog.public()
    assert not public["discovery_available"]
    assert {t["name"] for t in session.advertised()} == {"list_courses", "read_document", "calculate"}
    assert session.catalog.parse_message("/read_document inspect this", [])[1] == ["tool:read_document"]
    selected = ToolSession({"read", "ui"}, ["tool:read_document"], **AssistantService._tool_catalog_options(cfg))
    assert {t["name"] for t in selected.advertised()} == {"list_courses", "read_document"}
    with pytest.raises(InvalidInput, match="no longer permitted"):
        session.catalog.parse_message("/show_artifact show this", [])
    with pytest.raises(InvalidInput, match="reviewed"):
        session.discover({"select": ["tool:show_artifact"]})
    with pytest.raises(InvalidInput, match="does not enable"):
        session.dispatch({"name": "read_document", "arguments": {}})
    # A gateway's reviewed registry is not subject to the smaller local
    # discovery advertisement limit. Context/wire budgets still apply later.
    full = ToolCatalog({"read", "ui"})
    large = ToolCatalog({"read", "ui"}, allowed_names=set(full.tools), discovery=False)
    assert len(large.advertised([])) == len(full.tools) > 32
    assert large.public()['limits'] == {'max_advertised': 128, 'schema_tokens': None}


@pytest.mark.parametrize("preset", ["anthropic", "custom"])
def test_discovery_changes_advertisement_without_executing_selection(hub, account, preset):
    from .fake_llm import FakeLLM
    fake = FakeLLM([("discover_tools", {"select": ["tool:show_artifact"]}),
                    ("show_artifact", {"title": "Synthetic", "content": "A reusable answer"})])
    try:
        svc = AssistantService(hub, HubAPI(hub))
        svc.update_config({"preset": preset, "base_url": fake.url + ("/v1" if preset=="custom" else ""),
                           "model": "synthetic", "api_key": "sk-fake-not-real"})
        cid = svc.send(account["id"], "Prepare an artifact")["conversation_id"]
        svc.wait(cid, 10)
        conv = svc.get_conversation(account["id"], cid)
        assert conv["conversation"]["run_state"] == "idle", conv
        names = lambda r: {t.get("name") or t["function"]["name"] for t in r.get("tools", [])}
        assert "show_artifact" not in names(fake.requests[0])
        assert "show_artifact" in names(fake.requests[1])
        results = [r for m in conv["messages"] if m["role"] == "tool" for r in m["results"]]
        assert results[0]["name"] == "discover_tools" and results[0]["display"] is None
        assert results[1]["display"]["component"] == "artifact"
        assert valid_groups(svc._transcript(cid))
        # Per-message selection is not made sticky by discovery or the prior turn.
        fake.plan = []
        svc.send(account["id"], "Next question", cid)
        svc.wait(cid, 10)
        assert "show_artifact" not in names(fake.requests[-1])
    finally:
        fake.stop()


def test_manual_selection_persists_with_request_and_revocation_still_wins(hub, account, monkeypatch):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "custom", "base_url": "http://localhost:9", "model": "synthetic"})
    requests = []
    class Provider:
        def complete(self, system, transcript, tools, on_text):
            requests.append(tools)
            if len(requests) == 1:
                svc.update_config({"permissions": {"notes": False}})
                return Turn("", tool_calls=[ToolCall("c", "create_note", {"kind": "note", "title": "No write"})])
            return Turn("Permission changed")
        def close(self): pass
    monkeypatch.setattr("canvas_hub.assistant.make_provider", lambda cfg: Provider())
    cid = svc.send(account["id"], "/tracker inspect this", tool_selection=["tool:create_note"])["conversation_id"]
    svc.wait(cid, 5)
    assert "patch_component_state" in {t["name"] for t in requests[0]}
    assert "create_note" not in {t["name"] for t in requests[1]}
    assert not hub.annotations.list(account["id"])
    conv = svc.get_conversation(account["id"], cid)
    assert conv["messages"][0]["tool_selection"] == ["group:tracker", "tool:create_note"]
    svc2 = AssistantService(hub, HubAPI(hub))
    assert svc2.get_conversation(account["id"], cid)["messages"][0]["tool_selection"] == conv["messages"][0]["tool_selection"]
    before = len(svc2._transcript(cid))
    with pytest.raises(InvalidInput):
        svc2.send(account["id"], "New question", cid, tool_selection=["tool:create_note"])
    assert len(svc2._transcript(cid)) == before
    # Retry never advertises or executes discovery, regardless of earlier choices.
    svc2._set_state(cid, "error", "synthetic interruption")
    class RetryProvider:
        def complete(self, system, transcript, tools, on_text):
            assert tools == []
            return Turn("", tool_calls=[ToolCall("r", "discover_tools", {"select": ["group:notes"]})])
        def close(self): pass
    monkeypatch.setattr("canvas_hub.assistant.make_provider", lambda cfg: RetryProvider())
    svc2.retry(account["id"], cid); svc2.wait(cid, 5)
    assert "retry_tool_disabled" in svc2.get_conversation(account["id"], cid)["messages"][-1]["results"][0]["preview"]


def test_codex_discovery_dispatch_keeps_one_native_turn(hub, account, fake_codex):
    setup, messages = fake_codex
    setup(calls=[{"name": "discover_tools", "arguments": {"select": ["tool:show_artifact"]}},
                 {"name": "call_discovered_tool", "arguments": {"name": "show_artifact", "arguments": {"title": "Synthetic", "content": "Stored output"}}}],
          reply="Finished")
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "codex"})
    cid = svc.send(account["id"], "Make an artifact")["conversation_id"]
    svc.wait(cid, 10)
    conv = svc.get_conversation(account["id"], cid)
    assert conv["conversation"]["run_state"] == "idle", conv
    starts = [m for m in messages() if m.get("method") == "thread/start"]
    assert len(starts) == 1
    assert "show_artifact" not in {t["name"] for t in starts[0]["params"]["dynamicTools"]}
    assert any(r["display"] and r["display"]["component"] == "artifact" for m in conv["messages"] if m["role"] == "tool" for r in m["results"])
    assert valid_groups(svc._transcript(cid))


@pytest.mark.browser
@pytest.mark.parametrize("width", [380, 1180])
def test_tool_picker_slash_keyboard_chips_and_mobile_bounds(hub, account, monkeypatch, tmp_path, width):
    from playwright.sync_api import expect, sync_playwright
    from .test_components_ui import serve
    requests = []
    class Provider:
        def complete(self, system, transcript, tools, on_text):
            requests.append(tools)
            return Turn("Ready")
        def close(self): pass
    monkeypatch.setattr("canvas_hub.assistant.make_provider", lambda cfg: Provider())
    with serve(hub, []) as (base, token, api), sync_playwright() as p:
        first_profile = api.assistant.public_config()["active_profile"]
        second_profile = api.assistant.update_config({"profile_id": "new", "name": "Reviewed tools", "preset": "custom",
                                                      "base_url": "http://localhost:9/v1", "model": "synthetic"})["saved_profile"]
        original_catalog = api.assistant.tool_catalog
        def catalog(aid, profile_id=None):
            if profile_id == second_profile:
                return ToolCatalog({"read", "ui"}, allowed_names={"list_courses", "read_document"}, discovery=False).public()
            return original_catalog(aid, profile_id)
        monkeypatch.setattr(api.assistant, "tool_catalog", catalog)
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/?token={token}"); page.goto(f"{base}/#/assistant")
        field = page.locator(".composer textarea")
        field.fill("/tracker"); field.press("Enter")
        expect(page.get_by_role("button", name="Remove /tracker · Trackers & scores")).to_be_visible()
        assert not requests and not api.assistant.list_conversations(account["id"])
        toggle = page.get_by_role("button", name="Choose tools for this message")
        setup = page.locator('.composer select[title="Saved API setup (provider and model) for the next message"]')
        setup.select_option(second_profile)
        expect(page.locator(".assistant-tool-chip")).to_have_count(0)
        expect(toggle).to_be_enabled()
        toggle.click()
        expect(page.get_by_text("This gateway lists only tools reviewed in its setup", exact=False)).to_be_visible()
        expect(page.locator('[data-selection="tool:show_artifact"]')).to_have_count(0)
        page.keyboard.press("Escape")
        setup.select_option(first_profile)
        expect(toggle).to_be_enabled()
        field.fill("/tracker"); field.press("Enter")
        expect(page.get_by_role("button", name="Remove /tracker · Trackers & scores")).to_be_visible()
        assert not requests
        toggle.click()
        dialog = page.get_by_role("dialog", name="Choose assistant tools")
        expect(dialog).to_be_visible()
        search = page.get_by_role("searchbox", name="Search permitted tools")
        search.fill("show_artifact"); search.press("ArrowDown"); page.keyboard.press("Enter")
        expect(dialog.get_by_role("button", name="show_artifact", exact=False)).to_have_attribute("aria-pressed", "true")
        page.keyboard.press("Escape")
        expect(toggle).to_be_focused()
        expect(dialog).to_be_hidden()
        page.get_by_role("button", name="Remove show_artifact").click()
        toggle.click()
        bounds = dialog.bounding_box()
        assert bounds["x"] >= 0 and bounds["x"]+bounds["width"] <= width and bounds["y"] >= 0
        artifacts = Path(os.environ.get('CANVAS_HUB_NEXT_SCREENSHOTS', str(tmp_path)))
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / f'assistant-tools-{width}.png'))
        field.click()
        expect(dialog).to_be_hidden()
        field.fill("Explain this tracker")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_text("Ready", exact=True)).to_be_visible()
        assert "patch_component_state" in {t["name"] for t in requests[0]}
        expect(page.locator(".assistant-tool-chip")).to_have_count(0)
        page.reload()
        expect(page.get_by_text("Requested tools: /tracker", exact=True)).to_be_visible()
        assert page.locator("body").evaluate("el => el.scrollWidth <= innerWidth")
        browser.close()
    assert not errors
