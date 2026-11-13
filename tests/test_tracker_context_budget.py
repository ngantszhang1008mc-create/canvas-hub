"""Tool selection limits, source catalog paging and tool discovery; synthetic data only."""
import json

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.tool_catalog import ToolSession
from canvas_hub.boundary import HubAPI
from canvas_hub.services.context_sources import ContextSources
from canvas_hub.util import InvalidInput, now_iso

EXTRA_TRACKER_TOOLS = ['tool:' + name for name in (
    'list_context_sources', 'read_context_source', 'search_context_sources',
    'get_coverage', 'show_artifact')]


def test_overfull_tracker_selection_is_rejected_before_dispatch_or_history(hub, account, monkeypatch):
    api = HubAPI(hub)
    svc = api.assistant = AssistantService(hub, api)
    svc.update_config({'preset': 'codex'})
    permissions = svc.permissions(svc._load())
    extras = [*EXTRA_TRACKER_TOOLS, 'tool:wait_for_job']
    selection = ['group:tracker', *extras]
    session = ToolSession(permissions, ['group:tracker'])
    before_tools = session.advertised()

    # The bounded-reader and guarded-rebuild tools bring the old fixture's
    # selection to 33 schemas. Rejection must not trim or partially load it.
    with pytest.raises(InvalidInput) as discovered:
        session.discover({'select': extras})
    assert discovered.value.code == 'tool_selection_too_large'
    assert discovered.value.detail['advertised_count'] == 33
    assert discovered.value.detail['max_advertised'] == 32
    assert discovered.value.detail['over_by'] == 1
    assert session.manual == ['group:tracker'] and session.discovered == []
    assert session.advertised() == before_tools

    provider_calls = []
    def unexpected_provider(cfg):
        provider_calls.append(cfg)
        pytest.fail('an over-limit selection must stop before provider creation')
    monkeypatch.setattr('canvas_hub.assistant.make_provider', unexpected_provider)
    with pytest.raises(InvalidInput) as sent:
        svc.send(account['id'], 'Explain this tracker', tool_selection=selection)
    assert sent.value.code == 'tool_selection_too_large'
    assert sent.value.detail['advertised_count'] == 33
    assert not provider_calls
    assert svc.list_conversations(account['id']) == []
    assert not hub.db.query('SELECT 1 FROM assistant_messages')


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
