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
    with pytest.raises(InvalidInput):
        svc.update_config({"preset": "codex"})   # reserved, not implemented
    with pytest.raises(InvalidInput):
        svc.update_config({"preset": "custom", "base_url": "http://evil.example/v1"})
    pub = svc.update_config({"preset": "anthropic", "api_key": "sk-ant-secret-abcd"})
    assert pub["api_key_saved"] and "secret" not in json.dumps(pub) and pub["api_key_hint"].endswith("abcd")
    p = hub.paths.runtime / "assistant.json"
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
    assert captured["tools"][0]["eager_input_streaming"] is True
