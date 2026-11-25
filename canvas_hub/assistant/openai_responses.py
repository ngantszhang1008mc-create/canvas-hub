"""OpenAI Responses API provider for GPT-6+ models (e.g. gpt-6.2-astra, gpt-6.2-sol).

Starting with GPT-6.2, OpenAI requires the Responses API (``/v1/responses``) for
function/tool calling, rejects ``reasoning.effort="none"``, and appends top-level
``instructions`` after ``input`` (so placing the system prompt in a leading
``developer`` input message keeps prompt prefixes cacheable).
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import openai

from .providers import ProviderError, ToolCall, Turn, _neutral_assistant, compatible_output_tokens
from .request_shape import stable_json, stable_tools
from .usage import normalize_usage

VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def uses_openai_responses(cfg: dict) -> bool:
    """Return True when this OpenAI setup should use ``/v1/responses``."""
    if cfg.get("provider") == "openai_responses":
        return True
    if cfg.get("provider") != "openai_compatible":
        return False
    model = (cfg.get("model") or "").strip().lower()
    if not model.startswith("gpt-6"):
        return False
    base_url = (cfg.get("base_url") or "").strip()
    if cfg.get("preset") == "openai" and (not base_url or urlsplit(base_url).hostname == "api.openai.com"):
        return True
    return bool(base_url and urlsplit(base_url).hostname == "api.openai.com")


def _serialize_reasoning_item(item) -> dict | None:
    """Extract a stateless replayable reasoning item (with encrypted_content if present)."""
    if getattr(item, "type", None) != "reasoning":
        return None
    if hasattr(item, "model_dump"):
        data = item.model_dump(exclude_none=True)
    elif isinstance(item, dict):
        data = {k: v for k, v in item.items() if v is not None}
    else:
        return None
    if data.get("type") != "reasoning":
        return None
    # Strip transient status fields that the API may reject on input replay.
    out = {"type": "reasoning"}
    if data.get("id"):
        out["id"] = data["id"]
    if isinstance(data.get("summary"), list):
        out["summary"] = data["summary"]
    if data.get("encrypted_content"):
        out["encrypted_content"] = data["encrypted_content"]
    return out if ("encrypted_content" in out or out.get("summary")) else None


def _input_items(system: str, transcript: list[dict], model: str) -> list[dict]:
    """Build Responses API input items with a leading developer message for prefix caching."""
    items: list[dict] = []
    if system:
        items.append({"role": "developer", "content": system})
    for message in transcript:
        role = message["role"]
        if role == "user":
            items.append({"role": "user", "content": message["sent_text"]})
        elif role == "assistant":
            raw = message.get("raw") or {}
            if (raw.get("provider") == "openai_responses"
                    and raw.get("model") == model
                    and isinstance(raw.get("reasoning_items"), list)):
                for ritem in raw["reasoning_items"]:
                    if isinstance(ritem, dict) and ritem.get("type") == "reasoning":
                        items.append(ritem)
            text, calls = _neutral_assistant(message)
            if text:
                items.append({"role": "assistant", "content": text})
            if not message.get("incomplete_reason") and message.get("stop") != "truncated":
                for call in calls:
                    items.append({
                        "type": "function_call",
                        "name": call["name"],
                        "call_id": call["id"],
                        "arguments": stable_json(call.get("input") or {}),
                    })
        elif role == "tool":
            for result in message["results"]:
                items.append({
                    "type": "function_call_output",
                    "call_id": result["id"],
                    "output": result["content"],
                })
    # Drop orphan function_call / function_call_output entries after context compaction.
    answered = {item["call_id"] for item in items if item.get("type") == "function_call_output"}
    called = {item["call_id"] for item in items if item.get("type") == "function_call"}
    paired = answered & called
    return [item for item in items if "call_id" not in item or item["call_id"] in paired]


def _tool_call(item, allowed: set[str]) -> ToolCall:
    args = None
    error = None
    try:
        args = json.loads(item.arguments)
        if not isinstance(args, dict):
            raise ValueError
    except (TypeError, ValueError):
        args, error = None, "The tool arguments were not a JSON object."
    if item.name not in allowed:
        args, error = None, "This tool was not offered in the current request."
    return ToolCall(item.call_id, item.name, args, error)


def _format_api_error(exc: Exception) -> ProviderError:
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        body = getattr(exc, "body", None)
        detail = None
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = err.get("message")
        msg = detail or str(exc) or f"OpenAI API returned HTTP {status}"
        return ProviderError(f"OpenAI Responses API error ({status}): {msg}",
                             retryable=status in (408, 409, 429) or (status or 0) >= 500)
    return ProviderError(f"OpenAI Responses API stream interrupted: {exc}", retryable=True)


class OpenAIResponsesProvider:
    """OpenAI Responses API (``/v1/responses``) provider for GPT-6+ reasoning models."""

    name = "openai_responses"

    def __init__(self, cfg: dict):
        self.model = (cfg.get("model") or "").strip()
        if not self.model:
            raise ProviderError("Enter the model name for this provider in Assistant settings.")
        key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or os.environ.get("CANVAS_HUB_DEMO_OPENAI_API_KEY") or ""
        if not key:
            raise ProviderError("Add an OpenAI API key in Assistant settings or OPENAI_API_KEY.")
        base_url = (cfg.get("base_url") or "").strip() or None
        self.client = openai.OpenAI(
            api_key=key,
            base_url=base_url,
            timeout=120.0 if cfg.get("_single_attempt") else 600.0,
            max_retries=0,
        )
        self.reasoning = (cfg.get("thinking") or "").strip().lower()
        self.extra_params = dict(cfg.get("extra_params") or {})
        self.response_tokens = compatible_output_tokens(cfg)
        self.last_usage = None

    def close(self):
        self.client.close()

    def _build_kwargs(self, system: str, transcript: list[dict], tools: list[dict]) -> dict:
        kwargs: dict = {
            "model": self.model,
            "input": _input_items(system, transcript, self.model),
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        effort = self.reasoning or str(self.extra_params.get("reasoning_effort") or "").strip().lower()
        if effort in VALID_REASONING_EFFORTS:
            kwargs["reasoning"] = {"effort": effort}
        if self.response_tokens is not None:
            kwargs["max_output_tokens"] = self.response_tokens
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["schema"],
                    "strict": False,
                }
                for t in stable_tools(tools)
            ]
        return kwargs

    def complete(self, system: str, transcript: list[dict], tools: list[dict], on_text=None) -> Turn:
        self.last_usage = normalize_usage(None, self.name, final=False)
        kwargs = self._build_kwargs(system, transcript, tools)
        allowed = {t["name"] for t in tools}
        text = ""
        response = None
        terminal = None
        try:
            with self.client.responses.create(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, "type", "")
                    if etype in ("response.output_text.delta", "response.refusal.delta"):
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            text += delta
                            if on_text:
                                on_text(text)
                    elif etype in ("response.completed", "response.incomplete", "response.failed"):
                        response, terminal = event.response, etype
                        break
                    elif etype == "error":
                        code = getattr(event, "code", None)
                        msg = getattr(event, "message", None) or "OpenAI Responses stream error."
                        raise ProviderError(f"OpenAI Responses API error{f' ({code})' if code else ''}: {msg}",
                                            retryable=True)
        except ProviderError:
            raise
        except (openai.APIStatusError, openai.APIConnectionError, openai.APIError) as exc:
            raise _format_api_error(exc) from exc

        if response is None:
            raise ProviderError("The OpenAI response stream ended before completion. Try again.", retryable=True)

        self.last_usage = normalize_usage(getattr(response, "usage", None), self.name, final=True)
        if terminal == "response.failed":
            err = getattr(response, "error", None)
            code = getattr(err, "code", None) if err is not None else None
            msg = getattr(err, "message", None) if err is not None else "Request failed."
            raise ProviderError(f"OpenAI Responses API failed{f' ({code})' if code else ''}: {msg}", retryable=True)

        output_items = list(getattr(response, "output", None) or [])
        parts = [part for item in output_items if getattr(item, "type", None) == "message"
                 for part in (getattr(item, "content", None) or [])]
        answer = "".join(
            part.text if getattr(part, "type", None) == "output_text" else getattr(part, "refusal", "")
            for part in parts if getattr(part, "type", None) in ("output_text", "refusal")
        ) or text
        calls = []
        if terminal == "response.completed":
            calls = [_tool_call(item, allowed) for item in output_items if getattr(item, "type", None) == "function_call"]
        reasoning_items = [r for item in output_items if (r := _serialize_reasoning_item(item)) is not None]
        stop = (
            "truncated" if terminal == "response.incomplete"
            else "refusal" if any(getattr(p, "type", None) == "refusal" for p in parts)
            else "tool_use" if calls
            else "end"
        )
        raw: dict = {"provider": self.name, "model": self.model}
        if reasoning_items:
            raw["reasoning_items"] = reasoning_items
        return Turn(
            answer,
            tool_calls=calls,
            stop=stop,
            model=getattr(response, "model", None) or self.model,
            usage=self.last_usage,
            raw=raw,
        )
