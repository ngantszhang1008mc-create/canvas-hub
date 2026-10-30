"""Canvas address handling.

The user may paste a Canvas root (https://canvas.ust.hk/), a course link
(https://canvas.ust.hk/courses/73682/assignments/456957) or a bare host.
We establish the origin, keep the original destination, and refuse things
that cannot be a Canvas web origin (non-http(s), credentials in the URL,
IP literals on non-loopback for safety, etc.).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .util import InvalidInput

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


@dataclass(frozen=True)
class CanvasAddress:
    origin: str          # scheme://host[:port], no trailing slash
    entry_url: str       # normalized form of what the user pasted
    course_id: str | None


def parse_canvas_address(raw: str, *, allow_http_loopback: bool = True) -> CanvasAddress:
    if raw is None or not str(raw).strip():
        raise InvalidInput("Enter your Canvas web address, e.g. https://canvas.example.edu/")
    text = str(raw).strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise InvalidInput("Canvas address must use https://")
    if parts.username or parts.password:
        raise InvalidInput("Do not include a username or password in the address")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise InvalidInput("The address has no host name")
    loopback = _is_loopback(host)
    if scheme == "http" and not (allow_http_loopback and loopback):
        raise InvalidInput("Canvas address must use https:// (plain http is only accepted for local test servers)")
    if not loopback:
        try:
            ipaddress.ip_address(host)
            raise InvalidInput("Use the Canvas host name, not an IP address")
        except ValueError:
            pass
        if not _HOST_RE.match(host) or "." not in host:
            raise InvalidInput(f"'{host}' is not a valid host name")
    port = parts.port
    netloc = host if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f"{host}:{port}"
    origin = f"{scheme}://{netloc}"
    path = parts.path or "/"
    entry = urlunsplit((scheme, netloc, path, parts.query, ""))
    m = re.match(r"^/courses/(\d+)(?:/|$)", path)
    return CanvasAddress(origin=origin, entry_url=entry, course_id=m.group(1) if m else None)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def same_origin(url: str, origin: str) -> bool:
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    o = urlsplit(origin)
    def norm(sp):
        port = sp.port or (443 if sp.scheme == "https" else 80)
        return (sp.scheme.lower(), (sp.hostname or "").lower(), port)
    return norm(p) == norm(o)


_VOLATILE_QUERY = {"verifier", "wrap", "download_frd", "module_item_id", "_", "access_token", "sf_verifier", "ts"}


def canonical_url(url: str, origin: str | None = None) -> str:
    """Canonical form used for identity of link destinations: absolute, no
    fragment, volatile query parameters removed, sorted query."""
    absolute = urljoin(origin + "/", url) if origin else url
    p = urlsplit(absolute)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in _VOLATILE_QUERY]
    q.sort()
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(q), ""))


@dataclass(frozen=True)
class CanvasLink:
    kind: str                # page | file | assignment | quiz | discussion | announcement | module | module_item
                             # | folder | syllabus | course | external_tool | external | course_nav | other_canvas
    course_id: str | None
    ref: str | None          # id or page slug
    url: str                 # canonical absolute URL


_COURSE_PATTERNS = [
    (re.compile(r"^/courses/(\d+)/pages/([^/?#]+)/?$"), "page"),
    (re.compile(r"^/courses/(\d+)/wiki/([^/?#]+)/?$"), "page"),
    (re.compile(r"^/courses/(\d+)/files/(\d+)(?:/(?:download|preview))?/?$"), "file"),
    (re.compile(r"^/courses/(\d+)/files/folder/(.+)$"), "folder"),
    (re.compile(r"^/courses/(\d+)/assignments/(\d+)/?$"), "assignment"),
    (re.compile(r"^/courses/(\d+)/assignments/syllabus/?$"), "syllabus"),
    (re.compile(r"^/courses/(\d+)/quizzes/(\d+)/?$"), "quiz"),
    (re.compile(r"^/courses/(\d+)/discussion_topics/(\d+)/?$"), "discussion"),
    (re.compile(r"^/courses/(\d+)/announcements/(\d+)/?$"), "announcement"),
    (re.compile(r"^/courses/(\d+)/modules/items/(\d+)/?$"), "module_item"),
    (re.compile(r"^/courses/(\d+)/modules/(\d+)/?$"), "module"),
    (re.compile(r"^/courses/(\d+)/external_tools/(\d+)/?$"), "external_tool"),
    (re.compile(r"^/courses/(\d+)/?$"), "course"),
    (re.compile(r"^/courses/(\d+)/([a-z_]+)/?$"), "course_nav"),
]
_FILE_GLOBAL = re.compile(r"^/files/(\d+)(?:/(?:download|preview))?/?$")
_USER_FILE = re.compile(r"^/users/\d+/files/(\d+)(?:/(?:download|preview))?/?$")


def classify_link(href: str, origin: str, base_url: str | None = None) -> CanvasLink | None:
    """Classify a link found in Canvas content. Returns None for links we do
    not follow at all (mailto:, javascript:, anchors)."""
    if not href:
        return None
    h = href.strip()
    low = h.lower()
    if low.startswith(("mailto:", "javascript:", "tel:", "data:", "#")):
        return None
    try:
        absolute = urljoin((base_url or origin) + ("" if base_url else "/"), h)
        p = urlsplit(absolute)
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    url = canonical_url(absolute)
    if not same_origin(absolute, origin):
        return CanvasLink("external", None, None, url)
    path = p.path
    for pat, kind in _COURSE_PATTERNS:
        m = pat.match(path)
        if m:
            course = m.group(1)
            ref = m.group(2) if pat.groups >= 2 else None
            if kind == "course_nav":
                return CanvasLink("course_nav", course, ref, url)
            return CanvasLink(kind, course, ref, url)
    m = _FILE_GLOBAL.match(path) or _USER_FILE.match(path)
    if m:
        return CanvasLink("file", None, m.group(1), url)
    return CanvasLink("other_canvas", None, None, url)
