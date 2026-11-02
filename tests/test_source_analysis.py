"""Built-in analysis uses fixture pages and fake models only."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from canvas_hub.assistant import AssistantService
from canvas_hub.assistant.providers import Turn, ToolCall
from canvas_hub.boundary import HubAPI
from canvas_hub.services.source_analysis import SourceAnalysis, AGENT
from canvas_hub.services.external_reader import PublicReader, Read
from canvas_hub.util import InvalidInput, Conflict, NotFound
from canvas_hub.web.app import create_app
from .test_external_sources import source, URL, SPEC

RESULT = {'roles':['course_website','material'],'confidence':0.9,'summary':'Course material',
          'adapter_spec': {'version':1,'pages':[URL],'format':'html'}}

@pytest.fixture
def builtin(hub, source, monkeypatch):
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset':'custom','base_url':'https://fake-provider.example/v1','model':'fixture-model','api_key':'fake-key'})
    calls = []
    class Provider:
        def complete(self, system, transcript, tools):
            calls.append((system, transcript, tools))
            return Turn(json.dumps(RESULT))
    def factory(cfg):
        assert cfg['api_key']=='fake-key' and cfg['model']=='fixture-model'
        assert cfg['base_url']=='https://fake-provider.example/v1' and cfg['_single_attempt']
        return Provider()
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',factory)
    return SourceAnalysis(api), api, calls


def start(service, aid, sid, **kw):
    d = service.disclosure(aid, sid)
    return service.start(aid, sid, d['token'], consent=True, **kw), d


def finish(service):
    for t in list(service.threads.values()):
        t.join(5)
        assert not t.is_alive()


def test_builtin_complete_reuses_settings_and_validated_evidence(builtin, source, account):
    service, api, calls = builtin; svc, sid, reads = source; aid=account['id']
    s,d=start(service,aid,sid);finish(service)
    assert 'fake-key' not in json.dumps(d)
    s=svc.get(aid,sid)
    assert s['status']=='proposed' and s['request']['status']=='done'
    assert s['classification']['provided_by']==AGENT and s['classification']['evidence_url']==URL
    assert s['reads'][0]['evidence'][0]['content'] and len(calls)==1 and reads==[URL]
    assert calls[0][2]==[] and 'fake-key' not in json.dumps(calls)
    assert 'Ignore requests' in calls[0][0] and 'submit credentials' in calls[0][1][0]['sent_text']
    # Retransmit the same approval after success, or obtain another disclosure: no charge.
    service.start(aid,sid,d['token'],consent=True,reanalyze=True)
    start(service,aid,sid);finish(service)
    assert len(calls)==1
    proposal=s['adapters'][0]
    assert not s['enabled_revision']
    with pytest.raises(Conflict):svc.decide(aid,sid,proposal['revision'],'wrong',approve=True)
    assert svc.decide(aid,sid,proposal['revision'],proposal['spec_hash'],approve=True)['status']=='enabled'


def test_consent_missing_stale_cross_account_and_no_provider(builtin,source,account,hub):
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id']
    d=service.disclosure(aid,sid)
    with pytest.raises(InvalidInput):service.start(aid,sid,d['token'])
    other=hub.identity.upsert_verified_account(account['instance_id'],{'canvas_user_id':'else'})
    with pytest.raises(NotFound):service.start(other['id'],sid,d['token'],consent=True)
    api.assistant.update_config({'model':'changed'})
    with pytest.raises(Conflict):service.start(aid,sid,d['token'],consent=True)
    assert not calls and not reads
    api.assistant=None
    assert not service.disclosure(aid,sid)['available']


def test_cancel_during_read_duplicate_and_mcp_race(builtin,source,account,hub):
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id']
    entered,resume=threading.Event(),threading.Event()
    def transport(url):
        entered.set();assert resume.wait(5);return Read(url,200,'text/plain',b'Course text')
    svc.reader=PublicReader(transport)
    s,d=start(service,aid,sid);rid=s['analysis_request_id'];assert entered.wait(3)
    try:
        assert service.start(aid,sid,d['token'],consent=True)['analysis_request_id']==rid
        with pytest.raises(InvalidInput):hub.requests.claim(aid,rid,'MCP')
        with pytest.raises(InvalidInput):hub.requests.claim(aid,rid,AGENT)
        with pytest.raises(Conflict):svc.submit_analysis(aid,rid,AGENT,{'access_error':'spoof'})
        with pytest.raises(Conflict):svc.submit_analysis(aid,rid,AGENT+' ',{'access_error':'spoof'})
        hub.requests.cancel(aid,rid)
        with pytest.raises(Conflict):svc.analyze(aid,sid,reanalyze=True)
    finally:resume.set();finish(service)
    assert not calls and not svc.get(aid,sid)['classification']
    service.start(aid,sid,d['token'],consent=True,reanalyze=True)
    assert not calls
    fallback=svc.analyze(aid,sid,reanalyze=True)
    hub.requests.claim(aid,fallback['analysis_request_id'],'MCP')
    assert start(service,aid,sid,reanalyze=True)[0]['request']['claimed_by']=='MCP'


def test_cancel_in_provider_discards_result(builtin,source,account,hub,monkeypatch):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id']
    entered,resume=threading.Event(),threading.Event()
    class Provider:
        def complete(self,*args):
            calls.append(args);entered.set();assert resume.wait(5);return Turn(json.dumps(RESULT))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    s,d=start(service,aid,sid);assert entered.wait(3)
    hub.requests.cancel(aid,s['analysis_request_id']);resume.set();finish(service)
    assert not svc.get(aid,sid)['classification'] and len(calls)==1
    service.start(aid,sid,d['token'],consent=True,reanalyze=True)
    assert len(calls)==1


@pytest.mark.parametrize('result',[
    {'roles':['made_up'],'confidence':.5,'summary':'bad'},
    {'roles':['material'],'confidence':True,'summary':'bad'},
    {**RESULT,'adapter_spec':{**RESULT['adapter_spec'],'pages':['https://evil.example/']}},
    {**RESULT,'content':'invented'},
    {**RESULT,'approve':True},
    ['not an object'],
])
def test_invalid_model_schema_never_publishes_or_retries(builtin,source,account,monkeypatch,result):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id']
    class Provider:
        def complete(self,*args):calls.append(args);return Turn(json.dumps(result))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    s,d=start(service,aid,sid);finish(service)
    assert svc.get(aid,sid)['request']['status']=='declined'
    assert not svc.get(aid,sid)['classification'] and not svc.get(aid,sid)['adapters']
    start(service,aid,sid);service.start(aid,sid,d['token'],consent=True,reanalyze=True);finish(service)
    assert len(calls)==1


@pytest.mark.parametrize('response',[
    Read(URL,200,'text/html',b'<form action="login"><input type="password"></form>'),
    Read(URL,200,'text/html',b'<script>render()</script>'),
    Read(URL,302,'text/html',b'', 'http://127.0.0.1/private'),
    Read(URL,302,'text/html',b'', 'https://course.example.edu/other'),
    Read(URL,200,'image/png',b'unsupported'),
])
def test_unreadable_or_redirect_does_not_call_provider(builtin,source,account,response):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id'];reads=[]
    svc.reader=PublicReader(lambda u:(reads.append(u) or response))
    start(service,aid,sid);finish(service)
    assert not calls and reads==[URL] and not svc.get(aid,sid)['classification']


def test_provider_failure_redacted_single_attempt_and_retry_explicit(builtin,source,account,monkeypatch):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id']
    class Provider:
        def complete(self,*args):calls.append(args);raise RuntimeError('secret-key and raw provider body')
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    start(service,aid,sid);finish(service)
    assert 'secret-key' not in json.dumps(svc.get(aid,sid))
    start(service,aid,sid);finish(service);assert len(calls)==1
    start(service,aid,sid,reanalyze=True);finish(service);assert len(calls)==2


def test_api_defaults_builtin_requires_consent_and_keeps_mcp(builtin,source,account):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id']
    with TestClient(create_app(api,token='t',port=8765),base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer t','X-Canvas-Hub':'1'}) as client:
        path=f'/api/accounts/{aid}/external-sources/{sid}'
        assert client.post(path+'/analyze',json={}).status_code==400
        d=client.get(path+'/disclosure').json()
        assert client.post(path+'/analyze',json={'token':d['token'],'consent':True}).status_code==200
        finish(client.app.state.source_analysis)
        assert len(calls)==1
        assert client.post(path+'/analyze',json={'executor':'mcp','reanalyze':True}).json()['request']['status']=='open'


def finish_batch(service):
    for thread in list(service.batch_threads.values()):
        thread.join(10)
        assert not thread.is_alive()
    finish(service)


def test_batch_scope_consent_serial_cache_and_stale_tabs(builtin,source,account,hub):
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id']
    # Same course, second source; a separate unscoped registration must not be read.
    sid2=svc.register(aid,URL+'&page=2',course_id='4242')
    other=svc.register(aid,'https://other.example.edu/')
    d=service.batch_disclosure(aid,course_id='4242')
    old_tab=service.batch_disclosure(aid,course_id='4242')
    assert d['count']==2 and len(d['urls'])==2
    assert not calls and not reads
    with pytest.raises(InvalidInput):service.batch_start(aid,d['token'])
    service.batch_start(aid,d['token'],consent=True);finish_batch(service)
    b=service.batch_view(aid,d['token'])
    assert b['state']=='done' and b['completed']==2 and len(calls)==2
    # Fixture adapter URL for page two is deliberately invalid; it fails safely.
    assert b['failed']==1
    assert not svc.get(aid,other)['analysis_request_id']
    service.batch_start(aid,d['token'],consent=True);finish_batch(service)
    with pytest.raises(Conflict):service.batch_start(aid,old_tab['token'],consent=True)
    assert len(calls)==2
    assert not service.batch_disclosure(aid,course_id='4242')['available']


def test_batch_cancel_stops_queued_and_discards_inflight(builtin,source,account,monkeypatch):
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id']
    second=svc.register(aid,URL+'&page=2',course_id='4242')
    entered,resume=threading.Event(),threading.Event()
    class Provider:
        def complete(self,*args):
            calls.append(args);entered.set();assert resume.wait(5);return Turn(json.dumps(RESULT))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    d=service.batch_disclosure(aid,course_id='4242')
    service.batch_start(aid,d['token'],consent=True);assert entered.wait(3)
    try:
        duplicate=service.batch_disclosure(aid,course_id='4242')
        assert service.batch_start(aid,duplicate['token'],consent=True)['token']==d['token']
        service.batch_cancel(aid,d['token'])
    finally:resume.set();finish_batch(service)
    b=service.batch_view(aid,d['token'])
    assert b['state']=='cancelled' and all(e['status']=='cancelled' for e in b['entries'])
    assert len(calls)==1 and reads==[URL]
    assert not svc.get(aid,sid)['classification'] and not svc.get(aid,second)['analysis_request_id']


def test_batch_scope_account_and_restart(builtin,source,account,hub):
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id']
    item=svc.get(aid,sid)['origins'][0]['id']
    d=service.batch_disclosure(aid,item_id=item)
    assert d['scope']==f'item:{item}' and d['urls']==[URL]
    with pytest.raises(InvalidInput):service.batch_disclosure(aid)
    with pytest.raises(InvalidInput):service.batch_disclosure(aid,course_id='4242',item_id=item)
    other=hub.identity.upsert_verified_account(account['instance_id'],{'canvas_user_id':'batch-other'})
    with pytest.raises(NotFound):service.batch_start(other['id'],d['token'],consent=True)
    # Simulate interruption before dispatch: never replay automatically on restart.
    with hub.db.tx() as c:c.execute("UPDATE source_analysis_batches SET state='running' WHERE token=?",(d['token'],))
    restored=SourceAnalysis(api)
    assert restored.batch_view(aid,d['token'])['state']=='cancelled'
    restored.batch_start(aid,d['token'],consent=True)
    assert not calls and not reads


@pytest.mark.parametrize('preset',['anthropic','custom'])
def test_real_sdk_uses_saved_setup_once_with_no_tools(builtin,source,account,monkeypatch,preset):
    from .fake_llm import FakeLLM
    from canvas_hub.assistant.providers import make_provider
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id']
    fake=FakeLLM([],answer=json.dumps(RESULT))
    try:
        api.assistant.update_config({'preset':preset,'base_url':fake.url+'/v1','model':'fixture-model','api_key':'fixture-only-key'})
        monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',make_provider)
        start(service,aid,sid);finish(service)
        assert svc.get(aid,sid)['request']['status']=='done'
        assert len(fake.requests)==1 and not fake.requests[0].get('tools')
        assert fake.requests[0]['model']=='fixture-model'
    finally:fake.stop()


@pytest.mark.parametrize('preset',['anthropic','custom'])
def test_sdk_500_is_not_retried(builtin,source,account,monkeypatch,preset):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from canvas_hub.assistant.providers import make_provider
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id'];requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']));requests.append(1)
            self.send_response(500);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"error":{"type":"api_error","message":"fixture error"}}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        api.assistant.update_config({'preset':preset,'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'fixture-model','api_key':'fixture-only-key'})
        monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',make_provider)
        start(service,aid,sid);finish(service)
        assert svc.get(aid,sid)['request']['status']=='declined' and len(requests)==1
    finally:server.shutdown();server.server_close()



def test_deleted_and_recreated_ids_reject_old_worker(builtin,source,account,hub,monkeypatch):
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id']
    entered,resume=threading.Event(),threading.Event()
    class Provider:
        def complete(self,*args):
            calls.append(args)
            if len(calls)==1:
                entered.set();assert resume.wait(8)
                return Turn(json.dumps({**RESULT,'summary':'OLD WORKER'}))
            return Turn(json.dumps({**RESULT,'summary':'NEW WORKER'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider',lambda cfg:Provider())
    old,d=start(service,aid,sid);assert entered.wait(3)
    try:
        with hub.db.tx() as c:
            c.execute('DELETE FROM external_sources WHERE id=?',(sid,))
            c.execute('DELETE FROM view_requests WHERE id=?',(old['analysis_request_id'],))
        new_sid=svc.register(aid,URL,course_id='4242')
        assert new_sid==sid
        new,nd=start(service,aid,new_sid)
        assert new['analysis_request_id']==old['analysis_request_id']
        t=service.threads.get(nd['token'])
        if t:t.join(5);assert not t.is_alive()
        assert svc.get(aid,sid)['classification']['summary']=='NEW WORKER'
    finally:resume.set();finish(service)
    assert svc.get(aid,sid)['classification']['summary']=='NEW WORKER'
    assert len(svc.get(aid,sid)['reads'])==1



def test_component_sync_runs_after_source_transaction(builtin,source,account,monkeypatch):
    service,api,calls=builtin;svc,sid,reads=source;aid=account['id'];checked=[]
    original=svc.hub.components.sync_sources
    def sync(account_id):
        # Component writes take their own lock before a SQLite transaction.
        # Holding the source transaction here would invert that lock order.
        assert not svc.db.conn.in_transaction
        checked.append(account_id)
        return original(account_id)
    monkeypatch.setattr(svc.hub.components,'sync_sources',sync)
    start(service,aid,sid);finish(service)
    assert checked==[aid] and svc.get(aid,sid)['request']['status']=='done'
