"""Vertex through the real OpenAI SDK with synthetic ADC and HTTP responses."""
import copy
import json
import os
import stat
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import Policy
from canvas_hub.assistant.providers import ProviderError, Stopped, make_provider
from canvas_hub.assistant.vertex import VertexAIProvider
from canvas_hub.boundary import HubAPI
from canvas_hub.services.workspace import WorkspaceService
from canvas_hub.util import InvalidInput

from .test_provider_usage import CHAT, COUNTS, chat_sse, completion

CONFIG = {"preset": "vertex_ai", "provider": "vertex_ai", "vertex_project": "test-project",
          "vertex_location": "global", "model": "gemini-test"}
SIGNATURE = {"google": {"thought_signature": "opaque-synthetic-signature"}}
CALL = {"id": "call-1", "type": "function", "function": {"name": "create_note",
        "arguments": '{"kind":"note","title":"Vertex note"}'}, "extra_content": SIGNATURE}


def tool_response(streaming=True, *, finish="tool_calls", arguments=None):
    call = copy.deepcopy(CALL)
    if arguments is not None:
        call["function"]["arguments"] = arguments
    if not streaming:
        data = completion()
        data["choices"][0].update(message={"role": "assistant", "content": None,
                                            "tool_calls": [call], "extra_content": SIGNATURE}, finish_reason=finish)
        return httpx.Response(200, json=data)

    def chunk(delta, stop=None, usage=None):
        return "data: " + json.dumps({"id": "synthetic", "object": "chat.completion.chunk", "created": 0,
            "model": "google/gemini-test", "choices": [{"index": 0, "delta": delta, "finish_reason": stop}],
            "usage": usage}) + "\n\n"

    # Signatures can arrive later than the name/arguments and without an ID.
    args = call["function"].pop("arguments")
    call.pop("extra_content")
    call["function"]["arguments"] = args[:10]
    data = chunk({"tool_calls": [{"index": 0, **call}]})
    data += chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[10:]}, "extra_content": SIGNATURE}],
                   "extra_content": SIGNATURE})
    if finish:
        data += chunk({}, finish, COUNTS)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(data + "data: [DONE]\n\n").encode())


@pytest.fixture
def vertex_peer(monkeypatch):
    import google.auth
    import openai
    from google.auth.credentials import Credentials

    class FakeCredentials(Credentials):
        def __init__(self):
            super().__init__()
            self.refreshes = 0
            self._quota_project_id = "test-quota-project"

        def refresh(self, request):
            self.refreshes += 1
            self.token = f"synthetic-google-token-{self.refreshes}"

    peer = SimpleNamespace(credentials=FakeCredentials(), requests=[], responses=[], defaults=[], clients=[])
    original = openai.OpenAI

    def default(**kwargs):
        peer.defaults.append(kwargs)
        return peer.credentials, "ignored-adc-project"

    def respond(request):
        body = json.loads(request.content)
        peer.requests.append((request, body))
        if peer.responses:
            return peer.responses.pop(0)
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chat_sse())
        return httpx.Response(200, json=completion())

    def factory(**kwargs):
        assert kwargs["max_retries"] == 0
        client = original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        peer.clients.append(client)
        return client

    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(openai, "OpenAI", factory)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.invalid/v1")
    yield peer
    for client in peer.clients:
        client.close()


@pytest.mark.parametrize("location,host", [("global", "aiplatform.googleapis.com"),
    ("us-central1", "us-central1-aiplatform.googleapis.com"), ("asia-southeast1", "asia-southeast1-aiplatform.googleapis.com")])
@pytest.mark.parametrize("model", ["gemini-test", "google/gemini-test"])
def test_routing_auth_refresh_and_usage(vertex_peer, location, host, model):
    provider = make_provider({**CONFIG, "vertex_location": location, "model": model,
                              "api_key": "stale-secret", "base_url": "https://unrelated.invalid",
                              "thinking": "high", "context": {"response_tokens": 8000}})
    assert isinstance(provider, VertexAIProvider)
    text = []
    turn = provider.complete("System", CHAT, [], on_text=text.append)
    provider.complete("System", CHAT, [])  # valid credentials are reused
    vertex_peer.credentials.token = None   # expiring between tool rounds
    provider.complete("System", CHAT, [])
    assert vertex_peer.credentials.refreshes == 2
    assert vertex_peer.defaults == [{"scopes": ["https://www.googleapis.com/auth/cloud-platform"]}]
    for index, (request, body) in enumerate(vertex_peer.requests):
        assert str(request.url) == f"https://{host}/v1/projects/test-project/locations/{location}/endpoints/openapi/chat/completions"
        assert request.headers["authorization"] == f"Bearer synthetic-google-token-{2 if index == 2 else 1}"
        assert request.headers["x-goog-user-project"] == "test-quota-project"
        assert body["model"] == "google/gemini-test" and body["max_tokens"] == 8000
        assert body["reasoning_effort"] == "high"
    assert vertex_peer.requests[0][1]["stream_options"] == {"include_usage": True}
    assert text == ["Answer"] and turn.usage["total_tokens"] == 130
    assert turn.raw["provider"] == "vertex_ai"


@pytest.mark.parametrize("streaming", [True, False])
def test_tool_signatures_survive_replay_and_context_estimation(vertex_peer, streaming):
    vertex_peer.responses.append(tool_response(streaming))
    provider = make_provider(CONFIG)
    tools = [{"name": "create_note", "description": "Save a note", "schema": {"type": "object"}}]
    turn = provider.complete("System", CHAT, tools, on_text=(lambda _: None) if streaming else None)
    transcript = CHAT + [{"role": "assistant", "text": turn.text, "raw": turn.raw,
                          "tool_calls": [asdict(c) for c in turn.tool_calls]},
                         {"role": "tool", "results": [{"id": "call-1", "name": "create_note", "content": '{"saved":true}'}]}]
    provider.complete("System", transcript, tools)
    replay = vertex_peer.requests[-1][1]["messages"][2]
    assert replay["tool_calls"][0]["extra_content"] == SIGNATURE
    assert replay["extra_content"] == SIGNATURE
    assert json.loads(replay["tool_calls"][0]["function"]["arguments"]) == turn.tool_calls[0].input
    policy = Policy.for_config(CONFIG)
    measured = policy.measure(CONFIG, "System", transcript, tools)
    larger = copy.deepcopy(transcript)
    larger[1]["raw"]["tool_call_extra_content"]["call-1"]["google"]["thought_signature"] *= 100
    assert policy.measure(CONFIG, "System", larger, tools)["estimated_tokens"] > measured["estimated_tokens"] + 500
    assert measured["request_max_tokens"] == provider.response_tokens


@pytest.mark.parametrize("status,message,retryable", [(401, "Google credentials", False),
    (403, "Vertex AI access was denied", False), (404, "Vertex AI model or project not found", False),
    (429, "Rate limited", True), (503, "Provider API error", True)])
def test_errors_are_actionable_without_hidden_retries(vertex_peer, status, message, retryable):
    vertex_peer.responses.append(httpx.Response(status, json={"error": {"message": "synthetic rejection"}}))
    with pytest.raises(ProviderError, match=message) as error:
        make_provider(CONFIG).complete("System", CHAT, [])
    assert error.value.retryable is retryable and len(vertex_peer.requests) == 1


def test_missing_and_failed_credentials_do_not_send_or_leak(vertex_peer, monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError, RefreshError

    def missing(**kwargs):
        raise DefaultCredentialsError("private credential detail")

    with monkeypatch.context() as patch:
        patch.setattr("google.auth.default", missing)
        with pytest.raises(ProviderError, match="application-default login") as error:
            make_provider(CONFIG)
        assert "private credential detail" not in str(error.value)
    provider = make_provider(CONFIG)
    provider.complete("System", CHAT, [])
    vertex_peer.credentials.token = None

    def failed(request):
        raise RefreshError("private refresh detail")

    monkeypatch.setattr(vertex_peer.credentials, "refresh", failed)
    with pytest.raises(ProviderError, match="Could not refresh") as error:
        provider.complete("System", CHAT, [])
    assert "private refresh detail" not in str(error.value)
    assert len(vertex_peer.requests) == 1 and provider.last_usage["status"] == "unknown"


@pytest.mark.parametrize("finish,arguments", [("length", None), ("tool_calls", "not-json"), (None, None)])
def test_incomplete_calls_cannot_execute(vertex_peer, finish, arguments):
    vertex_peer.responses.append(tool_response(finish=finish, arguments=arguments))
    provider = make_provider(CONFIG)
    if finish is None:
        with pytest.raises(ProviderError, match="cut off"):
            provider.complete("System", CHAT, [], on_text=lambda _: None)
    else:
        turn = provider.complete("System", CHAT, [], on_text=lambda _: None)
        assert turn.tool_calls[0].input_error
        if finish == "length":
            assert turn.incomplete_reason == "output_limit"
    assert len(vertex_peer.requests) == 1


def test_cancellation_is_not_retried(vertex_peer):
    def stopped(text):
        raise Stopped()

    with pytest.raises(Stopped):
        make_provider(CONFIG).complete("System", CHAT, [], on_text=stopped)
    assert len(vertex_peer.requests) == 1


def test_saved_setup_connection_and_tool_loop(hub, account, vertex_peer):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({"preset": "custom", "model": "old", "base_url": "https://old.invalid/v1", "api_key": "old-secret"})
    public = svc.update_config(CONFIG)
    assert public["configured"] and not public["api_key_saved"] and not public["api_key_from_env"]
    assert public["reasoning_levels"] == ["low", "medium", "high"]
    assert public["vertex_location"] == "global" and not public["base_url"]
    assert svc.test()["ok"]
    vertex_peer.responses.extend([tool_response(), httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chat_sse())])
    cid = svc.send(account["id"], "Save a note", reasoning="low")["conversation_id"]
    svc.wait(cid, 10)
    saved = svc.get_conversation(account["id"], cid)
    assert saved["conversation"]["run_state"] == "idle", saved["conversation"]
    assert hub.annotations.list(account["id"])[0]["title"] == "Vertex note"
    assert vertex_peer.requests[-1][1]["messages"][-2]["tool_calls"][0]["extra_content"] == SIGNATURE
    resumed = AssistantService(hub, HubAPI(hub))
    resumed.send(account["id"], "Thanks", cid)
    resumed.wait(cid, 10)
    assert resumed.get_conversation(account["id"], cid)["conversation"]["run_state"] == "idle"
    replay = next(m for m in vertex_peer.requests[-1][1]["messages"] if m.get("tool_calls"))
    assert replay["tool_calls"][0]["extra_content"] == SIGNATURE
    settings = svc._path.read_text()
    assert "synthetic-google-token" not in settings and "old-secret" not in settings
    assert "unrelated-openai-secret" not in settings and "opaque-synthetic-signature" not in json.dumps(saved)
    if os.name != "nt":
        assert stat.S_IMODE(svc._path.stat().st_mode) == 0o600


@pytest.mark.parametrize("patch", [{"vertex_project": "../other"}, {"vertex_project": ""},
    {"vertex_location": "global.evil.invalid/path"}, {"vertex_location": ""}, {"vertex_location": 123},
    {"model": "other/model"}, {"api_key": "wrong-secret"}, {"base_url": "https://evil.invalid"}])
def test_invalid_settings_are_atomic(hub, vertex_peer, patch):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config(CONFIG)
    before = svc._path.read_text()
    with pytest.raises(InvalidInput):
        svc.update_config(patch)
    assert svc._path.read_text() == before and not vertex_peer.defaults and not vertex_peer.requests


def test_context_output_and_workspace_disclosure(hub, account, vertex_peer):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config(CONFIG)
    workspace = WorkspaceService(hub)
    first = workspace.prepare(HubAPI(hub), svc, account["id"], {"action": "ask"})["disclosure"]
    assert "aiplatform.googleapis.com/v1/projects/test-project/locations/global/" in first["endpoint"]
    for patch in ({"vertex_project": "another-project"}, {"vertex_location": "us-central1"}):
        svc.update_config(patch)
        changed = workspace.prepare(HubAPI(hub), svc, account["id"], {"action": "ask"})["disclosure"]
        assert changed["fingerprint"] != first["fingerprint"]
        first = changed
    with pytest.raises(ProviderError, match="conflict"):
        Policy.for_config({**CONFIG, "extra_params": {"max_tokens": 6000, "max_completion_tokens": 7000}})


@pytest.mark.browser
def test_settings_save_test_reload_and_chat(hub, account, vertex_peer):
    from playwright.sync_api import expect, sync_playwright
    from .test_components_ui import serve

    with serve(hub, []) as (base, token, api), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={token}")
        page.goto(f"{base}/#/assistant/settings")
        page.get_by_label("Provider", exact=True).select_option("vertex_ai")
        expect(page.get_by_label("API key", exact=True)).to_be_hidden()
        expect(page.get_by_label("Base URL", exact=True)).to_be_hidden()
        page.get_by_label("Google Cloud project ID", exact=True).fill("test-project")
        page.get_by_label("Vertex AI location", exact=True).fill("us-central1")
        page.get_by_label("Model", exact=True).fill("gemini-test")
        page.get_by_role("button", name="Save & test", exact=True).click()
        expect(page.get_by_text("Connected · synthetic", exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_label("Google Cloud project ID", exact=True)).to_have_value("test-project")
        expect(page.get_by_label("Vertex AI location", exact=True)).to_have_value("us-central1")
        page.get_by_label("Provider", exact=True).select_option("gemini")
        expect(page.get_by_label("Google Cloud project ID", exact=True)).to_be_hidden()
        expect(page.get_by_label("API key", exact=True)).to_be_visible()
        page.get_by_label("Provider", exact=True).select_option("vertex_ai")
        expect(page.get_by_label("Vertex AI location", exact=True)).to_be_visible()
        page.goto(f"{base}/#/assistant")
        page.locator(".composer textarea").fill("Hello Vertex")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator(".msg.assistant").last).to_have_text("Answer", timeout=15000)
        page.reload()
        expect(page.locator(".msg.assistant").last).to_have_text("Answer")
        browser.close()
    assert not errors
    assert len(vertex_peer.requests) == 2
