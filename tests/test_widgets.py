"""Placements, exact rules, review safety and migration without adapter 0013."""
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from canvas_hub import db as database
from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.testing.widget_demo import seed_widget_examples
from canvas_hub.util import Conflict, InvalidInput, NotFound

NOW = datetime(2026,11,1,6,30,tzinfo=timezone.utc)

@pytest.fixture
def examples(hub,fixture_pkg):
    return seed_widget_examples(hub,fixture_pkg,now=NOW)


def widget(hub,e,cid,now=NOW):
    key=cid if isinstance(cid,str) else f'component:{cid}'
    return next(w for w in hub.widgets.board(e['aid'],now=now)['widgets'] if w['key']==key)


def save(hub,e,cid,config):
    w=widget(hub,e,cid)
    return hub.widgets.save(e['aid'],w['key'],config,expected_version=w['version'])


def test_defaults_and_explicit_examples_keep_one_state(hub,examples):
    e=examples;aid=e['aid']
    assert all(widget(hub,e,e[c])['visible'] for c in ('daily','seminar','score'))
    prior=hub.components.get(aid,e['daily'])
    hub.components.set_state(aid,e['daily'],{'done':{'read':True}},expected_version=prior['version'])
    assert widget(hub,e,e['daily'])['component']['state']['done']=={'read':True}
    assert hub.components.get(aid,e['daily'])['version']==2
    assert not HubAPI(hub).read_item(aid,e['assignment'])['deadline_status']['done_locally']
    unplaced=hub.components.create(aid,'hours_tracker',{'title':'Hours','target':20})
    hub.components.pin(aid,unplaced['id'],True)
    assert widget(hub,e,unplaced['id'])['mode']=='library'
    assert not widget(hub,e,unplaced['id'])['visible']


def test_hidden_and_order_survive_reopen_and_no_daily_reset(hub,examples):
    e=examples
    hub.components.set_state(e['aid'],e['daily'],{'done':{'read':True}})
    save(hub,e,e['score'],{'mode':'library','rule':widget(hub,e,e['score'])['rule']})
    board=hub.widgets.board(e['aid']);keys=[w['key'] for w in board['widgets'] if w['version']][::-1]
    hub.widgets.reorder(e['aid'],keys,revision=board['revision'])
    hub.reopen()
    for _ in range(3):
        assert not widget(hub,e,e['score'])['visible']
    assert [w['key'] for w in hub.widgets.board(e['aid'])['widgets']]==keys
    assert widget(hub,e,e['daily'],datetime(2026,11,2,tzinfo=timezone.utc))['component']['state']['done']=={'read':True}


def test_date_window_uses_local_inclusive_dates(hub,examples):
    e=examples
    rule={'kind':'date_window','start':'2026-11-01','end':'2026-11-01','time_zone':'Asia/Hong_Kong','reason':'Explicit scoring date'}
    save(hub,e,e['score'],{'mode':'conditional','rule':rule})
    for stamp,visible in [('2026-10-31T15:59:59+00:00',False),('2026-10-31T16:00:00+00:00',True),('2026-11-01T15:59:59+00:00',True),('2026-11-01T16:00:00+00:00',False)]:
        assert widget(hub,e,e['score'],datetime.fromisoformat(stamp))['visible']==visible


@pytest.mark.parametrize('stamp,visible',[
    ('2026-11-01T03:59:59+00:00',False),('2026-11-01T04:00:00+00:00',True),
    ('2026-11-01T05:30:00+00:00',True),('2026-11-01T06:30:00+00:00',True),
    ('2026-11-02T04:59:59+00:00',True),('2026-11-02T05:00:00+00:00',False)])
def test_dst_repeated_hour_and_25_hour_day(hub,examples,stamp,visible):
    save(hub,examples,examples['daily'],{'mode':'conditional','rule':{'kind':'date_window',
        'start':'2026-11-01','end':'2026-11-01','time_zone':'America/New_York','reason':'Explicit day'}})
    assert widget(hub,examples,examples['daily'],datetime.fromisoformat(stamp))['visible']==visible


@pytest.mark.parametrize('rule',[
    {'kind':'code','eval':'true'}, {'kind':'event_today','item_id':True,'time_zone':'Asia/Hong_Kong'},
    {'kind':'date_window','start':'2026-02-30','end':'2026-03-01','time_zone':'Asia/Hong_Kong','reason':'x'},
    {'kind':'date_window','start':'2026-11-02','end':'2026-11-01','time_zone':'Asia/Hong_Kong','reason':'x'},
    {'kind':'date_window','start':'2026-11-01','end':'2026-11-01','time_zone':'invalid/time-zone','reason':'x'},
    {'kind':'date_window','start':'2026-11-01','end':'2026-11-01','time_zone':'Asia/Hong_Kong'},
    {'kind':'active_work'}, {'kind':'active_work','item_id':1,'course_id':'4242'},
])
def test_restricted_rule_validation(hub,examples,rule):
    with pytest.raises((InvalidInput,NotFound)):
        save(hub,examples,examples['daily'],{'mode':'conditional','rule':rule})


def test_event_requires_fresh_confirmed_owned_source(hub,examples):
    e=examples;aid=e['aid']
    assert widget(hub,e,e['seminar'])['condition']['source']['item_id']==e['event']
    hub.db.conn.execute("UPDATE item_fields SET last_observed_at='2026-01-01T00:00:00Z' WHERE item_id=? AND field='start_at'",(e['event'],))
    assert widget(hub,e,e['seminar'])['condition']['state']=='unknown'
    assert not widget(hub,e,e['seminar'])['visible']
    hub.db.conn.execute("UPDATE item_fields SET last_observed_at=?,value_json=? WHERE item_id=? AND field='start_at'",(NOW.isoformat(),json.dumps({'iso':'2026-11-01T14:00:00'}),e['event']))
    assert 'confirmed' in widget(hub,e,e['seminar'])['condition']['reason']
    hub.db.conn.execute("UPDATE item_fields SET value_json=? WHERE item_id=? AND field='start_at'",(json.dumps({'iso':123}),e['event']))
    assert widget(hub,e,e['seminar'])['condition']['state']=='unknown'
    hub.db.conn.execute("UPDATE items SET presence='unavailable' WHERE id=?",(e['event'],))
    assert 'unavailable' in widget(hub,e,e['seminar'])['condition']['reason']
    hub.db.conn.execute('DELETE FROM items WHERE id=?',(e['event'],))
    assert widget(hub,e,e['seminar'])['condition']['state']=='unknown'
    assert hub.components.get(aid,e['seminar'])['id']==e['seminar']
    save(hub,e,e['seminar'],{'mode':'library','rule':widget(hub,e,e['seminar'])['rule']})
    assert widget(hub,e,e['seminar'])['mode']=='library'


def test_active_work_is_explicit_and_references_are_checked(hub,examples):
    e=examples
    save(hub,e,e['seminar'],{'mode':'conditional','rule':{'kind':'active_work','course_id':'4242'}})
    assert not widget(hub,e,e['seminar'])['visible']
    hub.workspace.save(e['aid'],{'item_id':e['assignment']})
    assert widget(hub,e,e['seminar'])['visible']
    hub.workspace.save(e['aid'],{'clear':True})
    assert not widget(hub,e,e['seminar'])['visible']
    with pytest.raises(InvalidInput):
        save(hub,e,e['score'],{'mode':'conditional','rule':{'kind':'event_today','item_id':e['event'],'time_zone':'Asia/Hong_Kong'}})


def test_account_isolation_in_service_sql_and_rules(hub,examples):
    e=examples;acct=hub.identity.get_account(e['aid'])
    other=hub.identity.upsert_verified_account(acct['instance_id'],{'canvas_user_id':'other','time_zone':'UTC'})['id']
    assert len(hub.widgets.board(other)['widgets'])==3
    with pytest.raises(NotFound):
        hub.widgets.save(other,f"component:{e['score']}",{'mode':'pinned'},expected_version=0)
    c=hub.components.create(other,'checklist',{'rows':[{'id':'one','label':'Private'}]})
    with pytest.raises(NotFound):
        hub.widgets.save(other,f"component:{c['id']}",{'mode':'conditional','rule':{'kind':'event_today','item_id':e['event'],'time_zone':'UTC'}},expected_version=0)
    with pytest.raises(sqlite3.IntegrityError):
        hub.db.conn.execute("INSERT INTO overall_widgets(account_id,widget_key,component_id,mode,position) VALUES (?,? ,?,'pinned',9)",(other,'bad',e['score']))
    hub.identity.require_account(other)
    assert not any(w.get('component') for w in hub.widgets.board(other)['widgets'] if w['visible'])


def test_agent_proposal_needs_review_respects_hidden_choice_and_scope(hub,examples):
    e=examples;aid=e['aid']
    save(hub,e,e['daily'],{'mode':'library'})
    ctx=ToolContext(HubAPI(hub),account_id=aid)
    args={'component_id':e['daily'],'config':{'mode':'pinned'}}
    _,err,_=run_tool_display(ctx,'propose_overall_widget',args,{'read'})
    assert err
    _,err,_=run_tool_display(ctx,'propose_overall_widget',args,{'ui'})
    assert not err
    w=widget(hub,e,e['daily']);p=w['pending'];assert not w['visible']
    save(hub,e,e['daily'],{'mode':'library'}) # Explicit user choice invalidates proposal.
    with pytest.raises(Conflict):
        hub.widgets.review(aid,w['key'],proposal_id=p['id'],approve=True)
    hub.widgets.propose(aid,e['daily'],{'mode':'pinned'})
    p=widget(hub,e,e['daily'])['pending']
    hub.widgets.review(aid,w['key'],proposal_id=p['id'],approve=True)
    assert widget(hub,e,e['daily'])['visible']
    _,err,_=run_tool_display(ToolContext(HubAPI(hub),account_id=aid,scope_course_id='4242'),'propose_overall_widget',{'component_id':e['score'],'config':{'mode':'pinned'}},{'ui'})
    assert err


def test_review_revalidates_deleted_evidence_and_replaced_proposal(hub,examples):
    e=examples
    config={'mode':'conditional','rule':widget(hub,e,e['seminar'])['rule']}
    p=hub.widgets.propose(e['aid'],e['seminar'],config)
    newer=hub.widgets.propose(e['aid'],e['seminar'],config)
    with pytest.raises(Conflict):hub.widgets.review(e['aid'],p['widget_key'],proposal_id=p['proposal']['id'],approve=True)
    hub.db.conn.execute('DELETE FROM items WHERE id=?',(e['event'],))
    with pytest.raises(NotFound):hub.widgets.review(e['aid'],newer['widget_key'],proposal_id=newer['proposal']['id'],approve=True)
    hub.widgets.review(e['aid'],newer['widget_key'],proposal_id=newer['proposal']['id'],approve=False)


def test_version_conflicts_preserve_component_edits_and_preferences(hub,examples):
    e=examples;w=widget(hub,e,e['daily']);key=w['key']
    save(hub,e,e['daily'],{'mode':'library'})
    with pytest.raises(Conflict):hub.widgets.save(e['aid'],key,{'mode':'pinned'},expected_version=w['version'])
    c=hub.components.get(e['aid'],e['daily'])
    def update(done):
        try:return hub.components.set_state(e['aid'],c['id'],{'done':done},expected_version=c['version'])
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(update,[{'read':True},{'pack':True}]))
    assert sum(r is not None for r in results)==1
    assert not widget(hub,e,e['daily'])['visible']
    hub.components.undo(e['aid'],c['id'])
    assert widget(hub,e,e['daily'])['component']['state']['done']=={}


def test_timezone_setting_and_reorder_validation(hub,examples):
    e=examples;board=hub.widgets.board(e['aid'])
    hub.widgets.set_timezone(e['aid'],'America/New_York')
    assert hub.widgets.board(e['aid'])['time_zone']=='America/New_York'
    assert widget(hub,e,e['score'])['rule']['time_zone']=='Asia/Hong_Kong'
    with pytest.raises(InvalidInput):hub.widgets.set_timezone(e['aid'],'No/Such_Zone')
    with pytest.raises(Conflict):hub.widgets.reorder(e['aid'],[],revision=board['revision'])
    with pytest.raises(InvalidInput):hub.widgets.reorder(e['aid'],['deadlines','deadlines'],revision=hub.widgets.board(e['aid'])['revision'])


def test_migration_14_applies_without_13_retains_data_and_allows_later_13(monkeypatch):
    # Deliberately omit the sibling adapter migration, even after both features
    # land together. This verifies the independently deployable widget upgrade.
    files=[f for f in database._migration_files() if f[0]<=14 and f[0]!=13]
    monkeypatch.setattr(database,'_migration_files',lambda:files)
    c=database.connect(':memory:')
    with monkeypatch.context() as m:
        m.setattr(database,'_migration_files',lambda:[f for f in files if f[0]<=12])
        assert database.migrate(c)[-1]==12
    assert database.migrate(c)==[14]
    assert database.migrate(c)==[]
    with monkeypatch.context() as m:
        m.setattr(database,'_migration_files',lambda:sorted([*files,(13,'0013_synthetic_independent.sql','CREATE TABLE adapter_test(id INTEGER);')]))
        assert database.migrate(c)==[13]
    assert c.execute('SELECT COUNT(*) FROM overall_widgets').fetchone()[0]==0
    c.close()


def test_http_contract_and_read_only_refresh(hub,examples,monkeypatch):
    from fastapi.testclient import TestClient
    from canvas_hub.web.app import create_app
    e=examples
    client=TestClient(create_app(HubAPI(hub),token='t'*40,port=8730),base_url='http://127.0.0.1:8730')
    path=f"/api/accounts/{e['aid']}/widgets"
    assert client.get(path).status_code==401
    client.cookies.set('canvas_hub_token','t'*40)
    client.headers.update({'X-Canvas-Hub':'1','Origin':'http://127.0.0.1:8730'})
    monkeypatch.setattr('canvas_hub.assistant.make_provider',lambda *a,**k:pytest.fail('Home must not start a provider'))
    before=hub.db.one('SELECT COUNT(*) n FROM component_versions')['n']
    for _ in range(3):assert client.get(path).status_code==200
    assert hub.db.one('SELECT COUNT(*) n FROM component_versions')['n']==before
    assert client.put(path+'/time-zone',json={'time_zone':'Invalid/Zone'}).status_code==400
    assert client.put(path+'/deadlines',json={'config':{'mode':'library'},'expected_version':1,'arbitrary':True}).status_code==400
    assert client.put(path+'/deadlines',json={'config':{'mode':'library'},'expected_version':1}).status_code==200
    assert client.put(path+'/deadlines',json={'config':{'mode':'pinned'},'expected_version':1}).status_code==409
    assert client.get(f'/api/accounts/9999/widgets').status_code==404
    p=hub.widgets.propose(e['aid'],e['daily'],{'mode':'library'})
    assert client.post(path+'/'+p['widget_key']+'/review',json={'proposal_id':p['proposal']['id'],'approve':False}).status_code==200


def test_placements_source_overrides_and_progress_survive_backup(hub,examples):
    from canvas_hub.services import backup
    from .test_component_sync import score_spec,score_sources,field
    e=examples;aid=e['aid']
    v=hub.components.create(aid,'score_model',score_spec(),course_id='4242',sources=score_sources(e['assignment']))
    hub.components.set_source_override(aid,v['id'],'buckets/proposal/max',15)
    hub.components.set_state(aid,v['id'],{'values':{'proposal':8}})
    hub.widgets.save(aid,f"component:{v['id']}",{'mode':'pinned'},expected_version=0)
    field(hub,e['assignment'],'points_possible',20)
    hub.components.sync_sources(aid)
    assert widget(hub,e,v['id'])['component']['spec']['buckets'][0]['max']==15
    saved=backup.create_backup(hub)
    save(hub,e,v['id'],{'mode':'library'})
    backup.restore_backup(hub,saved)
    view=widget(hub,e,v['id'])
    assert view['mode']=='pinned'
    assert view['component']['state']['values']['proposal']==8
    assert view['component']['source_overrides']['buckets/proposal/max']['value']==15


def test_existing_component_history_is_unchanged_by_upgrade(tmp_path,monkeypatch,fixture_pkg):
    from canvas_hub.services import Hub
    from canvas_hub.paths import get_paths
    files=database._migration_files()
    with monkeypatch.context() as m:
        m.setattr(database,'_migration_files',lambda:[f for f in files if f[0]<=12])
        old=Hub(get_paths(tmp_path/'old-home'))
    # Simulate the old application without the migration-16 dependency hook.
    old.importer.after_changed_import=None
    instance=old.identity.add_instance(fixture_pkg['identity']['origin'])
    aid=old.identity.upsert_verified_account(instance['id'],{'canvas_user_id':'100001','time_zone':'Asia/Hong_Kong'})['id']
    old.importer.import_package(fixture_pkg)
    comp=old.components.create(aid,'checklist',{'rows':[{'id':'a','label':'Keep this progress'}]})
    old.components.set_state(aid,comp['id'],{'done':{'a':True}})
    old.components.pin(aid,comp['id'],True)
    before=old.components.get(aid,comp['id'])
    history=[dict(r) for r in old.db.query('SELECT * FROM component_versions')]
    old.reopen()
    assert old.components.get(aid,comp['id'])==before
    assert [dict(r) for r in old.db.query('SELECT * FROM component_versions')]==history
    assert next(w for w in old.widgets.board(aid)['widgets'] if w['component'])['mode']=='library'
    old.close()
