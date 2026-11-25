"""Unit tests for OpenAIResponsesProvider (GPT-6.2 Astra / Sol Responses API)."""
from __future__ import annotations

from types import SimpleNamespace

from canvas_hub.assistant import reasoning_levels
from canvas_hub.assistant.model_capacity import known
from canvas_hub.assistant.openai_responses import OpenAIResponsesProvider, uses_openai_responses
from canvas_hub.assistant.providers import OpenAICompatibleProvider, make_provider


class _FakeStream:
    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *_args):
        return False


def test_make_provider_routes_openai_gpt6_to_responses_api(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    cfg_astra = {
        "preset": "openai",
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-6.2-astra",
    }
    assert uses_openai_responses(cfg_astra) is True
    prov = make_provider(cfg_astra)
    try:
        assert isinstance(prov, OpenAIResponsesProvider)
    finally:
        prov.close()

    # Older models and non-OpenAI hosts stay on OpenAICompatibleProvider.
    cfg_4o = {**cfg_astra, "model": "gpt-4o-mini"}
    assert uses_openai_responses(cfg_4o) is False
    prov_4o = make_provider(cfg_4o)
    try:
        assert isinstance(prov_4o, OpenAICompatibleProvider)
    finally:
        prov_4o.close()

    cfg_other_host = {
        "preset": "custom",
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "gpt-6.2-astra",
    }
    assert uses_openai_responses(cfg_other_host) is False


def test_gpt62_astra_capacity_and_reasoning_levels():
    cfg = {
        "preset": "openai",
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-6.2-astra",
    }
    assert reasoning_levels(cfg) == ["low", "medium", "high", "xhigh", "max"]
    cap = known(cfg)
    assert cap is not None
    assert cap["context"] == 1_050_000
    assert cap["max_output"] == 128_000


def test_openai_responses_request_shape_streaming_tools_and_reasoning_replay(monkeypatch):
    captured = []

    class _FakeResponses:
        def create(self, **kwargs):
            captured.append(kwargs)
            if len(captured) == 1:
                reasoning_item = SimpleNamespace(
                    type="reasoning",
                    id="rs_1",
                    summary=[{"type": "summary_text", "text": "Checking deadlines"}],
                    encrypted_content="enc_blob_123",
                    model_dump=lambda exclude_none=True: {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Checking deadlines"}],
                        "encrypted_content": "enc_blob_123",
                    },
                )
                fn_call = SimpleNamespace(
                    type="function_call",
                    call_id="call_1",
                    name="get_deadlines",
                    arguments='{"days": 7}',
                )
                usage = SimpleNamespace(
                    input_tokens=1200,
                    output_tokens=180,
                    total_tokens=1380,
                    input_tokens_details=SimpleNamespace(cached_tokens=900),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=110),
                )
                resp = SimpleNamespace(model="gpt-6.2-astra", output=[reasoning_item, fn_call], usage=usage)
                return _FakeStream([SimpleNamespace(type="response.completed", response=resp)])
            msg_item = SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="You have 2 upcoming deadlines.")],
            )
            usage = SimpleNamespace(
                input_tokens=1500,
                output_tokens=40,
                total_tokens=1540,
                input_tokens_details=SimpleNamespace(cached_tokens=1200),
                output_tokens_details=SimpleNamespace(reasoning_tokens=10),
            )
            resp = SimpleNamespace(model="gpt-6.2-astra", output=[msg_item], usage=usage)
            return _FakeStream([
                SimpleNamespace(type="response.output_text.delta", delta="You have 2 "),
                SimpleNamespace(type="response.output_text.delta", delta="upcoming deadlines."),
                SimpleNamespace(type="response.completed", response=resp),
            ])

    monkeypatch.setenv("CANVAS_HUB_DEMO_OPENAI_API_KEY", "sk-demo-env-only")
    prov = OpenAIResponsesProvider({
        "preset": "openai",
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-6.2-astra",
        "thinking": "high",
        "context": {"reply_limit": "none"},
    })
    prov.client = SimpleNamespace(responses=_FakeResponses(), close=lambda: None)

    tools = [{"name": "get_deadlines", "description": "Read deadlines", "schema": {"type": "object", "properties": {"days": {"type": "integer"}}}}]
    turn1 = prov.complete("System instructions", [{"role": "user", "sent_text": "What is due?"}], tools)
    assert turn1.stop == "tool_use"
    assert len(turn1.tool_calls) == 1
    assert turn1.tool_calls[0].name == "get_deadlines"
    assert turn1.tool_calls[0].input == {"days": 7}
    assert turn1.usage["input_tokens"] == 1200
    assert turn1.usage["cache_read_input_tokens"] == 900
    assert turn1.usage["reasoning_output_tokens"] == 110
    assert turn1.raw["reasoning_items"][0]["encrypted_content"] == "enc_blob_123"

    # Verify GPT-6.2 official request shape on call 1
    req1 = captured[0]
    assert req1["model"] == "gpt-6.2-astra"
    assert req1["store"] is False
    assert req1["stream"] is True
    assert req1["include"] == ["reasoning.encrypted_content"]
    assert req1["reasoning"] == {"effort": "high"}
    assert "max_output_tokens" not in req1
    assert "instructions" not in req1  # placed in leading developer item for prefix caching
    assert req1["input"][0] == {"role": "developer", "content": "System instructions"}
    assert req1["tools"] == [{
        "type": "function",
        "name": "get_deadlines",
        "description": "Read deadlines",
        "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}},
        "strict": False,
    }]

    # Round 2: replay encrypted reasoning + function_call + function_call_output
    transcript2 = [
        {"role": "user", "sent_text": "What is due?"},
        {"role": "assistant", "text": "", "tool_calls": [{"id": "call_1", "name": "get_deadlines", "input": {"days": 7}}], "raw": turn1.raw},
        {"role": "tool", "results": [{"id": "call_1", "content": '{"items": []}'}]},
    ]
    deltas = []
    turn2 = prov.complete("System instructions", transcript2, tools, on_text=deltas.append)
    assert turn2.stop == "end"
    assert turn2.text == "You have 2 upcoming deadlines."
    assert deltas == ["You have 2 ", "You have 2 upcoming deadlines."]

    req2_types = [item.get("type") or item.get("role") for item in captured[1]["input"]]
    assert req2_types == ["developer", "user", "reasoning", "function_call", "function_call_output"]
    assert captured[1]["input"][2]["encrypted_content"] == "enc_blob_123"
