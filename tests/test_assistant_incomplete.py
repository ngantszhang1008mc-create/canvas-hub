"""Bounded recovery from incomplete replies; synthetic providers and HTTP only."""
import copy
import json

import httpx
import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import Policy, valid_groups
from canvas_hub.assistant.providers import ProviderError, ToolCall, Turn, compatible_output_tokens
from canvas_hub.assistant.usage import normalize_usage
from canvas_hub.boundary import HubAPI

from .test_provider_usage import sdk_peer


COUNTS = {"prompt_tokens": 44833, "completion_tokens": 4096, "total_tokens": 48929,
          "completion_tokens_details": {"reasoning_tokens": 4096}}
REASONING = "PRIVATE_SYNTHETIC_REASONING"


def install_plan(monkeypatch, turns):
    requests = []

    class Provider:
        def complete(self, system, transcript, tools, on_text):
            requests.append(copy.deepcopy({"transcript": transcript, "tools": tools}))
            assert len(requests) <= len(turns), "unexpected automatic provider retry"
            turn = turns[len(requests) - 1]
            if turn.text:
                on_text(turn.text)
            return turn

        def close(self):
            pass

    monkeypatch.setattr("canvas_hub.assistant.make_provider", lambda cfg: Provider())
    return requests


def service(hub):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "synthetic",
                       "context": {"capacity": 64000}})
    return svc


def test_reasoning_only_stop_retains_completed_write_and_retry_never_replays_it(hub, account, monkeypatch):
    call = lambda cid: ToolCall(cid, "create_note", {"kind": "note", "title": "Keep exactly one"})
    usage = normalize_usage(COUNTS, "openai_compatible")
    requests = install_plan(monkeypatch, [
        Turn("", [call("saved")], "tool_use", usage={"input_tokens": 20, "output_tokens": 5}),
        Turn("", stop="truncated", raw={"provider": "openai_compatible", "reasoning_content": REASONING}, usage=usage),
        Turn("", [call("do-not-replay")], "tool_use"),
        Turn("The earlier note is retained. Only the answer was continued."),
    ])
    svc = service(hub)
    cid = svc.send(account["id"], "Save one local note, then explain it", tool_selection=["tool:create_note"])["conversation_id"]
    svc.wait(cid, 5)
    result = svc.get_conversation(account["id"], cid)
    assert len(requests) == 2
    assert result["conversation"]["run_state"] == "error"
    assert "4096 reasoning tokens" in result["conversation"]["run_error"]
    assert result["messages"][-1]["incomplete_reason"] == "output_limit"
    assert REASONING not in json.dumps(result)
    notes = hub.annotations.list(account["id"])
    assert len(notes) == 1 and notes[0]["title"] == "Keep exactly one"
    before = svc._transcript(cid)
    assert before[-1]["raw"]["reasoning_content"] == REASONING
    rows = hub.db.query("SELECT status FROM assistant_context_usage WHERE conversation_id=? ORDER BY id", (cid,))
    assert [r["status"] for r in rows] == ["completed", "incomplete"]
    totals = result["context"]["total_usage"]
    assert (totals["input_tokens"], totals["output_tokens"], totals["reasoning_output_tokens"]) == (44853, 4101, 4096)

    # Restart and explicitly retry. Even a provider that ignores tools=[] cannot
    # repeat the completed write, and a second incomplete turn does not loop.
    svc = AssistantService(hub, HubAPI(hub))
    svc.retry(account["id"], cid); svc.wait(cid, 5)
    assert len(requests) == 3 and requests[-1]["tools"] == []
    assert hub.annotations.list(account["id"]) == notes
    assert svc._transcript(cid)[:len(before)] == before
    assert "retry_tool_disabled" in svc._transcript(cid)[-1]["results"][0]["content"]
    assert svc.get_conversation(account["id"], cid)["conversation"]["run_state"] == "error"
    svc.retry(account["id"], cid); svc.wait(cid, 5)
    assert len(requests) == 4 and requests[-1]["tools"] == []
    assert svc.get_conversation(account["id"], cid)["conversation"]["run_state"] == "idle"
    assert hub.annotations.list(account["id"]) == notes and valid_groups(svc._transcript(cid))


@pytest.mark.parametrize("text,stop,with_call,reason", [
    ("A partial explanation", "truncated", False, "output_limit"),
    ("", "truncated", True, "output_limit"),
    (" \n\t", "end", False, "empty_reply"),
])
def test_incomplete_terminal_reply_stops_without_dispatch_or_retry(hub, account, monkeypatch, text, stop, with_call, reason):
    calls = [ToolCall("cut", "create_note", {"kind": "note", "title": "Must not exist"})] if with_call else []
    requests = install_plan(monkeypatch, [Turn(text, calls, stop)])
    svc = service(hub)
    cid = svc.send(account["id"], "Synthetic question", tool_selection=["tool:create_note"])["conversation_id"]
    svc.wait(cid, 5)
    result = svc.get_conversation(account["id"], cid)
    assert len(requests) == 1 and result["conversation"]["run_state"] == "error"
    answer = next(m for m in result["messages"] if m["role"] == "assistant")
    assert answer["text"] == text and answer["incomplete_reason"] == reason
    assert not hub.annotations.list(account["id"])
    assert valid_groups(svc._transcript(cid))
    if with_call:
        rejected = json.loads(svc._transcript(cid)[-1]["results"][0]["content"])
        assert rejected["error"] == "response_incomplete" and rejected["executed"] is False


def test_reply_budget_respects_explicit_limits_endpoint_mode_and_wire_cap(sdk_peer):
    cfg = {"provider": "openai_compatible", "preset": "deepseek", "base_url": "https://api.deepseek.com/v1",
           "model": "deepseek-flash", "thinking": "low"}
    cases = [({}, 16384), ({"thinking": ""}, 16384), ({"thinking": "off"}, 4096),
             ({"base_url": "https://proxy.invalid/v1"}, 4096), ({"model": "unverified-alias"}, 4096),
             ({"context": {"capacity": 32768}}, 8192), ({"context": {"response_tokens": 4096}}, 4096),
             ({"extra_params": {"max_tokens": 2048}}, 2048)]
    for changes, expected in cases:
        configured = {**cfg, **changes}
        policy = Policy.for_config(configured)
        assert policy.response_tokens == expected and not policy.auto_compact
        assert compatible_output_tokens(configured) == expected
        assert policy.measure(configured, "System", [], [])["request_max_tokens"] == expected
    configured = {**cfg, "context": {"response_tokens": 8192}, "extra_params": {"max_tokens": 2048}}
    policy = Policy.for_config(configured)
    assert policy.response_tokens == 8192 and compatible_output_tokens(configured) == 2048
    for chosen, cap in ((cfg, 16384), (configured, 2048)):
        p, requests = sdk_peer({**chosen, "_response_tokens": Policy.for_config(chosen).response_tokens})
        p.complete("System", [{"role": "user", "sent_text": "Synthetic"}], [], on_text=lambda _: None)
        assert requests[-1]["max_tokens"] == cap
    with pytest.raises(ProviderError, match="conflict"):
        Policy.for_config({**cfg, "extra_params": {"max_tokens": 2048, "max_completion_tokens": 4096}})
    assert compatible_output_tokens({**cfg, "context": {"response_tokens": 16000}, "_response_tokens": 2000}) == 2000


def test_sdk_reasoning_only_length_stream_keeps_usage_and_does_not_retry(sdk_peer):
    def chunk(delta, finish=None, usage=None):
        return "data: " + json.dumps({"id": "synthetic", "object": "chat.completion.chunk", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            "usage": usage}) + "\n\n"
    stream = (chunk({"reasoning_content": REASONING}) + chunk({}, "length", COUNTS) + "data: [DONE]\n\n").encode()
    provider, requests = sdk_peer({"base_url": "https://api.deepseek.com/v1", "_response_tokens": 4096},
        handler=lambda *_: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream))
    visible = []
    turn = provider.complete("System", [{"role": "user", "sent_text": "Synthetic"}], [], on_text=visible.append)
    assert len(requests) == 1 and not visible and turn.text == ""
    assert turn.stop == "truncated" and turn.incomplete_reason == "output_limit"
    assert turn.usage["output_tokens"] == turn.usage["reasoning_output_tokens"] == 4096


@pytest.mark.browser
def test_incomplete_warning_and_reply_limit_in_browser(hub, account, monkeypatch, tmp_path):
    from playwright.sync_api import expect, sync_playwright
    from .test_components_ui import serve

    requests = install_plan(monkeypatch, [Turn("", stop="truncated", raw={"reasoning_content": REASONING}),
                                        Turn("A complete follow-up answer")])
    with serve(hub, []) as (base, token, api), sync_playwright() as pw:
        svc = api.assistant
        cid = svc.send(account["id"], "Synthetic question")["conversation_id"]
        svc.wait(cid, 5)
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 380, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/?token={token}")
            page.goto(f"{base}/#/assistant/{cid}")
            expect(page.get_by_role("note").filter(has_text="Incomplete reply")).to_be_visible()
            expect(page.get_by_text("The assistant stopped", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Retry answer", exact=True)).to_be_visible()
            expect(page.get_by_text("The assistant stopped", exact=True)).to_be_in_viewport()
            expect(page.get_by_role("button", name="Retry answer", exact=True)).to_be_in_viewport()
            expect(page.get_by_text("Output limit reached — details", exact=True)).to_be_visible()
            assert REASONING not in page.content() and len(requests) == 1
            page.screenshot(path=str(tmp_path / "incomplete-380.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            # A later preflight/compaction failure must not inherit the older
            # reply's cause or re-enable answer retry when context is too large.
            svc._set_state(cid, "error", "Context budget exceeded (synthetic)")
            page.reload()
            expect(page.get_by_text("Context budget exceeded (synthetic)", exact=True)).to_be_visible()
            expect(page.get_by_text("Output limit reached — details", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="Retry answer", exact=True)).to_have_count(0)
            svc.send(account["id"], "Only continue the answer; no actions", cid); svc.wait(cid, 5)
            page.reload()
            expect(page.get_by_text("A complete follow-up answer", exact=True)).to_be_visible()
            expect(page.get_by_role("note").filter(has_text="Incomplete reply")).to_be_visible()
            expect(page.get_by_text("The assistant stopped", exact=True)).to_have_count(0)
            page.set_viewport_size({"width": 1280, "height": 900})
            page.screenshot(path=str(tmp_path / "incomplete-1280.png"), full_page=True)
            pid = svc.public_config()["active_profile"]
            page.goto(f"{base}/#/assistant/settings?p={pid}")
            page.get_by_label("Reply token limit", exact=True).fill("8192")
            with page.expect_response(lambda r: r.url == base + "/api/assistant/config" and r.request.method == "PUT") as saved:
                page.get_by_role("button", name="Save", exact=True).click()
            assert saved.value.status == 200
            expect(page.get_by_label("Reply token limit", exact=True)).to_have_value("8192")
            assert svc._load(pid)["context"]["response_tokens"] == 8192
            assert len(requests) == 2 and not errors
        finally:
            browser.close()
