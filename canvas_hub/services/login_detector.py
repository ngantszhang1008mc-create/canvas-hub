#!/usr/bin/env python3
"""Inspect a public site in a fresh browser; never use a saved browser profile.

Requires playwright and Chromium. Only bounded, public, anonymous GETs are
allowed; browser requests are intercepted and fetched on validated, pinned IPs.
Outputs only hostnames, status codes and fixed evidence labels.
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import multiprocessing
import os
import signal
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
from .site_policy import MIME, diagnostic, document_policy

MAX_BYTES = 1_000_000
SENSITIVE = re.compile(r'token|secret|signature|password|credential|authorization|api.?key|^sig$|^key$|^code$|^saml|^ticket$|^verifier$|^x-amz-|^x-goog-', re.I)
MUTATION = re.compile(r'(^|/)(logout|signout|sign-out|sign_out|delete|remove|submit|enroll|unsubscribe|purchase|checkout)(/|$)', re.I)


def validate(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('unsafe_url')
    if p.port not in (None, 443 if p.scheme == 'https' else 80):
        raise ValueError('unsafe_port')
    host = p.hostname.lower().rstrip('.')
    if '.' not in host or host.endswith(('.local', '.internal', '.localhost', '.test', '.invalid')):
        raise ValueError('unsafe_host')
    path = p.path
    for _ in range(4):
        path = unquote(path)
    if '\\' in path or any(x in ('.', '..') for x in path.split('/')) or MUTATION.search(path):
        raise ValueError('unsafe_path')
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if SENSITIVE.search(k) or MUTATION.search('/' + unquote(k).strip('/')) or (k.lower() in ('action', 'do', 'method', 'operation') and MUTATION.search('/' + unquote(v).strip('/'))):
            raise ValueError('sensitive_or_mutating_query')
    addresses = socket.getaddrinfo(host, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global or ipaddress.ip_address(a[4][0]).is_multicast or ipaddress.ip_address(a[4][0]).is_reserved for a in addresses):
        raise ValueError('non_public_address')
    return p, addresses[0][4][0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, 443, timeout=8, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(url):
    p, ip = validate(url)
    conn = PinnedHTTPS(p.hostname, ip) if p.scheme == 'https' else http.client.HTTPConnection(ip, 80, timeout=8)
    try:
        conn.request('GET', (p.path or '/') + ('?' + p.query if p.query else ''), headers={'Host': p.netloc, 'User-Agent': 'CanvasHub-LoginProbe/1.0', 'Accept-Encoding': 'identity'})
        r = conn.getresponse()
        if r.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('compressed_response')
        body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('response_too_large')
        headers = {k: v for k, v in r.getheaders() if k.lower() in ('content-type', 'location', 'www-authenticate')}
        return r.status, headers, body
    finally:
        conn.close()


def classify(status, auth_challenge, signals, error=None):
    if error:
        return 'unsupported_unavailable', [error]
    if signals.get('challenge'):
        return 'unknown', ['bot_or_security_challenge']
    if status == 401 and auth_challenge:
        return 'login_required', ['http_authentication_challenge']
    if status in (401, 403):
        return 'access_denied', ['http_access_denied']
    if status != 200:
        return 'unsupported_unavailable', ['http_non_success']
    if signals.get('login_or_enrollment_shell'):
        return 'unknown', ['login_or_enrollment_shell']
    if signals.get('substantive_content') and (signals.get('password') or signals.get('login_form') or signals.get('login_gate')):
        return 'unknown', ['mixed_public_content_and_login']
    if signals.get('password') or signals.get('login_form'):
        return 'login_required', ['rendered_login_form']
    if signals.get('login_gate'):
        return 'login_required', ['rendered_login_gate']
    if signals.get('redirect_login_page'):
        return 'login_required', ['authentication_redirect_and_page_title']
    if signals.get('readable'):
        return 'readable', ['rendered_readable_content']
    return 'unknown', ['insufficient_rendered_evidence']


def _inspect(url, timeout_ms=20000, capture=False):
    result = {'initial_host': urlsplit(url).hostname, 'final_host': None, 'http_status': None, 'outcome': 'unknown', 'evidence': [], 'course_access': 'unknown', 'diagnostics': []}
    state = {'status': None, 'auth': False, 'error': None, 'host': None, 'n': 0, 'redirect_auth': False}
    try:
        target = url
        for _ in range(5):
            status, headers, body = fetch(target)
            location = next((v for k, v in headers.items() if k.lower() == 'location'), None)
            if status not in (301, 302, 303, 307, 308) or not location:
                break
            next_url = urljoin(target, location)
            if re.search(r'/(?:oauth2?/[^?]*/authorize|auth/login|login|signin)(?:/|$)', urlsplit(next_url).path, re.I):
                state['redirect_auth'] = True
            if urlsplit(target).scheme == 'https' and urlsplit(next_url).scheme != 'https':
                raise ValueError('insecure_redirect')
            target = next_url
        else:
            raise ValueError('too_many_redirects')
        state.update(status=status, auth=any(k.lower() == 'www-authenticate' for k in headers), host=urlsplit(target).hostname)
        result.update(final_host=state['host'], http_status=status)
        if status != 200:
            result['outcome'], result['evidence'] = classify(status, state['auth'], {})
            if status == 401 and state['auth']:
                challenge = next((v for k, v in headers.items() if k.lower() == 'www-authenticate'), '')
                result['auth_scheme'] = 'basic' if re.search(r'(?:^|,)\s*Basic(?:\s|$)', challenge, re.I) else 'other'
            return result
    except Exception as exc:
        result['outcome'], result['evidence'] = classify(None, False, {}, type(exc).__name__)
        return result
    with sync_playwright() as p:
        browser_options = {}
        if os.environ.get('CANVAS_HUB_CHROMIUM'):
            browser_options['executable_path'] = os.environ['CANVAS_HUB_CHROMIUM']
        elif os.environ.get('CANVAS_HUB_BROWSER_CHANNEL'):
            browser_options['channel'] = os.environ['CANVAS_HUB_BROWSER_CHANNEL']
        browser = p.chromium.launch(headless=True, args=['--disable-webrtc'], **browser_options)
        context = browser.new_context(service_workers='block', accept_downloads=False)
        context.route_web_socket('**/*', lambda ws: ws.close())
        context.add_init_script("Object.defineProperty(window, 'RTCPeerConnection', {value: undefined}); Object.defineProperty(window, 'webkitRTCPeerConnection', {value: undefined});")
        page = context.new_page()
        policy = document_policy(target, body)
        def intercept(route):
            req = route.request
            def deny(reason):
                if len(result['diagnostics']) < 24:
                    result['diagnostics'].append(diagnostic(req, reason))
                route.abort()
            if req.method != 'GET' or req.resource_type not in ('document', 'script', 'stylesheet'):
                deny('anonymous_request_scope'); return
            if req.resource_type == 'document' and req.url != target:
                deny('anonymous_navigation'); return
            host = urlsplit(req.url).hostname
            static = (req.url, req.resource_type) in policy['static']
            if req.resource_type != 'document' and not static and (urlsplit(req.url).scheme, host, urlsplit(req.url).port) != (urlsplit(target).scheme, state['host'], urlsplit(target).port):
                deny('cross_origin_resource'); return
            state['n'] += 1
            if state['n'] > 32:
                deny('request_limit'); return
            try:
                if req.resource_type == 'document':
                    response_status, response_headers, response_body = status, headers, body
                else:
                    response_status, response_headers, response_body = fetch(req.url)
                    if response_status in (301, 302, 303, 307, 308):
                        deny('subresource_redirect'); return
                    if static:
                        content_type = next((v for k, v in response_headers.items() if k.lower() == 'content-type'), '').split(';', 1)[0].lower()
                        if response_status != 200 or content_type not in MIME[req.resource_type]:
                            deny('static_response_rejected'); return
                route.fulfill(status=response_status, headers=response_headers, body=response_body)
            except Exception as exc:
                if req.resource_type == 'document': state['error'] = type(exc).__name__
                deny('unsafe_or_failed_request')
        context.route('**/*', intercept)
        try:
            page.goto(target, wait_until='domcontentloaded', timeout=timeout_ms)
            page.wait_for_timeout(1200)
            signals = page.evaluate('''() => {
              const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
              const text = (document.body?.innerText || '').toLowerCase();
              const pass = [...document.querySelectorAll('input[type=password]')].some(visible);
              const inputs = [...document.querySelectorAll('input')].filter(visible);
              const id = inputs.some(e => /email|user|login|account/i.test([e.type,e.name,e.id,e.placeholder,e.autocomplete].join(' ')));
              const submit = [...document.querySelectorAll('button,input[type=submit]')].filter(visible).some(e => /sign[ -]?in|log[ -]?in|next/i.test(e.innerText || e.value || ''));
              const gate = /log[ -]?in to (?:view|access|see|continue|read)|sign[ -]?in to (?:view|access|see|continue|read)/i.test(text);
              const titleAuth = /^(?:sign[ -]?in|log[ -]?in) to (?:your )?account$/i.test(document.title.trim());
              const loginOrEnrollmentShell = /join classes|add another class/i.test(text) && /log[ -]?in|sign[ -]?in/i.test(text);
              const substantive = [...document.querySelectorAll('main,article')].filter(visible).some(e => (e.innerText || '').replace(/\\s/g,'').length >= 300);
              return {password:pass, login_form:id && submit, login_gate:gate, title_auth:titleAuth, login_or_enrollment_shell:loginOrEnrollmentShell, substantive_content:substantive, challenge:/verify you are human|checking your browser|captcha|access denied by security/i.test(text), readable:text.replace(/\\s/g,'').length >= 100};
            }''')
            signals['redirect_login_page'] = state['redirect_auth'] and signals['title_auth']
            # Optional capture never includes an active credential entry screen,
            # embedded frames/canvas/shadow DOM, or visible personal identifiers.
            if capture and classify(state['status'], state['auth'], signals, state['error'])[0] == 'unknown':
                # A cross-origin redirect may be an unrelated identity page.
                if state['host'] != result['initial_host']:
                    result['capture_error'] = 'cross_origin_redirect'
                    context.close(); browser.close()
                    result['outcome'], result['evidence'] = classify(state['status'], state['auth'], signals, state['error'])
                    return result
                # Freeze page scripts before checking and masking, avoiding a
                # page swapping in a credential screen between check and capture.
                cdp = context.new_cdp_session(page)
                cdp.send('Emulation.setScriptExecutionDisabled', {'value': True})
                safe = page.evaluate('''() => {
                  const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                  const all = [...document.querySelectorAll('*')];
                  const sensitive = all.some(e => e.shadowRoot || (visible(e) && /^(IFRAME|CANVAS|VIDEO|OBJECT|EMBED)$/.test(e.tagName)));
                  const activeLogin = [...document.querySelectorAll('input[type=password],input[autocomplete=username],input[autocomplete=current-password]')].some(visible);
                  const text = document.body?.innerText || '';
                  return {sensitive, activeLogin, email: /[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}/i.test(text)};
                }''')
                safe['activeLogin'] = safe['activeLogin'] or signals['login_form'] or signals['password']
                safe['challenge'] = signals['challenge']
                if safe['email'] and not safe['sensitive'] and not safe['activeLogin'] and not safe['challenge']:
                    safe['email'] = page.evaluate('''() => {
                      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                      while (walker.nextNode()) {
                        const n = walker.currentNode;
                        n.nodeValue = n.nodeValue.replace(/[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}/gi, '[redacted]');
                      }
                      return /[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}/i.test(document.body?.innerText || '');
                    }''')
                if not any(safe.values()):
                    # Mask every form and editable control, including values and hidden fields.
                    png = page.screenshot(mask=[page.locator('form, input, textarea, select, [contenteditable]')], full_page=False)
                    if len(png) <= 2_000_000:
                        result['captured_png'] = png
                        result['capture_source_sha256'] = hashlib.sha256(url.encode()).hexdigest()
                    else:
                        result['capture_error'] = 'image_too_large'
                else:
                    result['capture_error'] = 'unsafe_or_ambiguous_page'
                    result['capture_blocked_by'] = [key for key, value in safe.items() if value]
        except BrowserTimeout:
            signals = {}; state['error'] = state['error'] or 'timeout'
        except Exception:
            signals = {}
        result.update(final_host=state['host'], http_status=state['status'])
        result['outcome'], result['evidence'] = classify(state['status'], state['auth'], signals, state['error'])
        context.close(); browser.close()
    return result


def _child(url, timeout_ms, output, capture=False):
    if os.name == 'posix':
        os.setsid()
    try:
        output.send(_inspect(url, timeout_ms, capture=capture))
    except BaseException:
        output.send({'initial_host': urlsplit(url).hostname, 'final_host': None, 'http_status': None,
                     'outcome': 'unsupported_unavailable', 'evidence': ['probe_error'], 'course_access': 'unknown'})
    finally:
        output.close()


def inspect(url, timeout_ms=20000, *, capture=False):
    """Use an isolated subprocess so DNS, browser and slow reads share one deadline."""
    if timeout_ms <= 0:
        raise ValueError('timeout must be positive')
    ctx = multiprocessing.get_context('spawn')
    incoming, outgoing = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_child, args=(url, timeout_ms, outgoing, capture))
    worker.start()
    outgoing.close()
    try:
        if incoming.poll(timeout_ms / 1000):
            return incoming.recv()
        return {'initial_host': urlsplit(url).hostname, 'final_host': None, 'http_status': None,
                'outcome': 'unsupported_unavailable', 'evidence': ['timeout'], 'course_access': 'unknown'}
    finally:
        if worker.is_alive():
            if os.name == 'posix':
                try:
                    if os.getpgid(worker.pid) == worker.pid:
                        os.killpg(worker.pid, signal.SIGTERM)
                    else:
                        worker.terminate()
                except ProcessLookupError:
                    pass
            else:
                worker.terminate()
        worker.join(1)
        if worker.is_alive():
            worker.kill(); worker.join(1)
        incoming.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='+')
    args = parser.parse_args()
    for url in args.url:
        print(json.dumps(inspect(url)))


if __name__ == '__main__':
    main()
