"""Token counts, model limits and the reply limit: accurate numbers, and the user's own choices."""
import json

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import Policy, calibration, estimate
from canvas_hub.assistant.model_capacity import _from_model_object, resolve
from canvas_hub.assistant.providers import ProviderError, Turn, anthropic_output_tokens, compatible_output_tokens
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput, now_iso

from .test_provider_usage import sdk_peer  # noqa: F401 - fixture: the real SDKs on an in-memory transport

CUSTOM = {"provider": "openai_compatible", "base_url": "https://llm.example.edu/v1", "model": "course-model"}
DEEPSEEK = {"provider": "openai_compatible", "preset": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"}
CLAUDE = {"provider": "anthropic", "model": "claude-opus-5-5"}


# ---- counting ----------------------------------------------------------------------------
def test_estimates_are_close_for_english_and_for_chinese():
    english = "The midterm covers lectures one to six and is held in the main hall. " * 40
    chinese = "期中考试涵盖第一讲到第六讲，在大礼堂举行。" * 40
    # English is about four characters per token; Chinese about one token per character.
    assert len(english) / 4.5 < estimate(english) < len(english) / 3
    assert len(chinese) * 0.8 < estimate(chinese) < len(chinese) * 1.3
    assert estimate("") < 10


def test_estimates_are_corrected_with_what_the_provider_counted():
    assert calibration([]) == {"factor": 1.0, "samples": 0}
    # Median of the usable pairs; tiny requests and missing counts teach nothing.
    learned = calibration([(1000, 800), (2000, 1500), (1000, 5000), (50, 500), (1000, None), (3000, 2400)])
    assert learned == {"factor": 0.8, "samples": 4}
    assert calibration([(1000, 10)])["factor"] == 0.3 and calibration([(1000, 90000)])["factor"] == 3.0
    transcript = [{"role": "user", "sent_text": "word " * 4000}]
    policy = Policy.for_config(CLAUDE)
    plain, corrected = policy.measure(CLAUDE, "system", transcript, []), policy.measure(CLAUDE, "system", transcript, [], learned)
    assert corrected["raw_estimated_tokens"] == plain["estimated_tokens"]
    assert corrected["estimated_tokens"] == pytest.approx(plain["estimated_tokens"] * 0.8, rel=0.01)
    assert plain["safety_margin_percent"] == 10 and corrected["safety_margin_percent"] == 3
    assert "provider's own counts" in corrected["estimator"]


def chat(hub, account, monkeypatch, usage, cfg=None):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "custom", "base_url": CUSTOM["base_url"], "model": CUSTOM["model"], **(cfg or {})})
    requests, configs = [], []

    class Provider:
        pending = None

        def __init__(self, config):
            configs.append(config)

        def complete(self, system, transcript, tools, on_text=None):
            requests.append(transcript)
            return Turn("Answer", usage=dict(usage(svc, len(requests))) if callable(usage) else dict(usage))

        def close(self):
            pass
    monkeypatch.setattr("canvas_hub.assistant.make_provider", Provider)
    with hub.db.tx() as c:
        cid = c.execute("INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)",
                        (account["id"], "Chat", now_iso(), now_iso())).lastrowid

    def ask(text):
        svc.send(account["id"], text, cid)
        svc.wait(cid, 10)
        result = svc.get_conversation(account["id"], cid)
        assert result["conversation"]["run_state"] == "idle", result["conversation"]
        return result["context"]
    return svc, ask, requests, configs


def test_the_next_estimate_uses_the_last_reported_count_for_the_same_model(hub, account, monkeypatch):
    # This provider counts 40% fewer tokens than the text-length estimate.
    def usage(svc, n):
        raw = next(iter(svc._context_reports.values()))["raw_estimated_tokens"]
        return {"input_tokens": int(raw * 0.6), "output_tokens": 25}
    svc, ask, _, _ = chat(hub, account, monkeypatch, usage)
    first = ask("Summarise week three. " * 60)
    assert first["budget"]["estimator_provenance"]["calibration_samples"] == 0
    reported = first["last_request"]["reported_input_tokens"]
    assert first["last_request"] == {"model": CUSTOM["model"], "estimated_input_tokens": first["budget"]["estimated_tokens"],
                                     "reported_input_tokens": reported, "reported_output_tokens": 25}
    second = ask("And week four?")
    budget = second["budget"]
    assert budget["estimator_provenance"]["calibration_samples"] == 1 and budget["estimator_provenance"]["calibration_factor"] == pytest.approx(0.6, abs=0.01)
    assert budget["estimated_tokens"] == pytest.approx(budget["raw_estimated_tokens"] * 0.6, rel=0.01)
    # The corrected estimate now matches what the provider reports for this request.
    assert second["last_request"]["reported_input_tokens"] == pytest.approx(second["last_request"]["estimated_input_tokens"], rel=0.02)
    # Another model starts uncorrected: one model's counting says nothing about another's.
    svc.update_config({"model": "another-model"})
    assert ask("Hello again")["budget"]["estimator_provenance"]["calibration_samples"] == 0


# ---- the model's real limits ---------------------------------------------------------------
def test_limits_come_from_the_provider_then_published_values_and_are_never_invented():
    assert resolve(CLAUDE) == {"context": 1_000_000, "max_output": 128_000, "source": "Anthropic's published model limits", "basis": "published"}
    assert resolve({**CLAUDE, "model": "claude-haiku-4-5"})["context"] == 200_000
    assert resolve(DEEPSEEK)["context"] == 1_048_576
    # An unknown model, a proxy in front of Claude, an alias: no guess.
    for cfg in (CUSTOM, {**CLAUDE, "base_url": "https://proxy.example.edu"}, {**CLAUDE, "model": "claude-next"},
                {**DEEPSEEK, "base_url": "https://relay.example.edu/v1"}, {"provider": "codex"}):
        assert resolve(cfg) == {"context": None, "max_output": None, "source": "not known for this model", "basis": "unknown"}
        policy = Policy.for_config(cfg)
        assert policy.capacity is None and not policy.enforced
    # What the provider reported for exactly this model and endpoint wins over the table.
    found = {"context": 400_000, "max_output": 32_000, "source": "reported by llm.example.edu", "model": CUSTOM["model"], "base_url": CUSTOM["base_url"]}
    assert resolve({**CUSTOM, "detected_limits": found}) == {"context": 400_000, "max_output": 32_000, "source": found["source"], "basis": "detected"}
    assert resolve({**CUSTOM, "model": "changed", "detected_limits": found})["basis"] == "unknown"
    # The user's own number wins over everything.
    policy = Policy.for_config({**CLAUDE, "context": {"capacity": 50_000}})
    assert (policy.capacity, policy.capacity_basis, policy.capacity_source) == (50_000, "configured", "set by you")


@pytest.mark.parametrize("answer,expected", [
    ({"id": "m", "context_length": 200000, "top_provider": {"max_completion_tokens": 8192}}, (200000, 8192)),
    ({"id": "m", "max_input_tokens": 1000000, "max_tokens": 128000}, (1000000, 128000)),
    ({"id": "m", "inputTokenLimit": 1048576, "outputTokenLimit": 65536}, (1048576, 65536)),
    ({"id": "m", "max_tokens": 4096}, (None, None)),                     # ambiguous on its own
    ({"id": "m", "object": "model", "owned_by": "x"}, (None, None)),
    ({"id": "m", "context_length": "big", "meta": {"n_ctx": 8192}}, (8192, None)),
])
def test_limits_are_read_from_the_shapes_servers_actually_return(answer, expected):
    assert _from_model_object(answer) == expected


def test_an_unknown_window_is_not_limited_and_a_known_one_stops_an_oversized_request(hub, account, monkeypatch):
    def usage(svc, n):                                    # this provider counts exactly what was estimated
        return {"input_tokens": next(iter(svc._context_reports.values()))["raw_estimated_tokens"], "output_tokens": 5}
    svc, ask, requests, _ = chat(hub, account, monkeypatch, usage)
    for _ in range(8):                                    # together far beyond the old invented 32,768-token limit
        context = ask("lecture notes " * 1400)
    assert len(requests) == 8 and context["budget"]["estimated_tokens"] > 40000
    assert context["budget"]["capacity"] is None and not context["budget"]["enforced"]
    # With a window the user set, the same request is refused before anything is sent, with real numbers.
    svc.update_config({"context": {"capacity": 20000}})
    cid = svc.list_conversations(account["id"])[0]["id"]
    svc.send(account["id"], "again", cid)
    svc.wait(cid, 10)
    error = svc.get_conversation(account["id"], cid)["conversation"]["run_error"]
    assert len(requests) == 8 and "too large for the model's context window" in error
    assert "the context window is 20,000 (set by you)" in error and "Nothing was sent" in error


def test_save_and_test_asks_the_provider_for_limits_and_remembers_them(hub, account, monkeypatch):
    svc, _, _, _ = chat(hub, account, monkeypatch, {"input_tokens": 10, "output_tokens": 1})
    assert svc.public_config()["limits"]["context_window"] is None
    asked = []
    monkeypatch.setattr("canvas_hub.assistant.detect_limits", lambda cfg: asked.append(cfg["model"]) or {
        "context": 262144, "max_output": 16384, "source": "reported by llm.example.edu", "model": cfg["model"],
        "base_url": cfg["base_url"], "checked_at": now_iso()})
    result = svc.test()
    assert result["ok"] and asked == [CUSTOM["model"]]
    assert result["limits"] | {"context_window": 262144, "context_basis": "detected", "max_output": 16384, "enforced": True} == result["limits"]
    again = AssistantService(hub, HubAPI(hub))            # remembered across a restart
    assert again.public_config()["limits"]["context_source"] == "reported by llm.example.edu"
    # A provider that does not say leaves the limit unknown rather than keeping a stale or made-up one.
    monkeypatch.setattr("canvas_hub.assistant.detect_limits", lambda cfg: None)
    again.update_config({"model": "other-model"})
    assert again.test()["limits"]["context_window"] is None


# ---- how long a reply may be -----------------------------------------------------------------
def test_reply_limit_is_the_users_choice(sdk_peer):  # noqa: F811
    no_limit, custom = {"context": {"reply_limit": "none"}}, {"context": {"reply_limit": "custom", "response_tokens": 1500}}
    # No limit: nothing is sent to an OpenAI-compatible provider, so the model's own maximum applies.
    assert compatible_output_tokens({**CUSTOM, **no_limit}) is None
    provider, requests = sdk_peer({**CUSTOM, **no_limit, "_response_tokens": Policy.for_config({**CUSTOM, **no_limit}).reply_tokens})
    provider.complete("System", [{"role": "user", "sent_text": "Hi"}], [], on_text=lambda _: None)
    assert "max_tokens" not in requests[-1] and "max_completion_tokens" not in requests[-1]
    # Claude requires a number, so "no limit" sends the model's own maximum.
    assert anthropic_output_tokens({**CLAUDE, **no_limit}) == 128_000
    assert anthropic_output_tokens({**CLAUDE, "model": "claude-next", **no_limit}) == 64_000
    # A custom limit is sent exactly.
    assert compatible_output_tokens({**CUSTOM, **custom}) == 1500 and anthropic_output_tokens({**CLAUDE, **custom}) == 1500
    provider, requests = sdk_peer({**CUSTOM, **custom, "_response_tokens": Policy.for_config({**CUSTOM, **custom}).reply_tokens})
    provider.complete("System", [{"role": "user", "sent_text": "Hi"}], [], on_text=lambda _: None)
    assert requests[-1]["max_tokens"] == 1500
    # Planning keeps room for the reply without wasting the window when there is no cap.
    assert Policy.for_config({**CLAUDE, **no_limit}).response_tokens == 128_000
    assert Policy.for_config({**CLAUDE, "context": {"reply_limit": "none", "capacity": 40_000}}).response_tokens == 10_000
    assert Policy.for_config({**CLAUDE, **custom}).response_tokens == 1500
    # An explicit max_tokens in the extra request settings still takes precedence.
    assert compatible_output_tokens({**CUSTOM, **no_limit, "extra_params": {"max_tokens": 700}}) == 700
    with pytest.raises(ProviderError, match="conflict"):
        Policy.for_config({**CUSTOM, "extra_params": {"max_tokens": 2048, "max_completion_tokens": 4096}})


def test_setups_saved_before_the_choice_keep_the_cap_they_had():
    assert Policy.for_config(CUSTOM).reply_limit == "legacy" and compatible_output_tokens(CUSTOM) == 4096
    assert compatible_output_tokens({**DEEPSEEK, "thinking": "low"}) == 16384
    assert compatible_output_tokens({**DEEPSEEK, "thinking": "off"}) == 4096
    assert anthropic_output_tokens(CLAUDE) == 6000
    old = {**CUSTOM, "context": {"response_tokens": 9000}}
    assert Policy.for_config(old).reply_limit == "custom" and compatible_output_tokens(old) == 9000


def test_limit_settings_are_validated(hub, account):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "custom", "base_url": CUSTOM["base_url"], "model": CUSTOM["model"]})
    for bad, message in (({"reply_limit": "custom"}, "maximum number of tokens per reply"),
                         ({"reply_limit": "sometimes"}, "reply_limit must be"),
                         ({"reply_limit": "custom", "response_tokens": 0}, "whole number"),
                         ({"capacity": 100}, "whole number"),
                         ({"capacity": 4096, "reply_limit": "custom", "response_tokens": 8192}, "smaller than the context window")):
        with pytest.raises(InvalidInput, match=message):
            svc.update_config({"context": bad})
    saved = svc.update_config({"context": {"reply_limit": "custom", "response_tokens": 300, "capacity": 4096}})
    assert saved["limits"] | {"reply_limit": "custom", "reply_tokens": 300, "context_window": 4096, "context_basis": "configured"} == saved["limits"]
    assert json.loads(svc._path.read_text())["profiles"][0]["context"]["response_tokens"] == 300
