"""Proposal receipts stay bounded and paging never executes the mutation again."""
import copy
import json

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.read_results import READ_BYTES
from canvas_hub.boundary import HubAPI
from canvas_hub.util import now_iso


def setup(hub, account, rows):
    aid = account['id']
    spec = {'title': 'Synthetic rebuild', 'buckets': [
        {'id': ('old_' + str(i).zfill(4)).ljust(64, 'x'), 'label': 'Row ' + str(i), 'max': 1}
        for i in range(rows)]}
    view = hub.components.create(aid, 'score_model', spec)
    view = hub.components.set_state(aid, view['id'], {
        'include': {view['spec']['buckets'][0]['id']: False},
        'values': {view['spec']['buckets'][-1]['id']: 0}})
    with hub.db.tx() as c:
        cid = c.execute('INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                        (aid, 'Synthetic rebuild receipt', now_iso(), now_iso())).lastrowid
    api = HubAPI(hub)
    ctx = ToolContext(api, account_id=aid, conversation_id=cid, read_results={})
    proposed = copy.deepcopy(view['spec'])
    mapping = {}
    for row in proposed['buckets']:
        old = row['id']
        row['id'] = 'new_' + old[4:]
        mapping[old] = row['id']
    args = {'component_id': view['id'], 'expected_version': view['version'],
            'state_token': hub.components.state_token(view), 'spec': proposed,
            'identity_map': {'rows': mapping}}
    return ctx, view, args


@pytest.mark.parametrize('change_rules', [False, True])
def test_large_200_row_rebuild_receipt_is_durable_and_reads_never_replay(hub, account, monkeypatch, change_rules):
    ctx, before, args = setup(hub, account, 200)
    if change_rules:
        for row in args['spec']['buckets']:
            row['max'] = 2
    real = hub.components.propose_rebuild
    calls = []
    def counted(*a, **kw):
        calls.append(True)
        return real(*a, **kw)
    monkeypatch.setattr(hub.components, 'propose_rebuild', counted)
    raw, error, display = run_tool_display(ctx, 'propose_component_rebuild', args, {'read', 'ui'})
    assert not error, raw
    reply = json.loads(raw)
    assert len(raw.encode()) <= READ_BYTES and 'truncated' not in raw
    assert reply['pending_approval'] and reply['component_id'] == before['id']
    assert reply['proposal_id'] == f"component:{before['id']}:version:{before['version'] + 1}"
    assert reply['migration'] == {'can_accept': not change_rules, 'preserved_fields': 2,
                                  'mapped_counts': {'rows': 200, 'groups': 0, 'scenarios': 0},
                                  'unresolved_count': 200 if change_rules else 0}
    assert 'never call propose_component_rebuild again' in reply['note']
    snapshot = ctx.read_results[reply['read_id']]['result']
    assert len(json.dumps(snapshot)) > 24000
    assert display['view']['pending_spec']['migration']['identity_map']['rows'] == args['identity_map']['rows']
    service = AssistantService(hub, ctx.api)
    service._append(ctx.conversation_id, 'tool', {'results': [{'id': 'proposal-once',
        'name': 'propose_component_rebuild', 'content': raw, 'display': display}], 'read_results': ctx.read_results})
    staged = hub.components.get(account['id'], before['id'])
    assert staged['state'] == before['state'] and staged['spec'] == before['spec']
    assert staged['pinned'] == before['pinned']
    count = hub.db.one('SELECT count(*) AS n FROM component_versions')['n']
    hub.reopen()
    ctx.read_results = {}
    for path, expected in [('/migration/identity_map/rows', args['identity_map']['rows']),
                           ('/migration/unresolved', snapshot['migration']['unresolved'])]:
        offset, entries = 0, {}
        while True:
            raw, error, _ = run_tool_display(ctx, 'read_tool_result',
                {'read_id': reply['read_id'], 'path': path, 'offset': offset, 'limit': 20}, {'read'})
            assert not error, raw
            page = json.loads(raw)
            assert page['snapshot_only'] and len(raw.encode()) <= READ_BYTES
            entries.update(page['entries'])
            if page['next_offset'] is None:
                break
            assert page['next_offset'] > offset
            offset = page['next_offset']
        assert (list(entries.values()) if isinstance(expected, list) else entries) == expected
    assert len(calls) == 1
    assert hub.db.one('SELECT count(*) AS n FROM component_versions')['n'] == count
    assert hub.components.get(account['id'], before['id']) == staged


def test_small_rebuild_receipt_remains_direct_with_visible_identity_and_counts(hub, account):
    ctx, before, args = setup(hub, account, 2)
    raw, error, _ = run_tool_display(ctx, 'propose_component_rebuild', args, {'read', 'ui'})
    assert not error, raw
    reply = json.loads(raw)
    assert 'read_id' not in reply and ctx.read_results == {} and len(raw.encode()) <= READ_BYTES
    assert reply['pending_approval'] and reply['component_id'] == before['id']
    assert reply['proposal_id'] == f"component:{before['id']}:version:{before['version'] + 1}"
    assert reply['migration']['identity_map']['rows'] == args['identity_map']['rows']
    assert reply['migration']['mapped_counts'] == {'rows': 2, 'groups': 0, 'scenarios': 0}
    assert reply['migration']['unresolved_count'] == 0 and reply['migration']['preserved_fields'] == 2
