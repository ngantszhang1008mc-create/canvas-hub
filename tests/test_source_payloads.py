"""Source-only token reductions, with real local indexing and fake providers."""
from copy import deepcopy
import json

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import valid_groups
from canvas_hub.assistant.providers import ProviderError
from canvas_hub.assistant.source_context import followup_source_context, with_source_context
from canvas_hub.services.context_sources import ContextSources
from canvas_hub.util import Conflict, now_iso
from .test_workspace import workspace, prepare, start


def attachment(w, body):
    hub, aid, iid = w['hub'], w['aid'], w['iid']
    rid = hub.resources.store_bytes(body.encode(), 'text/plain')
    with hub.db.tx() as c:
        c.execute('INSERT INTO item_resources(item_id,resource_id,filename,revision,observed_at) VALUES (?,?,?,1,?)',
                  (iid, rid, 'rules.txt', now_iso()))
    hub.resources.extract(rid, 'rules.txt')
    with hub.db.tx() as c:
        hub.search.index_resource(c, aid, iid, rid, 'Rules', body)
    return rid


def temporary(transcript):
    user = next(m for m in reversed(transcript) if m['role'] == 'user')
    return json.loads(user['sent_text'].split('Workspace source context (this request only):\n', 1)[1])


def tool_result(payload, *, name='read_context_source', error=False):
    return {'role': 'tool', 'results': [{'name': name, 'content': json.dumps(payload), 'is_error': error}]}


def test_small_source_once_per_request_history_unchanged_and_deliberate_reread_allowed(workspace):
    w = workspace
    body = 'UNIQUE_COMPLETE_RULE: Include three citations and the signed appendix.\n' * 12
    rid = attachment(w, body)
    prepared = prepare(w)
    src = next(s for s in prepared['context']['request_context']['sources'] if s['resource_id'] == rid)
    w['fake'].calls = [('read_context_source', {'source_id': src['source_id'], 'version': src['version']})]
    cid = prepared['conversation_id']
    # This old message is user history, even if it contains a large source.
    w['assistant']._append(cid, 'user', {'text': 'old user source ' * 40, 'sent_text': 'old user source ' * 40})
    old = w['assistant']._transcript(cid)
    result = start(w)
    w['assistant'].wait(result['conversation_id'], 5)
    assert len(w['fake'].requests) == 2
    assert w['assistant']._transcript(cid)[:len(old)] == old
    saved = w['assistant']._transcript(cid)[len(old)]
    assert 'UNIQUE_COMPLETE_RULE' not in json.dumps(saved)
    assert saved['source_context']['sources'] and src['version'] in json.dumps(saved)
    for request in w['fake'].requests:
        assert valid_groups(request['transcript'])
        assert json.dumps(request['transcript']).count('UNIQUE_COMPLETE_RULE') == 12
    last = temporary(w['fake'].requests[-1]['transcript'])
    supplied = next(s for s in last['sources'] if s['resource_id'] == rid)
    assert supplied['complete_read'] and supplied['ranges_in_history'] == [[0, len(body)]]
    assert supplied['supplied_ranges'] == []
    # Codex uses the fixed native dispatcher for newly discovered tools. Its
    # actual execution name must preserve the same source coverage/deduplication.
    dispatched = deepcopy(w['assistant']._transcript(cid))
    for message in dispatched:
        for result in message.get('results', []):
            if result.get('name') == 'read_context_source':
                result.update(name='call_discovered_tool', executed_tool='read_context_source')
    bridged = temporary(with_source_context(dispatched, prepared['context']['request_context']))
    observed = next(s for s in bridged['sources'] if s['resource_id'] == rid)
    assert observed['complete_read'] and observed['supplied_ranges'] == []
    # A later user request never replays body text in all previous user messages.
    result = start(w)
    w['assistant'].wait(cid, 5)
    assert json.dumps(w['fake'].requests[-1]['transcript']).count('UNIQUE_COMPLETE_RULE') == 12
    assert w['assistant']._conv(w['aid'], cid)['run_state'] == 'idle'


def test_large_source_full_traversal_search_stale_ranges_and_account_isolation(workspace):
    w = workspace
    body = ('General study content.\n' * 1300) + 'TAIL_EXCEPTION: Passing requires the appendix, even above 50 points.'
    rid = attachment(w, body)
    context = prepare(w)['context']['request_context']
    src = next(s for s in context['sources'] if s['resource_id'] == rid)
    assert 'text' not in src and src['total_chars'] == len(body)
    index = ContextSources(w['hub'].db)
    history = [{'role': 'user', 'text': 'Interpret every rule.'}]
    hit = index.search(w['aid'], 'TAIL_EXCEPTION', source_id=src['source_id'])
    history.append(tool_result(hit, name='search_context_sources'))
    projected = temporary(with_source_context(history, context))
    observed = next(s for s in projected['sources'] if s['resource_id'] == rid)
    assert observed['available_ranges'] and not observed['complete_read']
    assert 'TAIL_EXCEPTION' in json.dumps(history)
    pieces, offset = [], 0
    while offset is not None:
        result = index.read(w['aid'], src['source_id'], version=src['version'], offset=offset, limit=8000)
        pieces.append(result['text'])
        history.append(tool_result(result))
        offset = result['next_offset']
    assert ''.join(pieces) == body
    assert next(s for s in temporary(with_source_context(history, context))['sources'] if s['resource_id'] == rid)['complete_read']
    # Range bookkeeping never prevents a deliberate identical read.
    assert index.read(w['aid'], src['source_id'], version=src['version']) == index.read(w['aid'], src['source_id'], version=src['version'])
    changed = body + '\nREVISED_EXCEPTION: The appendix is now optional.'
    with w['hub'].db.tx() as c:
        w['hub'].search.index_resource(c, w['aid'], w['iid'], rid, 'Rules', changed)
    fresh = prepare(w)['context']['request_context']
    latest = next(s for s in temporary(with_source_context(history, fresh))['sources'] if s['resource_id'] == rid)
    assert latest['version'] != src['version'] and latest['available_ranges'] == [] and not latest['complete_read']
    with pytest.raises(Conflict):
        index.read(w['aid'], src['source_id'], version=src['version'])
    account = w['hub'].identity.get_account(w['aid'])
    other = w['hub'].identity.upsert_verified_account(account['instance_id'], {'canvas_user_id': 'source-other'})['id']
    assert index.catalog(other)['sources'] == []


def test_failed_or_incomplete_tool_output_cannot_establish_read_coverage(workspace):
    w = workspace
    rid = attachment(w, 'Gap safety.\n' * 900)
    context = prepare(w)['context']['request_context']
    src = next(s for s in context['sources'] if s['resource_id'] == rid)
    read = ContextSources(w['hub'].db).read(w['aid'], src['source_id'], version=src['version'])
    for result in (tool_result(read, error=True), tool_result({**read, 'text': read['text'][:2]}),
                   tool_result({**read, 'version': 'older-version'})):
        history = [{'role': 'user', 'text': 'Check all rules.'}, result]
        projected = next(s for s in temporary(with_source_context(history, context))['sources'] if s['resource_id'] == rid)
        assert projected['available_ranges'] == [] and not projected['complete_read']


@pytest.mark.parametrize('changed', [False, True])
def test_restart_retry_restores_only_the_approved_source_version(workspace, monkeypatch, changed):
    w = workspace
    body = 'RETRY_ORIGINAL_RULE: include the appendix.'
    rid = attachment(w, body)
    class Failed:
        def complete(self, *args, **kwargs):
            raise ProviderError('Synthetic interruption')
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda cfg: Failed())
    cid = start(w)['conversation_id']
    w['assistant'].wait(cid, 5)
    original = w['assistant']._transcript(cid)
    assert body not in json.dumps(original)
    if changed:
        with w['hub'].db.tx() as c:
            w['hub'].search.index_resource(c, w['aid'], w['iid'], rid, 'Rules', 'UNAPPROVED_NEW_RULE')
    restored = AssistantService(w['hub'], w['api'])
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda cfg: w['fake'])
    restored.retry(w['aid'], cid)
    restored.wait(cid, 5)
    assert restored._conv(w['aid'], cid)['run_state'] == 'idle'
    assert restored._transcript(cid)[:len(original)] == original
    payload = json.dumps(w['fake'].requests[-1]['transcript'])
    assert ('RETRY_ORIGINAL_RULE' in payload) is not changed
    assert 'UNAPPROVED_NEW_RULE' not in payload
    if changed:
        assert 'changed or is unavailable' in payload


def test_scoped_followup_keeps_references_and_selectively_restores_exact_sources(workspace):
    w = workspace
    bodies = ['FOLLOWUP_RULE_ALPHA: keep the appendix.', 'FOLLOWUP_RULE_BETA: keep the citations.']
    ids = [attachment(w, body) for body in bodies]
    cid = start(w)['conversation_id']
    w['assistant'].wait(cid, 5)
    before = w['assistant']._transcript(cid)
    sources = before[0]['source_context']['sources']
    chosen = next(s for s in sources if s['resource_id'] == ids[1])
    names = {t['name'] for t in w['fake'].requests[0]['tools']}
    assert {'read_context_source', 'list_components', 'show_checklist', 'get_component_context'} <= names
    assert 'get_component_context' in w['fake'].requests[0]['system'] and 'exact expected_version and state_token' in w['fake'].requests[0]['system']
    w['assistant'].send(w['aid'], 'Check only ' + chosen['source_id'], cid)
    w['assistant'].wait(cid, 5)
    payload = temporary(w['fake'].requests[-1]['transcript'])
    assert {s['source_id'] for s in payload['sources']} == {s['source_id'] for s in sources}
    assert sum(bool(s['supplied_ranges']) for s in payload['sources']) == 1
    assert bodies[1] in json.dumps(payload) and bodies[0] not in json.dumps(payload)
    assert all(body not in json.dumps(w['assistant']._transcript(cid)) for body in bodies)
    assert w['assistant']._transcript(cid)[:len(before)] == before
    with w['hub'].db.tx() as c:
        w['hub'].search.index_resource(c, w['aid'], w['iid'], ids[1], 'Rules', 'CHANGED_RULE_NEVER_RESTORED_SILENTLY')
    w['assistant'].send(w['aid'], 'Check only ' + chosen['source_id'], cid)
    w['assistant'].wait(cid, 5)
    payload = temporary(w['fake'].requests[-1]['transcript'])
    assert 'CHANGED_RULE_NEVER_RESTORED_SILENTLY' not in json.dumps(payload)
    assert any('changed or is unavailable' in warning for warning in payload['warnings'])
    other = w['assistant'].send(w['aid'], 'General question')['conversation_id']
    w['assistant'].wait(other, 5)
    assert 'Workspace source context (this request only)' not in json.dumps(w['fake'].requests[-1]['transcript'])


def test_followup_matches_complete_ids_and_never_substitutes_small_text_for_large_target(workspace):
    w = workspace
    bodies = {10001: 'SHORT_ID_RULE', 100010: 'LONGER_ID_RULE', 1000100: 'LARGE_TARGET_RULE\n' * 400}
    with w['hub'].db.tx() as c:
        for iid, body in bodies.items():
            c.execute("INSERT INTO items(id,account_id,item_type,source_key,course_canvas_id,title,first_observed_at,last_observed_at) VALUES (?,?,'page',?,?,?, ?,?)",
                      (iid, w['aid'], str(iid), w['course'], 'Synthetic rule ' + str(iid), now_iso(), now_iso()))
            w['hub'].search.index_item(c, w['aid'], iid, 'page', 'Synthetic rule ' + str(iid), {'body': body})
    scope = {'course_id': w['course'], 'item_id': None}
    sources = [s for s in ContextSources(w['hub'].db).catalog(w['aid'], course_id=w['course'])['sources'] if s['item_id'] in bodies]
    for source in sources:
        source['inline'] = source['total_chars'] <= 4000
    manifest = {'scope': scope, 'sources': sources, 'warnings': []}
    original = {'role': 'user', 'text': 'Read these rules.', 'source_context': manifest}
    for target, expected in [('item:100010', 'LONGER_ID_RULE'), ('item:1000100', None)]:
        transcript = [original, {'role': 'user', 'text': 'Only read `' + target + '`.'}]
        context = followup_source_context(w['hub'], w['aid'], transcript, scope)
        supplied = [s.get('text') for s in context['sources'] if s.get('text')]
        assert len(supplied) == (1 if expected else 0)
        if expected:
            assert expected in supplied[0] and 'SHORT_ID_RULE' not in supplied[0]
        assert len(context['sources']) == 3
        assert followup_source_context(w['hub'], w['aid'], transcript, {'course_id': 'other', 'item_id': None}) is None
    assert all('text' not in s for s in manifest['sources'])


def test_shared_attachment_is_sent_once_with_both_origins(workspace):
    w = workspace
    body = 'ONE_SHARED_POLICY: mandatory lab attendance.'
    rid = attachment(w, body)
    with w['hub'].db.tx() as c:
        other = c.execute("INSERT INTO items(account_id,item_type,source_key,course_canvas_id,title,first_observed_at,last_observed_at) VALUES (?,'file','shared-policy',?,'Shared policy',?,?)",
                          (w['aid'], w['course'], now_iso(), now_iso())).lastrowid
        c.execute('INSERT INTO item_resources(item_id,resource_id,filename,revision,observed_at) VALUES (?,?,?,1,?)',
                  (other, rid, 'rules.txt', now_iso()))
        c.execute("INSERT INTO item_links(account_id,from_item_id,to_item_id,relation,first_observed_at,last_observed_at) VALUES (?,?,?,'attachment',?,?)",
                  (w['aid'], w['iid'], other, now_iso(), now_iso()))
        w['hub'].search.index_resource(c, w['aid'], other, rid, 'Shared policy', body)
    context = prepare(w)['context']['request_context']
    assert json.dumps(context).count('ONE_SHARED_POLICY') == 1
    source = next(s for s in context['sources'] if s['resource_id'] == rid)
    assert source['also_linked_from'] == [other]
    assert {i['item_id'] for i in context['items']} == {w['iid'], other}


def test_manifest_overflow_is_explicit_and_all_sources_remain_readable(workspace):
    w = workspace
    for i in range(23):
        attachment(w, f'Synthetic document {i}. Each has a different complete rule.')
    context = prepare(w)['context']['request_context']
    assert len(context['sources']) == 20
    assert 'more sources to catalog' in ' '.join(context['warnings'])
    index = ContextSources(w['hub'].db)
    all_sources, offset = [], 0
    while offset is not None:
        page = index.catalog(w['aid'], item_id=w['iid'], offset=offset)
        all_sources += page['sources']
        offset = page['next_offset']
    assert len(all_sources) == 24
    assert all(index.read(w['aid'], s['source_id'], version=s['version'])['coverage']['complete_read'] for s in all_sources)


def test_legacy_syllabus_field_is_fully_readable_beyond_the_old_prefix(workspace):
    w = workspace
    body = '<p>Introductory material.</p>' * 1200 + '<p>LEGACY_TAIL_RULE: The oral exam is mandatory.</p>'
    # Simulate an older indexed item with a syllabus_body field stored as JSON,
    # which was omitted by the old searchable-field definition.
    with w['hub'].db.tx() as c:
        c.execute("INSERT INTO item_fields(item_id,field,value_json,compare_hash,first_observed_at,last_observed_at,last_changed_at) VALUES (?,'syllabus_body',?,'fixture',?,?,?)",
                  (w['iid'], json.dumps(body), now_iso(), now_iso(), now_iso()))
    prepared = prepare(w)['context']['request_context']
    src = next(s for s in prepared['sources'] if s['source_id'] == f"item:{w['iid']}")
    assert not src['inline'] and src['total_chars'] > 6000
    index = ContextSources(w['hub'].db)
    ending = index.read(w['aid'], src['source_id'], version=src['version'], offset=src['total_chars']-100, limit=100)
    assert 'LEGACY_TAIL_RULE' in ending['text'] and ending['next_offset'] is None
