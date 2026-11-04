"""Redirect safety regressions: synthetic pages and an isolated loopback sink."""
import threading
import socket
import time
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

# Exercise dormant connection logic explicitly; shipping default stays frozen.
pytestmark = pytest.mark.usefixtures("external_inspection_enabled")

from canvas_hub.services import site_connection as sc
from canvas_hub.services.site_connection import BrowserConnection
from .test_site_connection import wait

COURSE = 'https://course.example.edu'
IDENTITY = 'https://id.example.edu'
TARGET = COURSE + '/course'


@contextmanager
def browser_session(tmp_path, transport):
    base = tmp_path / 'browser-profiles'
    base.mkdir()
    session = BrowserConnection(base/'external-sites'/'account-1'/'profile', TARGET,
                                {COURSE, IDENTITY}, transport, headless=True)
    try:
        wait(session, lambda state: state['state'] == 'login_open')
        yield session
    finally:
        if session.snapshot()['state'] not in ('closed', 'error'):
            session.call('cancel')
        session.thread.join(5)
        assert not session.thread.is_alive()


@pytest.mark.browser
def test_chromium_never_receives_a_redirect_to_loopback(tmp_path):
    hit = threading.Event()
    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            hit.set()
            self.send_response(200); self.end_headers(); self.wfile.write(b'fixture')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    calls = []
    def transport(url, method, headers, body):
        calls.append(url)
        return 307, {'location': f'http://127.0.0.1:{server.server_port}/sink'}, [], b''
    try:
        with browser_session(tmp_path, transport) as session:
            assert session.snapshot()['redirect_blocked']
            assert not hit.wait(.2), 'Chromium followed a 307 outside the guarded transport'
            assert calls == [TARGET]
    finally:
        server.shutdown(); server.server_close(); worker.join(2)


@pytest.mark.browser
def test_approved_sso_keeps_real_document_origin_and_destination_cookies(tmp_path):
    calls, observed = [], threading.Event()
    def transport(url, method, headers, body):
        calls.append((url, method, headers, body))
        if url == TARGET:
            return 302, {'location': '/prepare'}, ['course=fixture; Path=/; Secure; HttpOnly'], b''
        if url == COURSE + '/prepare':
            assert 'course=fixture' in headers.get('cookie', '')
            return 307, {'location': IDENTITY + "/authorize?state=fixture%2Fkeep&label='fixture'"}, [], b''
        if url == IDENTITY + '/authorize?state=fixture%2Fkeep&label=%27fixture%27':
            assert 'course=' not in headers.get('cookie', '')
            return 308, {'location': '/login'}, ['identity=fixture; Path=/; Secure; HttpOnly'], b''
        if url == IDENTITY + '/login':
            assert 'identity=fixture' in headers.get('cookie', '')
            assert 'course=' not in headers.get('cookie', '')
            return 200, {'content-type': 'text/html'}, [], b'''<main>Sign in</main><script>
              fetch('/observed?ok='+(location.href==='https://id.example.edu/login'
                && location.origin==='https://id.example.edu'));</script>'''
        if url == IDENTITY + '/observed?ok=true':
            observed.set()
        return 200, {'content-type': 'text/plain'}, [], b'fixture'
    with browser_session(tmp_path, transport) as session:
        assert observed.wait(5), 'Final HTML must execute at its real destination origin'
        assert not session.snapshot()['redirect_blocked']
        assert all(method == 'GET' and body is None for _, method, _, body in calls)
        assert all(not {'authorization', 'origin', 'referer', 'x-csrf-token', 'x-xsrf-token'} & headers.keys()
                   for _, _, headers, _ in calls[1:4])


@pytest.mark.browser
def test_approved_post_redirect_is_new_get_without_source_credentials(tmp_path):
    calls, arrived = [], threading.Event()
    def transport(url, method, headers, body):
        calls.append((url, method, headers, body))
        if method == 'POST':
            assert url == COURSE + '/login' and body == b'password=fixture'
            return 303, {'location': IDENTITY + '/authorize'}, ['course=private-fixture; Path=/; Secure; HttpOnly'], b''
        if url == IDENTITY + '/authorize':
            assert method == 'GET' and body is None
            assert 'private-fixture' not in str(headers)
            assert not {'authorization', 'origin', 'referer', 'content-type', 'x-csrf-token'} & headers.keys()
            arrived.set()
            return 200, {'content-type': 'text/html'}, [], b'<main>Identity page</main>'
        return 200, {'content-type': 'text/html'}, [], b'''<form method="post" action="/login">
          <input type="password" name="password" value="fixture"></form>
          <script>document.querySelector('form').requestSubmit()</script>'''
    with browser_session(tmp_path, transport) as session:
        state = wait(session, lambda s: s['pending_post'] is not None)
        assert not any(method == 'POST' for _, method, _, _ in calls)
        session.call('approve_post', state['pending_post'])
        assert arrived.wait(5)
        assert sum(method == 'POST' for _, method, _, _ in calls) == 1


@pytest.mark.browser
@pytest.mark.parametrize('destination', [COURSE + '/next', IDENTITY + '/authorize'])
def test_each_redirect_rechecks_dns_before_connecting(tmp_path, monkeypatch, destination):
    resolved, connections = [], []
    def resolve(host, *args, **kwargs):
        resolved.append(host)
        # The first response is public. The next hop (even to the same host)
        # resolves privately and must never create a destination connection.
        address = '93.184.216.34' if len(resolved) == 1 else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
    class Connection:
        def __init__(self, host, ip):
            connections.append((host, ip))
        def request(self, method, path, **kwargs): pass
        def getresponse(self):
            headers = Message()
            headers['Location'] = destination
            return SimpleNamespace(status=307, headers=headers, getheader=headers.get, read1=lambda n: b'')
        def close(self): pass
    monkeypatch.setattr(sc.socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(sc, '_PinnedHTTPS', Connection)
    with browser_session(tmp_path, sc.pinned_request) as session:
        assert session.snapshot()['redirect_blocked']
        assert resolved == [urlsplit(TARGET).hostname, urlsplit(destination).hostname]
        assert connections == [('course.example.edu', '93.184.216.34')]


class Route:
    def __init__(self, url=TARGET, method='GET', kind='document'):
        self.request = SimpleNamespace(url=url, method=method, resource_type=kind,
            frame=SimpleNamespace(url=TARGET, parent_frame=None),
            all_headers=lambda: {'cookie': 'destination=fixture', 'authorization': 'fixture',
                'origin': COURSE, 'x-csrf-token': 'fixture', 'content-type': 'application/x-www-form-urlencoded'},
            post_data_buffer=b'password=fixture' if method == 'POST' else None)
        self.aborted, self.fulfilled = False, False
    def abort(self): self.aborted = True
    def fulfill(self, **kwargs): self.fulfilled = True


def routed_session(transport):
    session = object.__new__(BrowserConnection)
    session.target, session.domains = TARGET, {COURSE, IDENTITY}
    session.lock = threading.Lock()
    session.state = {'blocked_origins': [], 'pending_post': None}
    session._request_count, session._verify_mode = 0, False
    session._post_approval = session._post_pending = None
    session._post_key = b'synthetic-key'
    session._post_route = None
    session._policies = {TARGET: {'forms': {TARGET}}}
    session._redirect_expected = session._redirect_next = None
    session.transport = transport
    return session


@pytest.mark.parametrize(('status', 'method', 'kind', 'verify', 'reason'), [
    (307, 'POST', 'document', False, 'post_replay'),
    (308, 'POST', 'document', False, 'post_replay'),
    (302, 'GET', 'script', False, 'subresource'),
    (302, 'GET', 'document', True, 'verification'),
])
def test_redirect_cannot_replay_post_load_assets_or_change_verification(status, method, kind, verify, reason):
    cookies, calls = [], []
    def transport(url, method, headers, body):
        calls.append(url)
        return status, {'location': IDENTITY + '/login'}, ['secret=fixture; Secure'], b''
    session = routed_session(transport)
    session._verify_mode = verify
    route = Route(method=method, kind=kind)
    approval = None
    if method == 'POST':
        session._route(route, SimpleNamespace(add_cookies=cookies.extend))
        approval = session._post_pending[0]
    session._route(route, SimpleNamespace(add_cookies=cookies.extend), approval=approval)
    assert route.aborted and not route.fulfilled and not cookies
    assert calls == [TARGET] and session._redirect_next is None
    assert session.snapshot()['redirect_reason'] == reason


@pytest.mark.parametrize(('location', 'reason'), [
    ('https://unapproved.example.edu/login?token=fixture-private', 'unapproved_origin'),
    ('https://127.0.0.1/login', 'unsafe_target'),
    ('http://id.example.edu/login', 'unsafe_target'),
    ('https://user:password@id.example.edu/login', 'unsafe_target'),
    ('https://id.example.edu/%64elete', 'unsafe_target'),
    ('https://id.example.edu\\@course.example.edu/login', 'unsafe_target'),
    ('https://[malformed/login', 'unsafe_target'),
])
def test_redirect_targets_are_rejected_before_navigation(location, reason):
    session = routed_session(lambda *a: (302, {'location': location}, [], b''))
    route = Route()
    session._route(route, SimpleNamespace(add_cookies=lambda x: None))
    assert route.aborted and not route.fulfilled and session._redirect_next is None
    assert session.snapshot()['redirect_reason'] == reason
    assert 'fixture-private' not in str(session.snapshot()) and 'password' not in str(session.snapshot())


@pytest.mark.parametrize('stop', ['loop', 'limit', 'timeout', 'navigation_changed'])
def test_redirect_chain_is_bounded_and_does_not_copy_sensitive_headers(monkeypatch, stop):
    calls, clock = [], [100.0]
    monkeypatch.setattr(sc, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def transport(url, method, headers, body):
        calls.append((url, headers))
        if len(calls) > 1:
            assert headers == {'cookie': 'destination=fixture'}
        if stop == 'timeout': clock[0] += sc.REDIRECT_SECONDS
        location = TARGET if stop == 'loop' and len(calls) == 2 else COURSE + f'/hop-{len(calls)}'
        return 302, {'location': location}, [], b''
    session = routed_session(transport)
    navigation = sc._Navigation(TARGET, (TARGET,), clock[0] + sc.REDIRECT_SECONDS)
    while navigation:
        session._redirect_expected = navigation
        session._redirect_next = None
        route = Route(navigation.url if stop != 'navigation_changed' else COURSE + '/unexpected')
        session._route(route, SimpleNamespace(add_cookies=lambda x: None))
        assert route.aborted and not route.fulfilled
        navigation = session._redirect_next
        assert len(calls) <= sc.MAX_REDIRECTS + 1
    assert session.snapshot()['redirect_reason'] == stop
    assert len(calls) == {'loop': 2, 'limit': sc.MAX_REDIRECTS + 1, 'timeout': 1, 'navigation_changed': 0}[stop]
