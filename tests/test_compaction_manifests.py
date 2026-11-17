"""Compaction of source-heavy conversations keeps recoverable, scoped history."""
import json

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import estimate, valid_groups
from canvas_hub.assistant.providers import ProviderError
from canvas_hub.assistant.read_results import read
from canvas_hub.boundary import HubAPI
from canvas_hub.util import NotFound, now_iso

from .test_assistant_context import mock_provider


def source_heavy_chat(hub, account):
    svc = AssistantService(hub, HubAPI(hub))
    svc.update_config({'preset': 'custom', 'base_url': 'http://localhost:9/v1', 'model': 'synthetic',
                       'context': {'capacity': 64000}})
    with hub.db.tx() as c:
        cid = c.execute('INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                        (account['id'], 'Source-heavy chat', now_iso(), now_iso())).lastrowid
    sources = [{'source_id': f'synthetic-source-{n:04}', 'version': f'version-{n:04}',
                'start_char': n * 100, 'end_char': n * 100 + 100} for n in range(300)]
    for n in range(5):
        text = 'Historical user constraint. ' * 350 if n < 3 else f'Recent question {n}'
        svc._append(cid, 'user', {'text': text, 'sent_text': text})
        if n == 0:
            calls = [
                {'id': 'read-many', 'name': 'search_context_sources', 'input': {'query': 'synthetic'}},
                {'id': 'made-note', 'name': 'create_note', 'input': {'title': 'Synthetic note'}},
                {'id': 'failed-write', 'name': 'update_note', 'input': {'note_id': 123}},
                {'id': 'unknown-dispatch', 'name': 'call_discovered_tool', 'input': {'name': 'mystery'}},
            ]
            results = [
                {'id': 'read-many', 'name': 'search_context_sources', 'content': json.dumps({'sources': sources})},
                {'id': 'made-note', 'name': 'create_note', 'content': '{"note_id":123}'},
                {'id': 'failed-write', 'name': 'update_note', 'is_error': True, 'content': '{"error":"conflict"}'},
                {'id': 'unknown-dispatch', 'name': 'call_discovered_tool', 'is_error': True,
                 'content': '{"error":"interrupted"}'},
            ]
            svc._append(cid, 'assistant', {'tool_calls': calls})
            svc._append(cid, 'tool', {'results': results})
        if n == 3:
            svc._append(cid, 'assistant', {'tool_calls': [{'id': 'later-read', 'name': 'read_context_source',
                                                         'input': {'source_id': 'later-source', 'version': 'later-v1'}}]})
            svc._append(cid, 'tool', {'results': [{'id': 'later-read', 'name': 'read_context_source',
                                                   'content': '{"source_id":"later-source","version":"later-v1"}'}]})
        svc._append(cid, 'assistant', {'text': 'Acknowledged', 'tool_calls': []})
    return svc, cid, sources


def manifest_pages(ctx, read_id, path):
    items, offset = [], 0
    while offset is not None:
        text, failed, _ = run_tool_display(ctx, 'read_tool_result',
                                          {'read_id': read_id, 'path': path, 'offset': offset, 'limit': 20}, {'read'})
        assert not failed, text
        assert len(text.encode()) <= 4800
        page = json.loads(text)
        items.extend(page['entries'].values())
        offset = page['next_offset']
    return items


def test_large_manifest_compacts_pages_and_survives_recompaction(hub, account, monkeypatch):
    svc, cid, sources = source_heavy_chat(hub, account)
    before = svc._transcript(cid)
    calls, _ = mock_provider(monkeypatch)
    svc.compact(account['id'], cid)
    svc.wait(cid, 5)
    assert svc.get_conversation(account['id'], cid)['conversation']['run_state'] == 'idle'
    assert len(calls) == 1 and calls[0][2] == []
    assert svc._transcript(cid) == before
    projected = svc._context_projection(cid, account['id'], before)
    assert valid_groups(projected) and projected[1:] == before[-6:]
    assert 'lists below are incomplete' in projected[0]['sent_text']
    saved = json.loads(hub.db.one('SELECT summary_json FROM assistant_context WHERE conversation_id=?', (cid,))['summary_json'])
    archive = saved['manifest_archive']
    assert archive['reference_count'] > len(saved['reference_manifest'])
    assert estimate(saved['reference_manifest']) + estimate(saved['tool_execution_manifest']) < 2000
    assert {r['call_id']: r['outcome'] for r in saved['tool_execution_manifest'] if r['call_id'] != 'read-many'} == {
        'made-note': 'completed', 'failed-write': 'error', 'unknown-dispatch': 'unknown'}

    ctx = ToolContext(HubAPI(hub), account_id=account['id'], conversation_id=cid)
    first_id = archive['read_id']
    anchors = manifest_pages(ctx, first_id, archive['reference_path'])
    assert [r for r in anchors if r.get('source_id', '').startswith('synthetic-source-')] == sources
    assert len(manifest_pages(ctx, first_id, archive['execution_path'])) == 4
    denied, failed, _ = run_tool_display(ctx, 'read_tool_result', {'read_id': first_id}, set())
    assert failed and json.loads(denied)['error'] == 'unknown_or_disabled_tool'
    for other in (ToolContext(HubAPI(hub), account_id=account['id'] + 1, conversation_id=cid),
                  ToolContext(HubAPI(hub), account_id=account['id'], conversation_id=cid + 1)):
        with pytest.raises(NotFound):
            read(other, {'read_id': first_id})

    # A second compaction must inherit the *full* archive, not only its excerpt.
    svc._append(cid, 'user', {'text': 'A later question', 'sent_text': 'A later question'})
    svc._append(cid, 'assistant', {'text': 'Later answer', 'tool_calls': []})
    svc.compact(account['id'], cid)
    svc.wait(cid, 5)
    assert svc.get_conversation(account['id'], cid)['conversation']['run_state'] == 'idle'
    newer = json.loads(hub.db.one('SELECT summary_json FROM assistant_context WHERE conversation_id=?', (cid,))['summary_json'])
    newer_id = newer['manifest_archive']['read_id']
    assert newer_id != first_id
    hub.reopen()
    all_anchors = manifest_pages(ctx, newer_id, '/reference_manifest')
    assert [r for r in all_anchors if r.get('source_id', '').startswith('synthetic-source-')] == sources
    assert any(r.get('source_id') == 'later-source' for r in all_anchors)
    assert len(manifest_pages(ctx, first_id, '/reference_manifest')) == len(anchors)
    assert len(manifest_pages(ctx, newer_id, '/tool_execution_manifest')) == 5
    with hub.db.tx() as c:
        c.execute('DELETE FROM assistant_conversations WHERE id=?', (cid,))
    assert not hub.db.one('SELECT 1 FROM assistant_context_snapshots WHERE conversation_id=?', (cid,))
    with pytest.raises(NotFound):
        read(ctx, {'read_id': first_id})


def test_failed_large_compaction_does_not_install_snapshot(hub, account, monkeypatch):
    svc, cid, _ = source_heavy_chat(hub, account)
    before = svc._transcript(cid)
    calls, _ = mock_provider(monkeypatch, summary='invalid JSON')
    svc.compact(account['id'], cid)
    svc.wait(cid, 5)
    assert len(calls) == 1 and svc._transcript(cid) == before
    assert not hub.db.one('SELECT 1 FROM assistant_context WHERE conversation_id=?', (cid,))
    assert not hub.db.one('SELECT 1 FROM assistant_context_snapshots WHERE conversation_id=?', (cid,))


def test_automatic_compaction_continues_the_source_heavy_chat(hub, account, monkeypatch):
    svc, cid, _ = source_heavy_chat(hub, account)
    svc.update_config({'context': {'capacity': 64000, 'history_tokens': 4500, 'auto_compact': True}})
    calls, _ = mock_provider(monkeypatch)
    svc.send(account['id'], 'Continue the work', cid)
    svc.wait(cid, 5)
    result = svc.get_conversation(account['id'], cid)
    assert result['conversation']['run_state'] == 'idle', result['conversation']['run_error']
    assert result['context']['summary_requests'] == 1
    assert len(calls) == 2 and calls[0][2] == []
    assert calls[1][1][0]['context_summary']
    assert 'manifest_archive' in calls[1][1][0]['sent_text']
    assert result['messages'][-1]['text'] == 'Final answer'


@pytest.mark.parametrize('damage', ['missing', 'modified'])
def test_recompaction_fails_closed_if_previous_archive_is_missing(hub, account, monkeypatch, damage):
    svc, cid, _ = source_heavy_chat(hub, account)
    calls, _ = mock_provider(monkeypatch)
    svc.compact(account['id'], cid)
    svc.wait(cid, 5)
    checkpoint = dict(hub.db.one('SELECT * FROM assistant_context WHERE conversation_id=?', (cid,)))
    archive = json.loads(checkpoint['summary_json'])['manifest_archive']
    with hub.db.tx() as c:
        if damage == 'missing':
            c.execute('DELETE FROM assistant_context_snapshots WHERE conversation_id=?', (cid,))
        else:
            c.execute("UPDATE assistant_context_snapshots SET snapshot_json='{}' WHERE conversation_id=?", (cid,))
    ctx = ToolContext(HubAPI(hub), account_id=account['id'], conversation_id=cid)
    with pytest.raises(NotFound):
        read(ctx, {'read_id': archive['read_id']})
    svc._append(cid, 'user', {'text': 'Later question', 'sent_text': 'Later question'})
    before = svc._transcript(cid)
    calls.clear()
    svc.compact(account['id'], cid)
    svc.wait(cid, 5)
    result = svc.get_conversation(account['id'], cid)
    assert 'earlier compaction manifest is unavailable' in result['conversation']['run_error']
    assert not calls and svc._transcript(cid) == before
    assert dict(hub.db.one('SELECT * FROM assistant_context WHERE conversation_id=?', (cid,))) == checkpoint


def test_too_many_action_outcomes_still_fail_before_dispatch(hub, account):
    svc, cid, _ = source_heavy_chat(hub, account)
    # Add a large action group to the oldest part, preserving complete call/result pairs.
    transcript = svc._transcript(cid)
    calls = [{'id': f'action-{n}', 'name': 'create_note', 'input': {'title': f'Note {n}'}} for n in range(200)]
    results = [{'id': c['id'], 'name': 'create_note', 'content': '{}'} for c in calls]
    transcript[1:1] = [{'role': 'assistant', 'tool_calls': calls, 'seq': transcript[0]['seq']},
                       {'role': 'tool', 'results': results, 'seq': transcript[0]['seq']}]
    cfg = {'provider': 'openai_compatible', 'context': {'capacity': 64000}}
    def unexpected_provider(_cfg):
        pytest.fail('No summary request should be sent')
    with pytest.raises(ProviderError, match='Too many action outcomes'):
        svc._compact_context(cid, account['id'], cfg, transcript, unexpected_provider)
    assert not hub.db.one('SELECT 1 FROM assistant_context WHERE conversation_id=?', (cid,))
    assert not hub.db.one('SELECT 1 FROM assistant_context_snapshots WHERE conversation_id=?', (cid,))
