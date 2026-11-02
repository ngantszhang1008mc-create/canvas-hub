"""Local token telemetry, never the authority for free allowance or billing.

Version 1 uses total input (including cache reads/writes) and total output
(including reasoning). Detail counters are subsets, not additional tokens.
Missing counters stay None; estimates and nonterminal snapshots are distinct.
"""
from __future__ import annotations

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
          "cache_creation_input_tokens", "reasoning_output_tokens")


def _get(value, key):
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _at(value, *keys):
    for key in keys:
        value = _get(value, key)
    return _count(value)


def normalize_usage(value, provider, *, final=True):
    """Normalize only documented counters, preserving their native breakdown.

    ``gateway`` accepts PR35's MeteredUsage contract; it does not settle quota.
    ``legacy`` is the pre-v1 local input/output shape (Anthropic handled below).
    """
    if isinstance(value, dict) and value.get("usage_version") == 1:
        return value
    raw = {}

    def take(name, *path):
        result = _at(value, *path)
        if result is not None:
            raw[name] = result
        return result

    cached = written = reasoning = None
    warning = None
    if provider == "anthropic":
        fresh = take("input_tokens", "input_tokens")
        cached = take("cache_read_input_tokens", "cache_read_input_tokens")
        written = take("cache_creation_input_tokens", "cache_creation_input_tokens")
        take("cache_creation.ephemeral_5m_input_tokens", "cache_creation", "ephemeral_5m_input_tokens")
        take("cache_creation.ephemeral_1h_input_tokens", "cache_creation", "ephemeral_1h_input_tokens")
        # Some compatible/older endpoints omit cache counters. Do not silently
        # treat those missing categories as zero or call the subtotal complete.
        input_tokens = sum((fresh, cached, written)) if all(x is not None for x in (fresh, cached, written)) else None
        output_tokens = take("output_tokens", "output_tokens")
        reasoning = take("output_tokens_details.thinking_tokens", "output_tokens_details", "thinking_tokens")
    elif provider == "codex":
        input_tokens = take("inputTokens", "inputTokens")
        output_tokens = take("outputTokens", "outputTokens")
        cached = take("cachedInputTokens", "cachedInputTokens")
        written = take("cacheWriteInputTokens", "cacheWriteInputTokens")
        reasoning = take("reasoningOutputTokens", "reasoningOutputTokens")
        take("totalTokens", "totalTokens")
    elif provider == "gateway":
        input_tokens = take("input_tokens", "input_tokens")
        output_tokens = take("output_tokens", "output_tokens")
        cached = take("cached_input_tokens", "cached_input_tokens")
        written = take("cache_write_input_tokens", "cache_write_input_tokens")
        reasoning = take("reasoning_tokens", "reasoning_tokens")
        take("cache_write_30m_input_tokens", "cache_write_30m_input_tokens")
        take("cache_write_12h_input_tokens", "cache_write_12h_input_tokens")
    elif provider == "legacy":
        input_tokens = take("input_tokens", "input_tokens")
        output_tokens = take("output_tokens", "output_tokens")
        cached = take("cache_read_input_tokens", "cache_read_input_tokens")
        written = take("cache_creation_input_tokens", "cache_creation_input_tokens")
        reasoning = take("reasoning_output_tokens", "reasoning_output_tokens")
    else:  # Chat Completions: detail fields are already included in totals.
        input_tokens = take("prompt_tokens", "prompt_tokens")
        output_tokens = take("completion_tokens", "completion_tokens")
        cached = take("prompt_tokens_details.cached_tokens", "prompt_tokens_details", "cached_tokens")
        hit = take("prompt_cache_hit_tokens", "prompt_cache_hit_tokens")
        take("prompt_cache_miss_tokens", "prompt_cache_miss_tokens")
        if cached is None:
            cached = hit
        written = take("prompt_tokens_details.cache_write_tokens", "prompt_tokens_details", "cache_write_tokens")
        reasoning = take("completion_tokens_details.reasoning_tokens", "completion_tokens_details", "reasoning_tokens")
        total = take("total_tokens", "total_tokens")
        if provider == "gemini" and input_tokens is not None and output_tokens is not None:
            # Google's compatibility responses can report candidate completion
            # tokens separately from reasoning. Only add an explicit breakdown
            # when the reported total confirms that decomposition. Never infer
            # reasoning from a remainder (it can also include tool-use input).
            if total == input_tokens + output_tokens:
                pass  # Endpoint already included all output.
            elif reasoning is not None and total == input_tokens + output_tokens + reasoning:
                output_tokens += reasoning
            else:
                output_tokens = None
                warning = "gemini_output_breakdown_unavailable"
        elif total is not None and input_tokens is not None and output_tokens is not None and total != input_tokens + output_tokens:
            output_tokens = None
            warning = "inconsistent_provider_totals"
    complete = final and input_tokens is not None and output_tokens is not None
    return {"usage_version": 1, "source": "gateway" if provider == "gateway" else "provider",
            "status": "reported" if complete else "partial" if raw else "unknown", "final": final,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": written,
            "reasoning_output_tokens": reasoning,
            "total_tokens": input_tokens + output_tokens if complete else None,
            "provider_usage": raw, **({"normalization_warning": warning} if warning else {})}


def stored_usage(value, provider=None):
    """Read legacy persisted metrics without changing stored conversation data."""
    kind = {"anthropic": "anthropic", "allowance_gateway": "gateway"}.get(provider, "legacy")
    result = normalize_usage(value, kind)
    if isinstance(value, dict) and (value.get("source") == "estimated" or value.get("status") == "estimated"):
        return {**result, "source": "estimated", "status": "estimated"}
    return result


def usage_totals(values):
    """Reported subtotals plus per-field coverage; absent totals are not zero."""
    values = list(values)
    result = {key: 0 for key in FIELDS}
    missing = {key: 0 for key in FIELDS}
    unreported = estimated = 0
    for value in values:
        u = stored_usage(value)
        is_estimate = u.get("source") == "estimated" or u.get("status") == "estimated"
        eligible = u.get("final", True) and not is_estimate
        estimated += is_estimate
        unreported += not eligible or u.get("input_tokens") is None or u.get("output_tokens") is None
        for key in FIELDS:
            count = _count(u.get(key)) if eligible else None
            if count is None:
                missing[key] += 1
            else:
                result[key] += count
    return {**result, "unreported_requests": unreported, "estimated_requests": estimated,
            "requests": len(values), "missing_fields": missing,
            "status": "partial" if unreported else "reported", "quota_authoritative": False}
