"""Cross-feature acceptance: discovery -> approved adapter -> saved tool -> home."""
import json
import threading

import pytest

from canvas_hub.testing.integration_demo import seed_integration_examples
from canvas_hub import db as database
from canvas_hub.agent_tools import ToolContext, run_tool
from canvas_hub.boundary import HubAPI
from canvas_hub.util import Conflict, NotFound, now_iso
from .test_workspace import workspace, start


@pytest.fixture
def integrated(hub, fixture_pkg):
    return seed_integration_examples(hub, fixture_pkg)


def home_widget(hub, seed, cid):
    return next(w for w in hub.widgets.board(seed['aid'])['widgets'] if w['key'] == f'component:{cid}')


@pytest.mark.parametrize('scenario', ['unavailable', 'template_changed', 'record_removed'])
def test_external_failure_makes_home_condition_uncertain_without_losing_work(hub, integrated, scenario):
    seed, transport = integrated
    aid, cid = seed['aid'], seed['score']
    previous = hub.components.get(aid, cid)
    assert home_widget(hub, seed, cid)['visible']
    transport.select(scenario)
    hub.external_sources.refresh(aid, seed['source'])
    card = home_widget(hub, seed, cid)
    assert card['condition']['state'] == 'unknown'
    assert not card['visible']
    assert card['component']['state'] == previous['state']
    assert card['component']['spec'] == previous['spec']
    assert card['component']['source_status']['availability'] == 'partial'
    pinned = home_widget(hub, seed, seed['external_checklist'])
    assert pinned['visible'] and pinned['component']['state']['done']['outline']
    assert pinned['component']['source_status']['availability'] == 'partial'


def test_normalized_refresh_preserves_shared_state_overrides_hide_and_undo(hub, integrated, monkeypatch):
    seed, transport = integrated
    aid, cid, checklist = seed['aid'], seed['score'], seed['external_checklist']
    monkeypatch.setattr('canvas_hub.assistant.make_provider', lambda *_: pytest.fail('No model on refresh'))
    hub.components.set_state(aid, checklist, {'done': {'reflection': True}})
    hub.components.set_source_override(aid, cid, 'buckets/reflection/max', 35)
    previous = hub.components.get(aid, cid)['state']
    old = home_widget(hub, seed, checklist)
    hub.widgets.save(aid, old['key'], {'mode': 'library'}, expected_version=old['version'])
    note = hub.annotations.create(aid, kind='note', item_id=seed['external_item'], title='Keep my draft', body='Local work')
    requests = len(hub.requests.list(aid))
    transport.select('updated')
    refreshed = hub.external_sources.refresh_registered(aid, course_ids={'5555'})
    assert refreshed[0]['status'] == 'enabled'
    card = home_widget(hub, seed, cid)
    assert card['visible'] and card['component']['state'] == previous
    assert card['component']['spec']['buckets'][1]['max'] == 35
    assert card['component']['source_values']['buckets/reflection/max']['value'] == 40
    assert card['component']['spec']['buckets'][1]['label'] == 'Reflection revised · synthetic'
    assert hub.components.get(aid, checklist)['state']['done']['reflection']
    assert not home_widget(hub, seed, checklist)['visible']
    assert hub.annotations.get(aid, note['id'])['body'] == 'Local work'
    for _ in range(3):
        hub.widgets.board(aid)
        hub.external_sources.analyze(aid, seed['source'])
    assert len(hub.requests.list(aid)) == requests and len(transport.calls) == 2
    assert not hub.db.query('SELECT * FROM assistant_messages')
    assert not any(f['field'] in ('submitted_at', 'workflow_state', 'score')
                   for f in hub.query.get_item(aid, seed['external_item'])['fields'])
    hub.components.set_source_override(aid, cid, 'buckets/reflection/max', follow_source=True)
    assert home_widget(hub, seed, cid)['component']['spec']['buckets'][1]['max'] == 40
    hub.components.undo(aid, cid)
    assert home_widget(hub, seed, cid)['component']['spec']['buckets'][1]['max'] == 35


def test_source_recovery_and_approval_namespaces_do_not_change_widget_choice(hub, integrated):
    from canvas_hub.testing.integration_demo import propose_fixture_adapter
    seed, transport = integrated
    aid, sid, cid = seed['aid'], seed['source'], seed['score']
    proposal = hub.widgets.propose(aid, cid, {'mode': 'pinned'})['proposal']
    transport.select('unavailable'); hub.external_sources.refresh(aid, sid)
    assert home_widget(hub, seed, cid)['condition']['state'] == 'unknown'
    transport.select('updated'); hub.external_sources.refresh(aid, sid)
    assert home_widget(hub, seed, cid)['visible']
    source = propose_fixture_adapter(hub, aid, sid, transport, reanalyze=True)
    adapter = source['adapters'][0]
    assert home_widget(hub, seed, cid)['condition']['state'] == 'unknown'
    assert home_widget(hub, seed, seed['external_checklist'])['component']['source_status']['availability'] == 'partial'
    assert home_widget(hub, seed, cid)['pending']['id'] == proposal['id']
    with pytest.raises(Conflict):
        hub.widgets.review(aid, f'component:{cid}', proposal_id=adapter['spec_hash'], approve=True)
    with pytest.raises(Conflict):
        hub.external_sources.decide(aid, sid, adapter['revision'], proposal['id'], approve=True)
    hub.external_sources.decide(aid, sid, adapter['revision'], adapter['spec_hash'], approve=True)
    assert home_widget(hub, seed, cid)['mode'] == 'conditional'
    hub.widgets.review(aid, f'component:{cid}', proposal_id=proposal['id'], approve=False)
    assert hub.external_sources.get(aid, sid)['enabled_revision'] == adapter['revision']
    hub.external_sources.decline(aid, sid)
    assert home_widget(hub, seed, cid)['condition']['state'] == 'unknown'
    assert home_widget(hub, seed, seed['external_checklist'])['component']['source_status']['availability'] == 'partial'


def test_combined_account_course_and_approval_isolation(hub, integrated):
    seed, _ = integrated
    aid, cid, sid = seed['aid'], seed['score'], seed['source']
    account = hub.identity.get_account(aid)
    other = hub.identity.upsert_verified_account(account['instance_id'], {'canvas_user_id': 'integration-other'})['id']
    proposal = hub.widgets.propose(aid, cid, {'mode': 'pinned'})['proposal']
    adapter = hub.external_sources.get(aid, sid)['adapters'][0]
    for action in [lambda: hub.widgets.review(other, f'component:{cid}', proposal_id=proposal['id'], approve=True),
                   lambda: hub.external_sources.decide(other, sid, adapter['revision'], adapter['spec_hash'], approve=True),
                   lambda: hub.workspace.conversation(other, item_id=seed['external_item'])]:
        with pytest.raises(NotFound): action()
    ctx = ToolContext(HubAPI(hub), account_id=aid, scope_course_id='4242', scope_item_id=seed['assignment'])
    _, error = run_tool(ctx, 'propose_overall_widget', {'component_id': cid, 'config': {'mode': 'pinned'}}, {'ui'})
    assert error
    assert home_widget(hub, seed, cid)['pending']['id'] == proposal['id']
    from canvas_hub.testing.integration_demo import SOURCE_URL
    second = hub.external_sources.register(aid, SOURCE_URL, course_id='4242')
    assert second != sid and hub.external_sources.get(aid, second)['enabled_revision'] is None
    assert not any(w['component'] for w in hub.widgets.board(other)['widgets'])


@pytest.mark.parametrize('action', ['disable', 'decline_revision', 'access_error'])
def test_pausing_source_immediately_updates_pinned_tool_status(hub, integrated, action):
    seed, _ = integrated
    aid, sid, cid = seed['aid'], seed['source'], seed['external_checklist']
    before = hub.components.get(aid, cid)
    if action == 'disable':
        hub.external_sources.decline(aid, sid)
    elif action == 'decline_revision':
        adapter = hub.external_sources.get(aid, sid)['adapters'][0]
        hub.external_sources.decide(aid, sid, adapter['revision'], adapter['spec_hash'], approve=False)
    else:
        rid = hub.external_sources.analyze(aid, sid, reanalyze=True)['analysis_request_id']
        hub.requests.claim(aid, rid, 'Synthetic agent')
        hub.external_sources.submit_analysis(aid, rid, 'Synthetic agent', {'access_error':'Synthetic login required'})
    after = home_widget(hub, seed, cid)
    assert after['visible'] and after['component']['state'] == before['state']
    assert after['component']['spec'] == before['spec']
    assert after['component']['source_status']['availability'] == 'partial'
    assert home_widget(hub, seed, seed['score'])['condition']['state'] == 'unknown'


def test_component_definition_approval_is_separate_from_adapter_and_placement(hub, integrated):
    from canvas_hub.testing.integration_demo import propose_fixture_adapter
    seed, transport = integrated
    aid, sid, cid = seed['aid'], seed['source'], seed['score']
    original = hub.components.get(aid, cid)
    proposed = hub.components.propose_spec(aid, cid, {**original['spec'], 'title':'Reviewed model title'})
    placement = hub.widgets.propose(aid, cid, {'mode':'pinned'})['proposal']
    transport.select('updated')
    adapter = propose_fixture_adapter(hub, aid, sid, transport, reanalyze=True)['adapters'][0]
    hub.external_sources.decide(aid, sid, adapter['revision'], adapter['spec_hash'], approve=True)
    with pytest.raises(Conflict): hub.components.accept_spec(aid, cid, proposed['version'])
    assert hub.components.get(aid, cid)['title'] == original['title']
    assert home_widget(hub, seed, cid)['pending']['id'] == placement['id']
    latest = hub.components.propose_spec(aid, cid, {**hub.components.get(aid, cid)['spec'], 'title':'Reviewed model title'})
    hub.components.accept_spec(aid, cid, latest['version'])
    assert home_widget(hub, seed, cid)['mode'] == 'conditional'
    assert home_widget(hub, seed, cid)['component']['state'] == original['state']


def test_context_cancel_and_source_request_dedup_stay_independent(workspace, integrated):
    w = workspace; hub = w['hub']; seed, _ = integrated
    aid = seed['aid']; fake = w['fake']
    fake.entered, fake.release = threading.Event(), threading.Event()
    source = hub.external_sources.analyze(aid, seed['source'], reanalyze=True)
    rid = source['analysis_request_id']
    assert hub.external_sources.analyze(aid, seed['source'])['analysis_request_id'] == rid
    hub.requests.claim(aid, rid, 'Synthetic independent source agent')
    result = start(w, request_id='integration:context-cancel')
    assert fake.entered.wait(2)
    same = start(w, request_id='integration:context-cancel')
    assert same['reused_request'] and same['conversation_id'] == result['conversation_id']
    hub.workspace.save(aid, {'item_id': seed['external_item']})
    w['assistant'].stop(aid, result['conversation_id'])
    fake.release.set(); w['assistant'].wait(result['conversation_id'], 5)
    assert hub.requests.view(aid, rid)['status'] == 'claimed'
    hub.requests.cancel(aid, rid)
    assert hub.external_sources.get(aid, seed['source'])['enabled_revision'] == 1
    assert w['assistant'].get_conversation(aid, result['conversation_id'])['conversation']['scope_item_id'] == w['iid']
    assert hub.workspace.get(aid)['active']['item_id'] == seed['external_item']
    assert len(w['factories']) == 1


@pytest.mark.parametrize('versions', [range(1, 9), range(1, 13), range(1, 14), [*range(1, 13), 14]],
                         ids=['pre-pr23', 'pr23', 'adapters-first', 'widgets-first'])
def test_real_migration_sequences_retain_existing_data(tmp_path, monkeypatch, versions):
    files = database._migration_files()
    path = tmp_path / 'migration.sqlite3'; conn = database.connect(path)
    with monkeypatch.context() as m:
        m.setattr(database, '_migration_files', lambda: [f for f in files if f[0] in versions])
        database.migrate(conn)
    now = now_iso()
    conn.execute("INSERT INTO instances(id,origin,created_at) VALUES (1,'https://canvas.example.edu',?)", (now,))
    conn.execute("INSERT INTO accounts(id,instance_id,canvas_user_id,time_zone,first_verified_at,last_verified_at) VALUES (1,1,'synthetic','Asia/Hong_Kong',?,?)", (now, now))
    spec = json.dumps({'title': 'Keep my tool', 'sections': [{'id': 's', 'rows': [{'id': 'r', 'label': 'Keep my progress'}]}]})
    state = json.dumps({'done': {'r': True}})
    conn.execute("INSERT INTO components(id,account_id,kind,title,spec_json,state_json,version,pinned,created_at,updated_at) VALUES (1,1,'checklist','Keep my tool',?,?,2,1,?,?)", (spec, state, now, now))
    conn.execute("INSERT INTO component_versions(component_id,version,spec_json,state_json,changed_by,created_at) VALUES (1,1,?,?,'user',?)", (spec, '{"done":{"r":false}}', now))
    if 13 in versions:
        conn.execute("INSERT INTO external_sources(id,account_id,url,url_key,title,last_seen_at) VALUES (1,1,'https://course.example.edu/synthetic','stable','Keep source',?)", (now,))
    if 14 in versions:
        conn.execute('INSERT INTO overall_widget_settings(account_id) VALUES (1)')
        conn.execute("INSERT INTO overall_widgets(account_id,widget_key,component_id,mode,position) VALUES (1,'component:1',1,'library',0)")
    assert database.migrate(conn) == [f[0] for f in files if f[0] not in versions]
    assert database.migrate(conn) == []
    assert conn.execute('SELECT spec_json,state_json,pinned,version FROM components').fetchone()[:] == (spec, state, 1, 2)
    assert json.loads(conn.execute('SELECT state_json FROM component_versions').fetchone()[0]) == {'done': {'r': False}}
    if 13 in versions: assert conn.execute('SELECT title FROM external_sources').fetchone()[0] == 'Keep source'
    if 14 in versions: assert conn.execute('SELECT mode FROM overall_widgets').fetchone()[0] == 'library'
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.close()


def test_preview_controls_are_opt_in_authenticated_and_fixture_only(hub, integrated):
    from fastapi.testclient import TestClient
    from canvas_hub.web.app import create_app
    from canvas_hub.testing.integration_controls import install_integration_controls
    seed, transport = integrated
    app = create_app(HubAPI(hub), token='t'*40, port=8765)
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    headers = {'Authorization':'Bearer '+'t'*40, 'X-Canvas-Hub':'1'}
    assert client.get('/api/testing/demo', headers=headers).status_code == 404
    install_integration_controls(app, hub, seed, transport)
    assert client.get('/api/testing/demo').status_code == 401
    assert client.post('/api/testing/refresh', headers={'Authorization':'Bearer '+'t'*40}).status_code == 403
    assert client.post('/api/testing/refresh', headers={**headers, 'Origin':'https://example.org'}).status_code == 403
    assert client.post('/api/testing/scenario', json={'scenario':'unknown'}, headers=headers).status_code == 400
    assert client.post('/api/testing/scenario', json={'scenario':'updated'}, headers=headers).status_code == 200
    assert client.post('/api/testing/refresh', headers=headers).json()['source_status'] == 'enabled'
    assert client.post('/api/testing/propose', headers=headers).json()['source_status'] == 'proposed'
    assert len(transport.calls) == 2
    assert not client.get('/api/testing/demo', headers=headers).json()['external_network']
