"""Real SDKs on synthetic HTTP transports; no provider, key, or paid request."""
import copy
import json

import httpx
import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.codex import CodexProvider
from canvas_hub.assistant.providers import AnthropicProvider, OpenAICompatibleProvider, ProviderError, Stopped, Turn
from canvas_hub.assistant.usage import normalize_usage, stored_usage, usage_totals
from canvas_hub.boundary import HubAPI
from canvas_hub.util import now_iso


CHAT = [{"role": "user", "sent_text": "Synthetic question"}]
COUNTS = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
          "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
          "completion_tokens_details": {"reasoning_tokens": 20}}


def completion(usage=COUNTS):
    return {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "synthetic",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Answer"}, "finish_reason": "stop"}],
            "usage": usage}


def chat_sse(*, usage=COUNTS, finish=True, choices_with_usage=False):
    def chunk(choices, usage=None):
        return "data: " + json.dumps({"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0,
                                      "model": "synthetic", "choices": choices, "usage": usage}) + "\n\n"
    data = chunk([{"index": 0, "delta": {"content": "Answer"}, "finish_reason": None}])
    if finish:
        data += chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}], usage if choices_with_usage else None)
    if usage is not None and not choices_with_usage:
        data += chunk([], usage)
    return (data + "data: [DONE]\n\n").encode()


def anthropic_sse():
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_test", "type": "message",
            "role": "assistant", "model": "synthetic", "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 30, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 10, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Answer"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 30, "output_tokens_details": {"thinking_tokens": 20}}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


@pytest.fixture
def sdk_peer(monkeypatch):
    import openai
    import anthropic
    original_openai, original_anthropic = openai.OpenAI, anthropic.Anthropic
    opened, requests = [], []

    def make(cfg=None, handler=None, *, anthropic=False):
        cfg = {"model": "synthetic", "api_key": "sk-synthetic-not-real", **(cfg or {})}
        import httpx2
        transport = httpx2 if anthropic else httpx
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if handler:
                return handler(request, body)
            if anthropic:
                return transport.Response(200, headers={"content-type": "text/event-stream"}, content=anthropic_sse())
            if body.get("stream"):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chat_sse())
            return httpx.Response(200, json=completion())

        def factory(**kw):
            assert kw["max_retries"] == 0
            return (original_anthropic if anthropic else original_openai)(
                **kw, http_client=transport.Client(transport=transport.MockTransport(respond)))
        monkeypatch.setattr("anthropic.Anthropic" if anthropic else "openai.OpenAI", factory)
        p = (AnthropicProvider if anthropic else OpenAICompatibleProvider)(cfg)
        opened.append(p)
        return p, requests
    yield make
    for p in opened:
        p.close()


@pytest.mark.parametrize("cfg,enabled", [
    ({}, True),
    ({"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"}, True),
    ({"base_url": "https://api.deepseek.com/v1"}, True),
    ({"preset": "ollama", "base_url": "http://localhost:11434/v1"}, True),
    ({"base_url": "https://openrouter.ai/api/v1"}, False),
    ({"preset": "openai", "base_url": "https://custom.invalid/v1"}, False),
])
def test_stream_usage_support_follows_endpoint_not_preset(sdk_peer, cfg, enabled):
    p, requests = sdk_peer(cfg)
    turn = p.complete("Stable instructions", CHAT, [], on_text=lambda _: None)
    assert (requests[0].get("stream_options") == {"include_usage": True}) is enabled
    assert (turn.usage["input_tokens"], turn.usage["output_tokens"], turn.usage["total_tokens"]) == (100, 30, 130)
    assert turn.usage["cache_read_input_tokens"] == 60
    assert turn.usage["cache_creation_input_tokens"] == 10
    assert turn.usage["reasoning_output_tokens"] == 20  # subset; not another 20 output tokens


def test_usage_on_last_content_chunk_and_nonstream(sdk_peer):
    def reply(request, body):
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=chat_sse(choices_with_usage=True))
        return httpx.Response(200, json=completion())
    p, requests = sdk_peer(handler=reply)
    streamed = p.complete("System", CHAT, [], on_text=lambda _: None)
    plain = p.complete("System", CHAT, [])
    assert streamed.usage == plain.usage
    assert "stream_options" not in requests[-1]


def test_missing_usage_does_not_trigger_a_second_billable_request(sdk_peer):
    p, requests = sdk_peer(handler=lambda *_: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                           content=chat_sse(usage=None)))
    turn = p.complete("System", CHAT, [], on_text=lambda _: None)
    assert turn.text == "Answer" and len(requests) == 1
    assert turn.usage["status"] == "unknown" and turn.usage["input_tokens"] is None
    assert usage_totals([turn.usage])["unreported_requests"] == 1


def test_only_rejected_usage_option_is_removed_then_remembered(sdk_peer):
    def reply(request, body):
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'stream_options'",
                "param": "stream_options", "code": "unsupported_parameter", "type": "invalid_request_error"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chat_sse())
    p, requests = sdk_peer(handler=reply)
    p.complete("System", CHAT, [], on_text=lambda _: None)
    p.complete("System", CHAT, [], on_text=lambda _: None)
    assert len(requests) == 3 and all(r["stream"] for r in requests)
    assert ["stream_options" in r for r in requests] == [True, False, False]


def test_known_stream_rejection_falls_back_without_stream_options(sdk_peer):
    def reply(request, body):
        if body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "stream is not supported for this model"}})
        return httpx.Response(200, json=completion())
    p, requests = sdk_peer(handler=reply)
    assert p.complete("System", CHAT, [], on_text=lambda _: None).text == "Answer"
    assert len(requests) == 2 and "stream_options" not in requests[1]


@pytest.mark.parametrize("status,message", [(400, "stream_options must be an object"),
    (400, "Request failed while streaming"), (429, "stream is not supported for this model"),
    (500, "stream is not supported for this model")])
def test_ambiguous_failures_are_never_capability_retries(sdk_peer, status, message):
    p, requests = sdk_peer(handler=lambda *_: httpx.Response(status, json={"error": {"message": message}}))
    with pytest.raises(ProviderError):
        p.complete("System", CHAT, [], on_text=lambda _: None)
    assert len(requests) == 1


def test_single_attempt_summary_never_negotiates(sdk_peer):
    p, requests = sdk_peer({"_single_attempt": True}, handler=lambda *_: httpx.Response(
        400, json={"error": {"message": "Unsupported parameter: stream_options"}}))
    with pytest.raises(ProviderError):
        p.complete("System", CHAT, [], on_text=lambda _: None)
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["drop", "error_after_accept", "cancel"])
def test_accepted_stream_failure_or_stop_is_not_replayed(sdk_peer, failure):
    import openai
    closed = []
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield chat_sse(usage=None, finish=False).split(b"data: [DONE]")[0]
            if failure == "error_after_accept":
                raise openai.BadRequestError("stream is not supported for this model", response=httpx.Response(
                    400, request=httpx.Request("POST", "https://synthetic.invalid")),
                    body={"message": "stream is not supported for this model"})
            if failure == "drop":
                raise httpx.ReadError("synthetic dropped connection")
        def close(self):
            closed.append(True)
    p, requests = sdk_peer(handler=lambda *_: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()))
    def text(_):
        if failure == "cancel":
            raise Stopped()
    with pytest.raises((ProviderError, httpx.ReadError, Stopped)):
        p.complete("System", CHAT, [], on_text=text)
    assert len(requests) == 1 and closed
    assert p.last_usage["status"] == "unknown"


def test_anthropic_final_usage_replaces_snapshot_and_preserves_cache_semantics(sdk_peer):
    p, _ = sdk_peer(anthropic=True)
    turn = p.complete("System", CHAT, [], on_text=lambda _: None)
    assert turn.usage["input_tokens"] == 100 and turn.usage["output_tokens"] == 30
    assert turn.usage["provider_usage"]["input_tokens"] == 30
    assert turn.usage["reasoning_output_tokens"] == 20  # subset reported on the final message_delta
    assert turn.usage["total_tokens"] == 130


@pytest.mark.parametrize("anthropic", [False, True])
def test_tool_prefix_is_stable_for_reordered_groups_and_schemas(sdk_peer, anthropic):
    a = {"name": "a", "description": "tool A", "schema": {"type": "object", "required": ["z", "a"],
         "properties": {"z": {"type": "string"}, "a": {"enum": ["z", "a"], "type": "string"}}}}
    b = {"name": "b", "description": "tool B", "schema": {"type": "object"}}
    tools = [b, a]
    original = copy.deepcopy(tools)
    p, requests = sdk_peer(anthropic=anthropic)
    p.complete("Stable system\nCurrent scope", CHAT, tools, on_text=lambda _: None)
    changed = copy.deepcopy(a)
    changed["schema"] = dict(reversed(list(changed["schema"].items())))
    changed["schema"]["properties"] = dict(reversed(list(changed["schema"]["properties"].items())))
    p.complete("Stable system\nCurrent scope", CHAT, [changed, b], on_text=lambda _: None)
    assert json.dumps(requests[0]["tools"]) == json.dumps(requests[1]["tools"])
    assert tools == original
    schema = requests[0]["tools"][0]["input_schema"] if anthropic else requests[0]["tools"][0]["function"]["parameters"]
    assert schema["required"] == ["z", "a"] and schema["properties"]["a"]["enum"] == ["z", "a"]
    p.complete("Stable system\nCurrent scope", CHAT, [b], on_text=lambda _: None)
    assert len(requests[-1]["tools"]) == 1  # no stale tool retained after a group is disabled


def test_native_counters_missing_fields_and_estimates_are_not_added_twice():
    ds = normalize_usage({"prompt_tokens": 100, "completion_tokens": 30, "prompt_cache_hit_tokens": 60,
                          "prompt_cache_miss_tokens": 40, "completion_tokens_details": {"reasoning_tokens": 20}}, "deepseek")
    gateway = normalize_usage({"input_tokens": 100, "output_tokens": 30, "cached_input_tokens": 60,
                               "cache_write_input_tokens": 10, "reasoning_tokens": 20}, "gateway")
    legacy_anthropic = stored_usage({"input_tokens": 30, "cache_read_input_tokens": 60,
                                    "cache_creation_input_tokens": 10, "output_tokens": 30}, "anthropic")
    invalid = normalize_usage({"prompt_tokens": True, "completion_tokens": -1}, "openai_compatible")
    missing_anthropic = normalize_usage({"input_tokens": 30, "output_tokens": 10}, "anthropic")
    totals = usage_totals([ds, gateway, legacy_anthropic, invalid, missing_anthropic,
                          {"source": "estimated", "input_tokens": 500, "output_tokens": 60}])
    assert totals["input_tokens"] == 300 and totals["output_tokens"] == 100
    assert totals["cache_read_input_tokens"] == 180 and totals["reasoning_output_tokens"] == 40
    assert totals["unreported_requests"] == 3 and totals["estimated_requests"] == 1
    assert not totals["quota_authoritative"]
    assert invalid["input_tokens"] is None and missing_anthropic["input_tokens"] is None


@pytest.mark.parametrize("counts,output,status", [
    ({"prompt_tokens": 27, "completion_tokens": 45, "total_tokens": 576,
      "completion_tokens_details": {"reasoning_tokens": 504}}, 549, "reported"),
    ({"prompt_tokens": 27, "completion_tokens": 549, "total_tokens": 576,
      "completion_tokens_details": {"reasoning_tokens": 504}}, 549, "reported"),
    ({"prompt_tokens": 27, "completion_tokens": 45, "total_tokens": 576}, None, "partial"),
])
def test_gemini_reasoning_is_reconciled_only_with_explicit_counters(sdk_peer, counts, output, status):
    p, _ = sdk_peer({"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
                   handler=lambda *_: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chat_sse(usage=counts)))
    u = p.complete("System", CHAT, [], on_text=lambda _: None).usage
    assert u["output_tokens"] == output and u["status"] == status
    assert u["provider_usage"]["completion_tokens"] == counts["completion_tokens"]
    if output is None:
        assert u["reasoning_output_tokens"] is None and usage_totals([u])["unreported_requests"] == 1


def test_anthropic_interruption_keeps_snapshot_nonterminal_without_replay(sdk_peer):
    import httpx2
    partial = anthropic_sse().split(b"event: message_delta")[0]
    p, requests = sdk_peer(anthropic=True, handler=lambda *_: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=partial))
    with pytest.raises(ProviderError, match="cut off"):
        p.complete("System", CHAT, [], on_text=lambda _: None)
    assert len(requests) == 1 and not p.last_usage["final"]
    assert usage_totals([p.last_usage])["unreported_requests"] == 1


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_request_ledger_covers_missing_usage_and_survives_restart(hub, account, monkeypatch, outcome):
    calls = []
    class Provider:
        def complete(self, *args, **kwargs):
            calls.append(True)
            if outcome == "failed":
                raise ProviderError("synthetic failure")
            if outcome == "cancelled":
                raise Stopped()
            return Turn("Answer", usage=normalize_usage(COUNTS, "openai_compatible"), raw={"provider": "openai_compatible"})
        def close(self):
            pass
    monkeypatch.setattr("canvas_hub.assistant.make_provider", lambda _: Provider())
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "custom", "base_url": "https://synthetic.invalid", "model": "synthetic",
                       "context": {"capacity": 64000, "auto_compact": False}})
    cid = svc.send(account["id"], "Synthetic user message")["conversation_id"]
    svc.wait(cid, 5)
    row = hub.db.one("SELECT * FROM assistant_context_usage WHERE conversation_id=?", (cid,))
    assert row["status"] == outcome and row["purpose"] == "chat" and len(calls) == 1
    resumed = AssistantService(hub, HubAPI(hub))
    info = resumed.get_conversation(account["id"], cid)["context"]
    assert info["total_usage"]["requests"] == 1  # stored display message is not counted again
    assert info["summary_requests"] == 0
    assert info["total_usage"]["unreported_requests"] == (outcome != "completed")
    assert info["total_usage"]["input_tokens"] == (100 if outcome == "completed" else 0)
    # Reuse PR36's summary ledger: one manual summary must add exactly once.
    with hub.db.tx() as c:
        c.execute("INSERT INTO assistant_context_usage(conversation_id,provider,status,usage_json,estimated_input_tokens,created_at) "
                  "VALUES (?,'openai_compatible','completed',?,999,?)",
                  (cid, json.dumps({"input_tokens": 10, "output_tokens": 5}), now_iso()))
    info = resumed.get_conversation(account["id"], cid)["context"]
    assert info["total_usage"]["input_tokens"] == (110 if outcome == "completed" else 10)
    assert info["summary_usage"]["input_tokens"] == 10 and info["summary_requests"] == 1


def test_codex_cumulative_usage_deltas_cover_multiple_updates_and_tool_boundaries():
    p = CodexProvider({})
    p.server = object()
    p.thread_id, p.turn_id = "thread", "turn"
    totals = {"inputTokens": 100, "outputTokens": 30, "cachedInputTokens": 60,
              "reasoningOutputTokens": 20, "cacheWriteInputTokens": 10}
    def update(value):
        return {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread", "tokenUsage": {"total": value, "last": {"inputTokens": 1}}}}
    messages = [update(totals), update(totals), {"id": 1, "method": "item/tool/call", "params": {
        "threadId": "thread", "tool": "a", "callId": "call", "arguments": {}}}]
    class Server:
        def receive(self, *_):
            return messages.pop(0)
        def send(self, _):
            pass
    p.server = Server()
    first = p.complete("", [], [])
    assert first.usage["input_tokens"] == 100
    messages.extend([update(totals), update({k: v * 2 for k, v in totals.items()}),
                     {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}}])
    second = p.complete("", [{"role": "tool", "results": [{"id": "call", "content": "done"}]}], [])
    assert second.usage["input_tokens"] == 100 and second.usage["output_tokens"] == 30
    assert usage_totals([first.usage, second.usage])["input_tokens"] == 200
