"""Conservative local scrubbing before a reviewed page can leave the device.

Patterns reduce exposure but cannot recognize every possible secret; the user
must review the exact resulting text before explicitly authorizing transmission.
"""
import re
from urllib.parse import unquote

from ..util import InvalidInput

MAX_TEXT = 100_000
SENSITIVE_LINE = re.compile(
    r'(?i)\b(password|passcode|one.time.code|otp|mfa.code|api[ _-]?key|access[ _-]?token|'
    r'refresh[ _-]?token|session[ _-]?(?:id|token|key)|authorization|cookie|client[ _-]?secret|'
    r'private[ _-]?key|recovery[ _-]?code|verification[ _-]?code|token|secret|bearer|'
    r'sign[ _-]?in[ _-]?code|credential)\b')
URL = re.compile(r'(?i)\b(?:https?://|www\.)[^\s<>]+')
EMAIL = re.compile(r'(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b')
LONG_TOKEN = re.compile(r'(?<![\w])[A-Za-z0-9_+/=-]{32,}(?![\w])')


def validate_evidence_url(url):
    value = unquote(url)
    if SENSITIVE_LINE.search(value) or LONG_TOKEN.search(value) or EMAIL.search(value):
        raise InvalidInput('The source URL may contain sensitive access data; connected analysis is unavailable')


def sanitize_connected_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 200_000:
        raise InvalidInput('The page is empty or exceeds the local review limit')
    lines = []
    for raw in text.splitlines():
        if SENSITIVE_LINE.search(raw):
            lines.append('[sensitive line removed]')
            continue
        line = URL.sub('[link removed]', raw)
        line = EMAIL.sub('[email removed]', line)
        line = LONG_TOKEN.sub('[token removed]', line)
        lines.append(line)
    result = '\n'.join(lines).strip()[:MAX_TEXT]
    if len(re.sub(r'\[[^\]]+ removed\]|\s', '', result)) < 100:
        raise InvalidInput('Not enough reviewable course text remains after removing sensitive content')
    return result
