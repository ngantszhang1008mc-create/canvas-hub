"""External URL identity and readonly target validation; independent of Canvas canonicalization."""
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

from .util import InvalidInput

SENSITIVE = re.compile(r'token|secret|signature|password|credential|authorization|api.?key|^sig$|^key$|^code$|^saml|^ticket$|^verifier$|^x-amz-|^x-goog-', re.I)
MUTATION = re.compile(r'(^|/)(logout|signout|sign-out|sign_out|delete|remove|submit|enroll|unsubscribe|purchase|checkout)(/|$)', re.I)



def public_address(value):
    ip = ipaddress.ip_address(value)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def clean_url(raw: str) -> tuple[str, bool]:
    if not isinstance(raw, str) or len(raw) > 2000 or any(ord(c) < 33 for c in raw):
        raise InvalidInput('External URL must be a bounded HTTP(S) address')
    try:
        p = urlsplit(raw)
        host = (p.hostname or '').lower().rstrip('.').encode('idna').decode('ascii')
        port = p.port
    except (ValueError, UnicodeError) as e:
        raise InvalidInput('Invalid external URL') from e
    if p.scheme.lower() not in ('https', 'http') or not host or p.username or p.password:
        raise InvalidInput('External URL must use HTTP(S) without credentials')
    try:
        ip = ipaddress.ip_address(host)
        if not public_address(ip):
            raise InvalidInput('Private/internal external targets are forbidden')
    except ValueError:
        if '.' not in host or host.endswith(('.local', '.internal', '.localhost', '.test', '.invalid')):
            raise InvalidInput('Private/internal external targets are forbidden')
    if port not in (None, 80 if p.scheme.lower() == 'http' else 443):
        raise InvalidInput('Only standard HTTP(S) ports are supported')
    decoded = p.path
    for _ in range(4):
        decoded = unquote(decoded)
    if '\\' in decoded or any(x in ('.', '..') for x in decoded.split('/')) or MUTATION.search(decoded):
        raise InvalidInput('Mutation or ambiguous URL paths are forbidden')
    pairs = parse_qsl(p.query, keep_blank_values=True)
    needed = any(SENSITIVE.search(k) for k, _ in pairs)
    if any(MUTATION.search('/' + unquote(k).strip('/')) or (k.lower() in ('action','do','method','operation') and MUTATION.search('/' + unquote(v).strip('/'))) for k,v in pairs):
        raise InvalidInput('Mutation query is forbidden')
    # Preserve order, repeated keys, case, slash and meaningful course/term queries.
    safe = [(k, v) for k, v in pairs if not SENSITIVE.search(k) and not k.lower().startswith('utm_')]
    authority = f'[{host}]' if ':' in host else host
    return urlunsplit((p.scheme.lower(), authority, p.path or '/', urlencode(safe), '')), needed



def external_url(raw):
    """Metadata normalization. Unsafe targets stay visible, but cannot register a reader."""
    try:
        return clean_url(raw)[0]
    except InvalidInput:
        p = urlsplit(raw)
        pairs = [(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not SENSITIVE.search(k)]
        authority = p.netloc.rsplit('@',1)[-1]
        return urlunsplit((p.scheme,authority,p.path,urlencode(pairs),''))
