"""What a model can actually take in and put out.

A limit comes from the best source available, in this order:

1. the provider itself, asked when a setup is saved and tested (``detect``);
2. published limits for exact model identifiers (``known``);
3. nothing. An unknown model is *not* given an invented limit: the app then
   does not limit the request, and the provider enforces its own.

A value the user typed in Assistant settings always wins over all three; that
is applied by ``context.Policy``, not here.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from ..util import now_iso

# Claude model limits as published in Anthropic's model table (context window,
# maximum output), checked 2026-11-03. Exact identifiers only: a dated or
# renamed model is asked from the Models API instead of being guessed.
_CLAUDE = {name: (1_000_000, 128_000) for name in (
    "claude-fable-5-2", "claude-mythos-5-2", "claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5",
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5-5")}
_CLAUDE.update({"claude-haiku-4-5": (200_000, 64_000), "claude-haiku-4-5-20251001": (200_000, 64_000)})
_CLAUDE_SOURCE = "Anthropic's published model limits"

_DEEPSEEK_MODELS = frozenset({'deepseek-flash', 'deepseek-v4-pro'})
_DEEPSEEK_PROVENANCE = (
    'DeepSeek official model metadata, checked 2026-11-03: '
    'https://api-docs.deepseek.com/api/list-models/'
)
_DEEPSEEK_OUTPUT = 128_000

# Names different OpenAI-compatible servers use for the same two facts.
_CONTEXT_KEYS = ("context_length", "context_window", "max_context_length", "max_input_tokens", "input_token_limit",
                 "inputTokenLimit", "max_model_len", "n_ctx")
_OUTPUT_KEYS = ("max_completion_tokens", "max_output_tokens", "output_token_limit", "outputTokenLimit", "max_tokens")


def _official_host(cfg: dict, host: str) -> bool:
    endpoint = cfg.get('base_url')
    if (not isinstance(endpoint, str) or not endpoint
            or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)
            or any(c in endpoint for c in ('\\', '?', '#'))):
        return False
    try:
        url = urlsplit(endpoint)
        return (url.scheme == 'https' and url.hostname == host and url.username is None and url.password is None
                and url.port in (None, 443) and url.path in ('', '/', '/v1', '/v1/'))
    except ValueError:
        return False


def verified_capacity(cfg: dict) -> tuple[int, str] | None:
    """The documented DeepSeek window for its exact endpoint and models (kept for existing callers)."""
    model = cfg.get('model')
    if (cfg.get('provider') != 'openai_compatible' or not isinstance(model, str) or model not in _DEEPSEEK_MODELS
            or not _official_host(cfg, 'api.deepseek.com')):
        return None
    # Official /models documentation gives this exact input-plus-output limit
    # for both canonical models; do not infer it from a "deepseek" name prefix.
    return 1_048_576, _DEEPSEEK_PROVENANCE


def known(cfg: dict) -> dict | None:
    """Published limits for this exact model, without any network request."""
    model = (cfg.get("model") or "").strip()
    if cfg.get("provider") == "anthropic":
        # A custom base URL may be a proxy for some other model; only trust the table for Anthropic itself.
        if cfg.get("base_url") and not _official_host(cfg, "api.anthropic.com"):
            return None
        limits = _CLAUDE.get(model or "claude-opus-5-5")
        return {"context": limits[0], "max_output": limits[1], "source": _CLAUDE_SOURCE} if limits else None
    deepseek = verified_capacity(cfg)
    if deepseek:
        return {"context": deepseek[0], "max_output": _DEEPSEEK_OUTPUT, "source": deepseek[1]}
    return None


def _positive(value, low=256, high=50_000_000):
    return value if type(value) is int and low <= value <= high else None


def _from_model_object(data: dict) -> tuple[int | None, int | None]:
    """Read limits from whatever a ``GET /models/{id}`` answer contains."""
    layers = [data] + [data[k] for k in ("top_provider", "limits", "limit", "meta", "details", "capabilities")
                       if isinstance(data.get(k), dict)]
    context = next((v for layer in layers for k in _CONTEXT_KEYS if (v := _positive(layer.get(k)))), None)
    # ``max_tokens`` on its own often means the context size; only read it as an output cap beside a context value.
    output = next((v for layer in layers for k in _OUTPUT_KEYS
                   if (k != "max_tokens" or context) and (v := _positive(layer.get(k), low=16))), None)
    if context and output and output > context:
        output = None
    return context, output


def detect(cfg: dict) -> dict | None:
    """Ask the provider for this model's limits. One small metadata request; no tokens are used.

    Returns ``None`` when the provider does not say (many OpenAI-compatible
    servers do not) or cannot be reached. Never raises.
    """
    provider, model = cfg.get("provider"), (cfg.get("model") or "").strip()
    try:
        if provider == "anthropic":
            import anthropic
            kw = {"max_retries": 0, "timeout": 15.0}
            if cfg.get("api_key"):
                kw["api_key"] = cfg["api_key"]
            if (cfg.get("base_url") or "").strip():
                kw["base_url"] = cfg["base_url"].strip()
            client = anthropic.Anthropic(**kw)
            try:
                info = client.models.retrieve(model or "claude-opus-5-5")
                context, output = _positive(getattr(info, "max_input_tokens", None)), _positive(getattr(info, "max_tokens", None), low=16)
            finally:
                client.close()
            source = "reported by the Anthropic Models API"
        elif provider == "openai_compatible" and model:
            import os

            import openai
            client = openai.OpenAI(api_key=cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or "not-needed",
                                   base_url=(cfg.get("base_url") or "").strip() or None, timeout=15.0, max_retries=0)
            try:
                info = client.models.retrieve(model)
                data = info.model_dump() if hasattr(info, "model_dump") else dict(info)
            finally:
                client.close()
            context, output = _from_model_object(data if isinstance(data, dict) else {})
            host = urlsplit(cfg.get("base_url") or "").hostname or "the provider"
            source = f"reported by {host}"
        else:
            return None
    except Exception:  # noqa: BLE001 - detection is best effort; the setup still works without it
        return None
    if not context:
        return None
    return {"context": context, "max_output": output, "source": source, "model": model,
            "base_url": (cfg.get("base_url") or "").strip(), "checked_at": now_iso()}


def resolve(cfg: dict) -> dict:
    """The model's limits from detection or the published table; ``context`` is ``None`` when unknown."""
    found = cfg.get("detected_limits")
    if (isinstance(found, dict) and found.get("model") == (cfg.get("model") or "").strip()
            and found.get("base_url") == (cfg.get("base_url") or "").strip() and _positive(found.get("context"))):
        return {"context": found["context"], "max_output": _positive(found.get("max_output"), low=16),
                "source": found.get("source") or "reported by the provider", "basis": "detected"}
    table = known(cfg)
    if table:
        return {**table, "basis": "published"}
    return {"context": None, "max_output": None, "source": "not known for this model", "basis": "unknown"}
