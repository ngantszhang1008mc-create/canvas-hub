"""Sign in with ChatGPT through the local web API and synthetic OpenAI responses."""
import json
import os
import stat
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import openai
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant import chatgpt_auth as auth
from canvas_hub.assistant.chatgpt import ChatGPTProvider
from canvas_hub.assistant.providers import ProviderError
from canvas_hub.boundary import HubAPI
from canvas_hub.web.app import create_app


def test_chatgpt_sign_in_callback_refresh_and_disconnect(hub, monkeypatch):
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    app = create_app(api, token="test-token", port=8765)
    owner = TestClient(app, base_url="http://127.0.0.1:8765", headers={
        "Authorization": "Bearer test-token", "X-Canvas-Hub": "1",
    })
    browser = TestClient(app, base_url="http://127.0.0.1:8765")
    cfg_url, path = "/api/assistant/config", "/api/assistant/chatgpt"
    created = owner.put(cfg_url, json={"profile_id": "new", "preset": "chatgpt", "model": "gpt-test"}).json()
    profile = created["saved_profile"]
    assert not created["configured"]
    assert browser.post(path + "/start", json={"profile_id": profile}).status_code == 401
    assert owner.post(path + "/start", json={"profile_id": profile},
                      headers={"Origin": "https://other.invalid"}).status_code == 403

    def start():
        return {k: v[0] for k, v in parse_qs(urlsplit(owner.post(
            path + "/start", json={"profile_id": profile},
        ).json()["url"]).query).items()}

    params = start()
    assert params["client_id"] == "dynamic_agent_client"
    assert params["redirect_uri"] == "http://127.0.0.1:8765/auth/chatgpt/callback"
    assert params["code_challenge_method"] == "S256"
    callback = urlsplit(params["redirect_uri"]).path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    exchanges = []
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", lambda *_: SimpleNamespace(key=key.public_key()))

    def exchange(url, *, data, timeout):
        exchanges.append((url, data))
        if url.endswith("/revoke"):
            return httpx.Response(200)
        if data["grant_type"] == "refresh_token":
            return httpx.Response(200, json={"token_type": "Bearer", "access_token": "new-access-secret",
                                            "expires_in": 3600, "refresh_token": "new-refresh-secret"})
        identity = {"iss": auth.ISSUER, "aud": "oaiapp_test", "sub": "verified-subject",
                    "exp": time.time() + 500, "nonce": params["nonce"], "email": "example@example.test"}
        signed = jwt.encode(identity, key, algorithm="RS256")
        return httpx.Response(200, json={"id_token": signed, "token_type": "Bearer",
                                        "scope": auth.SCOPES, "expires_in": 30,
                                        "access_token": "initial-access-secret", "refresh_token": "initial-refresh-secret"})

    monkeypatch.setattr(httpx, "post", exchange)
    result = browser.get(callback, params={"state": "unrecognized", "code": "code"})
    assert "expired" in result.text and not exchanges
    result = browser.get(callback, params={"state": params["state"], "code": "test-code", "client_id": "oaiapp_test"})
    assert "ChatGPT connected" in result.text and result.headers["cache-control"] == "no-store"
    assert profile in result.text and "test-code" not in result.text
    assert exchanges[0][1]["code_verifier"] and exchanges[0][1]["client_id"] == "oaiapp_test"
    assert browser.get(callback, params={"state": params["state"], "code": "test-code"}).status_code == 200
    assert len(exchanges) == 1

    public = owner.get(cfg_url)
    assert public.json()["configured"] and public.json()["chatgpt_account"] == "example@example.test"
    assert "initial-access-secret" not in public.text and "refresh_token" not in public.text
    if os.name != "nt":
        assert stat.S_IMODE(api.assistant._path.stat().st_mode) == 0o600
    new_params = start()
    assert new_params["client_id"] == "oaiapp_test" and new_params["ext_agent_host_id"] == params["ext_agent_host_id"]

    def models(url, *, headers, timeout):
        assert url == auth.RESOURCE + "/models"
        assert headers["Authorization"] == "Bearer new-access-secret"
        return httpx.Response(200, json={"models": [
            {"slug": "gpt-test", "display_name": "GPT Test", "visibility": "list"},
            {"slug": "hidden-test", "visibility": "hidden"},
        ]})

    monkeypatch.setattr(httpx, "get", models)
    for _ in range(2):
        listed = owner.get(path + "/models", params={"profile_id": profile}).json()
        assert listed == {"models": [{"id": "gpt-test", "name": "GPT Test"}], "error": None}
    assert len(exchanges) == 2 and exchanges[1][1]["refresh_token"] == "initial-refresh-secret"
    assert owner.post(path + "/disconnect", json={"profile_id": profile}).json() == {"revoked": True}
    assert exchanges[-1][1]["token"] == "new-refresh-secret"
    assert not owner.get(cfg_url).json()["configured"]
    assert "refresh-secret" not in api.assistant._path.read_text()
    assert start()["client_id"] == "oaiapp_test"


def test_chatgpt_responses_stream_tools_and_usage(monkeypatch):
    requests = []
    events = []
    factory = openai.OpenAI

    def respond(request):
        requests.append(request)
        data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data)

    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: factory(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ))
    call = {"type": "function_call", "namespace": "canvas_hub", "name": "list_courses", "call_id": "call_123",
            "arguments": "{}", "status": "completed"}
    text = {"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "Answer", "annotations": []}]}
    usage = {"input_tokens": 16, "input_tokens_details": {"cached_tokens": 2}, "output_tokens": 8,
             "output_tokens_details": {"reasoning_tokens": 2}, "total_tokens": 24}

    def complete(output):
        events[:] = [{"type": "response.completed", "response": {"model": "gpt-test", "output": output, "usage": usage}}]

    provider = ChatGPTProvider({"model": "gpt-test", "_chatgpt_access": lambda: "oauth-access"})
    transcript = [{"role": "user", "sent_text": "Show courses"}]
    tools = [{"name": "list_courses", "description": "List courses", "schema": {"type": "object"}}]
    complete([call])
    turn = provider.complete("System instructions", transcript, tools)
    assert turn.stop == "tool_use" and turn.tool_calls[0].input == {}
    request = requests[0]
    assert str(request.url) == auth.RESOURCE + "/responses"
    assert request.headers["Authorization"] == "Bearer oauth-access"
    body = json.loads(request.content)
    assert body["store"] is False and body["stream"] is True and "max_output_tokens" not in body
    assert body["instructions"] == "System instructions" and body["tools"][0]["type"] == "namespace"

    complete([text])
    events.insert(0, {"type": "response.output_text.delta", "delta": "Answer"})
    transcript.extend([
        {"role": "assistant", "tool_calls": [{"name": "list_courses", "id": "call_123", "input": {}}]},
        {"role": "tool", "results": [{"id": "call_123", "content": "course"}]},
    ])
    deltas = []
    turn = provider.complete("System instructions", transcript, tools, on_text=deltas.append)
    assert deltas == ["Answer"] and turn.text == "Answer"
    assert turn.usage["total_tokens"] == 24 and turn.usage["cache_read_input_tokens"] == 2
    replay = json.loads(requests[1].content)["input"]
    assert replay[-2]["namespace"] == "canvas_hub"
    assert replay[-1] == {"type": "function_call_output", "call_id": "call_123", "output": "course"}

    events.clear()
    with pytest.raises(ProviderError, match="ended before completion"):
        provider.complete("System instructions", transcript, tools)


@pytest.mark.browser
def test_chatgpt_settings_browser(hub, account, monkeypatch):
    from playwright.sync_api import expect, sync_playwright

    from canvas_hub.assistant import chatgpt
    from .test_components_ui import serve

    monkeypatch.setattr(auth, "finish", lambda *_: {
        "client_id": "oaiapp_browser", "subject": "browser-account", "email": "student@example.test",
        "access_token": "test-access", "refresh_token": "test-refresh", "scopes": list(auth.PLAN_SCOPES),
        "expires_at": time.time() + 3600,
    })
    monkeypatch.setattr(auth, "revoke", lambda _: True)
    monkeypatch.setattr(chatgpt, "list_models", lambda _: [{"id": "gpt-test", "name": "GPT Test"}])

    with serve(hub, []) as (base, token, api), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def authorize(route):
            params = parse_qs(urlsplit(route.request.url).query)
            redirect = httpx.URL(params["redirect_uri"][0], params={
                "state": params["state"][0], "code": "fake-authorization-code", "client_id": "oaiapp_browser",
            })
            route.fulfill(status=302, headers={"Location": str(redirect)})

        page.route("https://auth.openai.com/api/accounts/authorize?*", authorize)
        page.goto(f"{base}/?token={token}")
        page.goto(f"{base}/#/assistant/settings?p=new")
        page.get_by_label("Provider", exact=True).select_option("chatgpt")
        expect(page.get_by_label("API key", exact=True)).to_be_hidden()
        page.get_by_role("button", name="Continue with ChatGPT", exact=True).click()
        expect(page.get_by_text("ChatGPT connected.", exact=False).first).to_be_visible()
        page.get_by_role("link", name="Return to Assistant settings").click()
        expect(page.get_by_text("Connected as student@example.test", exact=True)).to_be_visible()
        model = page.get_by_label("ChatGPT model", exact=True)
        model.select_option("gpt-test")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(model).to_have_value("gpt-test")
        page.get_by_role("button", name="Use this setup", exact=True).click()
        expect(page.get_by_text("In use", exact=True)).to_be_visible()
        prepared = hub.workspace.prepare(api, api.assistant, account["id"], {"action": "ask"})
        assert prepared["disclosure"]["endpoint"] == auth.RESOURCE + "/responses"
        page.get_by_role("button", name="Disconnect", exact=True).click()
        expect(page.get_by_text("ChatGPT disconnected.", exact=True)).to_be_visible()
        browser.close()
    assert not errors
