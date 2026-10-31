"""MCP server and in-app assistant (with scripted fake model endpoints)."""

import json
import os
import stat
import sys
import zipfile

import anyio
import pytest

from canvas_hub.agent_tools import TOOLS, ToolContext, run_tool
from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.providers import AnthropicProvider
from canvas_hub.boundary import HubAPI
from canvas_hub.services import backup
from canvas_hub.util import InvalidInput

from .fake_llm import FakeLLM


@pytest.fixture
def seeded(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    return hub, account


def test_tool_permissions_and_validation(seeded):
    hub, acct = seeded
    ctx = ToolContext(api=HubAPI(hub), account_id=acct["id"])
    text, err = run_tool(ctx, "get_deadlines", {"start": "2026-11-01T00:00:00+08:00", "end": "2026-12-01T00:00:00+08:00"}, {"read"})
    d = json.loads(text)
    assert not err and d["deadlines"][0]["title"] == "Project proposal"
    assert d["unresolved_dates"][0]["as_written"] == "Nov 13 at 11:59pm"
    # disabled permission, unknown args, wrong types, unknown tools
    assert run_tool(ctx, "create_note", {"kind": "note", "body": "x"}, {"read"})[1]
    assert run_tool(ctx, "search", {"query": "x", "evil": 1}, {"read"})[1]
    assert run_tool(ctx, "get_item", {"item_id": "1; DROP"}, {"read"})[1]
    assert run_tool(ctx, "run_shell", {}, {"read", "notes", "collect", "settings"})[1]
    # writes stay local and are never Canvas claims
    text, err = run_tool(ctx, "create_note", {"kind": "task", "title": "Start proposal"}, {"read", "notes"})
    assert not err and "nothing was sent to Canvas" in text
    # every schema is strict
    assert all(t.schema()["additionalProperties"] is False for t in TOOLS)


def test_account_fixed_in_assistant_context(seeded):
    hub, acct = seeded
    other = hub.identity.upsert_verified_account(acct["instance_id"], {"canvas_user_id": "999"})
    ctx = ToolContext(api=HubAPI(hub), account_id=other["id"])
    d = json.loads(run_tool(ctx, "search", {"query": "proposal"}, {"read"})[0])
    assert d["results"] == []          # cannot see the other account's data
    assert run_tool(ctx, "search", {"query": "proposal", "account_id": acct["id"]}, {"read"})[1]   # no override


def test_mcp_server_over_stdio(seeded, tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    hub, _ = seeded
    home = str(hub.paths.home)

    async def main():
        params = StdioServerParameters(command=sys.executable, args=["-m", "canvas_hub", "--home", home, "mcp"])
        async with stdio_client(params) as streams:
            async with ClientSession(*streams[:2]) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert "search" in names and "create_note" not in names and "start_collection" not in names
                r = await s.call_tool("search", {"query": "visualisation"})
                return json.loads(r.content[0].text)
    res = anyio.run(main)
    assert res["results"][0]["title"] == "Project brief"


def _assistant(hub, fake, preset):
    api = HubAPI(hub)
    svc = AssistantService(hub, api)
    cfg = {"preset": preset, "base_url": fake.url + ("/v1" if preset == "custom" else ""), "model": "test-model",
           "api_key": "sk-test-1234567890"}
    svc.update_config(cfg)
    return svc


@pytest.mark.parametrize("preset", ["anthropic", "custom"])
def test_assistant_tool_loop(seeded, preset):
    hub, acct = seeded
    fake = FakeLLM([("get_deadlines", {"days_ahead": 60, "start": "2026-11-01T00:00:00+08:00"}),
                    ("create_note", {"kind": "task", "title": "Write proposal"})])
    try:
        svc = _assistant(hub, fake, preset)
        r = svc.send(acct["id"], "What is due soon? Add a to-do.")
        svc.wait(r["conversation_id"], 30)
        conv = svc.get_conversation(acct["id"], r["conversation_id"])
        assert conv["conversation"]["run_state"] == "idle", conv["conversation"]
        roles = [m["role"] for m in conv["messages"]]
        assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
        assert "Project proposal" in conv["messages"][2]["results"][0]["preview"]
        assert "Write proposal" in conv["messages"][-1]["text"]
        assert hub.annotations.list(acct["id"])[0]["title"] == "Write proposal"
        h = fake.headers[0]
        if preset == "anthropic":
            assert "sk-test-1234567890" in (h.get("x-api-key") or h.get("authorization", ""))
            first = fake.requests[0]
            assert first["model"] == "test-model" and first["stream"] is True
            assert {t["name"] for t in first["tools"]} >= {"search", "create_note", "start_collection"}
            assert first["messages"][0]["content"].startswith("[Now: ")
            # the assistant turn is replayed exactly as received
            assert fake.requests[1]["messages"][1]["content"][1]["type"] == "tool_use"
        else:
            assert h["authorization"] == "Bearer sk-test-1234567890"
            assert fake.requests[1]["messages"][-1]["role"] == "tool"
        # a follow-up question continues the same conversation
        svc.send(acct["id"], "Thanks", r["conversation_id"])
        svc.wait(r["conversation_id"], 30)
        assert svc.get_conversation(acct["id"], r["conversation_id"])["conversation"]["run_state"] == "idle"
    finally:
        fake.stop()


def test_assistant_respects_permissions(seeded):
    hub, acct = seeded
    fake = FakeLLM([("create_note", {"kind": "note", "body": "x"})])
    try:
        svc = _assistant(hub, fake, "custom")
        svc.update_config({"permissions": {"notes": False, "collect": False}})
        r = svc.send(acct["id"], "Make a note")
        svc.wait(r["conversation_id"], 30)
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert "create_note" not in names and "start_collection" not in names and "search" in names
        msgs = svc.get_conversation(acct["id"], r["conversation_id"])["messages"]
        assert msgs[2]["results"][0]["is_error"] and hub.annotations.list(acct["id"]) == []
    finally:
        fake.stop()


def test_assistant_errors_and_key_storage(seeded):
    hub, acct = seeded
    svc = AssistantService(hub, HubAPI(hub))
    with pytest.raises(InvalidInput):
        svc.send(acct["id"], "hello")          # not configured
    codex = svc.update_config({"preset": "codex"})
    assert codex["configured"] and not codex["api_key_saved"]
    with pytest.raises(InvalidInput):
        svc.update_config({"preset": "custom", "base_url": "http://evil.example/v1"})
    pub = svc.update_config({"preset": "anthropic", "api_key": "sk-ant-secret-abcd"})
    assert pub["api_key_saved"] and "secret" not in json.dumps(pub) and pub["api_key_hint"].endswith("abcd")
    p = hub.paths.runtime / "assistant.json"
    if os.name != "nt":                   # Windows uses the per-user ACL inherited from LOCALAPPDATA
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # the key never ends up in backups or exports
    zp = backup.create_backup(hub)
    with zipfile.ZipFile(zp) as z:
        assert all(b"sk-ant-secret-abcd" not in z.read(n) for n in z.namelist())
    # unreachable provider -> readable error state, conversation kept
    svc.update_config({"base_url": "http://127.0.0.1:9/"})
    r = svc.send(acct["id"], "hello")
    svc.wait(r["conversation_id"], 60)
    conv = svc.get_conversation(acct["id"], r["conversation_id"])["conversation"]
    assert conv["run_state"] == "error" and conv["run_error"]


def test_anthropic_request_shape_on_claude_api(monkeypatch):
    """On the real Claude API the request streams, uses eager tool input and the
    default refusal fallback for models that support it."""
    captured = {}

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            class M:
                def to_dict(self):
                    return {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                            "model": "claude-opus-5-5", "usage": {}}
            return M()

    p = AnthropicProvider({"api_key": "k"})
    monkeypatch.setattr(p.client.beta.messages, "stream", lambda **kw: captured.update(kw) or FakeStream())
    turn = p.complete("sys", [{"role": "user", "sent_text": "q"}],
                      [{"name": "search", "description": "d", "schema": {"type": "object", "properties": {}}}])
    assert turn.text == "hi"
    assert captured["model"] == "claude-opus-5-5" and captured["fallbacks"] == "default"
    assert captured["betas"] == ["server-side-fallback-2026-07-01"]
    assert captured["tools"][0]["eager_input_streaming"] is True and "output_config" not in captured
    p = AnthropicProvider({"api_key": "k", "effort": "xhigh"})
    monkeypatch.setattr(p.client.beta.messages, "stream", lambda **kw: captured.update(kw) or FakeStream())
    p.complete("sys", [{"role": "user", "sent_text": "q"}], [])
    assert captured["output_config"] == {"effort": "xhigh"}


def test_reasoning_is_chosen_per_message(seeded, monkeypatch):
    hub, acct = seeded
    svc = AssistantService(hub, HubAPI(hub))
    runs = []
    monkeypatch.setattr(svc, "_run", lambda cid, aid, cfg, a: runs.append(cfg))
    claude = svc.update_config({"preset": "anthropic", "api_key": "sk-ant-x-1234"})["saved_profile"]
    assert svc.public_config()["profiles"][0]["reasoning_levels"] == ["low", "medium", "high", "xhigh", "max"]
    svc.send(acct["id"], "q", profile_id=claude, reasoning="xhigh")
    svc.send(acct["id"], "q", profile_id=claude)
    assert runs[0]["effort"] == "xhigh" and runs[1]["effort"] == ""
    with pytest.raises(InvalidInput):
        svc.send(acct["id"], "q", profile_id=claude, reasoning="off")       # DeepSeek-only level
    msgs = [m for c in svc.list_conversations(acct["id"]) for m in svc.get_conversation(acct["id"], c["id"])["messages"]]
    assert {m.get("reasoning") for m in msgs if m["role"] == "user"} == {"xhigh", ""}


def test_deepseek_thinking_max_and_reasoning_passback(seeded):
    hub, acct = seeded
    fake = FakeLLM([("get_deadlines", {"days_ahead": 30}), ("search", {"query": "proposal"})])
    try:
        svc = AssistantService(hub, HubAPI(hub))
        pub = svc.update_config({"preset": "deepseek", "base_url": fake.url + "/v1", "api_key": "sk-ds-123456789"})
        assert pub["model"] == "deepseek-flash" and pub["reasoning_levels"] == ["off", "low", "high", "max"]
        assert "thinking" not in pub                      # chosen per message, not saved with the setup
        r = svc.send(acct["id"], "What is due?", reasoning="max")
        svc.wait(r["conversation_id"], 30)
        conv = svc.get_conversation(acct["id"], r["conversation_id"])
        assert conv["conversation"]["run_state"] == "idle", conv["conversation"]
        first, last = fake.requests[0], fake.requests[-1]
        assert first["thinking"] == {"type": "enabled"} and first["reasoning_effort"] == "max"
        assert first["model"] == "deepseek-flash"
        sent = [m for m in last["messages"] if m["role"] == "assistant"]
        assert [m["reasoning_content"] for m in sent] == ["thinking step 0", "thinking step 1"]
        # a follow-up turn still replays earlier reasoning (required once tools were used)
        svc.send(acct["id"], "And next week?", r["conversation_id"], reasoning="off")
        svc.wait(r["conversation_id"], 30)
        assert svc.get_conversation(acct["id"], r["conversation_id"])["conversation"]["run_state"] == "idle"
        assert fake.requests[-1]["thinking"] == {"type": "disabled"} and "reasoning_effort" not in fake.requests[-1]
        # extra request settings merge in; app-owned fields are protected
        svc.update_config({"extra_params": {"reasoning_effort": "high", "top_k": 5}})
        svc.send(acct["id"], "hi", r["conversation_id"])
        svc.wait(r["conversation_id"], 30)
        assert fake.requests[-1]["reasoning_effort"] == "high" and "thinking" not in fake.requests[-1]
        assert fake.requests[-1]["top_k"] == 5
        svc.send(acct["id"], "hi", r["conversation_id"], reasoning="max")     # the chat's choice wins
        svc.wait(r["conversation_id"], 30)
        assert fake.requests[-1]["reasoning_effort"] == "max" and fake.requests[-1]["thinking"] == {"type": "enabled"}
        with pytest.raises(InvalidInput):
            svc.update_config({"extra_params": {"model": "other"}})
        with pytest.raises(InvalidInput):
            svc.send(acct["id"], "hi", reasoning="ultra")     # not a level this setup offers
        with pytest.raises(InvalidInput):
            svc.update_config({"thinking": "max"})           # no longer a saved setting
    finally:
        fake.stop()


def test_thinking_off_and_other_providers():
    from canvas_hub.assistant.providers import thinking_params
    assert thinking_params("deepseek", "off") == {"thinking": {"type": "disabled"}}
    assert thinking_params("deepseek", "") == {}
    assert thinking_params("openai", "high") == {"reasoning_effort": "high"}
    assert thinking_params("openai", "off") == {}


def test_assistant_setups(seeded):
    hub, acct = seeded
    from canvas_hub.util import NotFound
    p = hub.paths.runtime / "assistant.json"
    # a single setup saved by the previous version becomes the first saved setup
    p.write_text(json.dumps({"preset": "deepseek", "provider": "openai_compatible", "base_url": "https://api.deepseek.com/v1",
                             "model": "deepseek-flash", "api_key": "sk-ds-old-1111", "thinking": "max",
                             "permissions": {"collect": False}}))
    svc = AssistantService(hub, HubAPI(hub))
    pub = svc.public_config()
    assert [(x["id"], x["name"]) for x in pub["profiles"]] == [("p1", "DeepSeek")]
    assert pub["active_profile"] == "p1" and pub["configured"] and "thinking" not in pub["profiles"][0]
    assert pub["permissions"]["collect"] is False and pub["profiles"][0]["api_key_hint"].endswith("1111")
    # add a second setup; the first stays in use until switched
    pub = svc.update_config({"profile_id": "new", "name": "Claude", "preset": "anthropic", "api_key": "sk-ant-new-2222"})
    p2 = pub["saved_profile"]
    assert p2 != "p1" and pub["active_profile"] == "p1" and len(pub["profiles"]) == 2
    assert "sk-ds-old-1111" not in json.dumps(pub) and "sk-ant-new-2222" not in json.dumps(pub)
    assert svc._load()["provider"] == "openai_compatible"
    pub = svc.activate_profile(p2)
    assert pub["active_profile"] == p2 and pub["provider"] == "anthropic" and svc._load()["api_key"] == "sk-ant-new-2222"
    # editing one setup leaves the other untouched; permissions are shared
    svc.update_config({"profile_id": "p1", "model": "deepseek-pro", "permissions": {"collect": True}})
    assert svc._load("p1")["model"] == "deepseek-pro" and svc._load(p2)["model"] != "deepseek-pro"
    assert svc._load(p2)["permissions"]["collect"] is True
    # deleting the setup in use falls back to the remaining one
    pub = svc.delete_profile(p2)
    assert pub["active_profile"] == "p1" and [x["id"] for x in pub["profiles"]] == ["p1"]
    with pytest.raises(NotFound):
        svc.test(p2)
    with pytest.raises(NotFound):
        svc.update_config({"profile_id": p2, "model": "x"})
    # a deleted setup's ID is never handed to a new one, so stale references keep failing
    svc.delete_profile("p1")
    fresh = svc.update_config({"profile_id": "new", "preset": "deepseek"})["saved_profile"]
    assert fresh not in ("p1", p2)
    with pytest.raises(NotFound):
        svc.send(acct["id"], "hello", profile_id="p1")
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_assistant_setup_changes_are_serialised(seeded):
    import threading
    hub, _ = seeded
    svc = AssistantService(hub, HubAPI(hub))
    threads = [threading.Thread(target=svc.update_config, args=({"profile_id": "new", "preset": "deepseek", "name": f"s{i}"},))
               for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(x["name"] for x in svc.public_config()["profiles"]) == sorted(f"s{i}" for i in range(12))


def test_connection_test_does_not_block_the_server(seeded):
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from canvas_hub.web.app import create_app
    hub, _ = seeded
    hang = socket.socket()
    hang.bind(("127.0.0.1", 0))
    hang.listen()              # accepts connections, never answers: a provider that hangs
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({"preset": "custom", "base_url": f"http://127.0.0.1:{hang.getsockname()[1]}/v1", "model": "m"})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tok = "t" * 40
    srv = uvicorn.Server(uvicorn.Config(create_app(api, token=tok, port=port), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    base, hdr = f"http://127.0.0.1:{port}", {"Authorization": "Bearer " + tok, "X-Canvas-Hub": "1"}
    try:
        for _ in range(50):
            if srv.started:
                break
            time.sleep(0.1)
        threading.Thread(target=lambda: httpx.post(base + "/api/assistant/test", json={}, headers=hdr, timeout=30),
                         daemon=True).start()
        time.sleep(0.5)
        t0 = time.monotonic()
        r = httpx.get(base + "/api/assistant/config", headers=hdr, timeout=10)
        assert r.status_code == 200 and time.monotonic() - t0 < 2
    finally:
        srv.should_exit = True
        hang.close()


def test_message_uses_the_setup_named_by_the_chat(seeded):
    from canvas_hub.util import NotFound
    hub, acct = seeded
    first, second = FakeLLM([]), FakeLLM([])
    try:
        svc = AssistantService(hub, HubAPI(hub))
        svc.update_config({"preset": "custom", "name": "A", "base_url": first.url + "/v1", "model": "a"})
        b = svc.update_config({"profile_id": "new", "preset": "custom", "name": "B", "base_url": second.url + "/v1",
                               "model": "b"})["saved_profile"]
        a = svc.public_config()["active_profile"]
        assert a != b
        # the chat picked B but activation has not landed yet: the message still goes to B
        r = svc.send(acct["id"], "hello", profile_id=b)
        svc.wait(r["conversation_id"], 30)
        assert second.requests and not first.requests
        with pytest.raises(NotFound):          # never falls back to another provider
            svc.send(acct["id"], "hello", profile_id="p-unknown")
    finally:
        first.stop()
        second.stop()


ANSWER = "Your proposal is due on Friday at 23:59, and the reading for week nine is chapter four."


def _drafts_while_running(svc, account_id, cid, timeout=20):
    import time
    seen = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        d = svc.get_conversation(account_id, cid)
        if d.get("draft"):
            assert not [m for m in d["messages"] if m["role"] == "assistant"]   # never both the draft and the message
            seen.append(d["draft"]["text"])
        if d["conversation"]["run_state"] not in ("running", "cancelling"):
            return seen, d
        time.sleep(0.02)
    raise AssertionError("the run did not finish")


@pytest.mark.parametrize("preset", ["anthropic", "custom"])
def test_replies_stream_into_a_draft_while_they_arrive(seeded, preset):
    hub, acct = seeded
    fake = FakeLLM([], delay=0.03, answer=ANSWER)
    try:
        svc = _assistant(hub, fake, preset)
        r = svc.send(acct["id"], "When is the proposal due?")
        drafts, final = _drafts_while_running(svc, acct["id"], r["conversation_id"])
    finally:
        fake.stop()
    assert len(set(drafts)) >= 3, drafts                                     # it grew in steps
    assert all(ANSWER.startswith(d) for d in drafts)
    assert "draft" not in final and final["messages"][-1]["text"] == ANSWER
    if preset == "custom":
        assert fake.requests[-1].get("stream") is True


def test_endpoints_that_refuse_streaming_still_answer(seeded):
    hub, acct = seeded
    fake = FakeLLM([("list_courses", {})], openai_streaming=False, answer=ANSWER)
    try:
        svc = _assistant(hub, fake, "custom")
        r = svc.send(acct["id"], "Courses?")
        svc.wait(r["conversation_id"], 30)
        d = svc.get_conversation(acct["id"], r["conversation_id"])
    finally:
        fake.stop()
    assert d["conversation"]["run_state"] == "idle" and d["messages"][-1]["text"] == ANSWER
    assert [m["role"] for m in d["messages"]] == ["user", "assistant", "tool", "assistant"]   # tool calls still work
    assert [bool(q.get("stream")) for q in fake.requests] == [True, False, False]   # tried once, then asked plainly


def test_a_reply_cut_off_mid_stream_is_not_kept(seeded):
    hub, acct = seeded
    fake = FakeLLM([("list_courses", {})], cut_stream=True)
    try:
        svc = _assistant(hub, fake, "custom")
        r = svc.send(acct["id"], "Courses?")
        svc.wait(r["conversation_id"], 30)
        d = svc.get_conversation(acct["id"], r["conversation_id"])
    finally:
        fake.stop()
    assert d["conversation"]["run_state"] == "error" and "cut off" in d["conversation"]["run_error"]
    assert [m["role"] for m in d["messages"]] == ["user"] and "draft" not in d    # no half answer, no tool run


def test_stop_keeps_the_text_written_so_far(seeded):
    import time
    hub, acct = seeded
    fake = FakeLLM([], delay=0.1, answer=ANSWER)
    try:
        svc = _assistant(hub, fake, "custom")
        r = svc.send(acct["id"], "When is the proposal due?")
        cid = r["conversation_id"]
        for _ in range(200):
            if (svc.get_conversation(acct["id"], cid).get("draft") or {}).get("text", "").count(" ") >= 2:
                break
            time.sleep(0.02)
        svc.stop(acct["id"], cid)
        svc.wait(cid, 30)
        d = svc.get_conversation(acct["id"], cid)
    finally:
        fake.stop()
    text = d["messages"][-1]["text"]
    assert d["conversation"]["run_state"] == "idle" and "draft" not in d
    assert text and ANSWER.startswith(text) and text != ANSWER                # what was written, not more
