"""Anonymous detection uses synthetic pages and no real services."""
import socket
import pytest
from tools import login_probe as probe


@pytest.mark.parametrize('body,expected,evidence', [
    ('<form><input type="password"></form>', 'login_required', 'rendered_login_form'),
    ('<body><script>document.body.innerHTML="<form><input type=password></form>"</script></body>', 'login_required', 'rendered_login_form'),
    ('<a>Log in</a><article>' + 'Public course information. ' * 10 + '</article>', 'readable', 'rendered_readable_content'),
    ('<h1>Verify you are human</h1><p>' + 'security challenge ' * 10 + '</p>', 'unknown', 'bot_or_security_challenge'),
    ('<button>Log in</button><button>Join Classes</button><p>' + 'Welcome ' * 20 + '</p>', 'unknown', 'login_or_enrollment_shell'),
    ('<article>' + 'Public lecture notes. ' * 30 + '</article><form><input type="password"></form>', 'unknown', 'mixed_public_content_and_login'),
])
def test_rendered_fixtures(monkeypatch, body, expected, evidence):
    monkeypatch.setattr(probe, 'fetch', lambda url: (200, {'Content-Type':'text/html'}, body.encode()))
    r = probe._inspect('https://fixture.example/')
    assert (r['outcome'], r['evidence']) == (expected, [evidence])


def test_sso_redirect_plus_login_page(monkeypatch):
    def fake(url):
        if url.endswith('/'):
            return 302, {'Location':'https://identity.example/oauth2/v2.0/authorize'}, b''
        return 200, {'Content-Type':'text/html'}, b'<title>Sign in to your account</title><p>Sign in</p>'
    monkeypatch.setattr(probe, 'fetch', fake)
    r=probe._inspect('https://fixture.example/')
    assert r['outcome']=='login_required' and r['final_host']=='identity.example'
    assert r['evidence']==['authentication_redirect_and_page_title']


@pytest.mark.parametrize('status,headers,expected', [(401, {}, 'access_denied'), (403, {}, 'access_denied'), (401, {'WWW-Authenticate':'Basic realm="test"'}, 'login_required')])
def test_http_denials(monkeypatch,status,headers,expected):
    monkeypatch.setattr(probe, 'fetch', lambda url:(status, headers, b''))
    assert probe._inspect('https://fixture.example/')['outcome']==expected


def test_timeout_is_not_guessed_as_login(monkeypatch):
    def fail(url): raise TimeoutError()
    monkeypatch.setattr(probe, 'fetch', fail)
    assert probe._inspect('https://fixture.example/')['outcome']=='unsupported_unavailable'


def test_redirect_renders_under_final_origin(monkeypatch):
    seen = []
    def fake(url):
        seen.append(url)
        if url == 'https://fixture.example/':
            return 302, {'Location':'https://identity.example/login'}, b''
        if url == 'https://identity.example/login':
            return 200, {'Content-Type':'text/html'}, b'<body><script src="/gate.js"></script></body>'
        if url == 'https://identity.example/gate.js':
            return 200, {'Content-Type':'text/javascript'}, b'document.body.innerHTML="<input type=password>"'
        raise AssertionError('unexpected origin')
    monkeypatch.setattr(probe, 'fetch', fake)
    result = probe._inspect('https://fixture.example/')
    assert result['outcome'] == 'login_required'
    assert 'https://identity.example/gate.js' in seen


def test_total_deadline_stops_a_hung_worker(monkeypatch):
    import multiprocessing
    import time
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('process group check needs fork')
    original = multiprocessing.get_context
    monkeypatch.setattr(probe.multiprocessing, 'get_context', lambda method: original('fork'))
    def stuck(url, timeout_ms, output, capture=False):
        time.sleep(5)
    monkeypatch.setattr(probe, '_child', stuck)
    t = time.monotonic()
    assert probe.inspect('https://fixture.example/', timeout_ms=300)['evidence'] == ['timeout']
    assert time.monotonic() - t < 2


def test_private_and_sensitive_targets_rejected(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError): probe.validate('https://public.example/')
    with pytest.raises(ValueError): probe.validate('https://public.example/?token=never-log-this')
    with pytest.raises(ValueError): probe.validate('https://public.example/delete/1')
