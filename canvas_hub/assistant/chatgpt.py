"""ChatGPT plan provider for Canvas Hub's local assistant tools."""
from __future__ import annotations

import json

import httpx
import openai

from .chatgpt_auth import RESOURCE
from .providers import ProviderError, ToolCall, Turn, _neutral_assistant
from .request_shape import stable_json, stable_tools
from .usage import normalize_usage

NAMESPACE = "canvas_hub"
ERRORS = {
    "subscription_sharing_user_not_eligible": "This ChatGPT account or workspace is not eligible for plan usage in Canvas Hub.",
    "subscription_sharing_usage_limit_exceeded": "ChatGPT plan usage reached a limit. Review ChatGPT Settings → Usage.",
    "subscription_sharing_usage_unavailable": "ChatGPT could not check your plan usage. Try again later.",
    "subscription_sharing_user_unavailable": "ChatGPT could not check your workspace. Try again later.",
    "subscription_sharing_unsupported_capability": "ChatGPT plan usage does not support this model or request capability.",
    "chatpass_v2_scope_not_authorized": "Enable ChatGPT plan usage for Canvas Hub in Assistant settings.",
    "subscription_sharing_invalid_user": "ChatGPT could not validate this account. Reconnect it in Assistant settings.",
}


def _request_error(code=None, status=None, request_id=None):
    message = ERRORS.get(code) or {
        401: "ChatGPT declined this session. Reconnect in Assistant settings.",
        403: "ChatGPT denied plan usage for this request. Check your account and workspace permissions.",
        429: "ChatGPT rate limited this request. Review ChatGPT Settings → Usage.",
    }.get(status, "ChatGPT request failed. Try again or check your ChatGPT setup.")
    details = ", ".join(str(value) for value in (status, code, request_id) if value)
    return ProviderError(message + (f" ({details})" if details else ""),
                         retryable=status == 429 or (status or 0) >= 500)


def list_models(token):
    try:
        response = httpx.get(RESOURCE + "/models", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("Could not read the ChatGPT model list. Try again.", retryable=True) from exc
    if not response.is_success:
        error = data.get("error") if isinstance(data, dict) else None
        raise _request_error(error.get("code") if isinstance(error, dict) else None,
                             response.status_code, response.headers.get("x-request-id"))
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ProviderError("ChatGPT returned an unreadable model list.")
    return [{"id": m["slug"], "name": m.get("display_name") or m["slug"]}
            for m in data["models"] if isinstance(m, dict) and m.get("visibility") == "list"
            and isinstance(m.get("slug"), str) and m["slug"]]


def _input(transcript):
    items = []
    for message in transcript:
        role = message["role"]
        if role == "user":
            items.append({"role": "user", "content": message["sent_text"]})
        elif role == "assistant":
            text, calls = _neutral_assistant(message)
            if text:
                items.append({"role": "assistant", "content": text})
            if not message.get("incomplete_reason") and message.get("stop") != "truncated":
                for call in calls:
                    items.append({"type": "function_call", "name": call["name"], "namespace": NAMESPACE,
                                  "call_id": call["id"], "arguments": stable_json(call.get("input") or {})})
        elif role == "tool":
            for result in message["results"]:
                items.append({"type": "function_call_output", "call_id": result["id"], "output": result["content"]})
    # Context projections can suppress incomplete or removed pairs.
    answered = {item["call_id"] for item in items if item.get("type") == "function_call_output"}
    called = {item["call_id"] for item in items if item.get("type") == "function_call"}
    paired = answered & called
    return [item for item in items if "call_id" not in item or item["call_id"] in paired]


def _tool_call(item, allowed):
    args = None
    error = None
    try:
        args = json.loads(item.arguments)
        if not isinstance(args, dict):
            raise ValueError
    except (TypeError, ValueError):
        args, error = None, "The tool arguments were not a JSON object."
    if item.name not in allowed or getattr(item, "namespace", None) not in (None, NAMESPACE):
        args, error = None, "This tool was not offered in the current request."
    return ToolCall(item.call_id, item.name, args, error)


class ChatGPTProvider:
    name = "chatgpt"

    def __init__(self, cfg):
        self.model = cfg["model"]
        self.access_token = cfg["_chatgpt_access"]
        self.reasoning = cfg.get("thinking")
        self.last_usage = None

    def complete(self, system, transcript, tools, on_text=None):
        self.last_usage = None
        kwargs = {"model": self.model, "instructions": system, "input": _input(transcript),
                  "store": False, "stream": True}
        if self.reasoning:
            kwargs["reasoning"] = {"effort": self.reasoning}
        allowed = {t["name"] for t in tools}
        if tools:
            kwargs["tools"] = [{
                "type": "namespace", "name": NAMESPACE, "description": "Canvas Hub assistant tools",
                "tools": [{"type": "function", "name": t["name"], "description": t["description"],
                           "parameters": t["schema"], "strict": False} for t in stable_tools(tools)],
            }]
        text = ""
        response = None
        terminal = None
        try:
            with openai.OpenAI(api_key=self.access_token(), base_url=RESOURCE,
                               organization="", project="", max_retries=0, timeout=90) as client:
                with client.responses.create(**kwargs) as stream:
                    for event in stream:
                        if event.type in ("response.output_text.delta", "response.refusal.delta"):
                            text += event.delta
                            if on_text:
                                on_text(text)
                        elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                            response, terminal = event.response, event.type
                            break
                        elif event.type == "error":
                            raise _request_error(getattr(event, "code", None))
        except openai.APIStatusError as exc:
            raise _request_error(exc.code, exc.status_code, exc.request_id) from exc
        except (openai.APIConnectionError, openai.APIError) as exc:
            raise ProviderError("The ChatGPT response stream was interrupted. Try again.", retryable=True) from exc
        if response is None:
            raise ProviderError("The ChatGPT response stream ended before completion.", retryable=True)
        self.last_usage = normalize_usage(response.usage, "chatgpt")
        if terminal == "response.failed":
            raise _request_error(getattr(response.error, "code", None), request_id=getattr(response, "_request_id", None))
        parts = [part for item in response.output if item.type == "message" for part in item.content]
        answer = "".join(part.text if part.type == "output_text" else part.refusal
                         for part in parts if part.type in ("output_text", "refusal")) or text
        calls = []
        if terminal == "response.completed":
            calls = [_tool_call(item, allowed) for item in response.output if item.type == "function_call"]
        stop = ("truncated" if terminal == "response.incomplete" else "refusal" if any(p.type == "refusal" for p in parts)
                else "tool_use" if calls else "end")
        return Turn(answer, tool_calls=calls, stop=stop, model=response.model, usage=self.last_usage,
                    raw={"provider": self.name})
