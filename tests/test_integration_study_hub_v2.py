"""Source analysis + asynchronous collection on the same integrated database."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from canvas_hub import db as database
from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.providers import Turn
from canvas_hub.boundary import HubAPI
from canvas_hub.services.source_analysis import SourceAnalysis
from canvas_hub.services.external_reader import PublicReader, Read
from canvas_hub.util import now_iso
from .test_integration_study_hub import integrated, home_widget
from .test_collection_pipeline import Control, build, wait_for, fast_extraction


def test_external_publication_runs_all_import_hooks_after_commit(hub, integrated, monkeypatch):
    seed, transport = integrated
    seen = []
    discovery = hub.importer.after_discovery
    changed = hub.importer.after_changed_import
    def discover(aid, pkg):
        assert not hub.db.conn.in_transaction, 'discovery must see committed source publication'
        seen.append('discovery')
        discovery(aid, pkg)
    def sync(aid, **kwargs):
        assert not hub.db.conn.in_transaction, 'component lock must not be acquired under the source write transaction'
        seen.append('changed')
        return changed(aid, **kwargs)
    monkeypatch.setattr(hub.importer, 'after_discovery', discover)
    monkeypatch.setattr(hub.importer, 'after_changed_import', sync)
    monkeypatch.setattr(hub.importer, 'after_import', lambda aid: seen.append('legacy'), raising=False)
    transport.select('updated')
    assert hub.external_sources.refresh(seed['aid'], seed['source'])['status'] == 'enabled'
    assert seen == ['discovery', 'changed', 'legacy']
    assert home_widget(hub, seed, seed['score'])['component']['source_values']['buckets/reflection/max']['value'] == 40


@pytest.mark.parametrize('versions', [[], list(range(1,15)), list(range(1,16)), [*range(1,15),16]],
                         ids=['empty','eff9eb5','only-15','only-16'])
def test_real_15_16_upgrade_matrix(tmp_path, monkeypatch, versions):
    files = database._migration_files()
    conn = database.connect(tmp_path/'upgrade.sqlite3')
    try:
        with monkeypatch.context() as m:
            m.setattr(database, '_migration_files', lambda:[f for f in files if f[0] in versions])
            database.migrate(conn)
        if versions:
            now=now_iso()
            conn.execute("INSERT INTO instances(id,origin,created_at) VALUES (1,'https://fixture.example.edu',?)", (now,))
            conn.execute("INSERT INTO accounts(id,instance_id,canvas_user_id,first_verified_at,last_verified_at) VALUES (1,1,'fixture',?,?)", (now,now))
            source_config=json.dumps({'bindings':[{'item_id':123,'field':'title','target':'title'}]})
            conn.execute("INSERT INTO components(id,account_id,kind,title,spec_json,state_json,source_config_json,created_at,updated_at) VALUES (1,1,'checklist','Keep','{}','{\"done\":{\"r\":true}}',?,?,?)",(source_config,now,now))
            conn.execute("INSERT INTO external_sources(id,account_id,url,url_key,title,last_seen_at) VALUES (1,1,'https://fixture.example.edu/page','key','Keep source',?)",(now,))
            conn.execute("INSERT INTO overall_widgets(account_id,widget_key,component_id,mode,position) VALUES (1,'component:1',1,'library',0)")
            if 15 in versions:
                conn.execute("INSERT INTO source_analysis_attempts(token,account_id,source_id,fingerprint,created_at,state) VALUES ('keep-token',1,1,'hash',?,'finished')",(now,))
            if 16 in versions:
                conn.execute("INSERT INTO component_source_changes VALUES (1,'item','123')")
        assert database.migrate(conn)==[f[0] for f in files if f[0] not in versions]
        assert database.migrate(conn)==[]
        assert database.schema_version(conn)==max(f[0] for f in files)
        for table in ('source_analysis_attempts','source_analysis_batches','component_source_dependencies','component_source_changes'):
            assert conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone()
        if versions:
            assert json.loads(conn.execute('SELECT state_json FROM components').fetchone()[0])['done']['r']
            assert conn.execute('SELECT mode FROM overall_widgets').fetchone()[0]=='library'
            assert conn.execute('SELECT title FROM external_sources').fetchone()[0]=='Keep source'
            assert conn.execute('SELECT dependency_key FROM component_source_dependencies').fetchone()[0]=='123'
            if 15 in versions:assert conn.execute('SELECT token FROM source_analysis_attempts').fetchone()[0]=='keep-token'
            if 16 in versions:assert conn.execute('SELECT dependency_key FROM component_source_changes').fetchone()[0]=='123'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:conn.close()


def test_nested_import_hooks_commit_and_rollback(hub, account, fixture_pkg, monkeypatch):
    aid=account['id'];events=[]
    original=hub.importer.after_changed_import
    def changed(account_id, **kwargs):
        assert not hub.db.conn.in_transaction
        events.append('changed')
        return original(account_id, **kwargs)
    monkeypatch.setattr(hub.importer,'after_changed_import',changed)
    monkeypatch.setattr(hub.importer,'after_import',lambda aid:events.append('legacy'),raising=False)
    with pytest.raises(RuntimeError,match='rollback'):
        with hub.db.tx():
            hub.importer.import_package(fixture_pkg)
            assert not events
            raise RuntimeError('rollback')
    assert not events and not hub.db.query('SELECT * FROM items')
    assert not hub.db.query('SELECT * FROM component_source_changes')
    with hub.db.tx():
        hub.importer.import_package(fixture_pkg)
        assert not events
    assert events==['changed','legacy']
    assert not hub.db.query('SELECT * FROM component_source_changes')


def test_after_commit_callbacks_can_open_transactions_and_rollback_isolated(hub):
    calls=[]
    def first():
        assert not hub.db.conn.in_transaction
        calls.append('first')
        with hub.db.tx():hub.db.after_commit(lambda:calls.append('nested'))
    with hub.db.tx():
        hub.db.after_commit(first)
        with hub.db.tx():hub.db.after_commit(lambda:calls.append('second'))
    assert calls==['first','nested','second']
    with pytest.raises(ValueError):
        with hub.db.tx():
            hub.db.after_commit(lambda:calls.append('discard'))
            raise ValueError()
    hub.db.after_commit(lambda:calls.append('immediate'))
    assert calls==['first','nested','second','immediate']


def test_collection_stages_and_builtin_batch_share_store_without_losing_widgets(hub, integrated, monkeypatch):
    seed, transport = integrated
    aid=seed['aid'];account=hub.identity.get_account(aid)
    downloads=Control();converting=threading.Event();release=threading.Event()
    def extract(sha, name, *, should_stop):
        converting.set()
        while not release.wait(.01):
            if should_stop():
                from canvas_hub.collector.client import Cancelled
                raise Cancelled('fixture stopped')
        return hub.resources._save_extraction(sha,('ok',None,'integrationv2document '+name,'fixture',None))
    monkeypatch.setattr(hub.resources,'extract_bounded',extract)
    hub.components.set_state(aid,seed['external_checklist'],{'done':{'reflection':True}})
    hub.components.set_source_override(aid,seed['score'],'buckets/reflection/max',35)
    initial=hub.components.get(aid,seed['score'])['state']
    url='https://course.example.edu/synthetic/extra-notes'
    sid=hub.external_sources.register(aid,url,course_id='5555')
    public_reads=[]
    def public_read(target):
        public_reads.append(target)
        return Read(target,200,'text/html',b'<h1>Extra course notes</h1>') if target==url else transport(target)
    hub.external_sources.reader=PublicReader(public_read)
    transport.select('updated')
    calls=[]
    class Provider:
        def complete(self,*args):
            calls.append(args)
            return Turn(json.dumps({'roles':['material'],'confidence':.9,'summary':'Fixture notes'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    api=HubAPI(hub);api.assistant=AssistantService(hub,api)
    api.assistant.update_config({'preset':'custom','base_url':'http://127.0.0.1:9/v1','model':'fake'})
    analysis=SourceAnalysis(api)
    run=build(hub,account,downloads,count=3)
    run.scoped_courses=['5555']
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(run.run)
        try:
            wait_for(lambda:hub.jobs.get(run.job_id)['stats'].get('core_status')=='updated')
            wait_for(lambda:len(downloads.calls)==2)
            assert not future.done() and not hub.search.search(aid,'integrationv2document')['results']
            disclosure=analysis.batch_disclosure(aid,course_id='5555')
            assert disclosure['urls']==[url] and not calls
            analysis.batch_start(aid,disclosure['token'],consent=True)
            wait_for(lambda:analysis.batch_view(aid,disclosure['token'])['state']=='done')
            assert analysis.batch_view(aid,disclosure['token'])['completed']==1 and len(calls)==1
            analysis.batch_start(aid,disclosure['token'],consent=True)
            assert len(calls)==1 and hub.external_sources.get(aid,sid)['classification']
            wait_for(lambda:home_widget(hub,seed,seed['score'])['component']['source_values']['buckets/reflection/max']['value']==40)
            assert not downloads.release.is_set()
            downloads.release.set()
            wait_for(lambda:hub.jobs.get(run.job_id)['phases']['downloads'].get('done')==3)
            assert converting.wait(3) and not future.done()
            iid=hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='file' AND source_key='1'",(aid,))['id']
            component=hub.components.create(aid,'checklist',{'title':'Keep document notes','sections':[{'id':'s','rows':[{'id':'r','label':'Read it'}]}]},
                sources={'review_sources':[{'item_id':iid,'fields':[],'attachments':True}]})
            hub.components.set_state(aid,component['id'],{'done':{'r':True}})
            hub.widgets.save(aid,f"component:{component['id']}",{'mode':'pinned'},expected_version=0)
            before=hub.components.get(aid,component['id'])['source_status']['last_success_at']
            release.set()
            assert future.result(timeout=10)=='completed'
        finally:
            downloads.release.set();release.set()
    hits=hub.search.search(aid,'integrationv2document')['results']
    assert len(hits)==3 and all(h['document']['extraction_status']=='ok' for h in hits)
    back=hub.components.get(aid,component['id'])
    assert back['state']['done']['r'] and back['source_status']['last_success_at']>before
    card=home_widget(hub,seed,seed['score'])
    assert card['component']['state']==initial and card['component']['spec']['buckets'][1]['max']==35
    assert home_widget(hub,seed,seed['external_checklist'])['component']['state']['done']['reflection']
    assert home_widget(hub,seed,component['id'])['mode']=='pinned'
    assert not hub.db.query('SELECT * FROM component_source_changes')
    assert hub.jobs.get(run.job_id)['phases']['verification']['done']==1
    assert len(calls)==1 and len(downloads.calls)==len(set(downloads.calls))==3
    other=hub.identity.upsert_verified_account(account['instance_id'],{'canvas_user_id':'v2-other'})['id']
    assert not hub.search.search(other,'integrationv2document')['results']
    assert not hub.external_sources.list(other)


@pytest.mark.parametrize('cancel_target',['analysis','collection'])
def test_cancellation_does_not_cross_between_collect_and_analysis(hub, integrated, fast_extraction, monkeypatch, cancel_target):
    seed,transport=integrated;aid=seed['aid'];account=hub.identity.get_account(aid)
    url='https://course.example.edu/synthetic/cancel-scope'
    sid=hub.external_sources.register(aid,url,course_id='5555')
    hub.external_sources.reader=PublicReader(lambda u:Read(u,200,'text/plain',b'Fixture notes'))
    entered,released=threading.Event(),threading.Event();calls=[]
    class Provider:
        def complete(self,*args):
            calls.append(args);entered.set();assert released.wait(8)
            return Turn(json.dumps({'roles':['material'],'confidence':.9,'summary':'Fixture'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    api=HubAPI(hub);api.assistant=AssistantService(hub,api)
    api.assistant.update_config({'preset':'custom','base_url':'http://127.0.0.1:9/v1','model':'fake'})
    analysis=SourceAnalysis(api)
    downloads=Control();run=build(hub,account,downloads,count=2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(run.run)
        try:
            wait_for(lambda:len(downloads.calls)==2)
            offer=analysis.batch_disclosure(aid,course_id='5555')
            analysis.batch_start(aid,offer['token'],consent=True);assert entered.wait(3)
            if cancel_target=='analysis':
                analysis.batch_cancel(aid,offer['token'])
                assert hub.jobs.get(run.job_id)['state']=='running'
                downloads.release.set();assert future.result(timeout=5)=='completed'
            else:
                hub.jobs.request_cancel(run.job_id)
                assert future.result(timeout=5)=='cancelled'
                assert hub.external_sources.get(aid,sid)['request']['status']=='claimed'
            released.set()
            wait_for(lambda:not analysis.batch_threads)
        finally:downloads.release.set();released.set()
    result=hub.external_sources.get(aid,sid)
    assert bool(result['classification'])==(cancel_target=='collection')
    assert len(calls)==1
    analysis.batch_start(aid,offer['token'],consent=True)
    assert len(calls)==1
    assert not any(t['state']=='running' for t in hub.jobs.tasks(run.job_id))
    if cancel_target=='collection':
        assert hub.jobs.get(run.job_id)['phases']['downloads']['pending']==2
    else:
        assert hub.jobs.get(run.job_id)['phases']['processing']['done']==2
