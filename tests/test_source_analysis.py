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
from .test_codex import fake_codex

RESULT = {'roles':['course_website','material'],'confidence':0.9,'summary':'Course material',
          'adapter_spec': {'version':1,'pages':[URL],'format':'html'}}


def test_analysis_admission_rejects_before_consuming_disclosure(hub, source, account):
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset': 'codex'})
    service = SourceAnalysis(api)
    _, sid, _ = source
    offer = service.disclosure(account['id'], sid)
    for _ in range(8):
        assert service._admission.acquire(blocking=False)
    try:
        with pytest.raises(Conflict) as exc:
            service.start(account['id'], sid, offer['token'], consent=True)
        assert exc.value.code == 'source_analysis_busy'
        assert hub.db.one('SELECT state FROM source_analysis_attempts WHERE token=?', (offer['token'],))['state'] == 'offered'
    finally:
        for _ in range(8):
            service._admission.release()
    # Early validation errors also return the reservation.
    with pytest.raises(InvalidInput):
        service.start(account['id'], sid, offer['token'], consent=False)
    for _ in range(8):
        assert service._admission.acquire(blocking=False)
    for _ in range(8):
        service._admission.release()


def test_waiting_analysis_cancels_without_waiting_for_provider_slot(hub, source, account):
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset': 'codex'})
    service = SourceAnalysis(api)
    sources, sid, reads = source
    offer = service.disclosure(account['id'], sid)
    assert service._slots.acquire(blocking=False)
    assert service._slots.acquire(blocking=False)
    try:
        service.start(account['id'], sid, offer['token'], consent=True)
        thread = service.threads[offer['token']]
        rid = sources.get(account['id'], sid)['analysis_request_id']
        hub.requests.cancel(account['id'], rid)
        thread.join(3)
        assert not thread.is_alive()
        assert not reads
    finally:
        service._slots.release()
        service._slots.release()


def test_batch_admission_preserves_offer_when_full(hub, source, account):
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset': 'codex'})
    service = SourceAnalysis(api)
    offer = service.batch_disclosure(account['id'], course_id='4242')
    for _ in range(4):
        assert service._batch_admission.acquire(blocking=False)
    try:
        with pytest.raises(Conflict) as exc:
            service.batch_start(account['id'], offer['token'], consent=True)
        assert exc.value.code == 'source_analysis_busy'
        assert hub.db.one('SELECT state FROM source_analysis_batches WHERE token=?', (offer['token'],))['state'] == 'offered'
    finally:
        for _ in range(4):
            service._batch_admission.release()


@pytest.mark.parametrize('batch', [False, True])
def test_codex_analysis_cancel_closes_process_without_retry(hub, source, account, fake_codex, monkeypatch, batch):
    from pathlib import Path
    from canvas_hub.assistant.codex import CodexProvider
    setup, messages = fake_codex
    setup(hang=True)
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset': 'codex', 'model': 'chosen-model'})
    service = SourceAnalysis(api)
    svc, sid, reads = source
    aid = account['id']
    providers = []
    def factory(cfg):
        provider = CodexProvider(cfg)
        providers.append(provider)
        return provider
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', factory)
    if batch:
        disclosure = service.batch_disclosure(aid, course_id='4242')
        service.batch_start(aid, disclosure['token'], consent=True)
    else:
        disclosure = service.disclosure(aid, sid)
        service.start(aid, sid, disclosure['token'], consent=True)
    try:
        deadline = time.monotonic() + 10
        while not any(m.get('method') == 'turn/start' for m in messages()):
            assert time.monotonic() < deadline
            time.sleep(.02)
        thread_config = next(m['params'] for m in messages() if m.get('method') == 'thread/start')
        assert thread_config['model'] == 'chosen-model'
        if batch:
            service.batch_cancel(aid, disclosure['token'])
        else:
            hub.requests.cancel(aid, svc.get(aid, sid)['analysis_request_id'])
        finish(service)
        for thread in list(service.batch_threads.values()):
            thread.join(5)
            assert not thread.is_alive()
        assert svc.get(aid, sid)['request']['status'] == 'cancelled'
        assert svc.get(aid, sid)['classification'] is None
        assert providers[0].server.process.poll() is not None
        assert not Path(providers[0].server.workspace.name).exists()
        assert len([m for m in messages() if m.get('method') == 'turn/start']) == 1
        assert reads == [URL]
    finally:
        for provider in providers:
            provider.close()


@pytest.mark.parametrize('plan', [
    {'signed_out': True}, {'turn_error': True}, {'reply': 'private-error-sentinel'},
    {'calls': [{'name': 'run_shell', 'arguments': {'command': 'private-error-sentinel'}}]},
])
def test_codex_analysis_failure_is_redacted_and_never_executes_tools(hub, source, account, fake_codex, plan):
    setup, messages = fake_codex
    setup(**plan)
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset': 'codex', 'model': ''})
    service = SourceAnalysis(api)
    svc, sid, reads = source
    aid = account['id']
    disclosure = service.disclosure(aid, sid)
    service.start(aid, sid, disclosure['token'], consent=True)
    finish(service)
    result = svc.get(aid, sid)
    assert result['request']['status'] == 'declined' and result['classification'] is None
    assert 'private-error-sentinel' not in json.dumps(result)
    assert not any(m.get('id') == 1000 and 'result' in m for m in messages())
    service.start(aid, sid, disclosure['token'], consent=True)
    assert len([m for m in messages() if m.get('method') == 'turn/start']) <= 1


def test_codex_source_analysis_discloses_then_uses_sandboxed_cli(hub, source, account, fake_codex):
    setup, messages = fake_codex
    setup(reply=json.dumps(RESULT))
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset': 'codex', 'model': '', 'name': 'Codex fixture'})
    service = SourceAnalysis(api)
    svc, sid, reads = source
    aid = account['id']

    disclosure = service.disclosure(aid, sid)
    assert disclosure['available'] and disclosure['provider'] == 'codex'
    assert disclosure['endpoint'] == 'Codex CLI (OpenAI)'
    assert disclosure['model'] == 'Provider default'
    assert not messages() and not reads
    with pytest.raises(InvalidInput):
        service.start(aid, sid, disclosure['token'])
    assert not messages() and not reads

    service.start(aid, sid, disclosure['token'], consent=True)
    finish(service)
    assert svc.get(aid, sid)['request']['status'] == 'done'
    assert svc.get(aid, sid)['classification']['evidence_url'] == URL
    assert reads == [URL]
    requests = {m['method']: m.get('params') for m in messages() if 'method' in m}
    thread = requests['thread/start']
    assert thread['ephemeral'] and thread['sandbox'] == 'read-only'
    assert thread['dynamicTools'] == [] and thread['config']['mcp_servers'] == {}
    assert thread['config']['web_search'] == 'disabled'
    assert thread['config']['features.shell_tool'] is False


def test_codex_batch_discloses_and_requires_consent(hub, source, account, fake_codex):
    setup, messages = fake_codex
    setup(reply=json.dumps(RESULT))
    api = HubAPI(hub)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({'preset': 'codex', 'model': ''})
    service = SourceAnalysis(api)
    svc, sid, reads = source
    aid = account['id']
    disclosure = service.batch_disclosure(aid, course_id='4242')
    assert disclosure['available'] and disclosure['count'] == 1
    assert disclosure['endpoint'] == 'Codex CLI (OpenAI)' and disclosure['model'] == 'Provider default'
    with pytest.raises(InvalidInput):
        service.batch_start(aid, disclosure['token'])
    assert not reads and not messages()
    service.batch_start(aid, disclosure['token'], consent=True)
    for thread in list(service.batch_threads.values()):
        thread.join(10)
        assert not thread.is_alive()
    assert service.batch_view(aid, disclosure['token'])['state'] == 'done'
    assert svc.get(aid, sid)['request']['status'] == 'done' and reads == [URL]

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


@pytest.mark.parametrize('response,status', [
    (Read(URL,401,'text/html',b'',auth_challenge=True),'needs_login'),
    (Read(URL,401,'text/html',b''),'needs_attention'),
    (Read(URL,403,'text/html',b''),'needs_attention'),
    (Read(URL,503,'text/html',b''),'stale'),
])
def test_reader_failures_are_distinguished_without_model(builtin,source,account,response,status):
    service,api,calls=builtin;svc,sid,_=source;aid=account['id']
    svc.reader=PublicReader(lambda url:response)
    start(service,aid,sid);finish(service)
    assert svc.get(aid,sid)['status']==status
    assert not calls and not svc.get(aid,sid)['classification']


@pytest.mark.parametrize('response', [
    Read(URL,401,'text/html',b'',auth_challenge=True),
    Read(URL,403,'text/html',b''),
    Read(URL,302,'text/html',b'', 'https://identity.example/login'),
    Read(URL,200,'text/html',b'<script>renderCourse()</script>'),
])
def test_failed_analysis_never_calls_the_model_or_retries_by_itself(builtin, source, account, response):
    service, _, calls = builtin
    sources, sid, _ = source
    aid = account['id']
    reads = []
    sources.reader = PublicReader(lambda url: reads.append(url) or response)
    _, disclosure = start(service, aid, sid)
    finish(service)
    assert sources.get(aid, sid)['request']['status'] == 'declined' and not calls
    # A consumed disclosure never dispatches again, and nothing else re-reads the site.
    service.start(aid, sid, disclosure['token'], consent=True)
    finish(service)
    assert len(reads) == 1 and not calls


def test_enrollment_chooser_with_password_is_not_assumed_to_require_login(builtin, source, account):
    service, _, calls = builtin
    sources, sid, _ = source
    sources.reader = PublicReader(lambda url: Read(URL, 200, 'text/html', b'<button>Join Classes</button><form><input type=password><button>Log in</button></form>'))
    start(service, account['id'], sid); finish(service)
    assert sources.get(account['id'], sid)['status'] == 'needs_attention'
    assert not calls


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
    retry = service.batch_disclosure(aid,course_id='4242')
    assert retry['available'] and retry['count'] == 1 and retry['new_count'] == 0 and retry['retry_count'] == 1
    assert retry['urls'] == [URL+'&page=2']


def test_batch_explicit_retry_preserves_success_on_failure_and_can_reinspect(builtin, source, account, monkeypatch):
    service, api, calls = builtin; svc, sid, reads = source; aid = account['id']
    start(service, aid, sid); finish(service)
    original = svc.get(aid, sid)
    assert original['request']['status'] == 'done' and original['classification']
    class Failure:
        def complete(self, *args):
            calls.append(args)
            raise RuntimeError('temporary failure')
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Failure())
    start(service, aid, sid, reanalyze=True); finish(service)
    failed = svc.get(aid, sid)
    assert failed['request']['status'] == 'declined'
    assert failed['classification'] == original['classification']
    assert failed['adapters'] == original['adapters'] and failed['reads'] == original['reads']
    offer = service.batch_disclosure(aid, course_id='4242')
    assert offer['available'] and offer['retry_count'] == 1 and offer['new_count'] == 0
    assert len(calls) == 2  # disclosure alone never sends another request
    class Success:
        def complete(self, *args):
            calls.append(args)
            return Turn(json.dumps({**RESULT, 'summary': 'Updated after explicit retry'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Success())
    service.batch_start(aid, offer['token'], consent=True); finish_batch(service)
    updated = svc.get(aid, sid)
    assert updated['request']['status'] == 'done'
    assert updated['classification']['summary'] == 'Updated after explicit retry'
    assert len(calls) == 3 and service.batch_view(aid, offer['token'])['failed'] == 0
    assert not service.batch_disclosure(aid, course_id='4242')['available']


def test_reinspection_does_not_dispatch_duplicate_concurrent_attempts(builtin, source, account, monkeypatch):
    service, api, calls = builtin; svc, sid, reads = source; aid = account['id']
    start(service, aid, sid); finish(service)
    first = service.disclosure(aid, sid)
    stale = service.disclosure(aid, sid)
    entered, release = threading.Event(), threading.Event()
    class Blocking:
        def complete(self, *args):
            calls.append(args); entered.set(); assert release.wait(5)
            return Turn(json.dumps(RESULT))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: Blocking())
    service.start(aid, sid, first['token'], consent=True, reanalyze=True)
    try:
        assert entered.wait(3)
        with pytest.raises(Conflict):
            service.start(aid, sid, stale['token'], consent=True, reanalyze=True)
        assert len(calls) == 2 and svc.get(aid, sid)['analysis_running']
    finally:
        release.set(); finish(service)
    assert svc.get(aid, sid)['request']['status'] == 'done' and len(calls) == 2


def test_reinspection_access_failure_keeps_previous_classification(builtin, source, account, monkeypatch):
    service, api, calls = builtin; svc, sid, reads = source; aid = account['id']
    start(service, aid, sid); finish(service)
    before = svc.get(aid, sid)
    class AccessFailure:
        def complete(self, *args):
            return Turn(json.dumps({'access_error': 'Page is temporarily unavailable'}))
    monkeypatch.setattr('canvas_hub.services.source_analysis.make_provider', lambda cfg: AccessFailure())
    start(service, aid, sid, reanalyze=True); finish(service)
    after = svc.get(aid, sid)
    assert after['request']['status'] == 'declined' and after['status'] == 'needs_attention'
    assert after['classification'] == before['classification'] and after['reads'] == before['reads']
    assert service.batch_disclosure(aid, course_id='4242')['retry_count'] == 1


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
