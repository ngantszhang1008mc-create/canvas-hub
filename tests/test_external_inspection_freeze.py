"""Shipped disabled default: no external work, with saved data and Canvas intact."""
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from canvas_hub import external_inspection
from canvas_hub.boundary import HubAPI
from canvas_hub.services.source_analysis import SourceAnalysis, AGENT
from canvas_hub.util import Conflict, now_iso
from canvas_hub.web.app import create_app


@pytest.fixture
def saved(hub, account, fixture_pkg, monkeypatch):
    assert external_inspection.ENABLED is False
    hub.importer.import_package(fixture_pkg)
    aid=account['id']
    url='https://course.example.edu/saved'
    sid=hub.external_sources.register(aid,url,course_id='4242',title='Saved course link')
    # Seed historical public and connected data without any reader/provider.
    classification={'roles':['material'],'confidence':.8,'summary':'Saved course material',
                    'access_mode':'connected_local_reviewed','read_at':now_iso(),'provided_by':'fixture'}
    with hub.db.tx() as c:
        c.execute("UPDATE external_sources SET classification_json=?,last_success_at=?,status='enabled' WHERE id=?",
                  (json.dumps(classification),now_iso(),sid))
        c.execute("INSERT INTO external_reads(source_id,read_at,status,evidence_json) VALUES (?,?,?,?)",
                  (sid,now_iso(),'connected_read',json.dumps([{'url':url,'content':'Retained course text'}])))
    with monkeypatch.context() as patch:
        patch.setattr(external_inspection,'ENABLED',True)
        req=hub.requests.create(aid,url=url,external_source_id=sid)
        generic=hub.requests.create(aid,url='https://other.example.edu/request')
    with hub.db.tx() as c:
        c.execute('UPDATE external_sources SET analysis_request_id=? WHERE id=?',(req['id'],sid))
    profile=hub.paths.browser_profiles/'external-sites'/'preserved-fixture'
    profile.mkdir(parents=True); (profile/'sentinel').write_text('synthetic saved profile')
    return {'aid':aid,'sid':sid,'url':url,'request':req['id'],'generic':generic['id'],
            'profile':profile,'source':deepcopy(hub.external_sources.get(aid,sid))}


def block_work(monkeypatch, hub):
    def forbidden(*args,**kwargs):
        pytest.fail('Frozen external feature attempted reader, provider, detector or browser work')
    monkeypatch.setattr(hub.external_sources.reader,'fetch',forbidden)
    monkeypatch.setattr(hub.external_sources.reader,'read',forbidden)
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',forbidden)
    monkeypatch.setattr('canvas_hub.services.login_detection.inspect',forbidden)
    monkeypatch.setattr('canvas_hub.services.site_connection.BrowserConnection',forbidden)


def test_http_entrypoints_refuse_before_work_and_preserve_saved_data(hub, saved, monkeypatch):
    block_work(monkeypatch,hub)
    app=create_app(HubAPI(hub),token='fixture',port=8765)
    monkeypatch.setattr(app.state.source_analysis,'_config',lambda:pytest.fail('No provider setup should be consulted'))
    root=f'/api/accounts/{saved["aid"]}'
    path=f'{root}/external-sources/{saved["sid"]}'
    posts=[('/analyze',{'consent':True,'token':'old-offer'}),('/analyze',{'executor':'mcp'}),
           ('/analyze-connected',{'consent':True,'token':'old-offer'}),('/connected-disclosure',{}),
           ('/login-check',{}),('/site-connection/offer',{}),('/site-connection/start',{'consent':True,'token':'old-offer'}),
           ('/site-connection/http_auth',{'request':{'username':'synthetic','password':'synthetic-secret'}}),
           ('/site-connection/verify',{}),('/site-connection/retry_login',{}),('/site-connection/approve_post',{}),
           ('/site-connection/confirm_content',{}),('/https-source',{'consent':True}),('/refresh',{}),
           ('/decision',{'approve':True,'revision':1,'spec_hash':'0'*64})]
    with TestClient(app,base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer fixture','X-Canvas-Hub':'1'}) as client:
        for suffix,body in posts:
            response=client.post(path+suffix,json=body)
            assert response.status_code==409,(suffix,response.text)
            assert response.json()['error']=='external_inspection_paused'
            assert 'synthetic-secret' not in response.text
        assert client.post(root+'/external-analysis/old-batch/start',json={'consent':True}).json()['error']=='external_inspection_paused'
        assert client.post(root+'/requests',json={'url':saved['url']}).json()['error']=='external_inspection_paused'
        assert not client.get(path+'/disclosure').json()['available']
        assert not client.get(root+'/external-analysis/disclosure?course_id=4242').json()['available']
        assert client.get(path+'/login-check').json()['state']=='paused'
        assert client.get(path).json()['classification']==saved['source']['classification']
        listing=client.get(root+'/external-sources?course_id=4242').json()
        assert listing['inspection']['enabled'] is False
    assert hub.external_sources.get(saved['aid'],saved['sid'])==saved['source']
    assert (saved['profile']/'sentinel').read_text()=='synthetic saved profile'
    assert not hub.db.query('SELECT * FROM source_analysis_attempts')


def test_queued_workers_stop_and_external_mcp_cannot_bypass_freeze(hub, saved, monkeypatch):
    block_work(monkeypatch,hub)
    aid,sid,rid=saved['aid'],saved['sid'],saved['request']
    service=SourceAnalysis(HubAPI(hub))
    service._finish(aid,sid,saved['generic'],'stale-or-deleted-attempt')
    assert hub.requests.view(aid,saved['generic'])['status']=='open'
    with hub.db.tx() as c:
        c.execute("UPDATE view_requests SET status='claimed',claimed_by=? WHERE id=?",(AGENT,rid))
        c.execute("INSERT INTO source_analysis_attempts(token,account_id,source_id,request_id,fingerprint,state,created_at) VALUES ('queued',?,?,?,'fixture','running',?)",(aid,sid,rid,now_iso()))
        c.execute("INSERT INTO source_analysis_batches(token,account_id,scope_key,entries_json,state,created_at) VALUES ('batch',?,'course:4242',?,'running',?)",
                  (aid,json.dumps([{'token':'queued','source_id':sid,'url':saved['url'],'request_id':rid,'status':'running'}]),now_iso()))
    service._execute(aid,sid,rid,'queued',{})
    service._batch_run(aid,'batch')
    assert service.batch_view(aid,'batch')['state']=='cancelled'
    assert hub.requests.view(aid,rid)['status']=='cancelled'
    for call in [lambda:hub.requests.claim(aid,saved['generic'],'Fixture agent'),
                 lambda:hub.requests.submit(aid,saved['generic'],'Fixture agent',content='not accepted'),
                 lambda:hub.external_sources.submit_analysis(aid,rid,'Fixture agent',{})]:
        with pytest.raises(Conflict) as error:call()
        assert error.value.code=='external_inspection_paused'
    assert hub.requests.view(aid,saved['generic'])['inspection_paused']
    assert hub.external_sources.get(aid,sid)['reads']==saved['source']['reads']


def test_normal_canvas_import_requests_and_supplemental_collection_continue(hub, account, saved, fixture_pkg, monkeypatch):
    from canvas_hub.collector.traversal import CollectionRun
    block_work(monkeypatch,hub)
    aid=saved['aid']
    package=deepcopy(fixture_pkg);package['package_id']='canvas-while-inspection-paused'
    assert hub.importer.import_package(package)['account_id']==aid
    req=hub.requests.create(aid,url=account['origin']+'/courses/4242/pages/schedule')
    hub.requests.claim(aid,req['id'],'Fixture agent')
    assert hub.requests.submit(aid,req['id'],'Fixture agent',content='Saved Canvas schedule')['status']=='done'
    job=HubAPI(hub).request_collection(aid,mode='quick')
    run=object.__new__(CollectionRun)
    run.hub,run.account,run.job_id,run.scoped_courses=hub,account,job['id'],{'4242'}
    run.should_stop=lambda:False
    run._supplemental()
    assert hub.jobs.get(job['id'])['state']=='queued'
    assert hub.external_sources.get(aid,saved['sid'])['classification']==saved['source']['classification']


@pytest.mark.browser
@pytest.mark.parametrize('width',[1280,380])
def test_frozen_ui_keeps_links_and_saved_text_without_analysis_controls(hub,saved,monkeypatch,tmp_path,width):
    from playwright.sync_api import sync_playwright,expect
    from .test_components_ui import serve
    block_work(monkeypatch,hub)
    with serve(hub,[]) as (base,token,_),sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':width,'height':900})
        requests=[];page.on('request',lambda r:requests.append((r.method,r.url)))
        try:
            page.goto(f'{base}/?token={token}');page.goto(f'{base}/#/course/4242')
            listing=page.locator('.external-source-list')
            expect(listing).to_contain_text('External inspection is temporarily unavailable')
            expect(listing.get_by_role('link',name='Saved course link')).to_have_attribute('href',saved['url'])
            expect(listing.get_by_role('button',name='Analyse all',exact=False)).to_have_count(0)
            expect(page.locator('.external-source-nudge')).to_have_count(0)
            listing.get_by_role('button',name='View saved source').click()
            panel=page.get_by_role('complementary',name='Source Agent',exact=True)
            panel.get_by_text('Saved source text',exact=True).click()
            expect(panel).to_contain_text('Retained course text')
            assert '\nnull\n' not in panel.inner_text()
            expect(panel.get_by_role('button')).to_have_count(1) # close only
            assert not [(method,url) for method,url in requests if '/external-' in url and method!='GET']
            assert not [url for _,url in requests if '/login-check' in url or '/site-connection' in url]
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(tmp_path/f'inspection-paused-{width}.png'),full_page=True)
        finally:browser.close()
