"""Isolated interactive external-site sign-in.

All browser network traffic passes through a pinned, public-address transport.
This module never sends a page, cookie, or profile to an Assistant provider.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import queue
import re
import secrets
import socket
import threading
import time
from concurrent.futures import Future
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, urlunsplit

from ..external_urls import clean_url, public_address, MUTATION
from ..util import Conflict, InvalidInput
from .external_reader import _PinnedHTTPS, _DNS, MAX_BYTES

MAX_REQUEST = 200_000
POST_PATH = re.compile(r'/(?:login|log-in|signin|sign-in|auth|authorize|oauth|saml|session|token|mfa|verify|challenge|adfs)(?:/|$)', re.I)


def origin(raw):
    safe, secret = clean_url(raw)
    p = urlsplit(safe)
    if secret or p.scheme != 'https' or p.path != '/' or p.query:
        raise InvalidInput('Use an HTTPS site origin without a path or query')
    return urlunsplit((p.scheme, p.netloc, '', '', ''))


def request_origin(raw):
    p = urlsplit(raw)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise InvalidInput('Only approved HTTPS origins are allowed')
    return 'https://' + p.hostname.lower().rstrip('.')


def _public_ip(host):
    addresses = _DNS.submit(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM).result(timeout=5)
    if not addresses or any(not public_address(entry[4][0]) for entry in addresses):
        raise InvalidInput('Site resolved to a private address')
    return addresses[0][4][0]


def pinned_request(url, method, headers, body):
    """One bounded request, no redirect following, DNS pinning and verified TLS."""
    p = urlsplit(url)
    host = p.hostname
    ip = _public_ip(host)
    conn = _PinnedHTTPS(host, ip)
    allowed = {'accept', 'accept-language', 'content-type', 'cookie', 'authorization', 'origin', 'user-agent', 'x-csrf-token', 'x-xsrf-token'}
    outbound = {k: v for k, v in headers.items() if k.lower() in allowed}
    outbound.update({'Host': p.netloc, 'Accept-Encoding': 'identity'})
    started = time.monotonic()
    try:
        conn.request(method, (p.path or '/') + ('?' + p.query if p.query else ''), body=body, headers=outbound)
        response = conn.getresponse()
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise InvalidInput('Compressed response is unsupported')
        chunks, total = [], 0
        while True:
            chunk = response.read1(64000)
            total += len(chunk)
            if total > MAX_BYTES or time.monotonic() - started > 15:
                raise InvalidInput('Site response exceeds the size or time limit')
            if not chunk:
                break
            chunks.append(chunk)
        data = b''.join(chunks)
        # Only browser-relevant headers; never emit these values in logs or status.
        safe_headers = {}
        for key in ('content-type', 'location', 'cache-control', 'content-security-policy', 'access-control-allow-origin', 'access-control-allow-credentials'):
            value = response.getheader(key)
            if value:
                safe_headers[key] = value
        return response.status, safe_headers, response.headers.get_all('set-cookie', []), data
    finally:
        conn.close()


def _set_cookies(context, url, headers):
    p = urlsplit(url)
    for header in headers:
        jar = SimpleCookie()
        try:
            jar.load(header)
            for morsel in jar.values():
                default_path = p.path.rsplit('/', 1)[0] or '/'
                cookie_path = morsel['path'] or default_path
                cookie = {'name': morsel.key, 'value': morsel.value, 'url': f'{p.scheme}://{p.netloc}{cookie_path}',
                          'secure': bool(morsel['secure']), 'httpOnly': bool(morsel['httponly'])}
                if morsel['domain']:
                    # BrowserContext.add_cookies bypasses the response-origin check.
                    # Never let one approved origin inject another origin's cookies.
                    if morsel['domain'].lstrip('.').lower() != p.hostname.lower():
                        continue
                    # Keep it host-only even when the server requests Domain=host.
                if morsel['samesite'].lower() in ('lax', 'strict', 'none'):
                    cookie['sameSite'] = morsel['samesite'].capitalize()
                if morsel['max-age']:
                    cookie['expires'] = time.time() + max(0, int(morsel['max-age']))
                context.add_cookies([cookie])
        except Exception:
            # Unsupported cookie syntax fails closed: the site may ask for login again.
            continue


class BrowserConnection:
    """Own one Playwright persistent context on its dedicated thread."""
    def __init__(self, profile, target, domains, transport=pinned_request, *, headless=False):
        self.profile, self.target, self.domains = profile, target, set(domains)
        self.transport, self.headless = transport, headless
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.state = {'state': 'opening', 'blocked_origins': [], 'pending_post': None, 'blocked_submission_origin':None}
        self._post_approval = None
        self._post_pending = None
        self._request_count = 0
        self._verify_mode = False
        self._review_token = None
        self._review_deadline = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name='external-site-login')
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def call(self, action, arg=None, timeout=35):
        future = Future()
        self.events.put((action, arg, future))
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            raise Conflict('The site browser did not respond; try again')

    def _route(self, route, context):
        request = route.request
        try:
            host = request_origin(request.url)
            if self._verify_mode and (request.method != 'GET' or request.url != self.target or request.resource_type != 'document'):
                route.abort(); return
            if host not in self.domains:
                # Only disclose a hostname, never path, token or query.
                with self.lock:
                    blocked = set(self.state['blocked_origins'])
                    if len(blocked) < 10:
                        blocked.add(host)
                    self.state['blocked_origins'] = sorted(blocked)
                route.abort(); return
            # Approved identity origins can receive their own documents and
            # assets, but a page cannot use fetch/scripts/images to send data
            # to a different approved origin. Cross-origin document navigation
            # remains necessary for explicit SSO, with POSTs approved separately.
            if request.resource_type == 'document' and request.frame.parent_frame is not None:
                route.abort(); return
            if request.resource_type != 'document' and request_origin(request.frame.url) != host:
                route.abort(); return
            p = urlsplit(request.url)
            clean_url(request.url)  # Validate decoded paths, public host and mutation queries.
            self._request_count += 1
            if self._request_count > 300:
                route.abort(); return
            method = request.method.upper()
            if method not in ('GET', 'POST'):
                route.abort(); return
            if method == 'POST':
                identity = (host, p.path)
                with self.lock:
                    allowed = bool(self._post_approval and self._post_approval[:2] == identity and time.monotonic() < self._post_approval[2])
                    if allowed:
                        self._post_approval = None
                        self._post_pending = None
                        self.state['pending_post'] = None
                    elif POST_PATH.search(p.path):
                        token = secrets.token_urlsafe(18)
                        self._post_pending = (host, p.path, token)
                        self.state['pending_post'] = {'origin': host, 'request_id': token}
                    else:
                        self.state['blocked_submission_origin'] = host
                if not allowed:
                    route.abort(); return
            headers = request.all_headers()
            if method == 'POST':
                content_type = headers.get('content-type', '').split(';', 1)[0].lower()
                if content_type not in ('application/x-www-form-urlencoded', 'application/json'):
                    route.abort(); return
                length = headers.get('content-length', '')
                if length and (not length.isascii() or not length.isdecimal() or int(length) > MAX_REQUEST):
                    route.abort(); return
            data = request.post_data_buffer if method == 'POST' else None
            if data and len(data) > MAX_REQUEST:
                route.abort(); return
            status, headers, cookies, body = self.transport(request.url, method, headers, data)
            _set_cookies(context, request.url, cookies)
            route.fulfill(status=status, headers=headers, body=body)
        except Exception:
            try: route.abort()
            except Exception: pass

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            # Never follow a substituted account/site profile symlink into a
            # personal browser profile or another account's session.
            if self.profile.parent.parent.parent.is_symlink():
                raise InvalidInput('Browser profile root cannot be a symbolic link')
            for directory in (self.profile.parent.parent, self.profile.parent, self.profile):
                if directory.is_symlink():
                    raise InvalidInput('Site profile cannot be a symbolic link')
                directory.mkdir(exist_ok=True, mode=0o700)
                os.chmod(directory, 0o700)
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(str(self.profile), headless=self.headless,
                    accept_downloads=False, service_workers='block', offline=True, args=['--disable-webrtc'])
                context.route_web_socket('**/*', lambda ws: ws.close())
                context.add_init_script("""Object.defineProperty(window,'RTCPeerConnection',{value:undefined});
                    Object.defineProperty(window,'webkitRTCPeerConnection',{value:undefined});
                    const d=Object.getOwnPropertyDescriptor(Document.prototype,'cookie');
                    Object.defineProperty(Document.prototype,'cookie',{configurable:false,get:d.get,
                      set:function(v){if(!/;\\s*domain\\s*=/i.test(String(v)))d.set.call(this,v)}});""")
                context.route('**/*', lambda route: self._route(route, context))
                # A restored profile cannot send traffic before interception exists.
                context.set_offline(False)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(self.target, wait_until='domcontentloaded', timeout=15000)
                except Exception:
                    pass
                self.update(state='login_open')
                while True:
                    try:
                        action, arg, future = self.events.get_nowait()
                    except queue.Empty:
                        try: page.wait_for_timeout(200)
                        except Exception: break
                        continue
                    try:
                        if action == 'cancel':
                            future.set_result({'state':'closed'}); break
                        if action == 'approve_post':
                            with self.lock:
                                current = self.state['pending_post']
                                if not current or current != arg:
                                    raise Conflict('The login submission changed; review it again')
                                self._post_approval = (*self._post_pending[:2], time.monotonic()+60)
                                self.state['pending_post'] = None
                            future.set_result({'state':'approved_once'})
                        elif action == 'verify':
                            future.set_result(self._verify(page))
                        elif action == 'retry_login':
                            self._review_token = None
                            self.update(state='login_open')
                            try: page.goto(self.target, wait_until='domcontentloaded', timeout=15000)
                            except Exception: pass
                            future.set_result({'state':'login_open'})
                        elif action == 'confirm_content':
                            if not self._review_token or time.monotonic() > self._review_deadline or arg != self._review_token or page.url != self.target or self.snapshot()['state'] != 'content_visible':
                                self._review_token = None
                                raise Conflict('Review the requested page again before confirming')
                            self._review_token = None
                            self.update(state='connected')
                            future.set_result({'state':'connected'})
                        else:
                            raise InvalidInput('Unknown action')
                    except Exception as exc:
                        future.set_exception(exc if isinstance(exc, (Conflict, InvalidInput)) else Conflict('Site check failed; previous data retained'))
                context.close()
        except Exception:
            self.update(state='error')
            return
        self.update(state='closed')

    def _verify(self, page):
        cdp = None
        self._review_token = None
        try:
            # Verification is a single GET to the already approved source. No
            # page scripts, subresources, redirects or form submissions run.
            self._verify_mode = True
            cdp = page.context.new_cdp_session(page)
            cdp.send('Emulation.setScriptExecutionDisabled', {'value':True})
            response = page.goto(self.target, wait_until='domcontentloaded', timeout=15000)
            if page.url != self.target:
                self.update(state='reconnect_needed')
                return {'state':'reconnect_needed'}
            if not response or response.status != 200:
                self.update(state='reconnect_needed' if response and response.status in (401, 403) else 'access_unverified')
                return {'state':self.snapshot()['state']}
            evidence = page.evaluate('''() => {
                const area = document.querySelector('main,article');
                if (!area) return {form:!!document.querySelector('input[type=password],form[action*="login" i]'), chooser:false, text:''};
                const clone = area.cloneNode(true);
                clone.querySelectorAll('form,input,textarea,select,script,style,iframe,object').forEach(e=>e.remove());
                const text = (clone.innerText || clone.textContent || '').trim().slice(0,200000);
                const form = !!document.querySelector('input[type=password],form[action*="login" i]');
                const chooser = /join classes|add another class/i.test(document.body?.innerText || '') && /log[ -]?in|sign[ -]?in/i.test(document.body?.innerText || '');
                return {form, chooser, text};
            }''')
            if evidence['form'] or evidence['chooser'] or len(evidence['text']) < 100:
                # A login-only page may be an expired session. An optional
                # form beside substantive text, or an enrollment chooser,
                # does not establish that authentication is required.
                state = 'reconnect_needed' if evidence['form'] and len(evidence['text']) < 100 else 'access_unverified'
                self.update(state=state)
                return {'state':state}
            self.update(state='content_visible')
            self._review_token = secrets.token_urlsafe(24)
            self._review_deadline = time.monotonic() + 300
            # Text is returned only to the local UI, never to an Assistant.
            return {'state':'content_visible', 'content':evidence['text'], 'review_token':self._review_token}
        except Exception:
            self.update(state='access_unverified')
            return {'state':'access_unverified'}
        finally:
            if cdp:
                try:
                    cdp.send('Emulation.setScriptExecutionDisabled', {'value':False})
                    cdp.detach()
                except Exception:
                    pass
            self._verify_mode = False


class SiteConnections:
    """Offer-scoped explicit consent; profiles are per account and site origin."""
    def __init__(self, hub, browser_factory=None):
        self.hub = hub
        self.browser_factory = browser_factory or BrowserConnection
        self.lock = threading.RLock()
        self.offers = {}
        self.sessions = {}

    def _source(self, aid, sid):
        self.hub.identity.get_account(aid)
        return self.hub.external_sources._source(aid, sid)

    def _owner(self, aid):
        account = self.hub.identity.get_account(aid)
        return hashlib.sha256((account['origin'] + '\0' + account['canvas_user_id']).encode()).hexdigest()

    def offer(self, aid, sid, extra_origins):
        source = self._source(aid, sid)
        target, secret = clean_url(source['url'])
        if secret or source['access_needed'] or urlsplit(target).scheme != 'https':
            raise InvalidInput('This site requires a safe HTTPS source URL')
        if not isinstance(extra_origins, list) or len(extra_origins) > 5:
            raise InvalidInput('Specify at most five identity-provider origins')
        p = urlsplit(target)
        site = origin(f'{p.scheme}://{p.netloc}/')
        domains = sorted(set([site] + [origin(x) for x in extra_origins]))
        token = secrets.token_urlsafe(24)
        with self.lock:
            # Offers are short-lived and bounded; never store form data or cookies.
            now = time.monotonic()
            self.offers = {k: v for k, v in self.offers.items() if v['expires'] > now}
            if len(self.offers) >= 100:
                raise Conflict('Too many outstanding connection offers')
            self.offers[token] = {'aid':aid, 'sid':sid, 'owner':self._owner(aid), 'target':target, 'site':site, 'domains':domains, 'expires':now+300}
        return {'token':token, 'site':site, 'origins':domains, 'expires_in_seconds':300}

    def start(self, aid, sid, token, consent):
        source = self._source(aid, sid)
        if consent is not True:
            raise InvalidInput('Explicit consent is required before creating a profile')
        if not isinstance(token, str):
            raise InvalidInput('A current connection offer is required')
        with self.lock:
            offer = self.offers.get(token)
            if not offer or offer['aid'] != aid or offer['sid'] != sid or offer['expires'] <= time.monotonic():
                raise Conflict('Connection offer expired or changed; review it again')
            if source['url'] != offer['target'] or self._owner(aid) != offer['owner']:
                raise Conflict('Source changed; review the connection again')
            key = (aid, offer['site'])
            existing = self.sessions.get(key)
            if existing and existing.snapshot()['state'] not in ('closed', 'error'):
                raise Conflict('A site connection is already open for this account')
            if sum(s.snapshot()['state'] not in ('closed', 'error') for s in self.sessions.values()) >= 2:
                raise Conflict('Close another site connection before opening a new one')
            account = self.hub.identity.get_account(aid)
            profile_key = hashlib.sha256((account['origin'] + '\0' + account['canvas_user_id'] + '\0' + offer['site']).encode()).hexdigest()
            profile = self.hub.paths.browser_profiles / 'external-sites' / f'account-{aid}' / profile_key
            # Consume the offer before any browser or profile is created.
            del self.offers[token]
            session = self.browser_factory(profile, offer['target'], offer['domains'])
            session._owner_hash = offer['owner']
            self.sessions[key] = session
            return session.snapshot()

    def _session(self, aid, sid):
        source = self._source(aid, sid)
        p = urlsplit(source['url'])
        site = origin(f'{p.scheme}://{p.netloc}/')
        with self.lock:
            session = self.sessions.get((aid, site))
            return session if session and session.target == source['url'] and session._owner_hash == self._owner(aid) else None

    def status(self, aid, sid):
        session = self._session(aid, sid)
        return session.snapshot() if session else {'state':'not_connected'}

    def action(self, aid, sid, action, arg=None):
        session = self._session(aid, sid)
        if session is None or session.snapshot()['state'] in ('closed', 'error'):
            raise Conflict('Open the site connection first')
        if action not in ('cancel', 'verify', 'approve_post', 'retry_login', 'confirm_content'):
            raise InvalidInput('Unknown connection action')
        return session.call(action, arg)

    def close(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if session.snapshot()['state'] not in ('closed', 'error'):
                try: session.call('cancel', timeout=5)
                except Exception: pass
