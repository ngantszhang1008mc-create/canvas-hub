"""Focused byte-packing, source discovery and selection ergonomics regressions."""
import json

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.assistant.read_results import READ_BYTES, bound, read, wire_bytes
from canvas_hub.assistant.tool_catalog import ToolSession
from canvas_hub.boundary import HubAPI
from canvas_hub.services.context_sources import ContextSources
from canvas_hub.util import InvalidInput, now_iso
from .test_bounded_tool_reads import scenario  # synthetic local sources only


@pytest.fixture
def ctx(hub, account):
    with hub.db.tx() as c:
        cid = c.execute('INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                        (account['id'], 'Read ergonomics', now_iso(), now_iso())).lastrowid
    return ToolContext(HubAPI(hub), account_id=account['id'], conversation_id=cid, read_results={})


@pytest.mark.parametrize('text', ['a' * 8677, '規則"/\\\n😅' * 1500])
def test_text_pages_fill_final_envelope_without_splitting_unicode(ctx, text):
    reply = bound(ctx, 'get_item', {'body': text})
    offset, pieces = 0, []
    while True:
        page = read(ctx, {'read_id': reply['read_id'], 'path': '/body', 'offset': offset, 'chars': 4000})
        assert wire_bytes(page) <= READ_BYTES
        assert page['text'] == text[offset:page['end_offset']]
        pieces.append(page['text'])
        end = page['end_offset']
        if end < min(len(text), offset + 4000):
            extra = {**page, 'text': text[offset:end+1], 'end_offset': end + 1,
                     'next_offset': end + 1 if end + 1 < len(text) else None,
                     'complete': offset == 0 and end + 1 == len(text)}
            assert wire_bytes(extra) > READ_BYTES  # maximal prefix, not repeated halving
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert ''.join(pieces) == text
    if text.isascii():
        assert [len(piece) for piece in pieces] == [4000, 4000, 677]
        assert len(read(ctx, {'read_id': reply['read_id'], 'path': '/body'})['text']) == 4000
    with pytest.raises(InvalidInput, match='past the end'):
        read(ctx, {'read_id': reply['read_id'], 'path': '/body', 'offset': len(text) + 1})


def test_named_paths_keep_legacy_ordinals_and_real_item_identity(ctx):
    original = {'fields': {'body': {'value': 'x' * 9000}},
                'deadlines': [{'item_id': 655, 'title': 'The actual item', 'source_url': 'https://example.test/items/655',
                               'details': 'Long source data. ' * 500}]}
    reply = bound(ctx, 'get_item', original)
    fields = read(ctx, {'read_id': reply['read_id'], 'path': '/fields'})
    assert fields['entries']['body']['fields']['value']['read_path'] == '/fields/body/value'
    legacy = read(ctx, {'read_id': reply['read_id'], 'path': '@/1/0/0', 'chars': 10})
    assert legacy['path'] == '/fields/body/value' and legacy['text'] == 'x' * 10
    indexed_row = reply['index']['fields']['deadlines']['entries'][0]
    assert indexed_row['fields']['item_id'] == 655
    assert indexed_row['fields']['source_url'] == original['deadlines'][0]['source_url']
    with pytest.raises(InvalidInput, match='numeric object ordinals require @/'):
        read(ctx, {'read_id': reply['read_id'], 'path': '/1'})


def test_object_pages_use_final_envelope_budget(ctx):
    rows = [{'item_id': 700 + i, 'text': 'x' * 750} for i in range(9)]
    reply = bound(ctx, 'get_deadlines', {'deadlines': rows})
    page = read(ctx, {'read_id': reply['read_id'], 'path': '/deadlines', 'limit': 20})
    assert wire_bytes(page) <= READ_BYTES
    assert page['end_offset'] >= 5  # the old half-budget fit only two rows
    assert list(page['entries'].values()) == rows[:page['end_offset']]
    remaining = read(ctx, {'read_id': reply['read_id'], 'path': '/deadlines', 'offset': page['next_offset'], 'limit': 20})
    assert list(page['entries'].values()) + list(remaining['entries'].values()) == rows
    assert remaining['next_offset'] is None and wire_bytes(remaining) <= READ_BYTES


def test_catalog_pages_are_direct_with_oversized_row_fallback(ctx, hub, scenario):
    aid, _, _, _ = scenario
    service = ContextSources(hub.db)
    while service.coverage(aid, course_id='4242')['unindexed_sources']:
        service.catalog(aid, course_id='4242')
    expected = service.catalog(aid, course_id='4242', limit=20)['sources']

    def invoke(offset):
        raw, error, _ = run_tool_display(ctx, 'list_context_sources',
            {'course_id': '4242', 'include_preview': True, 'offset': offset, 'limit': 20}, {'read'})
        assert not error, raw
        assert len(raw.encode()) <= READ_BYTES
        return json.loads(raw)

    offset, found = 0, []
    while True:
        page = invoke(offset)
        assert 'read_id' not in page
        found.extend(source['source_id'] for source in page['sources'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert len(found) == service.coverage(aid, course_id='4242')['indexed_sources']
    assert len(found) == len(set(found))
    assert found[:len(expected)] == [source['source_id'] for source in expected]

    huge_url = 'https://example.test/' + '規則' * 4000
    with hub.db.tx() as c:
        c.execute('UPDATE items SET source_url=? WHERE id=?', (huge_url, expected[0]['item_id']))
    snapshot = invoke(0)
    assert snapshot['read_id']
    saved = ctx.read_results[snapshot['read_id']]['result']
    assert saved['sources'][0]['source_url'] == huge_url and saved['next_offset'] == 1
    text, offset = '', 0
    while True:
        page = read(ctx, {'read_id': snapshot['read_id'], 'path': '/sources/0/source_url', 'offset': offset})
        assert wire_bytes(page) <= READ_BYTES
        text += page['text']
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert text == huge_url


def test_discovery_reports_group_fit_and_actionable_atomic_overflow():
    session = ToolSession({'read', 'ui', 'notes', 'collect', 'deadlines'}, ['group:tracker'])
    before = session.advertised()
    listing = json.loads(session.discover({}))
    groups = {g['id']: g for g in listing['groups']}
    assert groups['documents']['tool_count'] == len(next(g for g in session.catalog.groups if g['id'] == 'documents')['tools'])
    assert groups['documents']['fit_if_added']['fits']
    assert not groups['courses']['fit_if_added']['fits']
    with pytest.raises(InvalidInput) as failure:
        session.discover({'select': ['group:documents', 'group:courses']})
    detail = failure.value.detail
    assert failure.value.code == 'tool_selection_too_large'
    assert detail['over_by'] == detail['advertised_count'] - detail['max_advertised'] > 0
    assert detail['manual_selection'] == ['group:tracker']
    assert detail['current_fit']['fits'] and detail['manual_fit']['remaining_slots'] > 0
    assert 'tool:read_context_source' in detail['individual_choices']
    assert session.advertised() == before
    session.discover({'select': ['tool:read_context_source', 'tool:list_context_sources']})
    assert session.catalog.selection_fit(session.manual + session.discovered)['fits']
