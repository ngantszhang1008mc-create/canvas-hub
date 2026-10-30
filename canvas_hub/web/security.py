"""Local HTTP companion protection.

* The server binds to 127.0.0.1 only (enforced by the CLI).
* Every /api request needs the per-installation token, either as the
  HttpOnly SameSite=Strict cookie set when the app is opened through its
  launch URL, or as ``Authorization: Bearer <token>`` (for local tools).
* The Host header must be the loopback address we serve (DNS-rebinding guard).
* State-changing requests must carry ``X-Canvas-Hub: 1`` and, when an Origin
  header is present, it must be our own origin (CSRF guard).
* All responses carry a strict Content-Security-Policy; collected HTML is
  already sanitized server-side, and the CSP is a second barrier.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

TOKEN_FILE = "api-token"
COOKIE = "canvas_hub_token"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# The page that runs assistant-written HTML/JS (opt-in). It is framed with sandbox="allow-scripts" (an opaque
# origin: no cookies, no access to the app or its API) and may not fetch, load or post anything.
SANDBOX_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
               "font-src data:; media-src data: blob:; connect-src 'none'; worker-src 'none'; frame-src 'none'; "
               "object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")


def load_or_create_token(runtime_dir: Path) -> str:
    p = Path(runtime_dir) / TOKEN_FILE
    if p.exists():
        tok = p.read_text().strip()
        if len(tok) >= 32:
            return tok
    tok = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


def token_ok(expected: str, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(expected.encode(), presented.encode())


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
