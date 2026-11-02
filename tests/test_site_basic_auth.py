"""Basic authentication stays in the local transport, using synthetic pages only."""
import base64
import json
import threading
from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from canvas_hub.boundary import HubAPI
from canvas_hub.services.site_connection import SiteConnections
from canvas_hub.util import Conflict, InvalidInput
from canvas_hub.web.app import create_app
from .test_external_sources import source
from .test_site_connection import FakeBrowser, wait
from .test_site_redirects import browser_session, COURSE, IDENTITY, TARGET

USERNAME = 'synthetic-user-491'
PASSWORD = 'synthetic-pass-491'
AUTH = 'Basic ' + base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()
CONTENT = b'<body><table><tr><td>' + b'Course lectures and assignment information. ' * 12 + b'</td></tr></table></body>'


@pytest.mark.browser
def test_basic_challenge_secure_entry_legacy_page_expiry_and_cleanup(tmp_path):
    calls, expired = [], [False]
    def transport(url, method, headers, body):
        calls.append((url, method, dict(headers)))
        if headers.get('authorization') == AUTH and not expired[0]:
            return 200, {'content-type':'text/html'}, [], CONTENT.replace(b'</body>', f'<p>{PASSWORD}</p></body>'.encode())
        return 401, {'content-type':'text/html', 'www-authenticate':'Basic realm="never-disclose-realm"'}, [], b'Authentication required'
    with browser_session(tmp_path, transport) as session:
        challenge = session.snapshot()['http_auth']
        assert challenge['scheme'] == 'basic'
        assert 'never-disclose-realm' not in json.dumps(session.snapshot())
        with pytest.raises(Conflict):
            session.call('http_auth', {'challenge':{**challenge, 'request_id':'stale'}, 'username':USERNAME, 'password':PASSWORD})
        assert len(calls) == 1
        assert session.call('http_auth', {'challenge':challenge, 'username':USERNAME, 'password':PASSWORD})['http_auth'] is None
        result = session.call('verify')
        assert result['state'] == 'content_visible'  # Older HTML has no main/article.
        assert PASSWORD not in json.dumps(result) and '[credential removed]' in result['content']
        session.call('confirm_content', result['review_token'])
        assert PASSWORD not in session.call('analysis_snapshot')['content']
        assert all(url == TARGET and method == 'GET' for url, method, _ in calls)
        assert 'authorization' not in calls[0][2]
        assert all(h.get('authorization') == AUTH for _, _, h in calls[1:])
        assert USERNAME not in json.dumps(session.snapshot()) and PASSWORD not in json.dumps(session.snapshot())
        expired[0] = True
        with pytest.raises(Conflict): session.call('analysis_snapshot')
        assert session.snapshot()['state'] == 'reconnect_needed'
        assert not session._basic_auth
    assert not session._credential_redactions and not session._basic_auth


@pytest.mark.browser
def test_basic_credentials_never_follow_redirects_or_assets(tmp_path):
    calls = []
    def transport(url, method, headers, body):
        calls.append((url, dict(headers)))
        if url == TARGET:
            if headers.get('authorization') == AUTH:
                return 302, {'location':IDENTITY + '/login'}, [], b''
            return 401, {'www-authenticate':'Basic realm="fixture"'}, [], b''
        assert 'authorization' not in headers
        if url == IDENTITY + '/login':
            return 200, {'content-type':'text/html'}, [], b'<script src="/asset.js"></script><p>Login</p>'
        return 200, {'content-type':'text/javascript'}, [], b''
    with browser_session(tmp_path, transport) as session:
        session.call('http_auth', {'challenge':session.snapshot()['http_auth'], 'username':USERNAME, 'password':PASSWORD})
        assert len(calls) >= 3
        assert all('authorization' not in headers for url, headers in calls if url != TARGET)
        assert not session.snapshot()['redirect_blocked']


@pytest.mark.browser
@pytest.mark.parametrize('status,headers,expected', [(401, {}, 'access_denied'), (403, {}, 'access_denied'),
    (401, {'www-authenticate':'Digest realm="fixture"'}, 'reconnect_needed')])
def test_denial_and_unsupported_authentication_are_distinct(tmp_path, status, headers, expected):
    with browser_session(tmp_path, lambda *a: (status, headers, [], b'')) as session:
        result = session.call('verify')
        assert result['state'] == expected
        if headers:
            assert session.snapshot()['http_auth']['scheme'] == 'unsupported'
            with pytest.raises(Conflict):
                session.call('http_auth', {'challenge':session.snapshot()['http_auth'], 'username':USERNAME, 'password':PASSWORD})
        else:
            assert session.snapshot()['http_auth'] is None


@pytest.mark.browser
def test_blocked_sso_popup_is_reported_without_sending_its_request(tmp_path):
    calls = []
    def transport(url, *args):
        calls.append(url)
        return 200, {'content-type':'text/html'}, [], b'<script>window.open("https://id.example.edu/login?token=private-fixture")</script>'
    with browser_session(tmp_path, transport) as session:
        state = wait(session, lambda s: s['popup_blocked'])
        assert calls == [TARGET]
        assert 'private-fixture' not in json.dumps(state)


def test_http_source_requires_review_of_separate_https_source(hub, account, source):
    sources, _, _ = source
    aid = account['id']
    sid = sources.register(aid, 'http://course.example.edu/legacy/', course_id='4242')
    service = SiteConnections(hub, browser_factory=FakeBrowser)
    with pytest.raises(InvalidInput): service.https_source(aid, sid, False)
    with pytest.raises(InvalidInput): service.offer(aid, sid, [])
    result = service.https_source(aid, sid, True)
    assert result['url'] == 'https://course.example.edu/legacy/' and result['id'] != sid
    assert sources.get(aid, sid)['url'] == 'http://course.example.edu/legacy/'
    assert service.status(aid, result['id'])['state'] == 'not_connected'
    assert not (hub.paths.browser_profiles / 'external-sites').exists()
    assert service.https_source(aid, sid, True)['id'] == result['id']


def test_local_credential_endpoint_requires_session_and_origin_protection(hub, account, source):
    _, sid, _ = source
    app = create_app(HubAPI(hub), token='fixture', port=8765)
    path = f'/api/accounts/{account["id"]}/external-sources/{sid}/site-connection/http_auth'
    payload = {'request':{'username':USERNAME, 'password':PASSWORD, 'challenge':{}}}
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.post(path, json=payload).status_code == 401
        client.headers.update({'Authorization':'Bearer fixture'})
        assert client.post(path, json=payload).status_code == 403
        client.headers.update({'X-Canvas-Hub':'1', 'Origin': 'https://unapproved.example'})
        assert client.post(path, json=payload).status_code == 403
        client.headers.pop('Origin')
        result = client.post(path, json=payload)
        assert result.status_code == 409
        assert PASSWORD not in result.text and USERNAME not in result.text


@pytest.mark.browser
def test_close_discards_queued_credentials_without_sending_them(tmp_path):
    calls, entered, release = [], threading.Event(), threading.Event()
    def transport(url, *args):
        calls.append(url); entered.set(); assert release.wait(5)
        return 401, {'www-authenticate':'Basic realm="fixture"'}, [], b''
    base = tmp_path/'browser-profiles'; base.mkdir()
    from canvas_hub.services.site_connection import BrowserConnection
    session = BrowserConnection(base/'external-sites'/'account-1'/'profile', TARGET, {COURSE}, transport, headless=True)
    try:
        assert entered.wait(5)
        session._cancel_requested.set()
        session.events.put(('cancel', None, Future()))
        pending = {'username':USERNAME, 'password':PASSWORD, 'challenge':{}}
        future = Future()
        session.events.put(('http_auth', pending, future))
    finally:
        release.set(); session.thread.join(5)
    assert not session.thread.is_alive() and pending == {}
    assert isinstance(future.exception(), Conflict)
    assert calls == [TARGET] and not session._basic_auth
