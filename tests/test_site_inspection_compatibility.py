"""Small realistic fixtures: no real accounts, network transports or model calls."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from canvas_hub.services import login_detector
from canvas_hub.services.connected_content import MAX_TEXT, TRUNCATION_MARKER, sanitize_connected_text
from canvas_hub.services.site_connection import BrowserConnection, SiteConnections
from canvas_hub.services.site_policy import auth_action, document_policy
from canvas_hub.util import Conflict
from .test_external_sources import source
from .test_site_connection import wait
from .test_site_redirects import Route, TARGET, browser_session, routed_session

pytestmark = pytest.mark.usefixtures('external_inspection_enabled')
FIXTURES = Path(__file__).parent / 'fixtures' / 'site_inspection'


@pytest.mark.browser
@pytest.mark.parametrize('provider,site,label', [
    ('microsoft', 'https://login.microsoftonline.com', 'Microsoft account lookup'),
    ('piazza', 'https://piazza.com', 'Piazza sign-in'),
])
def test_declared_login_resources_anonymous_detection_and_exact_approved_post(tmp_path, monkeypatch, provider, site, label):
    markup = (FIXTURES / (provider + '.html')).read_bytes()
    target = site + '/home'
    calls = []
    def transport(url, method='GET', headers=None, body=None):
        calls.append((url, method, headers or {}, body))
        if url == target:
            return 200, {'content-type': 'text/html'}, [], markup
        if method == 'POST':
            return 200, {'content-type': 'application/json'}, [], b'{}'
        if url.endswith('.css'):
            return 200, {'content-type': 'text/css'}, [], b'#login {display:block}'
        if url.endswith('.js'):
            if not url.startswith(site + '/'):
                assert not {'cookie', 'authorization', 'referer', 'origin', 'x-csrf-token'} & headers.keys()
            return 200, {'content-type': 'text/javascript'}, ['cdn=must-not-be-saved; Secure'], b'window.fixtureCDN=true;'
        raise AssertionError('Unexpected synthetic transport request')

    def anonymous(url):
        status, headers, _, body = transport(url, headers={})
        return status, headers, body
    monkeypatch.setattr(login_detector, 'fetch', anonymous)
    result = login_detector._inspect(target)
    assert result['outcome'] == 'login_required' and result['course_access'] == 'unknown'
    assert any('cloudfront.net' in u or 'aadcdn.msauth.net' in u for u, _, _, _ in calls)
    assert not any(method == 'POST' for _, method, _, _ in calls)
    assert all(d['reason'] == 'anonymous_request_scope' for d in result['diagnostics'])
    assert 'private-' not in json.dumps(result)

    calls.clear()
    base = tmp_path / 'profiles'; base.mkdir()
    session = BrowserConnection(base/'external-sites'/'account-1'/'profile', target, {site}, transport, headless=True)
    try:
        state = wait(session, lambda s: bool(s['pending_post']))
        assert state['pending_post']['endpoint'] == label
        assert not any(method == 'POST' for _, method, _, _ in calls)
        approved = state['pending_post']
        session.call('approve_post', approved)
        with pytest.raises(Conflict):
            session.call('approve_post', approved)
        posts = [(u, b) for u, method, _, b in calls if method == 'POST']
        assert len(posts) == 1 and b'private-' in posts[0][1]
        assert 'instrumentation' not in posts[0][0] and 'generic.report_js_error' not in posts[0][0]
        state = session.snapshot()
        assert any(d['reason'] == 'unverified_submission' for d in state['diagnostics'])
        assert any('telemetry' in d['endpoint'] for d in state['diagnostics'])
        assert any(d['reason'] == 'approved_once' for d in state['diagnostics'])
        assert 'private-' not in json.dumps(state) and '?' not in json.dumps(state['diagnostics'])
    finally:
        session.call('cancel')
        assert not session.thread.is_alive()


def test_static_scope_mime_redirect_and_credentials_are_enforced():
    site = 'https://login.microsoftonline.com'
    cdn = 'https://aadcdn.msauth.net'
    url = cdn + '/shared/1.0/content/js/login.fixture.js'
    calls, cookies = [], []
    response = [200, {'content-type': 'text/javascript'}]
    def transport(target, method, headers, body):
        calls.append((target, method, headers, body))
        return *response, ['session=private; Secure'], b'/* fixture */'
    session = routed_session(transport)
    session.domains = {site, cdn, 'https://other.cloudfront.net'}
    document = site + '/common/login'
    session._policies = {document: document_policy(document, f'<script src="{url}"></script>')}
    context = SimpleNamespace(add_cookies=cookies.extend)
    def route(target=url, kind='script'):
        r = Route(target, kind=kind); r.request.frame.url = document
        session._route(r, context)
        return r
    assert route().fulfilled
    assert not calls[0][2] and not cookies
    for target, kind in [(url+'?token=private', 'script'), (url.replace('login.fixture', 'undeclared'), 'script'),
                         (url.replace('aadcdn.msauth.net', 'other.cloudfront.net'), 'script'), (url, 'fetch')]:
        assert route(target, kind).aborted
    assert len(calls) == 1
    response[:] = [302, {'location': 'https://127.0.0.1/private'}]
    assert route().aborted and not cookies
    response[:] = [200, {'content-type': 'text/html'}]
    assert route().aborted and not cookies
    assert 'private' not in json.dumps(session.snapshot())


def test_auth_policy_rejects_telemetry_mutations_and_mixed_piazza_payloads():
    ms = 'https://login.microsoftonline.com/common/login'
    policy = document_policy(ms, (FIXTURES/'microsoft.html').read_bytes())
    assert auth_action(policy, 'https://login.microsoftonline.com/common/GetCredentialType?mkt=en-US', 'fetch', 'application/json')
    assert not auth_action(policy, ms+'/telemetry', 'fetch', 'application/json')
    assert not auth_action(policy, ms, 'fetch', 'application/json')
    policy = document_policy('https://piazza.com/home', (FIXTURES/'piazza.html').read_bytes())
    url = 'https://piazza.com/logic/api?method=user.login&aid=fixture'
    body = {'method':'user.login', 'params':{'email':'fixture@example.edu','pass':'fixture'}}
    assert auth_action(policy, url, 'xhr', 'application/json', json.dumps(body).encode())
    for changed in ({**body, 'logdata':{'event':'telemetry'}}, {**body, 'method':'user.delete'},
                    {**body, 'params':{**body['params'], 'action':'delete'}}):
        assert not auth_action(policy, url, 'xhr', 'application/json', json.dumps(changed).encode())
    assert not auth_action(policy, url+'&method=user.login', 'xhr', 'application/json', json.dumps(body).encode())


def test_pending_submission_is_bound_to_original_request_and_expires():
    calls = []
    context = SimpleNamespace(add_cookies=lambda _:None)
    session = routed_session(lambda *args: calls.append(args) or (200, {}, [], b'ok'))
    for change in ('body', 'replacement', 'expiry', 'cancel'):
        route = Route(method='POST')
        session._route(route, context)
        approval = session._post_pending[0]
        if change == 'body': route.request.post_data_buffer = b'password=changed'
        if change == 'expiry': session._post_deadline = time.monotonic()-1
        if change == 'cancel': session._discard_post()
        candidate = Route(method='POST') if change == 'replacement' else route
        session._route(candidate, context, approval=approval)
        assert candidate.aborted and route.aborted and not calls
        assert session.snapshot()['pending_post'] is None
    assert 'password' not in json.dumps(session.snapshot())


@pytest.mark.browser
def test_close_waits_for_browser_shutdown_and_allows_immediate_reopen(hub, account, source, monkeypatch):
    from playwright.sync_api import BrowserContext
    entered, release = threading.Event(), threading.Event()
    original = BrowserContext.close
    def delayed_close(context, *args, **kwargs):
        entered.set(); assert release.wait(5)
        return original(context, *args, **kwargs)
    monkeypatch.setattr(BrowserContext, 'close', delayed_close)
    def factory(profile, target, domains):
        return BrowserConnection(profile, target, domains, lambda *a: (200, {'content-type':'text/html'}, [], b'<main>fixture</main>'), headless=True)
    service = SiteConnections(hub, browser_factory=factory)
    aid, sid = account['id'], source[1]
    offer = service.offer(aid, sid, [])
    service.start(aid, sid, offer['token'], True)
    session = service._session(aid, sid)
    wait(session, lambda s:s['state']=='login_open')
    next_offer = service.offer(aid, sid, [])
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            closing = executor.submit(service.action, aid, sid, 'cancel')
            assert entered.wait(3)
            assert service.status(aid, sid)['state'] == 'closing' and not closing.done()
            with pytest.raises(Conflict): service.start(aid, sid, next_offer['token'], True)
            release.set()
            assert closing.result(timeout=5)['state'] == 'closed' and not session.thread.is_alive()
        service.start(aid, sid, next_offer['token'], True)
        wait(service._session(aid, sid), lambda s:s['state']=='login_open')
    finally:
        release.set(); service.close()


@pytest.mark.browser
def test_snapshot_folds_html_padding_preserves_code_and_marks_both_limits(tmp_path):
    padding = '\n' + ' ' * 82295 + '\n'
    course = 'Course lectures and assignment dates. ' * 20
    markup = [f'<main>{padding}<p>{course}</p><pre>if ready:\n    <span>print("lesson")</span>\n\tfinish()</pre><p>Meaningful final paragraph.</p></main>'.encode()]
    with browser_session(tmp_path, lambda *a:(200, {'content-type':'text/html'}, [], markup[0])) as session:
        result = session.call('verify')
        text = sanitize_connected_text(result['content'])
        assert len(text) < 1500 and 'Meaningful final paragraph.' in text
        assert 'if ready:\n    print("lesson")\n\tfinish()' in text
        assert TRUNCATION_MARKER not in text
        markup[0] = ('<main>' + course * 350 + '</main>').encode()
        result = session.call('verify')
        assert result['content'].endswith(TRUNCATION_MARKER) and len(result['content']) <= 200000
        text = sanitize_connected_text(result['content'])
        assert len(text) <= MAX_TEXT and text.endswith(TRUNCATION_MARKER)
        assert sanitize_connected_text(course+'\n   \n'*20000+'tail').endswith('tail')
        markup[0] = ('<main><p>'+course+'</p>'+'<span> </span>'*20010+'<p>Beyond node budget</p></main>').encode()
        limited = session.call('verify')['content']
        assert limited.endswith(TRUNCATION_MARKER) and 'Beyond node budget' not in limited


@pytest.mark.browser
def test_dynamic_content_is_explicitly_unverified_without_running_scripts_on_inspection(tmp_path):
    calls, rendered = [], threading.Event()
    course = 'Rendered course notes and assignment details. ' * 15
    def transport(url, *args):
        calls.append(url)
        if url.endswith('/rendered'):
            rendered.set(); return 200, {'content-type':'text/plain'}, [], b'ok'
        return 200, {'content-type':'text/html'}, [], ('<main id="root"></main><script>document.querySelector("#root").textContent='+json.dumps(course)+';fetch("/rendered")</script>').encode()
    with browser_session(tmp_path, transport) as session:
        assert rendered.wait(5)
        before = len(calls)
        result = session.call('verify')
        assert result['state'] == 'access_unverified' and 'content' not in result
        assert calls[before:] == [TARGET]
        assert session.snapshot()['extraction_limitation'] == 'dynamic_content_not_verified'
