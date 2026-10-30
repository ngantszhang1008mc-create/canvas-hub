"""Model providers for the in-app assistant.

* ``anthropic``         - Claude through the official Anthropic SDK (streamed).
* ``openai_compatible`` - any Chat Completions-compatible endpoint through the
  official OpenAI SDK with a configurable base URL (OpenAI, Google Gemini's
  OpenAI endpoint, OpenRouter, DeepSeek, local Ollama/LM Studio, ...).

Both take a provider-neutral transcript and return a ``Turn``. Claude's own
content blocks (including reasoning blocks) are kept verbatim and replayed
unchanged on later requests; history is append-only.

With ``on_text`` both stream: it is called with the reply's text so far as it
arrives (the whole text each time, so a retried request simply starts over).
The callback may raise ``Stopped`` to end the request early.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept the server-side refusal fallback ("default" routing) on the Claude API.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_TOKENS = 16000


class Stopped(Exception):
    """Raised by an ``on_text`` callback to stop a streaming reply (the user pressed Stop)."""


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict | None
    input_error: str | None = None      # set when the arguments were not valid JSON / truncated


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"                   # end | tool_use | refusal | truncated
    raw: dict | None = None             # provider-native assistant content to replay verbatim
    model: str | None = None
    usage: dict | None = None


def _neutral_assistant(msg: dict) -> tuple[str, list[dict]]:
    return msg.get("text") or "", msg.get("tool_calls") or []


# --------------------------------------------------------------------- Anthropic
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: dict):
        import anthropic
        self._sdk = anthropic
        self.model = cfg.get("model") or ANTHROPIC_DEFAULT_MODEL
        self.effort = cfg.get("effort") or None
        self.base_url = (cfg.get("base_url") or "").strip() or None
        kw = {"max_retries": 2, "timeout": 600.0}
        if cfg.get("api_key"):
            kw["api_key"] = cfg["api_key"]
        if self.base_url:
            kw["base_url"] = self.base_url
        try:
            self.client = anthropic.Anthropic(**kw)
        except anthropic.AnthropicError as e:
            raise ProviderError(f"Anthropic client could not start: {e}") from e

    def _messages(self, transcript: list[dict]) -> list[dict]:
        out = []
        for m in transcript:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["sent_text"]})
            elif m["role"] == "assistant":
                raw = m.get("raw") or {}
                if raw.get("provider") == "anthropic" and raw.get("content"):
                    out.append({"role": "assistant", "content": raw["content"]})
                else:
                    text, calls = _neutral_assistant(m)
                    blocks = ([{"type": "text", "text": text}] if text else []) + [
                        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("input") or {}} for c in calls]
                    out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no reply)"}]})
            elif m["role"] == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], "is_error": bool(r.get("is_error"))}
                    for r in m["results"]]})
        return out

    def complete(self, system: str, transcript: list[dict], tools: list[dict], on_text=None) -> Turn:
        sdk = self._sdk
        official = self.base_url is None
        tool_defs = []
        for t in tools:
            d = {"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
            if official:
                d["eager_input_streaming"] = True   # inputs are validated before any tool runs
            tool_defs.append(d)
        kwargs = dict(model=self.model, max_tokens=MAX_TOKENS, system=system, messages=self._messages(transcript),
                      cache_control={"type": "ephemeral"})
        if tool_defs:
            kwargs["tools"] = tool_defs
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        use_fallback = official and self.model in _FALLBACK_MODELS
        msg = None
        for attempt in range(2):
            try:
                if use_fallback:
                    opener = self.client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                              **kwargs)
                else:
                    opener = self.client.messages.stream(**kwargs)
                with opener as stream:
                    if on_text:
                        so_far = ""
                        for piece in stream.text_stream:
                            so_far += piece
                            on_text(so_far)
                    msg = stream.get_final_message()
                break
            except sdk.AuthenticationError as e:
                raise ProviderError("The API key was rejected by Anthropic. Check it in Assistant settings.") from e
            except sdk.PermissionDeniedError as e:
                raise ProviderError(f"This API key may not use {self.model}: {e.message}") from e
            except sdk.NotFoundError as e:
                raise ProviderError(f"Model or endpoint not found ({self.model}). Check the model name.") from e
            except sdk.RateLimitError as e:
                raise ProviderError("Rate limited by Anthropic. Wait a moment and try again.", retryable=True) from e
            except sdk.BadRequestError as e:
                raise ProviderError(f"Anthropic rejected the request: {e.message}") from e
            except sdk.APIStatusError as e:
                raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
            except sdk.APIConnectionError as e:
                raise ProviderError("Could not reach the Anthropic API. Check your internet connection.", retryable=True) from e
            except ValueError:
                # A streamed tool input that could not be parsed at all: re-issue once.
                if attempt:
                    raise ProviderError("The model produced an unreadable tool call twice.")
        data = msg.to_dict()
        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") if isinstance(b.get("input"), dict) else None,
                          None if isinstance(b.get("input"), dict) else "input was not a JSON object")
                 for b in content if b.get("type") == "tool_use"]
        stop = {"tool_use": "tool_use", "refusal": "refusal", "max_tokens": "truncated"}.get(data.get("stop_reason"), "end")
        if stop == "truncated" and calls:
            for c in calls:
                c.input_error = "tool input was cut off by the output limit; not executed"
        usage = data.get("usage") or {}
        return Turn(text=text, tool_calls=calls, stop=stop, raw={"provider": "anthropic", "content": content},
                    model=data.get("model"), usage={k: usage.get(k) for k in ("input_tokens", "output_tokens",
                                                                             "cache_read_input_tokens")})


# --------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, cfg: dict):
        import openai
        self._sdk = openai
        self.model = (cfg.get("model") or "").strip()
        if not self.model:
            raise ProviderError("Enter the model name for this provider in Assistant settings.")
        key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or "not-needed"
        self.client = openai.OpenAI(api_key=key, base_url=(cfg.get("base_url") or "").strip() or None,
                                    timeout=600.0, max_retries=2)
        # Extra request settings are the setup's defaults; a reasoning level
        # picked in the chat for this message takes precedence.
        self.extra_body = dict(cfg.get("extra_params") or {})
        self.extra_body.update(thinking_params(cfg.get("preset"), cfg.get("thinking") or ""))
        self.no_streaming = False

    def _messages(self, system: str, transcript: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in transcript:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["sent_text"]})
            elif m["role"] == "assistant":
                text, calls = _neutral_assistant(m)
                d: dict = {"role": "assistant", "content": text or None}
                reasoning = (m.get("raw") or {}).get("reasoning_content")
                if reasoning is not None:
                    # Thinking models (e.g. DeepSeek) require their reasoning to be sent
                    # back on later requests once tools are involved.
                    d["reasoning_content"] = reasoning
                if calls:
                    d["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": json.dumps(c.get("input") or {})}}
                                       for c in calls]
                elif not text:
                    d["content"] = "(no reply)"
                out.append(d)
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return out

    def _streamed(self, kwargs: dict, on_text) -> tuple:
        """Stream a Chat Completions reply; returns (content, reasoning, calls, finish, model, usage)."""
        text, reasoning, calls = "", None, {}
        finish = model = usage = None
        for chunk in self.client.chat.completions.create(**kwargs, stream=True):
            model = getattr(chunk, "model", None) or model
            usage = getattr(chunk, "usage", None) or usage
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            d = ch.delta
            if d is not None:
                if getattr(d, "content", None):
                    text += d.content
                    on_text(text)
                rc = getattr(d, "reasoning_content", None)
                if rc:
                    reasoning = (reasoning or "") + rc
                for tc in getattr(d, "tool_calls", None) or []:       # arrive in fragments, keyed by index
                    slot = calls.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if fn.name:
                            slot["name"] = fn.name
                        slot["arguments"] += fn.arguments or ""
            if ch.finish_reason:
                finish = ch.finish_reason
        if finish is None:      # the connection ended mid-reply: never keep half an answer or run half a tool call
            raise ProviderError("The provider's reply was cut off before it finished. Try again.", retryable=True)
        return text, reasoning, [calls[i] for i in sorted(calls)], finish, model, usage

    def complete(self, system: str, transcript: list[dict], tools: list[dict], on_text=None) -> Turn:
        sdk = self._sdk
        kwargs = dict(model=self.model, messages=self._messages(system, transcript))
        if self.extra_body:
            kwargs["extra_body"] = dict(self.extra_body)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                 "parameters": t["schema"]}} for t in tools]
        try:
            streamed = None
            if on_text and not self.no_streaming:
                try:
                    streamed = self._streamed(kwargs, on_text)
                except sdk.BadRequestError as e:
                    if "stream" not in str(e.message).lower():
                        raise
                    self.no_streaming = True   # this endpoint does not stream: ask again without, and stop trying
            resp = None if streamed else self.client.chat.completions.create(**kwargs)
        except sdk.AuthenticationError as e:
            raise ProviderError("The API key was rejected by the provider. Check it in Assistant settings.") from e
        except sdk.NotFoundError as e:
            raise ProviderError(f"Model or endpoint not found ({self.model}). Check the base URL and model name.") from e
        except sdk.RateLimitError as e:
            raise ProviderError("Rate limited by the provider. Wait a moment and try again.", retryable=True) from e
        except sdk.BadRequestError as e:
            raise ProviderError(f"The provider rejected the request: {e.message}") from e
        except sdk.APIStatusError as e:
            raise ProviderError(f"Provider API error {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except sdk.APIConnectionError as e:
            raise ProviderError("Could not reach the provider. Check the base URL and your connection.", retryable=True) from e
        if streamed:
            content, reasoning, raw_calls, finish, model, usage = streamed
        else:
            if not resp.choices:
                raise ProviderError("The provider returned no answer.")
            ch = resp.choices[0]
            m = ch.message
            content, reasoning, finish = m.content or "", getattr(m, "reasoning_content", None), ch.finish_reason
            model, usage = getattr(resp, "model", None), getattr(resp, "usage", None)
            raw_calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                         for tc in m.tool_calls or [] if getattr(tc, "function", None) is not None]
        calls = []
        for tc in raw_calls:
            try:
                args = json.loads(tc["arguments"] or "{}")
                err = None if isinstance(args, dict) else "arguments were not a JSON object"
            except ValueError:
                args, err = None, f"arguments were not valid JSON: {(tc['arguments'] or '')[:200]}"
            calls.append(ToolCall(tc["id"], tc["name"], args if isinstance(args, dict) else None, err))
        stop = "tool_use" if calls else {"length": "truncated", "content_filter": "refusal"}.get(finish, "end")
        if stop == "truncated" and calls:
            for c in calls:
                c.input_error = "tool input was cut off by the output limit; not executed"
        raw = {"provider": "openai_compatible"}
        if reasoning is not None:
            raw["reasoning_content"] = reasoning
        return Turn(text=content or "", tool_calls=calls, stop=stop, raw=raw, model=model,
                    usage={"input_tokens": getattr(usage, "prompt_tokens", None),
                           "output_tokens": getattr(usage, "completion_tokens", None)} if usage else None)


THINKING_LEVELS = ("", "off", "low", "medium", "high", "xhigh", "max")
# Request fields the extra settings may not override (the app owns them).
RESERVED_PARAMS = {"model", "messages", "tools", "tool_choice", "stream", "functions", "function_call"}


def thinking_params(preset: str | None, level: str) -> dict:
    """Request fields for a thinking level on OpenAI-compatible providers.

    DeepSeek: ``thinking: {type: enabled|disabled}`` plus ``reasoning_effort``.
    Others: the standard ``reasoning_effort`` field. Anything provider-specific
    beyond that goes in the user's extra request settings."""
    if not level:
        return {}
    if preset == "deepseek":
        if level == "off":
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"}, "reasoning_effort": level}
    if level == "off":
        return {}
    return {"reasoning_effort": level}


def make_provider(cfg: dict):
    kind = cfg.get("provider")
    if kind == "anthropic":
        return AnthropicProvider(cfg)
    if kind == "openai_compatible":
        return OpenAICompatibleProvider(cfg)
    if kind == "codex":
        raise ProviderError("The direct Codex connection is reserved for the Codex integration and is not implemented yet.")
    raise ProviderError("Choose a provider in Assistant settings.")
