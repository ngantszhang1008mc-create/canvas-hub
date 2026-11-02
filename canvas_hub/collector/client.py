"""Canvas access through the signed-in browser context.

Two kinds of reads, both using the *browser's own session* (no API token):

* ``page_snapshot`` navigates a real tab and reads the rendered DOM (used for
  Courses -> All Courses, which is the authoritative enrollment inventory).
* ``api_get`` / ``api_list`` issue same-origin GET requests to Canvas's
  documented REST API (/api/v1/...) from the browser context, exactly as the
  Canvas web UI does. Pagination follows the documented Link headers.

Only GET requests are ever issued. Every call is rate limited and retried a
bounded number of times. Authentication failures raise AuthRequired and are
never retried in a loop.
"""

from __future__ import annotations

import json
import re
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlsplit

from ..urls import same_origin

ACCEPT = "application/json+canvas-string-ids, application/json;q=0.9, */*;q=0.5"


class CollectorError(Exception):
    pass


class AuthRequired(CollectorError):
    """The Canvas session is missing or expired."""


class AccessDenied(CollectorError):
    def __init__(self, msg, status=403):
        super().__init__(msg)
        self.status = status


class NotFoundError(CollectorError):
    status = 404


class RateLimited(CollectorError):
    pass


class TransientError(CollectorError):
    pass


class Cancelled(CollectorError):
    pass


class RateGate:
    """Shared origin budget, cancellation-aware backoff and a short circuit.

    Limits request starts, not just each worker individually. API slots do not
    cover binary downloads: those have a separate bounded worker queue.
    """

    def __init__(self, min_interval=0.35, *, failure_limit=6, cooldown=30, max_concurrent=1):
        self.min_interval = min_interval
        self.failure_limit = failure_limit
        self.cooldown = cooldown
        self.max_concurrent = max_concurrent
        self._lock = threading.Lock()
        self._last = float("-inf")
        self._blocked_until = 0.0
        self._circuit_until = 0.0
        self._failures = 0
        self._active = 0

    def wait(self, should_stop):
        while True:
            if should_stop():
                raise Cancelled("stop requested")
            with self._lock:
                now = time.monotonic()
                if now < self._circuit_until:
                    raise RateLimited("Canvas request circuit is open; retry after cooldown")
                if self._circuit_until:
                    self._circuit_until = 0.0
                    self._failures = 0
                remaining = max(self._last + self.min_interval, self._blocked_until) - now
                if remaining <= 0:
                    self._last = now
                    return
            time.sleep(min(remaining, 0.05))

    @contextmanager
    def api_slot(self, should_stop):
        while True:
            if should_stop():
                raise Cancelled("stop requested")
            with self._lock:
                if self._active < max(1, min(2, self.max_concurrent)):
                    self._active += 1
                    break
            time.sleep(0.05)
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1

    def failure(self, delay=0):
        with self._lock:
            self._failures += 1
            now = time.monotonic()
            self._blocked_until = max(self._blocked_until, now + min(60, max(0, delay)))
            if self._failures >= self.failure_limit:
                self._circuit_until = now + self.cooldown

    def success(self):
        with self._lock:
            self._failures = 0


_gates = {}
_gates_lock = threading.Lock()


def shared_rate_gate(origin):
    key = origin.rstrip("/").lower()
    with _gates_lock:
        return _gates.setdefault(key, RateGate())


def _dispose(response):
    dispose = getattr(response, "dispose", None)
    if dispose:
        dispose()


def _retry_after(response):
    try:
        return min(60, max(0, float(response.headers.get("retry-after", 0))))
    except (ValueError, TypeError):
        return 0


@dataclass
class ListResult:
    items: list
    complete: bool
    pages: int
    reason: str | None = None


def _strip_json(text: str) -> Any:
    t = text.lstrip()
    if t.startswith("while(1);"):
        t = t[len("while(1);"):]
    return json.loads(t)


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^";]+)"?', part)
        if m and m.group(2) == "next":
            return m.group(1)
    return None


def _looks_like_login(url: str, origin: str) -> bool:
    if not same_origin(url, origin):
        return True
    path = urlsplit(url).path
    return path.startswith(("/login", "/saml", "/cas", "/users/sign_in")) or path in ("/logout",)


class CanvasClient:
    """Wraps a Playwright BrowserContext. Must be used only on the thread that
    owns the context."""

    def __init__(self, context, origin: str, *, min_interval: float = 0.35, max_retries: int = 3,
                 should_stop: Callable[[], bool] | None = None, timeout_ms: int = 45000,
                 rate_gate: RateGate | None = None):
        self.context = context
        self.origin = origin.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.should_stop = should_stop or (lambda: False)
        self.timeout_ms = timeout_ms
        # Explicit non-default intervals are used by local fixture providers.
        self.rate_gate = rate_gate or (shared_rate_gate(self.origin) if min_interval == 0.35 else RateGate(min_interval))
        self.requests = 0
        self.retries = 0
        self.bytes_received = 0

    # ---- low level --------------------------------------------------------
    def _throttle(self):
        self.rate_gate.wait(self.should_stop)

    def metrics(self):
        return {"requests": self.requests, "retries": self.retries, "bytes": self.bytes_received}

    def _retry(self, attempt, message, *, delay=None, error_type=TransientError, rate_gate=None):
        (rate_gate or self.rate_gate).failure(delay=delay if delay is not None else 2 ** attempt)
        if attempt > self.max_retries:
            raise error_type(message) from None
        self.retries += 1

    def _abs(self, path_or_url: str) -> str:
        url = path_or_url if path_or_url.startswith("http") else urljoin(self.origin + "/", path_or_url.lstrip("/"))
        if not same_origin(url, self.origin):
            raise CollectorError("refusing cross-origin API request")
        return url

    def _get(self, url: str, *, accept: str = ACCEPT, max_redirects: int = 0):
        attempt = hops = 0
        while True:
            try:
                with self.rate_gate.api_slot(self.should_stop):
                    self._throttle()
                    self.requests += 1
                    resp = self.context.request.get(url, headers={"Accept": accept}, max_redirects=max_redirects,
                                                    fail_on_status_code=False, timeout=self.timeout_ms)
            except (Cancelled, RateLimited):
                raise
            except Exception as e:
                if "closed" in str(e).lower():
                    raise AuthRequired("browser window was closed") from None
                attempt += 1
                self._retry(attempt, "Canvas network request failed")
                continue
            st = resp.status
            if st in (301, 302, 303, 307, 308):
                target = urljoin(url, resp.headers.get("location", ""))
                _dispose(resp)
                if _looks_like_login(target, self.origin):
                    raise AuthRequired("redirected to sign-in")
                hops += 1
                if hops > 5:
                    raise CollectorError("too many redirects")
                url = target
                continue
            if st == 401:
                _dispose(resp)
                raise AuthRequired("Canvas returned 401 (not signed in or session expired)")
            if st == 429 or (st == 403 and "rate limit" in (resp.text() or "").lower()):
                attempt += 1
                delay = max(2 ** (attempt + 1), _retry_after(resp))
                _dispose(resp)
                self._retry(attempt, "Canvas rate limit persisted after retries", delay=delay, error_type=RateLimited)
                continue
            if st >= 500:
                attempt += 1
                _dispose(resp)
                self._retry(attempt, f"Canvas server error {st}")
                continue
            self.rate_gate.success()
            return resp

    def api_get(self, path: str, params: dict | list | None = None) -> Any:
        url = self._abs(path)
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        resp = self._get(url)
        try:
            return self._decode(resp, url)
        finally:
            _dispose(resp)

    def _decode(self, resp, url):
        st = resp.status
        if st == 403:
            raise AccessDenied(f"access denied for {urlsplit(url).path}", 403)
        if st == 404:
            raise NotFoundError(f"not found: {urlsplit(url).path}")
        if st >= 400:
            raise CollectorError(f"HTTP {st} for {urlsplit(url).path}")
        ctype = resp.headers.get("content-type", "")
        text = resp.text()
        self.bytes_received += len(text.encode("utf-8"))
        if "json" not in ctype and not text.lstrip().startswith(("while(1);", "{", "[")):
            if "<html" in text[:500].lower():
                raise AuthRequired("received an HTML page instead of data (probably the sign-in page)")
            raise CollectorError(f"unexpected content type {ctype}")
        return _strip_json(text)

    def api_list(self, path: str, params: dict | list | None = None, *, max_pages: int = 200) -> ListResult:
        url = self._abs(path)
        p = list(params.items()) if isinstance(params, dict) else list(params or [])
        if not any(k == "per_page" for k, _ in p):
            p.append(("per_page", 100))
        url += ("&" if "?" in url else "?") + urlencode(p, doseq=True)
        items: list = []
        pages = 0
        while url:
            resp = self._get(url)
            try:
                data = self._decode(resp, url)
                nxt = _next_link(resp.headers.get("link"))
            finally:
                _dispose(resp)
            pages += 1
            if isinstance(data, dict):
                # Some endpoints wrap lists; callers handle dict results via api_get.
                data = data.get("items") if isinstance(data.get("items"), list) else [data]
            items.extend(data)
            if nxt and not same_origin(nxt, self.origin):
                return ListResult(items, False, pages, "pagination link pointed off-origin; stopped")
            url = nxt
            if url and pages >= max_pages:
                return ListResult(items, False, pages, f"stopped after {max_pages} pages")
        return ListResult(items, True, pages)

    def download(self, url: str, max_bytes: int) -> tuple[bytes, str | None, str]:
        """Read an authorized file with bounded retries and response cleanup."""
        u = url if url.startswith("http") else self._abs(url)
        attempt = hops = 0
        while True:
            target = urlsplit(u)
            if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password:
                raise CollectorError("invalid download URL")
            gate = self.rate_gate if same_origin(u, self.origin) else shared_rate_gate(f"{target.scheme}://{target.netloc}")
            gate.wait(self.should_stop)
            self.requests += 1
            try:
                # Follow redirects ourselves so every origin observes its own
                # rate/backoff budget. Cookie scoping remains Playwright's job.
                resp = self.context.request.get(u, max_redirects=0, fail_on_status_code=False, timeout=self.timeout_ms)
            except (Cancelled, AuthRequired, RateLimited):
                raise
            except Exception:
                attempt += 1
                self._retry(attempt, "Canvas download request failed", rate_gate=gate)
                continue
            try:
                if resp.status in (301, 302, 303, 307, 308):
                    hops += 1
                    if hops > 10:
                        raise CollectorError("too many download redirects")
                    location = resp.headers.get("location")
                    if not location:
                        raise CollectorError("download redirect missing destination")
                    destination = urljoin(u, location)
                    if target.scheme == "https" and urlsplit(destination).scheme != "https":
                        raise CollectorError("refusing insecure download redirect")
                    if same_origin(destination, self.origin) and _looks_like_login(destination, self.origin):
                        raise AuthRequired("download redirected to sign-in")
                    u = destination
                    continue
                if resp.status == 401:
                    raise AuthRequired("download requires sign-in")
                if resp.status == 429 or (resp.status == 403 and "rate limit" in (resp.text() or "").lower()):
                    attempt += 1
                    self._retry(attempt, "download rate limit persisted after retries",
                                delay=max(2 ** (attempt + 1), _retry_after(resp)), error_type=RateLimited, rate_gate=gate)
                    continue
                if resp.status == 403:
                    raise AccessDenied("download not permitted", 403)
                if resp.status == 404:
                    raise NotFoundError("file not found")
                if resp.status >= 500:
                    attempt += 1
                    self._retry(attempt, f"download server error {resp.status}", rate_gate=gate)
                    continue
                if resp.status >= 400:
                    raise CollectorError(f"download HTTP {resp.status}")
                if _looks_like_login(resp.url, self.origin) and same_origin(resp.url, self.origin):
                    raise AuthRequired("download redirected to sign-in")
                length = resp.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise CollectorError(f"file larger than limit ({length} bytes)")
                body = resp.body()
                self.bytes_received += len(body)
                if self.should_stop():
                    raise Cancelled("stop requested")
                if len(body) > max_bytes:
                    raise CollectorError(f"file larger than limit ({len(body)} bytes)")
                ctype = resp.headers.get("content-type")
                if ctype and "text/html" in ctype and b"<form" in body[:20000].lower() and b"password" in body[:20000].lower():
                    raise AuthRequired("download returned a sign-in page")
                gate.success()
                return body, ctype, resp.url
            finally:
                _dispose(resp)

    # ---- DOM ----------------------------------------------------------------
    def page_snapshot(self, path: str, extract_js: str, *, settle_selector: str | None = None,
                      settle_ms: int = 800, timeout_ms: int = 30000) -> tuple[str, Any]:
        """Open a page in a tab, wait for asynchronous content to settle and run
        an extraction script. Returns (final_url, extracted_value)."""
        url = self._abs(path)
        self._throttle()
        page = self.context.new_page()
        try:
            self.requests += 1
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            final = page.url
            if _looks_like_login(final, self.origin):
                raise AuthRequired("page navigation landed on the sign-in page")
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:  # noqa: BLE001 - long-polling pages never go idle
                pass
            if settle_selector:
                try:
                    page.wait_for_selector(settle_selector, timeout=timeout_ms)
                except Exception:  # noqa: BLE001
                    pass
            # Settle: identical extraction twice in a row, settle_ms apart.
            prev = None
            value = None
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                value = page.evaluate(extract_js)
                if value == prev:
                    break
                prev = value
                page.wait_for_timeout(settle_ms)
            return page.url, value
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
