"""Deterministic tool prefixes without editing message/history contents."""
from __future__ import annotations

import json


def stable_object(value):
    # Object insertion order is not JSON semantics. Array order *can* be, so
    # preserve required/enum/anyOf arrays, content blocks and tool-call order.
    if isinstance(value, dict):
        return {key: stable_object(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [stable_object(item) for item in value]
    return value


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_tools(tools):
    # Same enabled set => same prefix, independent of group selection order.
    # Loading a different set legitimately changes the prefix; cache hits are
    # still controlled by the provider, its model, routing, TTL and minimum size.
    return [stable_object(tool) for tool in sorted(tools, key=lambda t: t["name"])]
