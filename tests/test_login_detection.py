"""Anonymous source detection uses synthetic responses only."""
import threading
import time

import pytest

# Exercise dormant connection logic explicitly; shipping default stays frozen.
pytestmark = pytest.mark.usefixtures("external_inspection_enabled")
from fastapi.testclient import TestClient

from canvas_hub.boundary import HubAPI
from canvas_hub.services.login_detection import LoginDetection
from canvas_hub.util import Conflict, NotFound
from canvas_hub.web.app import create_app
from .test_external_sources import source


def wait(service, aid, sid):
    for _ in range(200):
        state = service.get(aid, sid)
        if state['state'] != 'running':
            return state
        time.sleep(.01)
    raise AssertionError('worker did not finish')


def test_explicit_request_account_scope_and_no_sensitive_output(hub, account, source):
    svc, sid, _ = source
    calls = []
    def detector(url):
        calls.append(url)
        return {'outcome':'login_required', 'final_host':'login.example', 'course_access':'unknown',
                'evidence':['http_authentication_challenge'], 'raw_page':'secret', 'captured_png':b'secret'}
    service=LoginDetection(hub, detector)
    aid=account['id']
    assert service.get(aid,sid)=={'state':'not_checked'} and not calls
    service.start(aid,sid)
    result=wait(service,aid,sid)
    assert len(calls)==1 and result['outcome']=='login_required'
    assert 'raw_page' not in result and 'captured_png' not in result
    with pytest.raises(NotFound): service.get(aid+9999,sid)


def test_duplicate_check_is_rejected_and_errors_are_redacted(hub, account, source):
    svc,sid,_=source; aid=account['id']; entered=threading.Event(); release=threading.Event()
    def detector(url):
        entered.set(); release.wait(5); raise RuntimeError('secret')
    service=LoginDetection(hub,detector)
    service.start(aid,sid); assert entered.wait(2)
    with pytest.raises(Conflict): service.start(aid,sid)
    release.set()
    assert wait(service,aid,sid)=={'state':'finished','outcome':'unsupported_unavailable','evidence':['probe_error'],'course_access':'unknown'}


def test_http_endpoint_is_explicit_and_sanitized(hub,account,source):
    svc,sid,_=source; aid=account['id']; app=create_app(HubAPI(hub),token='fixture',port=8765)
    app.state.login_detection.detector=lambda url:{'outcome':'unknown','evidence':['login_or_enrollment_shell'],'course_access':'unknown','secret':'no'}
    with TestClient(app,base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer fixture','X-Canvas-Hub':'1'}) as client:
        path=f'/api/accounts/{aid}/external-sources/{sid}/login-check'
        assert client.get(path).json()=={'state':'not_checked'}
        assert client.post(path,json={}).json()['state']=='running'
        assert wait(app.state.login_detection,aid,sid)['outcome']=='unknown'
        assert 'secret' not in client.get(path).json()


def test_automatic_failure_checks_deduplicate_and_do_not_queue_when_busy(hub, account, source):
    _, sid, _ = source
    aid = account['id']
    calls = []
    service = LoginDetection(hub, lambda url: calls.append(url) or {'outcome':'unknown'})
    service.start(aid, sid, failure_id=12)
    assert wait(service, aid, sid)['failure_id'] == 12
    service.start(aid, sid, failure_id=12)
    assert len(calls) == 1
    assert service._slots.acquire(False) and service._slots.acquire(False)
    try:
        assert service.start(aid, sid, failure_id=13)['evidence'] == ['probe_busy']
        service.start(aid, sid, failure_id=13)
        assert len(calls) == 1
    finally:
        service._slots.release(); service._slots.release()
    service.start(aid, sid)  # A manual retry remains available.
    wait(service, aid, sid)
    service.start(aid, sid, failure_id=13)
    assert len(calls) == 2
