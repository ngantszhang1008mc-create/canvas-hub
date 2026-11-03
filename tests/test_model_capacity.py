"""Capacity resolution never assumes a custom endpoint has official limits."""
import pytest

from canvas_hub.assistant.model_capacity import verified_capacity


def config(**changes):
    return {'provider': 'openai_compatible', 'model': 'deepseek-flash',
            'base_url': 'https://api.deepseek.com', **changes}


@pytest.mark.parametrize('endpoint,model', [
    ('https://api.deepseek.com', 'deepseek-flash'),
    ('https://api.deepseek.com/', 'deepseek-v4-pro'),
    ('https://api.deepseek.com/v1', 'deepseek-flash'),
    ('https://api.deepseek.com/v1/', 'deepseek-v4-pro'),
    ('https://api.deepseek.com:443/v1', 'deepseek-flash'),
    ('HTTPS://API.DEEPSEEK.COM:443/', 'deepseek-v4-pro'),
])
def test_exact_official_models_have_documented_capacity(endpoint, model):
    capacity, provenance = verified_capacity(config(base_url=endpoint, model=model))
    assert capacity == 1_048_576
    assert '2026-11-03' in provenance
    assert 'https://api-docs.deepseek.com/api/list-models/' in provenance


@pytest.mark.parametrize('endpoint', [
    None, '', 'api.deepseek.com',
    'http://api.deepseek.com', 'ftp://api.deepseek.com',
    'https://proxy.example/v1', 'https://api.deepseek.com.example/v1',
    'https://api.deepseek.com./v1',
    'https://user:password@api.deepseek.com', 'https://@api.deepseek.com',
    'https://api.deepseek.com:444', 'https://api.deepseek.com:invalid',
    'https://api.deepseek.com:99999', 'https://[api.deepseek.com]',
    'https://api.deepseek.com/beta', 'https://api.deepseek.com/v1/chat/completions',
    'https://api.deepseek.com/?q=value', 'https://api.deepseek.com/#fragment',
    'https://api.deepseek.com/?', 'https://api.deepseek.com/#',
    ' https://api.deepseek.com', 'https://api.deep\nseek.com',
    'https://api.deepseek.com\\@example.com',
])
def test_unverified_endpoint_uses_no_assumed_capacity(endpoint):
    assert verified_capacity(config(base_url=endpoint)) is None


@pytest.mark.parametrize('model', [
    None, 'unknown', 'deepseek-chat', 'deepseek-reasoner',
    'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp', 'deepseek-v4-pro-0813',
])
def test_aliases_and_unverified_model_ids_remain_unresolved(model):
    assert verified_capacity(config(model=model)) is None


@pytest.mark.parametrize('provider', [None, 'anthropic', 'codex', 'allowance_gateway'])
def test_other_provider_protocols_remain_unresolved(provider):
    assert verified_capacity(config(provider=provider)) is None


def test_capacity_fact_leaves_application_overrides_unchanged():
    cfg = config(context={'capacity': 32768, 'response_tokens': 4096})
    assert verified_capacity(cfg)[0] == 1_048_576
    assert cfg['context'] == {'capacity': 32768, 'response_tokens': 4096}
