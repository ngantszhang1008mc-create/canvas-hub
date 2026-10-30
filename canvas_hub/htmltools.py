"""Untrusted HTML handling: sanitizing, text extraction, link extraction,
and normalization for change detection.

Collected Canvas HTML is untrusted. It is sanitized before storage (scripts,
styles, event handlers, iframes, forms removed) and the UI additionally
serves it under a strict Content-Security-Policy.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

import nh3

_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "del", "details",
    "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
    "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "s", "small", "span", "strong", "sub",
    "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "ol": {"start", "type"},
    "details": {"open"},
}


def sanitize(html: str | None) -> str:
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
          "blockquote", "pre", "hr", "dd", "dt", "details", "summary", "figure", "section", "article"}
_SKIP = {"script", "style", "noscript", "template"}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in ("td", "th"):
            self.parts.append(" | ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href
                self._link_text = []
        if tag == "img":
            a = dict(attrs)
            if a.get("alt"):
                self.parts.append(f"[image: {a['alt']}]")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


def to_text(html: str | None) -> str:
    if not html:
        return ""
    p = _TextParser()
    p.feed(html)
    p.close()
    text = "".join(p.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if not line:
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(line)
            blank = False
    return "\n".join(out).strip()


def links(html: str | None) -> list[tuple[str, str]]:
    """(href, link text) pairs in document order, including links in
    collapsed/hidden markup (details, display:none) since the source HTML
    contains them regardless of presentation."""
    if not html:
        return []
    p = _TextParser()
    p.feed(html)
    p.close()
    return p.links


_VOLATILE_URL_PARAMS = re.compile(r"([?&](?:verifier|_|ts|sf_verifier|download_frd)=[^&\"'\s]*)", re.I)


def normalized_for_compare(html: str | None) -> str:
    """Normalization that ignores harmless formatting differences: whitespace,
    attribute order, volatile URL tokens. Text content and link targets are
    preserved so meaningful edits still compare as different."""
    if not html:
        return ""
    text = to_text(html)
    hrefs = sorted({_VOLATILE_URL_PARAMS.sub("", unescape(h)) for h, _ in links(html)})
    return " ".join(text.split()) + "\n" + "\n".join(hrefs)
