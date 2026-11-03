"""Isolated interactive external-site sign-in.

All browser network traffic passes through a pinned, public-address transport.
This module never sends a page, cookie, or profile to an Assistant provider.
"""
from __future__ import annotations

import hashlib
import base64
import http.client
import os
import queue
import re
import secrets
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from ..external_urls import clean_url, public_address, MUTATION
from .. import external_inspection
from ..util import Conflict, InvalidInput
from .external_reader import _PinnedHTTPS, _DNS, MAX_BYTES

MAX_REQUEST = 200_000
MAX_REDIRECTS = 5
REDIRECT_SECONDS = 20
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


def _navigation_url(raw, base=None):
    """Validate a redirect without stripping its in-memory SSO query values."""
    if not isinstance(raw, str) or not raw or len(raw) > 2000 or '\\' in raw or any(ord(c) < 33 for c in raw):
        raise InvalidInput('Invalid redirect target')
    try:
        raw = urljoin(base, raw) if base else raw
        safe, _ = clean_url(raw)
        p, canonical = urlsplit(raw), urlsplit(safe)
        request_origin(safe)  # HTTPS only, no user info or nonstandard port.
        # Match Chromium's URL representation, but keep signed query bytes intact.
        return urlunsplit(('https', canonical.netloc,
                          quote(p.path or '/', safe="/%:@!$&'()*+,;=-._~"),
                          quote(p.query, safe="%/?@!$&()*+,;=:-._~[]"), ''))
    except (ValueError, UnicodeError) as exc:
        raise InvalidInput('Invalid redirect target') from exc


@dataclass(frozen=True)
class _Navigation:
    url: str
    seen: tuple[str, ...]
    deadline: float


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
        for key in ('content-type', 'location', 'www-authenticate', 'cache-control', 'content-security-policy', 'access-control-allow-origin', 'access-control-allow-credentials'):
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
        self.state = {'state': 'opening', 'blocked_origins': [], 'pending_post': None,
                      'blocked_submission_origin':None, 'redirect_blocked':False, 'redirect_reason':None,
                      'http_auth':None, 'popup_blocked':False, 'failure_reason':None, 'http_status':None}
        self._basic_auth = {}
        self._basic_pending = None
        self._credential_redactions = set()
        self._post_approval = None
        self._post_pending = None
        self._request_count = 0
        self._verify_mode = False
        self._review_token = None
        self._review_deadline = 0
        self._verified_digest = None
        self._confirmed_digest = None
        self._page = None
        self._redirect_next = None
        self._redirect_expected = None
        self._cancel_requested = threading.Event()
        self._closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='external-site-login')
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def call(self, action, arg=None, timeout=35):
        if action == 'cancel':
            self._cancel_requested.set()
        future = Future()
        with self.lock:
            if self._closed.is_set() or (action != 'cancel' and self._cancel_requested.is_set()):
                raise Conflict('The site connection is closing or closed')
            self.events.put((action, arg, future))
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # Stale queued actions must not run, but closing intent must survive
            # a slow in-flight request even after its caller stops waiting.
            if action != 'cancel':
                future.cancel()
            raise Conflict('The site browser did not respond; check connection status before retrying')

    def _allowed_origin(self, host):
        if host in self.domains:
            return True
        # Only disclose an origin, never a path, query, or authentication value.
        with self.lock:
            blocked = set(self.state['blocked_origins'])
            if len(blocked) < 10:
                blocked.add(host)
            self.state['blocked_origins'] = sorted(blocked)
        return False

    def _block_redirect(self, reason):
        self.update(redirect_blocked=True, redirect_reason=reason)

    def _queue_redirect(self, request, status, location, navigation):
        """Schedule a fresh GET; never give an HTTP redirect to Chromium.

        Playwright routes only the first request of an HTTP redirect chain.
        Fulfilling a 3xx would therefore bypass this module's checks. Fetching
        the chain ourselves and fulfilling its last body would instead give
        that document the *wrong origin*. Abort and navigate to each checked
        URL so Chromium gives it its real origin and destination-scoped cookies.
        """
        if self._verify_mode:
            self._block_redirect('verification'); return False
        if request.resource_type != 'document':
            self._block_redirect('subresource'); return False
        if status not in (301, 302, 303, 307, 308):
            self._block_redirect('unsupported'); return False
        if request.method.upper() == 'POST' and status in (307, 308):
            # An approval covers one exact POST only, even on the same origin.
            self._block_redirect('post_replay'); return False
        try:
            current = _navigation_url(request.url)
            target = _navigation_url(location, current)
        except InvalidInput:
            self._block_redirect('unsafe_target'); return False
        if not self._allowed_origin(request_origin(target)):
            self._block_redirect('unapproved_origin'); return False
        chain = navigation or _Navigation(current, (current,), time.monotonic() + REDIRECT_SECONDS)
        if time.monotonic() >= chain.deadline:
            self._block_redirect('timeout'); return False
        if target in chain.seen:
            self._block_redirect('loop'); return False
        if len(chain.seen) > MAX_REDIRECTS:
            self._block_redirect('limit'); return False
        self._redirect_next = _Navigation(target, (*chain.seen, target), chain.deadline)
        return True

    def _navigate(self, page, url=None):
        if url is not None:
            url = _navigation_url(url)
            self._redirect_next = _Navigation(url, (url,), time.monotonic() + REDIRECT_SECONDS)
        while self._redirect_next and not self._cancel_requested.is_set():
            navigation, self._redirect_next = self._redirect_next, None
            remaining = navigation.deadline - time.monotonic()
            if remaining <= 0:
                self._block_redirect('timeout'); break
            self._redirect_expected = navigation
            try:
                # No copied POST body, authorization, CSRF or source headers.
                # Every new GET re-enters _route and pinned_request, including
                # fresh DNS validation before any destination connection.
                page.goto(navigation.url, wait_until='domcontentloaded', timeout=min(15000, max(1, int(remaining * 1000))))
            except Exception:
                pass  # An intercepted redirect is intentionally aborted.
            finally:
                self._redirect_expected = None

    def _route(self, route, context):
        if not external_inspection.ENABLED:
            route.abort()
            return
        request = route.request
        navigation = None
        try:
            if getattr(self, '_cancel_requested', None) and self._cancel_requested.is_set():
                route.abort(); return
            host = request_origin(request.url)
            if self._verify_mode and (request.method != 'GET' or request.url != self.target or request.resource_type != 'document'):
                route.abort(); return
            if not self._allowed_origin(host):
                route.abort(); return
            # Approved identity origins can receive their own documents and
            # assets, but a page cannot use fetch/scripts/images to send data
            # to a different approved origin. Cross-origin document navigation
            # remains necessary for explicit SSO, with POSTs approved separately.
            if request.resource_type == 'document' and request.frame.parent_frame is not None:
                route.abort(); return
            if request.resource_type == 'document' and getattr(self, '_page', None) is not None and request.frame.page != self._page:
                self.update(popup_blocked=True)
                route.abort(); return  # Popups cannot redirect the owned window.
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
            if request.resource_type == 'document':
                expected = getattr(self, '_redirect_expected', None)
                self._redirect_expected = None
                if expected:
                    if method != 'GET' or _navigation_url(request.url) != expected.url:
                        # A changed navigation must not silently start a new
                        # chain and escape the hop/deadline limits.
                        self._block_redirect('navigation_changed'); route.abort(); return
                    navigation = expected
                    if time.monotonic() >= navigation.deadline:
                        self._block_redirect('timeout'); route.abort(); return
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
            if navigation and len(navigation.seen) > 1:
                # These are the NEW destination request's cookies, selected by
                # Chromium. Never copy credentials/headers from the prior hop.
                headers = {k: v for k, v in headers.items() if k.lower() in
                           ('accept', 'accept-language', 'user-agent', 'cookie')}
            # App-entered Basic credentials never enter Chromium, its cache or
            # profile. They authorize only the challenged exact document GET,
            # never an asset, POST or another URL (including redirect hops).
            basic = getattr(self, '_basic_auth', {}).get(request.url)
            if basic and method == 'GET' and request.resource_type == 'document' and not (navigation and len(navigation.seen) > 1):
                headers = {**headers, 'authorization': basic}
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
            headers = {k.lower(): v for k, v in headers.items()}
            challenge = headers.pop('www-authenticate', '')
            if request.resource_type == 'document':
                self.update(http_status=status, failure_reason=None)
                if status == 401 and challenge:
                    basic_supported = method == 'GET' and bool(re.search(r'(?:^|,)\s*Basic(?:\s|$)', challenge, re.I))
                    token = secrets.token_urlsafe(24)
                    self._basic_pending = (request.url, token, 'utf-8' if re.search(r'charset\s*=\s*"?UTF-8', challenge, re.I) else 'latin-1') if basic_supported else None
                    getattr(self, '_basic_auth', {}).pop(request.url, None)
                    self.update(http_auth={'scheme':'basic' if basic_supported else 'unsupported', 'origin':host, 'request_id':token},
                                failure_reason='http_authentication')
                    # Native auth prompts cannot work through an intercepted
                    # transport; use the guarded local entry UI instead.
                elif status in (401, 403):
                    self._basic_pending = None
                    getattr(self, '_basic_auth', {}).pop(request.url, None)
                    self.update(http_auth=None, failure_reason='access_denied')
                elif status >= 400:
                    self.update(failure_reason='page_unavailable')
                elif status == 200:
                    self._basic_pending = None
                    self.update(http_auth=None)
            if navigation and time.monotonic() >= navigation.deadline:
                self._block_redirect('timeout'); route.abort(); return
            if 300 <= status < 400:
                if self._queue_redirect(request, status, headers.get('location'), navigation):
                    _set_cookies(context, request.url, cookies)
                route.abort(); return
            _set_cookies(context, request.url, cookies)
            route.fulfill(status=status, headers=headers, body=body)
        except Exception:
            if request.resource_type == 'document':
                self.update(failure_reason='request_failed')
            if navigation and len(navigation.seen) > 1:
                self._block_redirect('request_failed')
            try: route.abort()
            except Exception: pass

    def _run(self):
        launched = False
        try:
            external_inspection.require_enabled()
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
                browser_options = {}
                if os.environ.get('CANVAS_HUB_CHROMIUM'):
                    browser_options['executable_path'] = os.environ['CANVAS_HUB_CHROMIUM']
                elif os.environ.get('CANVAS_HUB_BROWSER_CHANNEL'):
                    browser_options['channel'] = os.environ['CANVAS_HUB_BROWSER_CHANNEL']
                context = pw.chromium.launch_persistent_context(str(self.profile), headless=self.headless,
                    accept_downloads=False, service_workers='block', offline=True, args=['--disable-webrtc'], **browser_options)
                launched = True
                context.route_web_socket('**/*', lambda ws: ws.close())
                context.add_init_script("""Object.defineProperty(window,'RTCPeerConnection',{value:undefined});
                    Object.defineProperty(window,'webkitRTCPeerConnection',{value:undefined});
                    const d=Object.getOwnPropertyDescriptor(Document.prototype,'cookie');
                    Object.defineProperty(Document.prototype,'cookie',{configurable:false,get:d.get,
                      set:function(v){if(!/;\\s*domain\\s*=/i.test(String(v)))d.set.call(this,v)}});""")
                context.route('**/*', lambda route: self._route(route, context))
                page = context.pages[0] if context.pages else context.new_page()
                self._page = page
                def popup(opened):
                    if opened != page:
                        self.update(popup_blocked=True)
                        try: opened.close()
                        except Exception: pass
                context.on('page', popup)
                # A restored profile cannot send traffic before interception exists.
                context.set_offline(False)
                self._navigate(page, self.target)
                self.update(state='login_open')
                while True:
                    try:
                        action, arg, future = self.events.get_nowait()
                    except queue.Empty:
                        try:
                            if self._redirect_next:
                                self._navigate(page)
                            else:
                                page.wait_for_timeout(200)
                        except Exception: break
                        continue
                    try:
                        if not future.set_running_or_notify_cancel():
                            continue
                        if action != 'cancel':
                            external_inspection.require_enabled()
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
                        elif action == 'http_auth':
                            future.set_result(self._enter_http_auth(page, arg))
                        elif action == 'retry_login':
                            self._review_token = None
                            self._confirmed_digest = None
                            self.update(state='login_open', redirect_blocked=False, redirect_reason=None,
                                        popup_blocked=False, failure_reason=None)
                            self._navigate(page, self.target)
                            future.set_result({'state':'login_open'})
                        elif action == 'confirm_content':
                            if not self._review_token or time.monotonic() > self._review_deadline or arg != self._review_token or page.url != self.target or self.snapshot()['state'] != 'content_visible':
                                self._review_token = None
                                raise Conflict('Review the requested page again before confirming')
                            self._review_token = None
                            self._confirmed_digest = self._verified_digest
                            self.update(state='connected')
                            future.set_result({'state':'connected'})
                        elif action == 'analysis_snapshot':
                            expected = self._confirmed_digest
                            if not expected or self.snapshot()['state'] != 'connected':
                                raise Conflict('Confirm the requested course page first')
                            result = self._verify(page)
                            if result.get('state') != 'content_visible' or self._verified_digest != expected:
                                self._confirmed_digest = None
                                raise Conflict('The page changed or access expired; inspect and confirm it again')
                            self._review_token = None
                            self._confirmed_digest = expected
                            self.update(state='connected')
                            future.set_result({'content':result['content']})
                        else:
                            raise InvalidInput('Unknown action')
                    except Exception as exc:
                        future.set_exception(exc if isinstance(exc, (Conflict, InvalidInput)) else Conflict('Site check failed; previous data retained'))
                    finally:
                        # Do not retain local credential-entry requests in the
                        # event loop while waiting for the next user action.
                        if action == 'http_auth' and isinstance(arg, dict):
                            arg.clear()
                        arg = None
                context.close()
        except Exception:
            self.update(state='error', failure_reason='browser_unavailable' if not launched else 'browser_closed')
            return
        finally:
            with self.lock:
                self._closed.set()
            # Pending credential submissions must not survive a closed browser.
            while True:
                try:
                    pending_action, pending_arg, pending_future = self.events.get_nowait()
                except queue.Empty:
                    break
                if pending_action == 'http_auth' and isinstance(pending_arg, dict):
                    pending_arg.clear()
                if not pending_future.done():
                    pending_future.set_exception(Conflict('The site connection closed'))
            self._basic_auth.clear()
            self._basic_pending = None
            self._credential_redactions.clear()
            self.update(http_auth=None)
        self.update(state='closed')

    def _enter_http_auth(self, page, arg):
        pending = self._basic_pending
        if self._cancel_requested.is_set() or not isinstance(arg, dict) or not pending or arg.get('challenge') != self.snapshot().get('http_auth'):
            raise Conflict('The authentication challenge changed; reopen the requested page')
        if getattr(self, '_login_method', 'browser') == 'basic' and pending[0] != self.target:
            raise Conflict('The Basic sign-in page changed; no credentials were sent')
        username, password = arg.get('username'), arg.get('password')
        if (not isinstance(username, str) or not isinstance(password, str) or not 1 <= len(username) <= 1024
                or not 1 <= len(password) <= 4096 or ':' in username
                or any(ord(c) < 32 or ord(c) == 127 for c in username + password)):
            raise InvalidInput('Enter a valid username and password in the local sign-in form')
        try:
            encoded = base64.b64encode((username + ':' + password).encode(pending[2])).decode('ascii')
        except UnicodeError:
            raise InvalidInput('This site does not support these credential characters') from None
        self._basic_auth[pending[0]] = 'Basic ' + encoded
        self._credential_redactions.update((username, password, encoded))
        self._basic_pending = None
        self._review_token = self._confirmed_digest = None
        self.update(http_auth=None, state='login_open')
        self._navigate(page, pending[0])
        return self.snapshot()

    def _verify(self, page):
        cdp = None
        self._review_token = None
        self._verified_digest = None
        self._basic_pending = None
        self.update(http_status=None, http_auth=None)
        def failure_state():
            state = self.snapshot()
            if state.get('http_status') in (401, 403):
                return 'reconnect_needed' if state.get('http_auth') else 'access_denied'
            return 'access_unverified'
        try:
            # Verification is a single GET to the already approved source. No
            # page scripts, subresources, redirects or form submissions run.
            self._redirect_next = self._redirect_expected = None
            self._verify_mode = True
            cdp = page.context.new_cdp_session(page)
            cdp.send('Emulation.setScriptExecutionDisabled', {'value':True})
            response = page.goto(self.target, wait_until='domcontentloaded', timeout=15000)
            if page.url != self.target:
                self.update(state=failure_state())
                return {'state':self.snapshot()['state']}
            if not response or response.status != 200:
                self.update(state=('reconnect_needed' if self.snapshot().get('http_auth') else 'access_denied')
                            if response and response.status in (401, 403) else 'access_unverified')
                return {'state':self.snapshot()['state']}
            evidence = page.evaluate('''() => {
                const area = document.querySelector('main,article') || document.body;
                if (!area) return {form:!!document.querySelector('input[type=password],form[action*="login" i]'), chooser:false, text:''};
                const walker = document.createTreeWalker(area,NodeFilter.SHOW_TEXT);
                let text='', count=0, node;
                while ((node=walker.nextNode()) && count++ < 5000 && text.length < 200000) {
                  const el=node.parentElement;
                  if (!el || el.closest('form,input,textarea,select,script,style,iframe,object,noscript,[hidden],[aria-hidden="true"]')) continue;
                  let visible=true;
                  for(let p=el; p && p!==area.parentElement; p=p.parentElement){
                    const style=getComputedStyle(p);
                    if(style.display==='none'||style.visibility==='hidden'||style.opacity==='0'){visible=false;break;}
                  }
                  if(visible) text += node.nodeValue + '\\n';
                }
                text=text.trim().slice(0,200000);
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
            for secret in sorted(self._credential_redactions, key=len, reverse=True):
                evidence['text'] = evidence['text'].replace(secret, '[credential removed]')
            self._verified_digest = hashlib.sha256(evidence['text'].encode()).hexdigest()
            self._review_token = secrets.token_urlsafe(24)
            self._review_deadline = time.monotonic() + 300
            # Text is returned only to the local UI, never to an Assistant.
            return {'state':'content_visible', 'content':evidence['text'], 'review_token':self._review_token}
        except Exception:
            # Chromium may reject an empty denial response before goto returns
            # a Response. Keep the trusted transport's current status evidence.
            self.update(state=failure_state())
            return {'state':self.snapshot()['state']}
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

    def https_source(self, aid, sid, consent):
        external_inspection.require_enabled()
        source = self._source(aid, sid)
        p = urlsplit(source['url'])
        if consent is not True or p.scheme != 'http' or source['access_needed']:
            raise InvalidInput('Review the HTTPS version of this public HTTP address first')
        target = urlunsplit(('https', p.netloc, p.path, p.query, ''))
        # A different scheme is a separately reviewed source. Do not rewrite
        # existing evidence or send credentials over an insecure connection.
        service = self.hub.external_sources
        new_sid = service.register(aid, target, course_id=source['course_id'], title=target)
        with self.hub.db.tx() as c:
            c.execute('INSERT OR IGNORE INTO external_source_origins SELECT ?,item_id FROM external_source_origins WHERE source_id=?', (new_sid, sid))
        return service.get(aid, new_sid)

    def offer(self, aid, sid, extra_origins, *, method='browser'):
        external_inspection.require_enabled()
        source = self._source(aid, sid)
        target, secret = clean_url(source['url'])
        if secret or source['access_needed'] or urlsplit(target).scheme != 'https':
            raise InvalidInput('This site requires a safe HTTPS source URL')
        if not isinstance(extra_origins, list) or len(extra_origins) > 5:
            raise InvalidInput('Specify at most five identity-provider origins')
        if method not in ('browser', 'basic') or (method == 'basic' and extra_origins):
            raise InvalidInput('Choose browser sign-in or Basic sign-in for this site only')
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
            self.offers[token] = {'aid':aid, 'sid':sid, 'owner':self._owner(aid), 'target':target, 'site':site, 'domains':domains, 'method':method, 'expires':now+300}
        return {'token':token, 'site':site, 'origins':domains, 'method':method, 'expires_in_seconds':300}

    def start(self, aid, sid, token, consent):
        external_inspection.require_enabled()
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
            # Basic entry stays in the local form; do not expose the deliberately
            # suppressed native challenge as a misleading browser error window.
            options = {'headless': True} if offer['method'] == 'basic' else {}
            session = self.browser_factory(profile, offer['target'], offer['domains'], **options)
            session._owner_hash = offer['owner']
            session._login_method = offer['method']
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
        source = self._source(aid, sid)
        if urlsplit(source['url']).scheme != 'https':
            return {'state':'not_supported', 'message':'Site connections require HTTPS. Public link analysis is still available.'}
        session = self._session(aid, sid)
        return {**session.snapshot(), 'method':session._login_method,
                'origins':sorted(session.domains)} if session else {'state':'not_connected'}

    def action(self, aid, sid, action, arg=None):
        if action != 'cancel':
            external_inspection.require_enabled()
        session = self._session(aid, sid)
        if session is None or session.snapshot()['state'] in ('closed', 'error'):
            raise Conflict('Open the site connection first')
        if action not in ('cancel', 'verify', 'http_auth', 'approve_post', 'retry_login', 'confirm_content', 'analysis_snapshot'):
            raise InvalidInput('Unknown connection action')
        result = session.call(action, arg)
        if action == 'analysis_snapshot':
            return {**result, 'url':session.target, 'owner':session._owner_hash}
        return result

    def close(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if session.snapshot()['state'] not in ('closed', 'error'):
                try: session.call('cancel', timeout=5)
                except Exception: pass
