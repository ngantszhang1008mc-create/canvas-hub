"""Multi-step local flows, complete retrieval and historical-state safety.

The scripted provider proves plumbing and bounded context, not live-model task
quality. No benchmark/private data is embedded in these synthetic fixtures.
"""
import copy
import json

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.context import Policy, context_error, valid_groups
from canvas_hub.assistant.providers import ToolCall, Turn
from canvas_hub.assistant.read_results import READ_BYTES, bound, read
from canvas_hub.boundary import HubAPI
from canvas_hub.util import NotFound, now_iso
from .test_components import HMAW


def ask(name, args):
    result = yield name, args
    assert not result.get('error'), result
    return result


def retrieve(read_id, path):
    offset, entries, text, kind = 0, {}, '', None
    while True:
        result = yield from ask('read_tool_result', {'read_id': read_id, 'path': path, 'offset': offset})
        if 'text' in result:
            text += result['text']; kind = 'text'
        elif 'entries' in result:
            kind = result['kind']
            for key, value in result['entries'].items():
                if key in result.get('key_paths', {}):
                    key = yield from retrieve(read_id, result['key_paths'][key])
                if isinstance(value, dict) and value.get('complete') is False and 'read_path' in value:
                    value = yield from retrieve(read_id, value['read_path'])
                entries[key] = value
        else:
            return result['value']
        if result['next_offset'] is None:
            break
        assert result['next_offset'] > offset
        offset = result['next_offset']
    return text if kind == 'text' else list(entries.values()) if kind == 'array' else entries


@pytest.fixture
def scenario(hub, account, fixture_pkg):
    package = copy.deepcopy(fixture_pkg)
    assignment = next(i for i in package['items'] if i['type'] == 'assignment')
    for n in range(48):
        item = copy.deepcopy(assignment)
        item.update(canvas_id=str(50000+n), title='Synthetic overdue task ' + str(n))
        item['fields']['title'] = item['title']
        item['fields']['due_at']['iso'] = '2026-10-01T12:00:00+00:00'
        package['items'].append(item)
    hub.importer.import_package(package)
    aid = account['id']
    spec = copy.deepcopy(HMAW)
    for row in spec['buckets']:
        row['note'] = 'Synthetic rule and exception evidence. ' * 7
    hmaw = hub.components.create(aid, 'score_model', spec, course_id='4242')
    hub.components.set_state(aid, hmaw['id'], {'values': {'sde': 12}, 'include': {'nest': False}})
    others = [hub.components.create(aid, 'score_model', {'title': 'AISC tracker ' + str(n),
        'buckets': [{'id': 'r'+str(i), 'label': 'Synthetic activity ' + str(i), 'max': 5,
                     'note': 'Detailed old activity rule. ' * 8} for i in range(rows)]}, course_id='4242')
        for n, rows in enumerate((121, 80))]
    with hub.db.tx() as c:
        iid = c.execute("INSERT INTO items(account_id,item_type,source_key,course_canvas_id,title,first_observed_at,last_observed_at) VALUES (?,'page','full-rules','4242','Synthetic tracker rules',?,?)",
                        (aid, now_iso(), now_iso())).lastrowid
        body = 'SOURCE RULES: retained full evidence. ' * 200 + 'FINAL EXCEPTION: preserve manual values.'
        c.execute("INSERT INTO item_fields(item_id,field,value_json,compare_hash,last_observed_at,first_observed_at,last_changed_at) VALUES (?,'body',?,'synthetic',?,?,?)",
                  (iid, json.dumps(body), now_iso(), now_iso(), now_iso()))
        hub.search.index_item(c, aid, iid, 'page', 'Synthetic tracker rules', {'body': body})
    return aid, hmaw['id'], [v['id'] for v in others], body


@pytest.mark.parametrize('task', ['hmaw', 'aisc_hmaw', 'overdue'])
def test_realistic_multistep_reads_finish_under_32k_without_masking_or_compaction(hub, account, scenario, monkeypatch, task):
    aid, hmaw, others, body = scenario
    api = HubAPI(hub)
    svc = api.assistant = AssistantService(hub, api)
    svc.update_config({'preset': 'deepseek', 'api_key': 'synthetic-not-real',
                       'context': {'capacity': 32768, 'response_tokens': 4096, 'auto_compact': False}})
    before = {cid: hub.components.get(aid, cid) for cid in [hmaw, *others]}
    seen = {}

    def workflow():
        if task == 'overdue':
            args = {'start': '2026-10-01T00:00:00Z', 'end': '2026-10-02T00:00:00Z', 'status': 'unfinished', 'include_calendar': False}
            first = yield from ask('get_deadlines', args)
            assert not first['complete']
            repeated = yield from ask('get_deadlines', args)
            assert repeated['read_id'] == first['read_id'] and repeated['unchanged_snapshot']
            seen['deadlines'] = yield from retrieve(first['read_id'], '/deadlines')
            assert len(seen['deadlines']) == 48
            yield from ask('get_deadline_views', {})
            return '48 unfinished deadlines read; the saved filters and completion state are unchanged.'
        listing = yield from ask('list_components', {'query': 'HMAW'})
        assert [v['component_id'] for v in listing['components']] == [hmaw]
        if task == 'aisc_hmaw':
            for cid in others:
                large = yield from ask('get_component', {'component_id': cid})
                assert large['read_id'] and not large['complete']
                yield from ask('update_component', {'component_id': cid, 'pin': False})
        current = yield from ask('get_component', {'component_id': hmaw})
        spec = yield from retrieve(current['read_id'], '/definition')
        assert spec == before[hmaw]['spec']
        matches = yield from ask('search', {'query': 'Synthetic tracker rules', 'course_id': '4242'})
        source_item = next(v['item_id'] for v in matches['results'] if v['title'] == 'Synthetic tracker rules')
        catalog = yield from ask('list_context_sources', {'item_id': source_item})
        sources = catalog['sources'] if 'sources' in catalog else (yield from retrieve(catalog['read_id'], '/sources'))
        source = next(s for s in sources if s['title'] == 'Synthetic tracker rules')
        offset, text = 0, ''
        while True:
            part = yield from ask('read_context_source', {'source_id': source['source_id'], 'version': source['version'], 'offset': offset, 'limit': 2000})
            text += part['text']
            if part['next_offset'] is None: break
            offset = part['next_offset']
        assert text == body and 'FINAL EXCEPTION' in text
        fresh = yield from ask('get_component_context', {'component_id': hmaw})
        assert fresh['state_token']
        yield from ask('update_component', {'component_id': hmaw, 'spec': {**spec, 'title': 'HMAW rebuilt proposal'}})
        return 'Rebuild proposed for review; manual values preserved. Completed actions were not replayed.'

    flow, requests, budgets = workflow(), [], []
    class Provider:
        def complete(self, system, transcript, tools, on_text):
            assert all('read_results' not in m for m in transcript)
            report = Policy.for_config(svc._load()).measure(svc._load(), system, transcript, tools)
            assert report['capacity'] == 32768 and not report['over_budget']
            budgets.append(report['total_with_response'])
            requests.append(copy.deepcopy(transcript))
            try:
                name, args = next(flow) if len(requests) == 1 else flow.send(json.loads(transcript[-1]['results'][0]['content']))
                assert name in {t['name'] for t in tools}
                return Turn('', [ToolCall('step-'+str(len(requests)), name, args)], 'tool_use')
            except StopIteration as done:
                return Turn(done.value)
        def close(self): pass
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda cfg: Provider())
    selection = ['group:tracker', 'tool:list_context_sources', 'tool:read_context_source'] if task != 'overdue' else ['tool:get_deadline_views']
    cid = svc.send(aid, 'Synthetic ' + task + ' task', tool_selection=selection)['conversation_id']
    svc.wait(cid, 10)
    result = svc.get_conversation(aid, cid)
    assert result['conversation']['run_state'] == 'idle', result['conversation']
    assert len(requests) >= 8 and result['messages'][-1]['text']
    assert valid_groups(svc._transcript(cid))
    assert result['context']['summary_requests'] == 0
    for ident, old in before.items():
        new = hub.components.get(aid, ident)
        assert new['state'] == old['state'] and new['spec'] == old['spec']
        if task == 'aisc_hmaw' and ident in others: assert not new['pinned']
        else: assert new['pinned'] == old['pinned']
    assert bool(hub.components.get(aid, hmaw)['pending_spec']) == (task != 'overdue')
    # Earlier provider projections remain exact prefixes; no historical masking.
    for previous, later in zip(requests, requests[1:]):
        assert later[:len(previous)] == previous
    print(f'{task}: {len(requests)} scripted provider rounds; peak estimated input+reserve {max(budgets)}/29491 usable tokens')


def test_snapshot_complete_retrieval_restart_scope_and_live_change(hub, account, monkeypatch):
    api = HubAPI(hub); svc = AssistantService(hub, api)
    with hub.db.tx() as c:
        cid = c.execute('INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                        (account['id'], 'Snapshot', now_iso(), now_iso())).lastrowid
    ctx = ToolContext(api, account_id=account['id'], conversation_id=cid, read_results={})
    original = {'fields': {'a/b~c': {'body': '規則' * 8000 + 'FINAL'}}, 'rows': [{'id': str(i), 'value': i} for i in range(80)]}
    reply = bound(ctx, 'get_item', original)
    assert len(json.dumps(reply, ensure_ascii=False).encode()) < READ_BYTES
    svc.update_config({'preset': 'custom', 'base_url': 'http://localhost:9/v1', 'model': 'synthetic', 'context': {'capacity': 64000}})
    svc._append(cid, 'user', {'text': 'Original synthetic instruction. ' * 300})
    svc._append(cid, 'assistant', {'tool_calls': [{'id': 'read', 'name': 'get_item', 'input': {'item_id': 1}}]})
    svc._append(cid, 'tool', {'results': [{'id': 'read', 'name': 'get_item', 'content': json.dumps(reply)}], 'read_results': ctx.read_results})
    svc._append(cid, 'assistant', {'text': 'Snapshot retained.'})
    for _ in range(2):
        svc._append(cid, 'user', {'text': 'Synthetic follow-up'})
        svc._append(cid, 'assistant', {'text': 'Acknowledged'})
    hub.reopen(); ctx.read_results = {}
    text, offset = '', 0
    while True:
        page = read(ctx, {'read_id': reply['read_id'], 'path': '/fields/a~1b~0c/body', 'offset': offset, 'chars': 4000})
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= READ_BYTES
        text += page['text']
        if page['next_offset'] is None: break
        offset = page['next_offset']
    assert text == original['fields']['a/b~c']['body']
    assert bound(ctx, 'get_item', original)['unchanged_snapshot']
    newer = bound(ctx, 'get_item', {**original, 'revision': 2})
    assert newer['read_id'] != reply['read_id']
    # Spaces in the actual tool serializer count too, not just canonical JSON.
    dense = bound(ctx, 'get_item', {'rows': [0] * 2000})
    assert dense['read_id'] and len(json.dumps(dense).encode()) <= READ_BYTES
    assert read(ctx, {'read_id': reply['read_id'], 'path': '/rows/79/value'})['value'] == 79
    history = svc._transcript(cid)
    assert all('read_results' not in m for m in history)
    from .test_assistant_context import mock_provider
    mock_provider(monkeypatch)
    svc.compact(account['id'], cid); svc.wait(cid, 5)
    assert svc.get_conversation(account['id'], cid)['conversation']['run_state'] == 'idle'
    assert svc._transcript(cid) == history
    assert reply['read_id'] in svc._context_projection(cid, account['id'], history)[0]['sent_text']
    assert read(ctx, {'read_id': reply['read_id'], 'path': '/rows/79/value'})['value'] == 79
    long_key = '複雜字段/' * 6000
    unusual = {long_key: {'tail': 'Full rule'}, 'nested': {'a/b~c': 'Original rule'}}
    large = bound(ctx, 'get_item', unusual)
    assert len(json.dumps(large, ensure_ascii=False).encode()) < READ_BYTES
    root = read(ctx, {'read_id': large['read_id']})
    label, path = next(iter(root['key_paths'].items()))
    text, offset = '', 0
    while True:
        page = read(ctx, {'read_id': large['read_id'], 'path': path, 'offset': offset})
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= READ_BYTES
        text += page['text']
        if page['next_offset'] is None: break
        offset = page['next_offset']
    assert text == long_key and root['entries'][label] == unusual[long_key]
    assert read(ctx, {'read_id': large['read_id'], 'path': '/' + long_key.replace('~', '~0').replace('/', '~1')})['entries'] == unusual[long_key]
    denied, failed, _ = run_tool_display(ctx, 'read_tool_result', {'read_id': reply['read_id']}, set())
    assert failed and json.loads(denied)['error'] == 'unknown_or_disabled_tool'
    for other in [ToolContext(api, account_id=account['id']+1, conversation_id=cid, read_results={}),
                  ToolContext(api, account_id=account['id'], conversation_id=cid+1, read_results={})]:
        with pytest.raises(NotFound): read(other, {'read_id': reply['read_id']})


def test_margin_is_explicit_and_verified_capacity_never_overrides_application_limit():
    cfg = {'provider': 'openai_compatible', 'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-flash'}
    assert Policy.for_config(cfg).capacity == 1048576
    assert Policy.for_config({**cfg, 'context': {'capacity': 32768}}).capacity == 32768
    policy = Policy.for_config({'provider': 'codex'})
    report = policy.measure({'provider': 'codex'}, '', [], [])
    report.update(used={'history': 4494, 'tools': 32110, 'documents': 7413}, schema_tokens=6946, system_tokens=1863,
                  framing_tokens=1472, total_with_response=58394)
    message = str(context_error(report))
    assert 'effective budget 57600' in message and '10% safety margin (6400)' in message
    assert 'aggregate excess 794' in message and 'retains the same history' in message


def test_real_tool_indexes_keep_full_fields_and_existing_page_cursors(hub, account, fixture_pkg, scenario):
    aid, hmaw, _, _ = scenario
    package = copy.deepcopy(fixture_pkg)
    package['package_id'] += '-extra-courses'
    template = next(i for i in package['items'] if i['type'] == 'course')
    package['items'] = []
    for n in range(25):
        course = copy.deepcopy(template)
        course.update(canvas_id=str(70000+n), title='Synthetic course ' + str(n))
        course['fields'].update(name=course['title'], title=course['title'], course_code='SYN'+str(n))
        package['items'].append(course)
    hub.importer.import_package(package)
    api = HubAPI(hub); svc = AssistantService(hub, api)
    with hub.db.tx() as c:
        cid = c.execute('INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                        (aid, 'Read surfaces', now_iso(), now_iso())).lastrowid
        item_id = c.execute("SELECT id FROM items WHERE source_key='full-rules'").fetchone()['id']
        body = 'Complete rule. ' * 1200 + 'FINAL EXCEPTION'
        c.execute("UPDATE item_fields SET value_json=? WHERE item_id=? AND field='body'", (json.dumps(body), item_id))
    ctx = ToolContext(api, account_id=aid, conversation_id=cid, read_results={})
    def invoke(name, args):
        text, error, _ = run_tool_display(ctx, name, args, {'read'})
        assert not error, text
        assert len(text.encode()) <= READ_BYTES
        return json.loads(text)
    for name, args, field in [('get_item', {'item_id': item_id}, 'fields'),
                              ('list_courses', {}, 'courses'),
                              ('search', {'query': 'Synthetic', 'limit': 50}, 'results'),
                              ('query_data', {'sql': 'SELECT * FROM items ORDER BY id'}, 'rows'),
                              ('list_items', {'course_id': '4242'}, 'items'),
                              ('list_context_sources', {'course_id': '4242'}, 'sources')]:
        reply = invoke(name, args)
        if name == 'list_context_sources':
            assert 'read_id' not in reply  # one direct, byte-bounded catalog page
            assert reply['sources'] and reply['next_offset'] > 0
            continue
        assert reply['read_id'] and not reply['complete']
        flow = retrieve(reply['read_id'], '')
        request = next(flow)
        while True:
            try: request = flow.send(invoke(*request))
            except StopIteration as complete:
                original = complete.value; break
        assert field in original
        if name == 'get_item': assert original['fields']['body']['value'] == body
        if name == 'list_courses': assert len(original['courses']) == 26
        if name in {'search', 'list_items'}: assert original['next_cursor']
        if name == 'list_context_sources': assert original['next_offset'] > 0
    narrow = invoke('list_components', {'query': 'hmaw', 'limit': 1})
    assert [v['component_id'] for v in narrow['components']] == [hmaw]
    page = invoke('list_components', {'limit': 1})
    assert page['next_offset'] == 1
    assert invoke('list_components', {'offset': 2, 'limit': 1})['next_offset'] is None
    # An MCP/fixed gateway caller without the reader keeps its established shape.
    legacy = ToolContext(api, account_id=aid)
    raw, error, _ = run_tool_display(legacy, 'get_component', {'component_id': hmaw}, {'read'})
    assert not error and 'read_id' not in json.loads(raw)
