"""Direct Codex assistant: stdio, the common tool loop, cancellation, and UI."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from canvas_hub.assistant import AssistantService, reasoning_levels
from canvas_hub.assistant.codex import CodexProvider
from canvas_hub.assistant.providers import ProviderError
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    script = Path(__file__).with_name("fake_codex.py")
    if os.name == "nt":
        executable = tmp_path / "fake codex.cmd"
        executable.write_text(f'@echo off\r\n"{sys.executable}" "{script}"\r\n', encoding="utf-8")
    else:
        executable = tmp_path / "fake codex"
        executable.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name='__main__')\n")
        executable.chmod(0o755)
    log = tmp_path / "rpc.jsonl"
    monkeypatch.setenv("CANVAS_HUB_CODEX", str(executable))
    monkeypatch.setenv("CANVAS_HUB_FAKE_CODEX_LOG", str(log))

    def setup(**plan):
        monkeypatch.setenv("CANVAS_HUB_FAKE_CODEX_PLAN", json.dumps(plan))

    def messages():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return setup, messages


def test_codex_streams_and_preserves_early_notifications(fake_codex):
    setup, messages = fake_codex
    setup(reply="A streamed Codex answer", early=True)
    provider = CodexProvider({"model": "requested-model", "thinking": "high"})
    chunks = []
    try:
        turn = provider.complete("Canvas Hub instructions", [{"role": "user", "sent_text": "Question"}], [], chunks.append)
        assert turn.text == "Early text\n\nA streamed Codex answer"
        assert turn.model == "codex-test-model" and turn.usage == {"input_tokens": 12, "output_tokens": 8}
        assert any("strea" in text for text in chunks)
        assert turn.raw == {"provider": "codex"}
        workspace = provider.server.workspace.name
    finally:
        provider.close()
    assert not Path(workspace).exists() and provider.server.process.poll() is not None
    requests = {m["method"]: m.get("params") for m in messages() if "method" in m}
    thread = requests["thread/start"]
    assert thread["ephemeral"] and thread["environments"] == [] and thread["sandbox"] == "read-only"
    assert thread["approvalPolicy"] == "never" and thread["model"] == "requested-model"
    assert thread["config"]["mcp_servers"] == {}
    assert thread["config"]["features.shell_tool"] is False
    assert requests["turn/start"]["effort"] == "high"


def test_codex_uses_the_assistant_tool_loop_and_selected_account(hub, account, fixture_pkg, fake_codex):
    setup, messages = fake_codex
    hub.importer.import_package(fixture_pkg)
    setup(calls=[{"name": "search", "arguments": {"query": "proposal"}},
                 {"name": "show_artifact", "arguments": {"content": "A finished plan", "filename": "plan.md"}}])
    svc = AssistantService(hub, HubAPI(hub))
    cfg = svc.update_config({"preset": "codex", "model": "", "name": "My Codex"})
    assert cfg["configured"] and not cfg["api_key_saved"] and not cfg["api_key_from_env"]
    run = svc.send(account["id"], "Read my course and make a plan", reasoning="high")
    cid = run["conversation_id"]
    svc.wait(cid, 10)
    conv = svc.get_conversation(account["id"], cid)
    assert conv["conversation"]["run_state"] == "idle", conv["conversation"]
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert "Project proposal" in conv["messages"][2]["results"][0]["preview"]
    assert conv["messages"][4]["results"][0]["display"]["component"] == "artifact"
    thread = next(m["params"] for m in messages() if m.get("method") == "thread/start")
    assert "account_id" not in next(t for t in thread["dynamicTools"] if t["name"] == "search")["inputSchema"]["properties"]
    # A new run can replay the app's history without relying on a persisted Codex thread.
    setup(reply="Follow-up answer")
    svc.send(account["id"], "Thanks", cid)
    svc.wait(cid, 10)
    assert svc.get_conversation(account["id"], cid)["messages"][-1]["text"] == "Follow-up answer"
    prompt = [m["params"] for m in messages() if m.get("method") == "turn/start"][-1]["input"][0]["text"]
    assert "Read my course" in prompt and "Thanks" in prompt
    assert '"display"' not in prompt


def test_codex_cannot_bypass_permissions_or_approve_native_tools(hub, account, fake_codex):
    setup, messages = fake_codex
    setup(approval=True, calls=[{"name": "create_note", "arguments": {"kind": "note", "body": "must not save"}},
                               {"name": "search", "arguments": {"query": "x", "account_id": 999}}])
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "codex", "permissions": {"notes": False, "collect": False}})
    cid = svc.send(account["id"], "Read only")["conversation_id"]
    svc.wait(cid, 10)
    conv = svc.get_conversation(account["id"], cid)
    assert conv["conversation"]["run_state"] == "idle", conv["conversation"]
    results = [r for m in conv["messages"] if m["role"] == "tool" for r in m["results"]]
    assert len(results) == 2 and all(r["is_error"] for r in results)
    assert hub.annotations.list(account["id"]) == []
    thread = next(m["params"] for m in messages() if m.get("method") == "thread/start")
    names = {t["name"] for t in thread["dynamicTools"]}
    assert "search" in names and not names & {"create_note", "start_collection", "prepare_message", "run_shell"}
    denial = next(m for m in messages() if m.get("id") == 999 and "method" not in m)
    assert "error" in denial and "result" not in denial


@pytest.mark.parametrize("plan,expected", [
    ({"signed_out": True}, "codex login"), ({"exit": True}, "exited"),
    ({"bad_json": True}, "unreadable"), ({"thread_error": True}, "Update"),
    ({"turn_error": True}, "capacity error"),
])
def test_codex_errors_keep_the_conversation_and_stop_the_process(hub, account, fake_codex, plan, expected):
    setup, _ = fake_codex
    setup(**plan)
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "codex"})
    result = svc.test()
    assert result["ok"] is False and expected in result["message"]
    cid = svc.send(account["id"], "Hello")["conversation_id"]
    svc.wait(cid, 10)
    conv = svc.get_conversation(account["id"], cid)
    assert conv["conversation"]["run_state"] == "error" and expected in conv["conversation"]["run_error"]
    assert conv["messages"][0]["text"] == "Hello"


def test_codex_missing_cli_is_actionable(monkeypatch):
    monkeypatch.setenv("CANVAS_HUB_CODEX", "/not/installed/codex")
    provider = CodexProvider({})
    with pytest.raises(ProviderError, match="Install the Codex CLI"):
        provider.complete("test", [], [])
    provider.close()


def test_codex_stop_during_reasoning_and_timeout_both_clean_up(hub, account, fake_codex, monkeypatch):
    setup, messages = fake_codex
    setup(hang=True, early=True)
    providers = []

    def make(cfg):
        provider = CodexProvider(cfg)
        providers.append(provider)
        return provider

    monkeypatch.setattr("canvas_hub.assistant.make_provider", make)
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "codex"})
    cid = svc.send(account["id"], "Think quietly")["conversation_id"]
    deadline = time.monotonic() + 5
    while not (svc.get_conversation(account["id"], cid).get("draft") or {}).get("text"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    svc.stop(account["id"], cid)
    svc.wait(cid, 5)
    assert svc.get_conversation(account["id"], cid)["conversation"]["run_state"] == "idle"
    assert svc.get_conversation(account["id"], cid)["messages"][-1]["text"] == "Early text"
    assert providers[0].server.process.poll() is not None
    monkeypatch.setattr("canvas_hub.assistant.codex.TURN_TIMEOUT", 0.05)
    provider = CodexProvider({})
    try:
        with pytest.raises(ProviderError, match="timed out"):
            provider.complete("test", [{"role": "user", "sent_text": "test"}], [])
    finally:
        provider.close()
    assert provider.server.process.poll() is not None


def test_codex_settings_reject_keys_and_preserve_profile_choice(hub, fake_codex):
    setup, _ = fake_codex
    setup()
    svc = AssistantService(hub, HubAPI(hub))
    original = svc.update_config({"preset": "anthropic", "api_key": "sk-ant-private-key"})["saved_profile"]
    for data in ({"api_key": "secret"}, {"base_url": "https://example.com"}, {"extra_params": {"sandbox": "danger-full-access"}}):
        with pytest.raises(InvalidInput, match="CLI login"):
            svc.update_config({"profile_id": "new", "preset": "codex", **data})
    cfg = svc.update_config({"profile_id": "new", "preset": "codex", "name": "Codex"})
    pid = cfg["saved_profile"]
    assert pid != original and cfg["active_profile"] == original
    svc.activate_profile(pid)
    assert svc.test(pid)["ok"]
    assert reasoning_levels(svc._load(pid)) == ["low", "medium", "high", "xhigh", "max"]
    saved = json.loads(svc._path.read_text())
    codex = next(p for p in saved["profiles"] if p["id"] == pid)
    assert codex["api_key"] == "" and codex["extra_params"] == {} and codex["model"] == ""


def test_a_conversation_stays_busy_until_codex_has_closed(hub, account, fake_codex, monkeypatch):
    from threading import Event
    setup, _ = fake_codex
    setup(reply="Complete")
    closing, release = Event(), Event()
    original_close = CodexProvider.close

    def slow_close(provider):
        closing.set()
        assert release.wait(timeout=5)
        original_close(provider)

    monkeypatch.setattr(CodexProvider, "close", slow_close)
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "codex"})
    cid = svc.send(account["id"], "Hello")["conversation_id"]
    try:
        assert closing.wait(timeout=5)
        with pytest.raises(InvalidInput, match="still working"):
            svc.send(account["id"], "A second message", cid)
    finally:
        release.set()
        svc.wait(cid, 5)
    assert svc.get_conversation(account["id"], cid)["conversation"]["run_state"] == "idle"


@pytest.mark.browser
def test_codex_setup_and_chat_work_in_the_browser(hub, account, fake_codex):
    from playwright.sync_api import expect, sync_playwright

    from .test_components_ui import serve
    setup, messages = fake_codex
    setup(reply="Codex is connected")
    with serve(hub, []) as (base, token, api), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={token}")
        page.goto(f"{base}/#/assistant/settings?p=new")
        page.get_by_label("Provider", exact=True).select_option("codex")
        page.get_by_label("Name", exact=True).fill("Codex setup")
        expect(page.get_by_label("API key", exact=True)).to_be_hidden()
        expect(page.get_by_label("Base URL", exact=True)).to_be_hidden()
        expect(page.get_by_text("Extra request settings", exact=False)).to_be_hidden()
        picker = page.get_by_label("Codex model", exact=True)                    # the CLI's own model list
        expect(picker.locator("option")).to_have_text(["Codex default (as set in Codex)", "Codex Fast", "Codex Deep"])
        picker.select_option("codex-deep")
        expect(page.get_by_text("Install Codex CLI", exact=False)).to_be_visible()
        page.get_by_role("button", name="Save & test", exact=True).click()
        expect(page.get_by_text("Connected · codex-test-model", exact=True)).to_be_visible(timeout=10000)
        expect(page.get_by_role("heading", name="Codex setup", exact=True)).to_be_visible(timeout=10000)
        page.get_by_role("button", name="Use this setup", exact=True).click()
        expect(page.get_by_role("button", name="Use this setup", exact=True)).to_be_hidden()
        page.goto(f"{base}/#/assistant")
        chat_model = page.locator(".composer-bar select[title='Codex model for the next message']")
        expect(chat_model).to_have_value("")                                     # the setup's model, not overridden
        expect(chat_model.locator("option").first).to_have_text("Model: Codex Deep (setup)")
        reasoning = page.locator(".composer-bar select[title='Reasoning effort for the next message'] option")
        expect(reasoning).to_have_text(["Reasoning: default", "Reasoning: low", "Reasoning: medium", "Reasoning: high",
                                        "Reasoning: xhigh", "Reasoning: max"])
        page.locator(".composer textarea").fill("Hello with the setup's model")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator(".msg.assistant").last).to_have_text("Codex is connected", timeout=15000)
        expect(page.locator(".msg.user .msg-meta").last).to_have_text("Codex setup")   # no override was sent
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
        chat_model.select_option("codex-fast")                                   # switched for this message
        expect(reasoning).to_have_text(["Reasoning: default", "Reasoning: low", "Reasoning: medium"])
        page.locator(".composer textarea").fill("Hello Codex")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator(".msg.assistant").last).to_have_text("Codex is connected", timeout=15000)
        expect(page.locator(".msg.user .msg-meta").last).to_have_text("Codex setup · codex-fast")
        page.reload()
        expect(chat_model).to_have_value("codex-fast")                           # remembered in this browser
        browser.close()
    assert not errors, errors
    saved = api.assistant._load()
    assert saved["provider"] == "codex" and saved["model"] == "codex-deep"
    started = [m["params"] for m in messages() if m.get("method") == "thread/start"]
    assert [s.get("model") for s in started[-2:]] == ["codex-deep", "codex-fast"]


def test_codex_models_come_from_the_cli(hub, account, fake_codex):
    setup, messages = fake_codex
    setup(reply="ok")
    svc = AssistantService(hub, HubAPI(hub))
    listed = svc.codex_models()
    assert listed["error"] is None
    assert [(m["id"], m["name"], m["default"], m["efforts"]) for m in listed["models"]] == [
        ("codex-fast", "Codex Fast", True, ["low", "medium"]),
        ("codex-deep", "Codex Deep", False, ["low", "medium", "high", "xhigh", "max"])]   # hidden and "ultra" left out
    def starts():
        return sum(1 for m in messages() if m.get("method") == "initialize")
    assert starts() == 1 and svc.codex_models()["models"] == listed["models"] and starts() == 1   # reused
    svc.codex_models(refresh=True)
    assert starts() == 2
    # Without a model the user's own Codex configuration decides: none is named to Codex.
    svc.update_config({"preset": "codex", "name": "Codex"})
    cid = svc.send(account["id"], "Hi")["conversation_id"]
    svc.wait(cid, 10)
    assert "model" not in next(m["params"] for m in reversed(messages()) if m.get("method") == "thread/start")
    # One message can use another model; other providers cannot be switched this way.
    cid = svc.send(account["id"], "Hi", model="codex-deep", reasoning="max")["conversation_id"]
    svc.wait(cid, 10)
    assert next(m["params"] for m in reversed(messages()) if m.get("method") == "thread/start")["model"] == "codex-deep"
    first = svc.get_conversation(account["id"], cid)["messages"][0]
    assert first["model"] == "codex-deep" and first["reasoning"] == "max"
    for bad in ("", " ", "-rm", "a b", "x" * 101):
        if bad.strip():
            with pytest.raises(InvalidInput):
                svc.send(account["id"], "Hi", model=bad)
    svc.update_config({"profile_id": "new", "preset": "deepseek", "api_key": "sk-test-abc", "activate": True})
    with pytest.raises(InvalidInput, match="only available for Codex"):
        svc.send(account["id"], "Hi", model="codex-deep")


def test_codex_model_list_errors_are_reported(hub, fake_codex):
    setup, _ = fake_codex
    setup(no_models=True)
    out = AssistantService(hub, HubAPI(hub)).codex_models()
    assert out["models"] == [] and "model list unavailable" in out["error"]
