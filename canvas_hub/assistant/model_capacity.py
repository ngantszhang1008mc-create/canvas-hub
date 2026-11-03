"""Documented capacities for exact provider endpoints and model identifiers.

These are provider limits, not application spending or output budgets. Unknown
endpoints and aliases remain unresolved; this module makes no network requests.
"""
from __future__ import annotations

from urllib.parse import urlsplit


_DEEPSEEK_MODELS = frozenset({'deepseek-flash', 'deepseek-v4-pro'})
_DEEPSEEK_PROVENANCE = (
    'DeepSeek official model metadata, checked 2026-11-03: '
    'https://api-docs.deepseek.com/api/list-models/'
)


def verified_capacity(cfg: dict) -> tuple[int, str] | None:
    """Return a verified provider capacity; callers apply user policy overrides."""
    model = cfg.get('model')
    if (cfg.get('provider') != 'openai_compatible'
            or not isinstance(model, str) or model not in _DEEPSEEK_MODELS):
        return None
    endpoint = cfg.get('base_url')
    if (not isinstance(endpoint, str) or not endpoint
            or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)
            or any(c in endpoint for c in ('\\', '?', '#'))):
        return None
    try:
        url = urlsplit(endpoint)
        if (url.scheme != 'https' or url.hostname != 'api.deepseek.com'
                or url.username is not None or url.password is not None
                or url.port not in (None, 443)
                or url.path not in ('', '/', '/v1', '/v1/')):
            return None
    except ValueError:
        return None
    # Official /models documentation gives this exact input-plus-output limit
    # for both canonical models; do not infer it from a "deepseek" name prefix.
    return 1_048_576, _DEEPSEEK_PROVENANCE
