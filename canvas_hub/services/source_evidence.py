"""Bounded classification evidence, separate from complete local source text."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..external_urls import clean_url
from ..util import InvalidInput, sha256_text


def _spread(values, count):
    if len(values) <= count:
        return values
    return [values[(len(values)-1)*i//(count-1)] for i in range(count)]


def _ranges(text):
    if len(text) <= 4000:
        return [[0, len(text)]]
    # Beginning, interior and ending evidence, plus a few role-bearing passages.
    # These windows are expressly partial; they are never grading-rule extraction.
    spans = [[(len(text)-600)*i//4, (len(text)-600)*i//4+600] for i in range(5)]
    signals = list(re.finditer(r'(?i)\b(?:syllabus|assignment|submit|course|lecture|reference|grading|sign.in)\b', text))
    for match in _spread(signals, 4):
        start = max(0, min(len(text)-400, match.start()-120))
        spans.append([start, start+400])
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def classification_evidence(text, *, url, fmt, response=None):
    spans = _ranges(text)
    coverage = {"purpose": "classification_only", "version": sha256_text(text),
                "basis": "locally retained extracted text; not original image/PDF or collection coverage",
                "total_chars": len(text), "supplied_chars": sum(b-a for a, b in spans),
                "ranges": spans, "complete_read": spans == [[0, len(text)]]}
    evidence = {"url": url, "format": fmt, "untrusted_data": True, "coverage": coverage,
                "excerpts": [{"start_char": a, "end_char": b, "text": text[a:b]} for a, b in spans]}
    if response is not None and fmt == "html":
        soup = BeautifulSoup(response.body, 'html.parser')
        for tag in soup(['script', 'style', 'iframe', 'object', 'noscript', 'form']):
            tag.decompose()
        evidence['title'] = soup.title.get_text(' ', strip=True)[:240] if soup.title else ''
        nodes = soup.select('h1,h2,h3,main,article,li,tr')
        evidence['dom_examples'] = [{"tag": node.name,
                                     "id": str(node.get('id', ''))[:120],
                                     "classes": [str(c)[:80] for c in node.get('class', [])[:3]],
                                     "text": node.get_text(' ', strip=True)[:160]}
                                    for node in _spread(nodes, 8)]
        links = []
        for link in soup.select('a[href]'):
            try:
                target, needs_access = clean_url(urljoin(response.url, link['href']))
            except (InvalidInput, ValueError):
                continue
            if not needs_access and len(target) <= 400:
                links.append({"label": link.get_text(' ', strip=True)[:120], "url": target})
        evidence['links'] = _spread(links, 8)
        evidence['dom_coverage'] = 'Representative nodes/links from the fetched DOM; not a complete DOM.'
    # Connected pages deliberately supply no DOM, links, or title not present in
    # the exact approved text. The caller keeps its existing origin-only URL.
    return evidence
