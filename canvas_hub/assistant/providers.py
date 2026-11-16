"""Model providers for the in-app assistant.

* ``anthropic``         - Claude through the official Anthropic SDK (streamed).
* ``codex``             - the locally installed Codex app-server and its own login.
* ``openai_compatible`` - any Chat Completions-compatible endpoint through the
  official OpenAI SDK with a configurable base URL (OpenAI, Google Gemini's
  OpenAI endpoint, OpenRouter, DeepSeek, local Ollama/LM Studio, ...).

All providers take a provider-neutral transcript and return a ``Turn``. Claude's own
content blocks (including reasoning blocks) are kept verbatim and replayed
unchanged on later requests; history is append-only.

With ``on_text`` replies stream: it is called with the reply's text so far as it
arrives (the whole text each time, so a retried request simply starts over).
The callback may raise ``Stopped`` to end the request early.
"""

from __future__ import annotations

import json
from contextlib import closing
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .request_shape import stable_json, stable_object, stable_tools
from .usage import normalize_usage
from .model_capacity import resolve as resolve_limits, verified_capacity

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


def _reply_mode(cfg: dict) -> str:
    settings = cfg.get("context") or {}
    if settings.get("reply_limit") in ("none", "custom"):
        return settings["reply_limit"]
    return "custom" if "response_tokens" in settings else "legacy"


def anthropic_output_tokens(cfg: dict) -> int:
    """The ``max_tokens`` sent to Claude. The API requires one, so "no limit" means the model's own maximum.

    This adapter supports output_config.effort, not manual thinking budgets.
    Effort is soft guidance; max_tokens includes both thinking and answer text:
    https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost
    """
    settings = cfg.get("context") or {}
    extra = cfg.get("extra_params") or {}
    if cfg.get("thinking") not in (None, "") or {"thinking", "budget_tokens"} & extra.keys():
        raise ProviderError("Anthropic thinking/budget_tokens settings are not supported by this adapter. "
                            "Use the chat's reasoning effort and an explicit output limit instead; "
                            "configured thinking was not changed and no request was sent.")
    limits = [extra[k] for k in ("max_tokens", "max_completion_tokens") if k in extra]
    configured = limits + [v for k, v in settings.items() if k == "response_tokens"]
    if "_response_tokens" in cfg and cfg["_response_tokens"] is not None:
        configured.append(cfg["_response_tokens"])
    if any(type(v) is not int or v < 1 for v in configured):
        raise ProviderError("Anthropic output limits must be positive integers; no request was sent.")
    if len(limits) == 2 and limits[0] != limits[1]:
        raise ProviderError("Anthropic max_tokens and max_completion_tokens conflict. "
                            "Choose one explicit output limit; no request was sent.")
    # max_completion_tokens is retained as the output-limit alias already
    # recognized by context policy; only max_tokens is sent to Anthropic.
    if limits:
        return limits[0]
    if cfg.get("_response_tokens") is not None:
        return cfg["_response_tokens"]
    mode = _reply_mode(cfg)
    if mode == "custom":
        return settings["response_tokens"]
    if mode == "none":
        # Replies are streamed, so a large ceiling does not risk a request timeout.
        return resolve_limits(cfg)["max_output"] or 64000
    return 6000 if settings.get("capacity", 64000) <= 64000 else MAX_TOKENS


def compatible_output_tokens(cfg: dict) -> int | None:
    """The reply cap sent to an OpenAI-compatible provider; ``None`` sends none, so the model's own maximum applies."""
    extra, settings = cfg.get("extra_params") or {}, cfg.get("context") or {}
    limits = [extra[k] for k in ("max_tokens", "max_completion_tokens") if k in extra]
    configured = limits + [settings[k] for k in ("response_tokens",) if k in settings]
    if "_response_tokens" in cfg and cfg["_response_tokens"] is not None:
        configured.append(cfg["_response_tokens"])
    if any(type(v) is not int or v < 1 for v in configured):
        raise ProviderError("Reply token limits must be positive integers; no request was sent.")
    if len(limits) == 2 and limits[0] != limits[1]:
        raise ProviderError("max_tokens and max_completion_tokens conflict. Choose one reply token limit; no request was sent.")
    if limits:
        return limits[0]
    if cfg.get("_response_tokens") is not None:
        return cfg["_response_tokens"]
    mode = _reply_mode(cfg)
    if mode == "custom":
        return settings["response_tokens"]
    if mode == "none":
        return None
    # A setup saved before the choice existed keeps the automatic caps it had.
    default = 4096
    capability = verified_capacity(cfg)
    if capability:
        options = {**extra, **thinking_params(cfg.get("preset"), cfg.get("thinking") or "")}
        mode = options["thinking"].get("type") if isinstance(options.get("thinking"), dict) else None
        thinking = mode == "enabled" or (mode is None and options.get("reasoning_effort") not in ("none", "off"))
        if thinking:
            # DeepSeek defaults to thinking; reasoning and text share max_tokens.
            # https://api-docs.deepseek.com/api/create-chat-completion/
            default = min(16384, max(4096, settings.get("capacity", capability[0]) // 4))
    return default


def incomplete_reply(text, stop, tool_calls) -> str | None:
    if stop == "truncated":
        return "output_limit"
    if stop != "refusal" and not tool_calls and not (text or "").strip():
        return "empty_reply"
    return None


def incomplete_reply_message(turn) -> str:
    if turn.incomplete_reason == "output_limit":
        message = "The provider reached its output limit before finishing. "
        usage = turn.usage or {}
        output, reasoning = usage.get("output_tokens"), usage.get("reasoning_output_tokens")
        if type(output) is int:
            message += f"Reported output: {output} tokens"
            if type(reasoning) is int:
                message += f" (including {reasoning} reasoning tokens)"
            message += ". "
        message += ("Partial text is saved as incomplete. " if turn.text.strip() else "No final answer was returned. ")
        message += "Raise the reply limit, or choose no limit, in Assistant settings (or lower reasoning) before retrying; a higher limit can use more quota. "
    else:
        message = "The provider returned no answer or tool call. Check the provider and output-format settings before retrying. "
    return message + ("Earlier tool results are saved; no tools from this incomplete reply were executed. "
                      "Retry answer makes a new request with tools disabled. Review previous results before sending a new message that needs tools.")


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

    @property
    def incomplete_reason(self) -> str | None:
        return incomplete_reply(self.text, self.stop, self.tool_calls)


def _neutral_assistant(msg: dict) -> tuple[str, list[dict]]:
    return msg.get("text") or "", msg.get("tool_calls") or []


# --------------------------------------------------------------------- Anthropic
class AnthropicProvider:
    name = "anthropic"

    def close(self):
        self.client.close()

    def __init__(self, cfg: dict):
        import anthropic
        self._sdk = anthropic
        self.model = cfg.get("model") or ANTHROPIC_DEFAULT_MODEL
        self.effort = cfg.get("effort") or None
        self.base_url = (cfg.get("base_url") or "").strip() or None
        self.single_attempt = cfg.get("_single_attempt", False)
        self.response_tokens = anthropic_output_tokens(cfg)
        # A transport failure can occur after acceptance/billing. Recovery is
        # the existing explicit Retry action, never an invisible SDK replay.
        kw = {"max_retries": 0, "timeout": 120.0 if self.single_attempt else 600.0}
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
        self.last_usage = normalize_usage(None, "anthropic", final=False)
        sdk = self._sdk
        official = self.base_url is None
        tool_defs = []
        for t in stable_tools(tools):
            d = {"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
            if official:
                d["eager_input_streaming"] = True   # inputs are validated before any tool runs
            tool_defs.append(d)
        kwargs = dict(model=self.model, max_tokens=self.response_tokens, system=system, messages=self._messages(transcript),
                      cache_control={"type": "ephemeral"})
        if tool_defs:
            kwargs["tools"] = tool_defs
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        use_fallback = not self.single_attempt and official and self.model in _FALLBACK_MODELS
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
                        self.last_usage = normalize_usage(stream.current_message_snapshot.usage, "anthropic", final=False)
                        so_far += piece
                        on_text(so_far)
                msg = stream.get_final_message()
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
        except ValueError as e:
            raise ProviderError("The model produced an unreadable tool call. Review the reply before retrying.") from e
        data = msg.to_dict()
        self.last_usage = normalize_usage(data.get("usage"), "anthropic", final=bool(data.get("stop_reason")))
        if not data.get("stop_reason"):
            raise ProviderError("The provider's reply was cut off before it finished. Try again.", retryable=True)
        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") if isinstance(b.get("input"), dict) else None,
                          None if isinstance(b.get("input"), dict) else "input was not a JSON object")
                 for b in content if b.get("type") == "tool_use"]
        stop = {"tool_use": "tool_use", "refusal": "refusal", "max_tokens": "truncated"}.get(data.get("stop_reason"), "end")
        if stop == "truncated" and calls:
            for c in calls:
                c.input_error = "tool input was cut off by the output limit; not executed"
        return Turn(text=text, tool_calls=calls, stop=stop, raw={"provider": "anthropic", "content": content},
                    model=data.get("model"), usage=self.last_usage)


# --------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatibleProvider:
    name = "openai_compatible"

    def close(self):
        self.client.close()

    def __init__(self, cfg: dict):
        import openai
        self._sdk = openai
        self.model = (cfg.get("model") or "").strip()
        if not self.model:
            raise ProviderError("Enter the model name for this provider in Assistant settings.")
        key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or "not-needed"
        self.client = openai.OpenAI(api_key=key, base_url=(cfg.get("base_url") or "").strip() or None,
                                    timeout=120.0 if cfg.get("_single_attempt") else 600.0,
                                    max_retries=0)
        # Extra request settings are the setup's defaults; a reasoning level
        # picked in the chat for this message takes precedence.
        self.extra_body = dict(cfg.get("extra_params") or {})
        self.extra_body.update(thinking_params(cfg.get("preset"), cfg.get("thinking") or ""))
        self.response_tokens = compatible_output_tokens(cfg)
        self.output_limit_key = "max_completion_tokens" if cfg.get("preset") == "openai" else "max_tokens"
        self.single_attempt = cfg.get("_single_attempt", False)
        self.no_streaming = False
        host = urlsplit(str(self.client.base_url)).hostname
        self.usage_provider = "gemini" if host == "generativelanguage.googleapis.com" else "openai_compatible"
        # Officially documented opt-in. OpenRouter reports usage automatically;
        # custom endpoints are unknown unless the user supplies stream_options.
        self.include_usage = host in {"api.openai.com", "api.deepseek.com", "generativelanguage.googleapis.com", "ollama.com"}
        if cfg.get("preset") == "ollama" and host in {"localhost", "127.0.0.1", "::1"}:
            self.include_usage = True

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
                                        "function": {"name": c["name"], "arguments": stable_json(c.get("input") or {})}}
                                       for c in calls]
                elif not text:
                    d["content"] = "(no reply)"
                out.append(d)
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return out

    def _open_stream(self, kwargs):
        """Capability fallback ONLY around the initial HTTP request creation.

        An error while consuming an accepted stream must never enter this path.
        At most one retry per explicitly rejected option, and none for summaries.
        """
        options = dict(self.extra_body.get("stream_options") or {})
        if self.include_usage:
            options["include_usage"] = True
        stream_kwargs = {**kwargs, "stream": True}
        if options:
            stream_kwargs["stream_options"] = stable_object(options)
        while True:
            try:
                return self.client.chat.completions.create(**stream_kwargs)
            except self._sdk.BadRequestError as e:
                if self.single_attempt:
                    raise
                if "stream_options" in stream_kwargs and _unsupported_stream_parameter(e, "stream_options"):
                    stream_kwargs.pop("stream_options")
                    self.include_usage = False
                    # Remember a rejection of a user-specified option for this
                    # provider instance, too; never retry it each tool round.
                    self.extra_body.pop("stream_options", None)
                elif _unsupported_stream_parameter(e, "stream"):
                    self.no_streaming = True
                    return None
                else:
                    raise

    def _streamed(self, kwargs: dict, on_text) -> tuple | None:
        """Stream a Chat Completions reply; returns (content, reasoning, calls, finish, model, usage)."""
        text, reasoning, calls = "", None, {}
        finish = model = usage = None
        opened = self._open_stream(kwargs)
        if opened is None:
            return None
        with closing(opened) as stream:
            for chunk in stream:
                model = getattr(chunk, "model", None) or model
                usage = getattr(chunk, "usage", None) or usage
                if usage is not None:
                    self.last_usage = normalize_usage(usage, self.usage_provider, final=False)
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
        self.last_usage = normalize_usage(None, self.usage_provider, final=False)
        sdk = self._sdk
        kwargs = dict(model=self.model, messages=self._messages(system, transcript))
        if self.response_tokens and not any(k in self.extra_body for k in ("max_tokens", "max_completion_tokens")):
            kwargs[self.output_limit_key] = self.response_tokens
        extra = {k: v for k, v in self.extra_body.items() if k != "stream_options"}
        if extra:
            kwargs["extra_body"] = stable_object(extra)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                 "parameters": t["schema"]}} for t in stable_tools(tools)]
        try:
            streamed = None
            if on_text and not self.no_streaming:
                streamed = self._streamed(kwargs, on_text)
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
            self.last_usage = normalize_usage(getattr(resp, "usage", None), self.usage_provider)
            if not resp.choices:
                raise ProviderError("The provider returned no answer.")
            ch = resp.choices[0]
            m = ch.message
            content, reasoning, finish = m.content or "", getattr(m, "reasoning_content", None), ch.finish_reason
            model, usage = getattr(resp, "model", None), getattr(resp, "usage", None)
            raw_calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                         for tc in m.tool_calls or [] if getattr(tc, "function", None) is not None]
        self.last_usage = normalize_usage(usage, self.usage_provider)
        calls = []
        for tc in raw_calls:
            try:
                args = json.loads(tc["arguments"] or "{}")
                err = None if isinstance(args, dict) else "arguments were not a JSON object"
            except ValueError:
                args, err = None, f"arguments were not valid JSON: {(tc['arguments'] or '')[:200]}"
            calls.append(ToolCall(tc["id"], tc["name"], args if isinstance(args, dict) else None, err))
        # A context-managed output cap can stop a tool-bearing response. Even
        # parseable arguments must not execute when the provider marks it cut off.
        stop = {"length": "truncated", "content_filter": "refusal"}.get(finish, "tool_use" if calls else "end")
        if stop == "truncated" and calls:
            for c in calls:
                c.input_error = "tool input was cut off by the output limit; not executed"
        raw = {"provider": "openai_compatible"}
        if reasoning is not None:
            raw["reasoning_content"] = reasoning
        return Turn(text=content or "", tool_calls=calls, stop=stop, raw=raw, model=model,
                    usage=self.last_usage)


def _unsupported_stream_parameter(error, parameter):
    """Only an unambiguous 400 validation rejection allows capability fallback."""
    if getattr(error, "status_code", None) != 400:
        return False
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return False
    body = body.get("error", body)
    if not isinstance(body, dict):
        return False
    names = {parameter} if parameter == "stream" else {"stream_options", "stream_options.include_usage", "include_usage"}
    if body.get("param") in names and body.get("code") in {"unsupported_parameter", "unknown_parameter", "unrecognized_parameter"}:
        return True
    message = str(body.get("message", "")).lower().strip()
    for name in names:
        token = r"[`'\"]?" + re.escape(name) + r"[`'\"]?"
        if re.fullmatch(token + r" (?:is |are )?(?:not supported|unsupported)(?: for (?:this|the) (?:model|endpoint))?\.?", message):
            return True
        if re.fullmatch(r"(?:unsupported|unknown|unrecognized) (?:parameter|field|argument):? " + token + r"\.?", message):
            return True
    return False


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
    if kind == 'allowance_gateway':
        from .gateway import AllowanceGatewayProvider
        return AllowanceGatewayProvider(cfg)
    if kind == "anthropic":
        return AnthropicProvider(cfg)
    if kind == "openai_compatible":
        return OpenAICompatibleProvider(cfg)
    if kind == "codex":
        from .codex import CodexProvider
        return CodexProvider(cfg)
    raise ProviderError("Choose a provider in Assistant settings.")
