"""Source-only token reductions, with real local indexing and fake providers."""
from copy import deepcopy
import json

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import valid_groups
from canvas_hub.assistant.providers import ProviderError, Turn
from canvas_hub.assistant.source_context import with_source_context
from canvas_hub.services.context_sources import ContextSources
from canvas_hub.services.external_reader import PublicReader, Read
from canvas_hub.util import Conflict, now_iso
from .test_workspace import workspace, prepare, start
from .test_source_analysis import builtin, finish, start as start_analysis, RESULT
from .test_external_sources import source, URL, SPEC, HTML
from .test_connected_analysis import Connections
from .test_allowance_gateway import gateway, setup as gateway_setup
from canvas_hub.boundary import HubAPI
from canvas_hub.services.source_analysis import SourceAnalysis


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


def test_public_classification_is_bounded_preserves_full_local_content_and_checks_dom(builtin, source, account, monkeypatch):
    service, api, calls = builtin
    svc, sid, reads = source
    body = b'<html><title>Actual course title</title><main>' + b'Lecture introduction. '*5000 + HTML + b'<h2>FINAL EXCEPTION</h2><a href="/policy">Policy</a></main></html>'
    svc.reader = PublicReader(lambda url: (reads.append(url) or Read(url, 200, 'text/html', body)))
    class Provider:
        def complete(self, *args):
            calls.append(args)
            return Turn(json.dumps({**RESULT, 'adapter_spec': SPEC}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Provider())
    start_analysis(service, account['id'], sid)
    finish(service)
    result = svc.get(account['id'], sid)
    assert result['request']['status'] == 'done' and len(calls) == 1 and reads == [URL]
    sent = json.loads(calls[0][1][0]['sent_text'])
    assert sent['title'] == 'Actual course title' and sent['dom_examples'] and sent['links']
    assert not sent['coverage']['complete_read'] and sent['coverage']['supplied_chars'] <= 4600
    full = json.loads(svc.db.one('SELECT evidence_json FROM external_reads WHERE source_id=?', (sid,))['evidence_json'])[0]['content']
    assert len(full) > 100000 and 'FINAL EXCEPTION' in full
    assert all(full[x['start_char']:x['end_char']] == x['text'] for x in sent['excerpts'])
    assert any('FINAL EXCEPTION' in x['text'] for x in sent['excerpts'])
    assert result['classification']['evidence_coverage'] == sent['coverage']
    assert len(calls[0][1][0]['sent_text']) < len(full) / 8
    # Valid syntax is insufficient: a selector absent from the fetched DOM fails.
    previous = deepcopy(result['classification'])
    bad = deepcopy(SPEC)
    bad['records']['selector'] = '.invented-card'
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',
                        lambda cfg: type('Bad', (), {'complete': lambda *a: Turn(json.dumps({**RESULT, 'adapter_spec': bad}))})())
    start_analysis(service, account['id'], sid, reanalyze=True)
    finish(service)
    assert svc.get(account['id'], sid)['classification'] == previous
    assert svc.get(account['id'], sid)['request']['status'] == 'declined'
    assert reads == [URL, URL]


def test_connected_classification_samples_only_reviewed_text_and_keeps_full_snapshot(builtin, source, account, monkeypatch):
    service, api, _ = builtin
    svc, sid, reads = source
    calls = []
    class Provider:
        def complete(self, *args):
            calls.append(args)
            return Turn(json.dumps({'roles': ['material'], 'confidence': .8, 'summary': 'Synthetic'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Provider())
    connection = Connections(account, 'Approved course lecture material. ' * 1500 + '\nFinal grading exception.')
    offer = service.connected_disclosure(account['id'], sid, connection)
    service.start(account['id'], sid, offer['token'], consent=True, connected=True)
    finish(service)
    assert len(calls) == 1 and not reads
    payload = json.loads(calls[0][1][0]['sent_text'])
    assert not any(k in payload for k in ('dom_examples', 'title', 'links'))
    assert '/2026' not in payload['url'] and not payload['coverage']['complete_read']
    assert all(offer['content'][r['start_char']:r['end_char']] == r['text'] for r in payload['excerpts'])
    stored = json.loads(svc.db.one('SELECT evidence_json FROM external_reads WHERE source_id=?', (sid,))['evidence_json'])[0]
    assert stored['content'] == offer['content'] and stored['access_mode'] == 'connected_local_reviewed'


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


def test_gateway_receives_request_sources_once_and_keeps_normalized_usage(source, account, hub, gateway):
    sources, sid, reads = source
    api = HubAPI(hub)
    assistant, _ = gateway_setup(api, gateway)
    iid = hub.db.one("SELECT id FROM items WHERE item_type='assignment' AND canvas_id='7001'")['id']
    body = 'GATEWAY_SOURCE_RULE: Include the complete appendix.'
    attachment({'hub': hub, 'aid': account['id'], 'iid': iid}, body)
    payload = {'action': 'breakdown', 'item_id': iid}
    prepared = hub.workspace.prepare(api, assistant, account['id'], payload)
    result = hub.workspace.start(api, assistant, account['id'], {
        **payload, 'disclosure_fingerprint': prepared['disclosure']['fingerprint'], 'acknowledge': True})
    assistant.wait(result['conversation_id'], 5)
    status = assistant.get_conversation(account['id'], result['conversation_id'])
    assert status['conversation']['run_state'] == 'idle', status
    assert len(gateway.requests) == 1
    assert json.dumps(gateway.requests[0]['body']).count('GATEWAY_SOURCE_RULE') == 1
    assert 'GATEWAY_SOURCE_RULE' not in json.dumps(assistant._transcript(result['conversation_id']))
    assert status['context']['total_usage']['input_tokens'] == 100
    assert status['context']['total_usage']['cache_read_input_tokens'] == 10
    # Classification still uses the same enrolled gateway, one private attempt,
    # and the existing source/provider consent before sending any evidence.
    service = SourceAnalysis(api)
    gateway.action = lambda request: gateway.events(request, json.dumps(RESULT))
    offer = service.disclosure(account['id'], sid)
    assert offer['provider'] == 'allowance_gateway' and len(gateway.requests) == 1
    service.start(account['id'], sid, offer['token'], consent=True)
    finish(service)
    assert len(gateway.requests) == 2 and reads == [URL]
    assert sources.get(account['id'], sid)['request']['status'] == 'done'
    model_payload = json.loads(gateway.requests[1]['body']['messages'][1]['content'])
    assert model_payload['coverage']['complete_read']
    assert len(list((hub.paths.runtime / 'gateway-attempts').glob('*.json'))) == 2


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
