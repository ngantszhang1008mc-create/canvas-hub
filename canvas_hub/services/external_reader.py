"""Bounded public GET reader. No browser, cookies, proxy auth, generated code or scripts.

DNS results are checked and the connection is pinned to an approved IP. TLS still
verifies the original host. Redirects are validated before another connection.
The injectable transport is a Python test seam, never a configuration option.
"""
from __future__ import annotations

import concurrent.futures
import http.client
import re
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..external_urls import clean_url, public_address
from ..util import InvalidInput, parse_iso

MAX_BYTES = 1_000_000
_DNS = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="external-dns")

SELECTOR = re.compile(r'^(?:[a-zA-Z][\w-]*)?(?:[.#][\w-]+)?(?:\[[\w-]+(?:="[\w .:-]+")?\])?$')


def check_selector(value, *, self_allowed=False):
    if self_allowed and value == ':self':
        return value
    if not isinstance(value, str) or not value or len(value) > 150 or not SELECTOR.fullmatch(value):
        raise InvalidInput('Use one simple tag, .class, #id or [attribute] selector; executable expressions are unsupported')
    return value


def validate_spec(raw):
    if not isinstance(raw, dict) or set(raw) - {'version', 'pages', 'format', 'records', 'limitations'}:
        raise InvalidInput('Adapter supports version, pages, format, records, limitations only; custom code requires manual review')
    if type(raw.get('version')) is not int or raw.get('version') != 1 or raw.get('format') not in ('html', 'text', 'pdf'):
        raise InvalidInput('Adapter version 1 supports html, text or pdf')
    pages = raw.get('pages')
    if not isinstance(pages, list) or not 1 <= len(pages) <= 8:
        raise InvalidInput('Approve 1–8 exact page URLs; domain-wide crawling is unsupported')
    urls = []
    for url in pages:
        safe, needed = clean_url(url)
        if needed:
            raise InvalidInput('Adapters are public-only; signed URLs and site login require a connected agent')
        urls.append(safe)
    if len(set(urls)) != len(urls):
        raise InvalidInput('Duplicate adapter page')
    limits = raw.get('limitations', '')
    if not isinstance(limits, str) or len(limits) > 2000:
        raise InvalidInput('Limitations must be short text')
    out = {'version': 1, 'pages': urls, 'format': raw['format'], 'limitations': limits}
    record = raw.get('records')
    if record is not None:
        if out['format'] != 'html' or not isinstance(record, dict) or set(record) != {'selector', 'kind', 'fields'}:
            raise InvalidInput('Structured records require HTML selector, kind and fields')
        if record['kind'] not in ('assignment', 'material'):
            raise InvalidInput('Only verified assignment/material fields are supported; submission/grades stay unknown')
        check_selector(record['selector'])
        fields = record['fields']
        if not isinstance(fields, dict) or not {'id', 'title'} <= set(fields) or set(fields) - {'id', 'title', 'due_at', 'points_possible', 'body'}:
            raise InvalidInput('Records need id/title and optional due_at, points_possible, body')
        clean_fields = {}
        for name, field in fields.items():
            if not isinstance(field, dict) or set(field) - {'selector', 'attribute'} or 'selector' not in field:
                raise InvalidInput('Field extraction needs selector and optional attribute')
            check_selector(field['selector'], self_allowed=True)
            attr = field.get('attribute')
            if attr is not None and (not isinstance(attr, str) or not re.fullmatch(r'(?:data-[\w-]+|datetime|content|title)', attr)):
                raise InvalidInput('Only text and data attributes are supported')
            clean_fields[name] = dict(field)
        out['records'] = {**record, 'fields': clean_fields}
    return out


@dataclass
class Read:
    url: str
    status: int
    content_type: str
    body: bytes
    location: str | None = None


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, timeout=8, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def public_get(url):
    p = urlsplit(url)
    addresses = _DNS.submit(socket.getaddrinfo, p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM).result(timeout=5)
    if not addresses or any(not public_address(a[4][0]) for a in addresses):
        raise InvalidInput('DNS resolved to a private/internal address')
    ip = addresses[0][4][0]
    if p.scheme == 'https':
        conn = _PinnedHTTPS(p.hostname, ip)
    else:
        conn = http.client.HTTPConnection(ip, timeout=8)
    started = time.monotonic()
    try:
        conn.request('GET', (p.path or '/') + ('?' + p.query if p.query else ''), headers={
            'Host': p.netloc, 'User-Agent': 'CanvasHub-PublicReader/1.0', 'Accept-Encoding': 'identity',
            'Accept': 'text/html,text/plain,application/pdf'})
        resp = conn.getresponse()
        if resp.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise InvalidInput('Compressed external responses are unsupported')
        chunks, total = [], 0
        while True:
            chunk = resp.read1(64_000)
            total += len(chunk)
            if total > MAX_BYTES or time.monotonic() - started > 15:
                raise InvalidInput('External response exceeded size/time limit')
            if not chunk:
                break
            chunks.append(chunk)
        return Read(url, resp.status, resp.getheader('Content-Type', '').split(';')[0].lower(),
                    b''.join(chunks), resp.getheader('Location'))
    finally:
        conn.close()


class PublicReader:
    def __init__(self, transport=None):
        self.transport = transport or public_get

    def fetch(self, url, approved):
        for _ in range(4):
            safe, secret = clean_url(url)
            if secret or safe not in approved:
                raise InvalidInput('URL/redirect is outside exact approved scope')
            r = self.transport(safe)
            if len(r.body) > MAX_BYTES:
                raise InvalidInput('External response exceeded size limit')
            if r.status in (301, 302, 303, 307, 308):
                if not r.location:
                    raise InvalidInput('Redirect lacks destination')
                url = urljoin(safe, r.location)
                continue
            if r.status in (401, 403):
                raise InvalidInput('Site access requires login/permission', code='needs_login')
            if r.status != 200:
                raise InvalidInput('Site read failed; previous observations retained', code='stale')
            return r
        raise InvalidInput('Too many external redirects')

    def extract(self, r, spec):
        expected = {'html': ('text/html', 'application/xhtml+xml'), 'text': ('text/plain',), 'pdf': ('application/pdf',)}
        if r.content_type not in expected[spec['format']]:
            raise InvalidInput('Content type changed; reanalyse this source', code='needs_attention')
        records = []
        if spec['format'] == 'pdf':
            parsed = subprocess.run([sys.executable, '-m', 'canvas_hub.services.external_pdf'], input=r.body,
                capture_output=True, timeout=12, check=True)
            text = parsed.stdout.decode('utf-8', errors='replace')[:200_000]
        elif spec['format'] == 'text':
            text = r.body.decode('utf-8', errors='replace')[:200_000]
        else:
            soup = BeautifulSoup(r.body, 'html.parser')
            if soup.select('input[type="password"]') or soup.select('form[action*="login"]'):
                raise InvalidInput('Login page is not course content', code='needs_login')
            for tag in soup(['script', 'style', 'iframe', 'object', 'noscript', 'form']):
                tag.decompose()
            text = soup.get_text(' ', strip=True)[:200_000]
            rec = spec.get('records')
            if rec:
                nodes = soup.select(rec['selector'], limit=301)
                if not nodes or len(nodes) > 300:
                    raise InvalidInput('Record template missing or over limit; reanalyse', code='needs_attention')
                for node in nodes:
                    fields = {}
                    for field, rule in rec['fields'].items():
                        element = node if rule['selector'] == ':self' else node.select_one(rule['selector'])
                        value = element.get(rule['attribute']) if element and rule.get('attribute') else element.get_text(' ', strip=True) if element else None
                        if not isinstance(value, str) or not value.strip():
                            raise InvalidInput('A declared source field is missing; previous values retained', code='needs_attention')
                        value = value.strip()
                        if field == 'due_at':
                            dt = parse_iso(value)
                            if dt is None:
                                raise InvalidInput('Deadline must be an explicit ISO timestamp with timezone', code='needs_attention')
                            value = dt.isoformat()
                        elif field == 'points_possible':
                            try:
                                value = float(value)
                                if not 0 <= value <= 1e9:
                                    raise ValueError()
                            except ValueError as e:
                                raise InvalidInput('Invalid source points', code='needs_attention') from e
                        elif len(value) > (20_000 if field == 'body' else 500):
                            raise InvalidInput('Source field is too large', code='needs_attention')
                        fields[field] = value
                    records.append({'kind': rec['kind'], 'fields': fields})
        if not text.strip():
            raise InvalidInput('No readable source content', code='needs_attention')
        return {'url': r.url, 'content': text, 'records': records}

    def read(self, spec):
        # All pages validate/extract before any observation is published.
        result = [self.extract(self.fetch(url, spec['pages']), spec) for url in spec['pages']]
        ids = [rec['fields']['id'] for p in result for rec in p['records']]
        if len(set(ids)) != len(ids):
            raise InvalidInput('Duplicate record IDs across adapter pages', code='needs_attention')
        return result
