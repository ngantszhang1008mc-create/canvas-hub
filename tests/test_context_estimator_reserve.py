"""Offline transport-representation and output-reserve checks; no live calls."""
import copy
import json
import math

import pytest

from canvas_hub.assistant.context import Policy, encoded, estimate
from canvas_hub.assistant.providers import AnthropicProvider, OpenAICompatibleProvider, ProviderError

from .test_provider_usage import sdk_peer  # SDK on a synthetic in-memory transport.


def report(cfg, transcript=(), tools=()):
    return Policy.for_config(cfg).measure(cfg, "system", list(transcript), list(tools))


def legacy(value):
    text = value if isinstance(value, str) else encoded(value)
    ascii_chars = sum(ord(c) < 128 for c in text)
    return math.ceil(ascii_chars / 3 + len(text.encode("utf-8")) - ascii_chars) + 8


@pytest.mark.parametrize("value", ["", "short text", "a" * 10001, "中文 😀", "\"\\\n\t" * 50,
                                  {"nested": {"content": '{"literal":"\\n"}'}}])
def test_fallback_divisor_and_unicode_weight_are_unchanged(value):
    assert estimate(value) == legacy(value)


@pytest.mark.parametrize("base_url", [
    None, "",
    "https://unknown.invalid/v1", "http://api.openai.com/v1", "https://api.openai.com.evil.invalid/v1",
    "https://user@api.openai.com/v1", "https://user:password@api.openai.com/v1",
    "https://api.openai.com/v1?proxy=1", "https://api.openai.com/v1#fragment",
    "https://api.openai.com:444/v1", "https://api.openai.com:invalid/v1",
    "https://api.openai.com/v1/proxy", " https://api.openai.com/v1", "https://api.openai.com\\proxy/v1",
    "http://localhost:11434/v1",
])
def test_unknown_or_noncanonical_endpoint_keeps_serialized_fallback(base_url):
    cfg = {"provider": "openai_compatible", "model": "gpt-named-but-unverified", "base_url": base_url}
    transcript = [{"role": "user", "sent_text": '\\"\n' * 200}]
    measured = report(cfg, transcript)
    messages = OpenAICompatibleProvider._messages(None, "", transcript)[1:]
    assert measured["used"]["history"] == legacy(messages) + 24
    assert measured["estimator_provenance"]["message_representation"] == "serialized_projection"
    assert measured["estimator_provenance"]["tokenizer"] is None
    assert not measured["estimator_provenance"]["model_calibrated"]


@pytest.mark.parametrize("provider,base_url", [
    ("anthropic", "https://api.anthropic.com"),
    ("openai_compatible", "https://api.openai.com/v1"),
    ("openai_compatible", "https://api.deepseek.com:443/v1/"),
])
def test_documented_content_counts_literal_text_not_outer_http_escaping(provider, base_url):
    cfg = {"provider": provider, "base_url": base_url, "model": "unknown-tokenizer"}
    content = '{"path":"C:\\\\tmp","quote":"\\\"","line":"\\n"}\n' * 100
    transcript = [{"role": "user", "sent_text": content}]
    saved = copy.deepcopy(transcript)
    measured = report(cfg, transcript)
    shell = encoded([{"role": "user", "content": ""}])
    expected = math.ceil((len(shell) + len(content)) / 3) + 8 + 24
    assert measured["used"]["history"] == expected
    assert measured["used"]["history"] < legacy([{"role": "user", "content": content}]) + 24
    assert transcript == saved
    provenance = measured["estimator_provenance"]
    assert provenance["message_representation"] == "decoded_text_with_structural_framing"
    assert provenance["method"] == "utf8_heuristic_v1"
    assert provenance["tokenizer"] is None and not provenance["model_calibrated"]
    assert not provenance["network_token_count"]


def test_chat_arguments_keep_json_escapes_and_native_reasoning():
    cfg = {"provider": "openai_compatible", "base_url": "https://api.deepseek.com/v1"}
    transcript = [{"role": "assistant", "text": "answer", "tool_calls": [
        {"id": "call", "name": "inspect", "input": {"quote": '\\"\n' * 100}}],
        "raw": {"reasoning_content": '思考\n"' * 100}}]
    messages = OpenAICompatibleProvider._messages(None, "", transcript)[1:]
    shell = copy.deepcopy(messages)
    pieces = [shell[0]["content"], shell[0]["reasoning_content"], shell[0]["tool_calls"][0]["function"]["arguments"]]
    shell[0]["content"] = shell[0]["reasoning_content"] = shell[0]["tool_calls"][0]["function"]["arguments"] = ""
    # Concatenation is only for an independent arithmetic check, never a request.
    expected = estimate(encoded(shell) + "".join(pieces)) + 24
    saved = copy.deepcopy(transcript)
    measured = report(cfg, transcript)
    assert measured["used"]["history"] == expected
    assert transcript == saved
    transcript[0]["raw"]["reasoning_content"] = ""
    assert report(cfg, transcript)["used"]["history"] < measured["used"]["history"]


def test_anthropic_preserves_native_blocks_signatures_unknown_fields_and_nested_input():
    cfg = {"provider": "anthropic", "base_url": "https://api.anthropic.com"}
    blocks = [
        {"type": "thinking", "thinking": '思考\n"' * 80, "signature": 'signed\\"' * 50},
        {"type": "redacted_thinking", "data": 'opaque\\"' * 80},
        {"type": "text", "text": 'answer\n"' * 40},
        {"type": "tool_use", "id": "call", "name": "inspect", "input": {"nested": '\\"' * 90}},
        {"type": "future_native_block", "data": 'retained\\"' * 60},
    ]
    transcript = [{"role": "assistant", "text": "display text is not the replayed native content",
                   "raw": {"provider": "anthropic", "content": blocks}}]
    saved = copy.deepcopy(transcript)
    shell = [{"role": "assistant", "content": copy.deepcopy(blocks)}]
    texts = [shell[0]["content"][0]["thinking"], shell[0]["content"][2]["text"]]
    shell[0]["content"][0]["thinking"] = shell[0]["content"][2]["text"] = ""
    expected = estimate(encoded(shell) + "".join(texts)) + 24
    assert report(cfg, transcript)["used"]["history"] == expected
    assert transcript == saved
    for index in (0, 1, 3, 4):
        smaller = copy.deepcopy(transcript)
        smaller[0]["raw"]["content"].pop(index)
        assert report(cfg, smaller)["used"]["history"] < expected


@pytest.mark.parametrize("provider,base_url", [("anthropic", "https://api.anthropic.com"), ("openai_compatible", "https://api.openai.com/v1")])
def test_mixed_document_and_tool_results_keep_all_cost_and_explicit_limits(provider, base_url):
    cfg = {"provider": provider, "base_url": base_url, "context": {"tool_tokens": 512, "document_tokens": 512}}
    transcript = [{"role": "tool", "results": [
        {"id": "a", "name": "read_document", "content": '規定\n"' * 500},
        {"id": "b", "name": "get_component", "content": '\\"' * 1000, "is_error": True},
    ]}]
    measured = report(cfg, transcript)
    assert measured["used"]["documents"] > 512 and measured["used"]["tools"] > 512
    assert measured["over_budget"]
    assert measured["total_with_response"] == measured["overhead_tokens"] + sum(measured["used"].values()) + measured["response_reserved"]
    assert measured["effective_capacity"] == int(measured["capacity"] * .9)
    assert measured["safety_headroom"] == measured["capacity"] - measured["effective_capacity"]
    assert not Policy.for_config(cfg).auto_compact


@pytest.mark.parametrize("context,extra,reserve,wire", [
    ({}, {}, 6000, 6000),
    ({"capacity": 64000}, {}, 6000, 6000),
    ({"capacity": 64001}, {}, 16000, 16000),
    ({"capacity": 200000}, {}, 16000, 16000),
    ({"capacity": 4096}, {}, 6000, 6000),  # stop rather than silently truncate the cap
    ({"response_tokens": 1024}, {}, 1024, 1024),
    ({}, {"max_tokens": 1024}, 1024, 1024),
    ({}, {"max_completion_tokens": 2048}, 2048, 2048),
    ({"response_tokens": 8000}, {"max_tokens": 1024}, 8000, 1024),
    ({"response_tokens": 1024}, {"max_tokens": 12000}, 12000, 12000),
    ({"capacity": 64000}, {"max_tokens": 90000}, 90000, 90000),
])
def test_capacity_default_and_explicit_output_caps_match_wire(sdk_peer, context, extra, reserve, wire):
    cfg = {"provider": "anthropic", "effort": "max", "context": context, "extra_params": extra}
    saved = copy.deepcopy(cfg)
    policy = Policy.for_config(cfg)
    measured = report(cfg)
    assert policy.response_tokens == measured["response_reserved"] == reserve
    assert measured["request_max_tokens"] == wire
    assert measured["over_budget"] == (reserve + measured["overhead_tokens"] > measured["effective_capacity"])
    assert not policy.auto_compact and cfg == saved
    provider, requests = sdk_peer({**cfg, "_response_tokens": reserve}, anthropic=True)
    provider.complete("system", [{"role": "user", "sent_text": "question"}], [])
    assert requests[0]["max_tokens"] == wire
    assert requests[0]["output_config"] == {"effort": "max"}
    assert "thinking" not in requests[0]
    assert len(requests) == 1 and cfg == saved


@pytest.mark.parametrize("thinking", [
    {"thinking": "high"}, {"thinking": {"type": "enabled", "budget_tokens": 10000}},
    {"extra_params": {"thinking": {"type": "enabled", "budget_tokens": 10000}}},
    {"extra_params": {"thinking": {"type": "adaptive"}}},
    {"extra_params": {"budget_tokens": 10000}},
])
def test_unsupported_thinking_is_rejected_before_constructing_client(monkeypatch, thinking):
    cfg = {"provider": "anthropic", **thinking}
    saved = copy.deepcopy(cfg)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: pytest.fail("must reject before client creation"))
    with pytest.raises(ProviderError, match="configured thinking was not changed"):
        Policy.for_config(cfg)
    with pytest.raises(ProviderError, match="configured thinking was not changed"):
        AnthropicProvider(cfg)
    assert cfg == saved


@pytest.mark.parametrize("value", [0, -1, True, "6000", None])
def test_invalid_output_cap_is_rejected_not_replaced(value):
    with pytest.raises(ProviderError, match="positive integers"):
        Policy.for_config({"provider": "anthropic", "extra_params": {"max_tokens": value}})


def test_conflicting_provider_caps_are_rejected_and_summary_bound_stays_explicit(sdk_peer):
    with pytest.raises(ProviderError, match="conflict"):
        Policy.for_config({"provider": "anthropic", "extra_params": {"max_tokens": 6000, "max_completion_tokens": 8000}})
    provider, requests = sdk_peer({"_single_attempt": True, "_response_tokens": 2000,
                                   "context": {"response_tokens": 16000}}, anthropic=True)
    provider.complete("system", [{"role": "user", "sent_text": "summary"}], [])
    assert requests[0]["max_tokens"] == 2000


def test_codex_and_unknown_anthropic_endpoint_remain_serialized():
    for cfg in ({"provider": "codex", "model": "gpt-6.1-sol"},
                {"provider": "anthropic"},
                {"provider": "anthropic", "base_url": "https://custom.invalid"}):
        provenance = report(cfg, [{"role": "user", "sent_text": '\\"' * 100}])["estimator_provenance"]
        assert provenance["message_representation"] == "serialized_projection"
        assert provenance["message_format"] is None and provenance["tokenizer"] is None
