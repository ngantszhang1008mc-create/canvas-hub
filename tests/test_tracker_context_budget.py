"""Reported tracker-budget regression; synthetic saved history and providers only."""
import copy
import json

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import Policy, context_error, valid_groups
from canvas_hub.assistant.providers import ToolCall, Turn
from canvas_hub.assistant.tool_catalog import ToolSession
from canvas_hub.boundary import HubAPI
from canvas_hub.services.context_sources import ContextSources
from canvas_hub.util import InvalidInput, now_iso

from .test_components import HMAW


@pytest.mark.parametrize('characters', [6500, 6800])
def test_tracker_results_share_unused_budget_without_masking_or_replaying(hub, account, monkeypatch, characters):
    aid = account['id']
    api = HubAPI(hub)
    svc = api.assistant = AssistantService(hub, api)
    svc.update_config({'preset': 'codex'})
    view = hub.components.create(aid, 'score_model', HMAW)
    hub.components.set_state(aid, view['id'], {'include': {'nest': False}, 'values': {'sde': 12}})
    ctx = ToolContext(api, account_id=aid)
    with hub.db.tx() as c:
        cid = c.execute('INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                        (aid, 'Synthetic stopped tracker work', now_iso(), now_iso())).lastrowid
    svc._append(cid, 'user', {'text': 'Rebuild the old HMAW tracker', 'sent_text': 'Rebuild the old HMAW tracker'})
    # Retain a successful earlier mutation and the large legacy result verbatim.
    svc._append(cid, 'assistant', {'tool_calls': [{'id': 'saved-pin', 'name': 'update_component',
                                                  'input': {'component_id': view['id'], 'pin': False}}]})
    text, error, _ = run_tool_display(ctx, 'update_component', {'component_id': view['id'], 'pin': False}, {'read', 'ui'})
    assert not error
    svc._append(cid, 'tool', {'results': [{'id': 'saved-pin', 'name': 'update_component', 'content': text, 'is_error': False}]})
    svc._append(cid, 'assistant', {'tool_calls': [{'id': 'legacy-read', 'name': 'discover_tools', 'input': {'select': ['group:tracker']}}]})
    legacy = json.dumps({'legacy_schema_description': '規' * characters}, ensure_ascii=False)
    svc._append(cid, 'tool', {'results': [{'id': 'legacy-read', 'name': 'discover_tools', 'content': legacy, 'is_error': False}]})
    before = svc._transcript(cid)
    initial = hub.components.get(aid, view['id'])
    cfg = svc._load()
    tools = ToolSession(svc.permissions(cfg), ['group:tracker']).advertised()
    report = Policy.for_config(cfg).measure(cfg, svc._system(account, svc.permissions(cfg)), before, tools)
    assert report['used']['tools'] > report['available'] * .4  # original implicit cap failed
    assert not report['over_budget'] and not Policy.for_config(cfg).auto_compact

    requests = []
    class Provider:
        def complete(self, system, transcript, advertised, on_text):
            requests.append((copy.deepcopy(transcript), advertised))
            assert legacy in [r['content'] for m in transcript if m['role'] == 'tool' for r in m['results']]
            if not advertised:
                return Turn('', tool_calls=[ToolCall('retry-must-not-write', 'update_component',
                                                     {'component_id': view['id'], 'pin': True})])
            if len(requests) == 1:
                return Turn('', tool_calls=[ToolCall('read-now', 'get_component_context', {'component_id': view['id']})])
            if len(requests) == 2:
                assert json.loads(transcript[-1]['results'][0]['content'])['state_token']
                replacement = {**HMAW, 'title': 'HMAW reviewed definition'}
                return Turn('', tool_calls=[ToolCall('proposal', 'update_component', {'component_id': view['id'], 'spec': replacement})])
            return Turn('Definition proposal ready for review; completed actions were not repeated.')
        def close(self): pass
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda cfg: Provider())
    svc._record_final_state(cid, 'error', 'Original context budget stop')
    svc.retry(aid, cid)
    svc.wait(cid, 5)
    assert hub.components.get(aid, view['id'])['version'] == initial['version']
    assert not hub.components.get(aid, view['id'])['pinned']
    assert len(requests) == 1 and requests[0][1] == []
    assert json.loads(svc._transcript(cid)[-1]['results'][0]['content'])['error'] == 'retry_tool_disabled'
    requests.clear()
    before = svc._transcript(cid)
    svc.send(aid, '/tracker Recreate the old HMAW tracker after reading its content.', cid)
    svc.wait(cid, 5)
    result = svc.get_conversation(aid, cid)
    assert result['conversation']['run_state'] == 'idle', result['conversation']
    after = hub.components.get(aid, view['id'])
    assert after['state'] == initial['state'] and after['spec'] == initial['spec']
    assert after['pending_spec'] and not after['pinned']
    assert len(requests) == 3 and all(r[1] for r in requests)
    assert svc._transcript(cid)[:len(before)] == before and valid_groups(svc._transcript(cid))
    assert not hub.db.one("SELECT 1 FROM assistant_context_usage WHERE conversation_id=? AND purpose='summary'", (cid,))


def test_explicit_caps_and_total_capacity_still_stop_with_accurate_labels():
    history = [{'role': 'tool', 'results': [{'name': 'get_component', 'content': 'x' * 9000}]}]
    cfg = {'provider': 'codex', 'context': {'tool_tokens': 512}}
    report = Policy.for_config(cfg).measure(cfg, 'system', history, [])
    assert report['over_budget'] and report['limits']['tools'] == 512
    message = str(context_error(report))
    assert 'tool results' in message and 'tool schemas' in message and 'get_component' in message
    assert 'Retry answer cannot run tools' in message and 'Do not repeat completed changes' in message
    cfg = {'provider': 'codex', 'context': {'capacity': 4096, 'response_tokens': 512}}
    assert Policy.for_config(cfg).measure(cfg, 'system', history, [])['over_budget']
    assert Policy.for_config({'provider': 'openai_compatible', 'model': 'unknown'}).capacity == 32768
    assert Policy.for_config({'provider': 'codex', 'model': 'gpt-6.1-sol'}).capacity == 64000
    assert Policy.for_config({'provider': 'codex', 'context': {'capacity': 32000}}).capacity == 32000


def test_source_catalog_keeps_metadata_pages_and_full_tail_rules(hub, account):
    aid = account['id']
    original = '課程規定 ' * 1200 + 'FINAL EXCEPTION: preserve the last rule.'
    with hub.db.tx() as c:
        for n in range(25):
            title = '課程來源' * 70 + str(n)
            iid = c.execute('INSERT INTO items(account_id,item_type,source_key,title,source_url,first_observed_at,last_observed_at) VALUES (?,?,?,?,?,?,?)',
                            (aid, 'page', f'catalog-{n}', title, f'https://example.test/source/{n}', now_iso(), now_iso())).lastrowid
            hub.search.index_item(c, aid, iid, 'page', title, {'body': original})
    index = ContextSources(hub.db)
    rows, offset = [], 0
    while True:
        page = index.catalog(aid, offset=offset, byte_limit=8000)
        assert not page['preview_included'] and all('preview' not in row for row in page['sources'])
        assert len(json.dumps(page, ensure_ascii=False).encode('utf-8')) < 9500
        rows.extend(page['sources'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert len(rows) == len({r['source_id'] for r in rows}) == 25
    assert all(r['version'] and r['source_url'] and r['title'] and r['total_chars'] > 4000 for r in rows)
    rich = index.catalog(aid, include_preview=True)
    assert rich['preview_included'] and rich['sources'][0]['preview']
    assert 'not a complete summary' in rich['preview_kind']
    source, pieces, offset = rows[-1], [], 0
    while True:
        page = index.read(aid, source['source_id'], version=source['version'], offset=offset, limit=1500)
        pieces.append(page['text'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert 'FINAL EXCEPTION: preserve the last rule.' in ''.join(pieces)
    assert len(''.join(pieces)) == source['total_chars']


def test_codex_discovery_only_sends_new_schemas_but_can_explicitly_reread():
    session = ToolSession({'read', 'ui'}, ['group:tracker'])
    initial = session.advertised()
    for _ in range(7):
        result = json.loads(session.discover({'select': ['group:tracker']}, include_schemas=True))
        assert result['schemas'] == [] and 'tools' not in result and 'groups' not in result
        assert 'get_component' in result['schemas_already_available'] and not result['executed']
    loaded = json.loads(session.discover({'select': ['tool:read_context_source']}, include_schemas=True))
    assert [t['name'] for t in loaded['schemas']] == ['read_context_source']
    repeated = json.loads(session.discover({'select': ['tool:read_context_source']}, include_schemas=True))
    assert repeated['schemas'] == []
    refreshed = json.loads(session.discover({'select': ['tool:read_context_source'], 'refresh_schemas': True}, include_schemas=True))
    assert 'read_context_source' in {t['name'] for t in refreshed['schemas']}
    metadata = json.loads(session.discover({'query': 'tracker'}))
    assert metadata['groups'] and metadata['tools']
    session.discover({'select': []}, include_schemas=True)
    with pytest.raises(InvalidInput, match='not currently loaded'):
        session.dispatch({'name': 'read_context_source', 'arguments': {}})
    assert session.advertised() == initial


@pytest.mark.parametrize('history,tool_results,overhead,available', [
    (1468, 20461, 6586, 46918), (1751, 20461, 9205, 44299), (1619, 19693, 10306, 43198)])
def test_exact_reported_budget_amounts_fit_without_capacity_increase(history, tool_results, overhead, available):
    cfg = {'provider': 'codex', 'model': 'gpt-6.1-sol', 'thinking': 'xhigh'}
    policy = Policy.for_config(cfg)
    transcript = [{'role': 'user', 'sent_text': ''}, {'role': 'tool', 'results': [
        {'id': str(n), 'name': 'discover_tools', 'content': ''} for n in range(3)]}]
    system = ''
    def report():
        return policy.measure(cfg, system, transcript, [])
    def fill(target, write, read):
        lo, hi = 0, target * 4
        while lo < hi:
            mid = (lo + hi) // 2
            write(mid)
            if read() < target:
                lo = mid + 1
            else:
                hi = mid
        write(lo)
        assert read() == target
    def write_history(n):
        transcript[0]['sent_text'] = 'x' * n
    def write_tools(n):
        for i, result in enumerate(transcript[1]['results']):
            result['content'] = 'x' * (n // 3 + (i < n % 3))
    def write_system(n):
        nonlocal system
        system = 'x' * n
    fill(history, write_history, lambda: report()['used']['history'])
    fill(tool_results, write_tools, lambda: report()['used']['tools'])
    fill(overhead, write_system, lambda: report()['overhead_tokens'])
    measured = report()
    assert measured['capacity'] == 64000 and measured['available'] == available
    assert tool_results > int(available * .4) and history + tool_results < available
    assert not measured['over_budget'] and not measured['near_limit']
