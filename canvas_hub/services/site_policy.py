"""Small, source-bound compatibility rules for isolated sign-in browsers.

An approved identity origin is not permission for arbitrary CDN traffic or POSTs.
Only exact static URLs / form actions declared by a fetched document are retained.
See docs/SITE_INSPECTION_COMPATIBILITY.md for the anonymous evidence and limits.
"""
import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..external_urls import clean_url

MICROSOFT = 'https://login.microsoftonline.com'
STATIC = {
    MICROSOFT: {
        'https://aadcdn.msauth.net': ('/ests/2.1/content/', '/shared/1.0/content/'),
    },
    'https://piazza.com': {
        'https://dvngeac8rg9mb.cloudfront.net': ('/javascripts/', '/css/'),
    },
}
MIME = {'script': {'application/javascript', 'text/javascript', 'application/x-javascript'},
        'stylesheet': {'text/css'}}


def site_origin(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, '', '', ''))


def static_kind(document, url):
    """Query-free, literal static paths on an evidenced provider's exact host."""
    try:
        safe, secret = clean_url(url)
        p = urlsplit(url)
        prefixes = STATIC.get(site_origin(document), {}).get(site_origin(url), ())
        if (secret or p.query or p.fragment or safe != url or not prefixes
                or not p.path.startswith(prefixes) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', p.path)):
            return None
        return 'script' if p.path.endswith('.js') else 'stylesheet' if p.path.endswith('.css') else None
    except Exception:
        return None


def document_policy(url, body):
    """Parse server-supplied markup only; never trust a script-mutated DOM policy."""
    soup = BeautifulSoup(body, 'html.parser')
    static, forms, auth = set(), set(), {}
    for el in soup.select('script[src],link[rel=stylesheet][href]')[:80]:
        target = urljoin(url, el.get('src') or el.get('href'))
        kind = 'script' if el.name == 'script' else 'stylesheet'
        if static_kind(url, target) == kind:
            static.add((target, kind))
    for form in soup.select('form')[:40]:
        if (form.get('method', '').lower() == 'post'
                and form.select_one('input[type=password i],input[autocomplete=username],input[name=SAMLResponse],input[name=wresult]')):
            forms.add(urljoin(url, form.get('action') or url))
    if site_origin(url) == MICROSOFT:
        # The Microsoft login bootstrap declares these actions. Restrict both
        # the configuration key AND the endpoint; telemetry keys cannot opt in.
        routes = {'urlPost': (r'/(?:common|organizations|[0-9a-fA-F-]{36})/login', 'Microsoft sign-in'),
                  'urlGetCredentialType': (r'/(?:common|organizations|[0-9a-fA-F-]{36})/GetCredentialType', 'Microsoft account lookup')}
        for script in soup.select('script:not([src])'):
            value = script.string or ''
            match = re.search(r'(?:\$Config|ServerData)\s*=\s*(\{)', value)
            if not match:
                continue
            try:
                config, _ = json.JSONDecoder().raw_decode(value[match.start(1):])
            except (ValueError, TypeError):
                continue
            for key, (path, label) in routes.items():
                target = urljoin(url, config.get(key, '')) if isinstance(config.get(key), str) else ''
                if target and site_origin(target) == MICROSOFT and re.fullmatch(path, urlsplit(target).path):
                    auth[target] = label
            # Microsoft declares versioned lazy bundles as strings in its
            # bootstrap. They must meet the same exact-host/path/query rules.
            for target in re.findall(r'https://[A-Za-z0-9._/-]+\.(?:js|css)', value):
                kind = static_kind(url, target)
                if kind and len(static) < 80:
                    static.add((target, kind))
    piazza = site_origin(url) == 'https://piazza.com' and any(
        urljoin(url, script.get('src', '')) == 'https://piazza.com/piazzza_js/ajax.js' for script in soup.select('script[src]'))
    return {'static': static, 'forms': forms, 'auth': auth, 'piazza_login': piazza}


def auth_action(policy, url, resource_type, content_type, body=b''):
    if url in policy.get('forms', ()) and resource_type == 'document' and content_type == 'application/x-www-form-urlencoded':
        return 'Declared sign-in form'
    label = policy.get('auth', {}).get(url)
    if label == 'Microsoft account lookup' and resource_type in ('xhr', 'fetch') and content_type == 'application/json':
        return label
    if label == 'Microsoft sign-in' and resource_type == 'document' and content_type == 'application/x-www-form-urlencoded':
        return label
    if (policy.get('piazza_login') and site_origin(url) == 'https://piazza.com' and urlsplit(url).path == '/logic/api'
            and resource_type in ('xhr', 'fetch') and content_type == 'application/json'):
        pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        query = dict(pairs)
        try:
            value = json.loads(body)
            if (len(query) == len(pairs) and set(query) <= {'method', 'aid'} and query.get('method') == 'user.login'
                    and re.fullmatch(r'[a-z0-9]{1,40}', query.get('aid', 'x'))
                    and isinstance(value, dict) and set(value) == {'method', 'params'} and value['method'] == 'user.login'
                    and isinstance(value['params'], dict) and set(value['params']) == {'email', 'pass'}
                    and all(isinstance(v, str) and 0 < len(v) <= 4096 for v in value['params'].values())):
                return 'Piazza sign-in'
        except (ValueError, TypeError, UnicodeError):
            pass
    return None


def diagnostic(request, reason, *, endpoint='[path withheld]', status=None):
    """No paths, query strings, headers, bodies, hashes or exception messages."""
    try:
        p = urlsplit(request.url)
        host = p.hostname or ''
        origin = 'https://' + host if p.scheme == 'https' and not p.username and not p.password else '[invalid origin]'
        if endpoint == '[path withheld]':
            if host == 'piazza.com' and p.path == '/logic/api':
                # Classify only known operation names; never emit query values.
                methods = [v for k, v in parse_qsl(p.query) if k == 'method']
                endpoint = ('Piazza sign-in API' if methods == ['user.login'] else
                            'Piazza error telemetry' if methods == ['generic.report_js_error'] else 'Piazza API')
            elif host == 'login.microsoftonline.com':
                match = re.fullmatch(r'/(?:common|organizations|[0-9a-fA-F-]{36})/(login|GetCredentialType|SAS/BeginAuth|SAS/EndAuth|SAS/ProcessAuth|instrumentation/dssostatus|instrumentation/reportprogress)', p.path)
                if match:
                    endpoint = ('Microsoft telemetry ' if match[1].startswith('instrumentation/') else '') + '/{tenant}/' + match[1]
    except ValueError:
        origin = '[invalid origin]'
    method = request.method.upper()
    kind = request.resource_type
    result = {'origin': origin, 'method': method if method in ('GET', 'POST', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'DELETE') else 'OTHER',
              'resource_type': kind if kind in ('document', 'script', 'stylesheet', 'image', 'font', 'xhr', 'fetch', 'ping') else 'other',
              'endpoint': endpoint, 'reason': reason}
    if isinstance(status, int):
        result['http_status'] = status
    return result
