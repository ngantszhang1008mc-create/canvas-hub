"""Focused source-evidence and state-guidance checks; no external providers."""
from copy import deepcopy
import json

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.assistant import AssistantService, SYSTEM_PROMPT
from canvas_hub.assistant.context import Policy, SUMMARY_KEYS, data_version, live_data_version, valid_groups
from canvas_hub.assistant.providers import Turn
from canvas_hub.assistant.source_context import followup_source_context, with_source_context
from canvas_hub.boundary import HubAPI
from canvas_hub.services.context_sources import ContextSources
from canvas_hub.util import Conflict, NotFound, now_iso


def source(hub, aid, key, body):
    with hub.db.tx() as c:
        iid = c.execute("INSERT INTO items(account_id,item_type,source_key,title,first_observed_at,last_observed_at) "
                        "VALUES (?,'page',?,?,?,?)", (aid, key, key, now_iso(), now_iso())).lastrowid
        hub.search.index_item(c, aid, iid, 'page', key, {'body': body})
    return ContextSources(hub.db).catalog(aid, item_id=iid)['sources'][0]


def saved_summary(hub, account):
    aid = account['id']
    item = source(hub, aid, 'Original rule', 'The old formula requires ten hours.')
    svc = AssistantService(hub, HubAPI(hub))
    with hub.db.tx() as c:
        cid = c.execute("INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)",
                        (aid, 'Synthetic summary', now_iso(), now_iso())).lastrowid
    for n in range(5):
        text = 'Preserve my deliberate choices. ' * 180
        svc._append(cid, 'user', {'text': text, 'sent_text': text})
        svc._append(cid, 'assistant', {'text': 'Historical answer', 'tool_calls': []})
    before = svc._transcript(cid)
    summary = {k: [] for k in SUMMARY_KEYS}
    summary.update(goals=['Keep my chosen tracker state.'], decisions=['Historical rule: ten hours.'],
                   reference_manifest=[{'source_id': item['source_id'], 'version': item['version']}],
                   tool_execution_manifest=[{'tool': 'update_component', 'outcome': 'completed'}],
                   _live_state_version=live_data_version(hub, aid))
    with hub.db.tx() as c:
        c.execute('INSERT INTO assistant_context VALUES (?,?,?,?,?)',
                  (cid, before[5]['seq'], json.dumps(summary), data_version(hub, aid), now_iso()))
        hub.search.index_item(c, aid, item['item_id'], 'page', item['title'], {'body': 'The revised rule requires twelve hours.'})
    return svc, cid, before, item


def test_source_changed_summary_retains_intent_without_reinflating_history(hub, account):
    svc, cid, before, item = saved_summary(hub, account)
    checkpoint = dict(hub.db.one('SELECT * FROM assistant_context WHERE conversation_id=?', (cid,)))
    projected = svc._context_projection(cid, account['id'], before)
    assert len(projected) == 5 and valid_groups(projected)
    assert projected[1:] == before[6:]
    text = projected[0]['sent_text']
    assert 'Keep my chosen tracker state.' in text and '"outcome":"completed"' in text
    assert 'Source evidence is stale' in text and 'stale_revalidation_required' in text
    assert item['version'] in text and 'A summary never establishes source coverage' in text
    assert svc.context_status(account['id'], cid, before)['stale']
    with pytest.raises(Conflict):
        ContextSources(hub.db).read(account['id'], item['source_id'], version=item['version'])
    other = hub.identity.upsert_verified_account(account['instance_id'], {'canvas_user_id': 'other-summary'})
    with pytest.raises(NotFound):
        svc._context_projection(cid, other['id'], before)
    assert dict(hub.db.one('SELECT * FROM assistant_context WHERE conversation_id=?', (cid,))) == checkpoint
    hub.reopen()
    resumed = AssistantService(hub, HubAPI(hub))
    assert resumed._transcript(cid) == before
    assert resumed._context_projection(cid, account['id'], before) == projected


def test_recompaction_does_not_recertify_stale_source_facts(hub, account):
    svc, cid, _, _ = saved_summary(hub, account)
    svc._append(cid, 'user', {'text': 'Continue the same goal.', 'sent_text': 'Continue the same goal.'})
    before = svc._transcript(cid)
    calls = []

    class SummaryProvider:
        def complete(self, system, transcript, tools, on_text):
            calls.append((transcript, tools))
            assert 'stale_revalidation_required' in transcript[0]['sent_text']
            on_text('')
            return Turn(json.dumps({k: ['Keep the goal.'] if k == 'goals' else [] for k in SUMMARY_KEYS}),
                        usage={'input_tokens': 100, 'output_tokens': 20})

        def close(self):
            pass

    cfg = {'provider': 'codex', 'context': {'capacity': 64000}}
    assert not Policy.for_config(cfg).auto_compact
    projected = svc._compact_context(cid, account['id'], cfg,
        svc._context_projection(cid, account['id'], before), lambda cfg: SummaryProvider())
    stored = hub.db.one('SELECT * FROM assistant_context WHERE conversation_id=?', (cid,))
    assert stored['data_version'] == data_version(hub, account['id'])
    assert json.loads(stored['summary_json'])['_source_evidence_stale'] is True
    assert 'Source evidence is stale' in projected[0]['sent_text']
    assert svc.context_status(account['id'], cid, before)['stale']
    assert len(calls) == 1 and calls[0][1] == [] and svc._transcript(cid) == before


def test_followup_keeps_all_references_but_invalidates_stale_read_coverage(hub, account):
    aid = account['id']
    sources = [source(hub, aid, 'Old rule', 'OLD_RULE'), source(hub, aid, 'Chosen rule', 'CHOSEN_RULE'),
               source(hub, aid, 'Long rule', 'LONG_RULE ' * 1000)]
    for item in sources:
        item['inline'] = item['total_chars'] <= 4000
    scope = {'course_id': None, 'item_id': None}
    manifest = {'scope': scope, 'sources': sources, 'warnings': []}
    old_read = ContextSources(hub.db).read(aid, sources[0]['source_id'], version=sources[0]['version'])
    history = [{'role': 'user', 'text': 'Read these rules.', 'source_context': manifest},
               {'role': 'tool', 'results': [{'name': 'read_context_source', 'content': json.dumps(old_read)}]},
               {'role': 'user', 'text': 'Check only ' + sources[1]['source_id']}]
    before = deepcopy(history)
    with hub.db.tx() as c:
        hub.search.index_item(c, aid, sources[0]['item_id'], 'page', sources[0]['title'], {'body': 'NEW_RULE'})
    context = followup_source_context(hub, aid, history, scope)
    assert [(s['source_id'], s['version']) for s in context['sources']] == [(s['source_id'], s['version']) for s in sources]
    assert [s['text'] for s in context['sources'] if 'text' in s] == ['CHOSEN_RULE']
    projected = with_source_context(history, context)
    payload = json.loads(projected[-1]['sent_text'].split('Workspace source context (this request only):\n')[1])
    old, chosen, long = payload['sources']
    assert old['version_status'] == 'stale_or_unavailable' and not old['complete_read']
    assert old['historical_ranges_in_history'] == [[0, len('OLD_RULE')]]
    assert old['ranges_in_history'] == old['supplied_ranges'] == old['available_ranges'] == []
    assert chosen['version_status'] == long['version_status'] == 'current'
    assert chosen['complete_read'] and not long['complete_read'] and long['supplied_ranges'] == []
    assert 'NEW_RULE' not in json.dumps(payload) and history == before
    other = hub.identity.upsert_verified_account(account['instance_id'], {'canvas_user_id': 'other-source'})
    with pytest.raises(NotFound):
        ContextSources(hub.db).metadata(other['id'], sources[1]['source_id'], version=sources[1]['version'])


def test_component_receipts_use_guarded_state_guidance_without_reader(hub, account):
    ctx = ToolContext(HubAPI(hub), account_id=account['id'], read_results=None)
    raw, error, display = run_tool_display(ctx, 'show_checklist',
        {'title': 'Safe checklist', 'sections': [{'id': 's', 'rows': [{'id': 'r', 'label': 'Check rule'}]}]}, {'read', 'ui'})
    assert not error and display
    summary = json.loads(raw)['summary']
    assert 'get_component_context' in summary and 'fresh expected_version/state_token' in summary
    assert 'read it back with get_component.' not in summary
    assert 'partial coverage' in SYSTEM_PROMPT and 'do not infer a' in SYSTEM_PROMPT
    assert 'guess missing rules or chase unrelated courses' in SYSTEM_PROMPT


def test_presence_change_is_rehydrated_before_next_provider_turn(hub, account):
    aid = account['id']
    item = source(hub, aid, 'Presence rule', 'RETAINED_OLD_RULE')
    read = ContextSources(hub.db).read(aid, item['source_id'], version=item['version'])
    request_context = {'scope': {'course_id': None, 'item_id': None}, 'warnings': [],
                       'sources': [{**item, 'inline': True, 'text': read['text']}]}
    original_context = deepcopy(request_context)
    svc = AssistantService(hub, HubAPI(hub))
    with hub.db.tx() as c:
        cid = c.execute("INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)",
                        (aid, 'Presence change', now_iso(), now_iso())).lastrowid
    history = [{'role': 'user', 'text': 'Check this rule.'},
               {'role': 'assistant', 'tool_calls': [{'id': 'read-once', 'name': 'read_context_source', 'input': {}}]},
               {'role': 'tool', 'results': [{'id': 'read-once', 'name': 'read_context_source', 'content': json.dumps(read)}]}]
    original_history = deepcopy(history)
    cfg = {'provider': 'codex', 'context': {'capacity': 64000}}

    def prepare():
        projected, attempted = svc._prepare_context(cid, aid, cfg, 'system', history, [],
            lambda cfg: pytest.fail('must not call a provider'), allow_compact=False, request_context=request_context)
        assert not attempted and valid_groups(projected)
        return json.loads(projected[0]['sent_text'].split('Workspace source context (this request only):\n')[1])

    assert prepare()['sources'][0]['complete_read']
    # Importer presence checks preserve the source index and its version.
    with hub.db.tx() as c:
        c.execute("UPDATE items SET presence='source_unavailable' WHERE id=?", (item['item_id'],))
    assert ContextSources(hub.db).metadata(aid, item['source_id'])['version'] == item['version']
    payload = prepare()
    retained = payload['sources'][0]
    assert retained['source_id'] == item['source_id'] and retained['version'] == item['version']
    assert retained['presence'] == 'source_unavailable' and retained['version_status'] == 'stale_or_unavailable'
    assert not retained['complete_read'] and retained['available_ranges'] == retained['supplied_ranges'] == []
    assert retained['historical_ranges_in_history'] == [[0, len(read['text'])]]
    assert any('currently source_unavailable' in warning for warning in payload['warnings'])
    assert 'RETAINED_OLD_RULE' not in json.dumps(payload)
    assert history == original_history and request_context == original_context
