"""Open an external course link, save what it says, or explain why it could not be read.

The user asks for every inspection; nothing is opened just because a link was
found. One worker thread owns a Chromium profile that belongs to Canvas Hub and
to one Canvas account. It is separate from the Canvas sign-in profile and from
the user's own browsers, and it is never exported.

A read loads the page the way a browser would and then decides, without any
model, which of these happened:

``ok``              readable text was found and saved as a searchable item
``login_required``  the site wants a sign-in; ``login_method`` says which kind
``network``         the computer is offline, or the site cannot be reached
``not_found`` / ``forbidden`` / ``site_error`` / ``blocked`` / ``empty`` /
``unsupported`` / ``unsafe`` / ``error``

Three kinds of sign-in are handled:

``basic``  the site sent an HTTP Basic challenge. The user types the username
           and password into Canvas Hub's own form; they are attached only to
           requests for that one origin.
``sso``    the site sent the browser to a different sign-in host (a university
           single sign-on). The user signs in, with any second factor, in a
           visible window; Canvas Hub never sees what is typed.
``form``   the site shows its own sign-in form. Same visible window.

When sign-in succeeds the read continues by itself, and other links that were
waiting on the same sign-in are tried again.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from ..external_urls import clean_url, public_address
from ..util import Conflict, InvalidInput, NotFound, canonical_json, now_iso, sha256_text

log = logging.getLogger("canvas_hub.link_inspection")

NAVIGATION_MS = 25_000
SETTLE_MS = 7_000
MAX_TEXT = 200_000
MAX_FILE_BYTES = 1_000_000
LOGIN_SECONDS = 15 * 60
AUTO_CHECKS = 4
PROVIDED_BY = "Canvas Hub link inspection"

OUTCOMES = ("ok", "login_required", "network", "not_found", "forbidden", "site_error", "blocked", "empty",
            "unsupported", "unsafe", "error")
TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json")
HTML_TYPES = ("text/html", "application/xhtml+xml")

LOGIN_PATH = re.compile(r"(^|[/._-])(login|log-in|log_in|signin|sign-in|sign_in|sso|cas|saml2?|adfs|oauth2?|authorize|"
                        r"authenticate|idp|shibboleth|openid|wp-login)([/._?-]|$)", re.I)
IDP_HOST = re.compile(r"(^|\.)(login\.microsoftonline\.com|login\.live\.com|login\.windows\.net|accounts\.google\.com|"
                      r"[\w-]+\.okta\.com|[\w-]+\.duosecurity\.com|[\w-]+\.auth0\.com|[\w-]+\.onelogin\.com)$|"
                      r"^(cas|sso|idp|login|signin|auth|adfs|shib|shibboleth|weblogin|passport)[\w-]*\.", re.I)
BOT_TITLE = re.compile(r"just a moment|attention required|verify (that )?you are (a )?human|are you a robot|captcha|"
                       r"access denied.*(bot|automated)|security check", re.I)
# Requests whose address is literally on this computer or a private network. The browser is told
# to drop them, so a public page cannot bounce an inspection into local services.
PRIVATE_URL = re.compile(r"^https?://(?:[^/@]*@)?(?:localhost|[^/:]+\.(?:local|internal|localhost)|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|"
                         r"192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+|0\.0\.0\.0|"
                         r"\[(?:::1|f[cd][0-9a-f]{2}:[^\]]*|fe80:[^\]]*)\])(?::\d+)?(?:[/?#]|$)", re.I)
JOIN_TEXT = re.compile(r"join (a |your )?class|add another class|enrol|enroll|create (an )?account|sign up", re.I)

# What the page shows right now. Text comes from the rendered page, so hidden
# elements and scripts are not included. Form values are never read.
FACTS_JS = r"""() => {
  const visible = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const inputs = [...document.querySelectorAll('input')];
  const password = inputs.filter(i => (i.type || '').toLowerCase() === 'password');
  const user = inputs.filter(i => ['email', 'text', 'tel', ''].includes((i.type || '').toLowerCase()) &&
    /user|email|e-mail|login|account|itsc|netid|identifier|loginfmt/i.test([i.name, i.id, i.autocomplete, i.placeholder, i.getAttribute('aria-label')].join(' ')));
  const forms = [...document.querySelectorAll('form')].filter(f =>
    /login|log-in|signin|sign-in|sso|auth|session/i.test([f.action, f.id, f.className, f.name].join(' ')));
  const prompts = [...document.querySelectorAll('a,button,[role=button]')].filter(e => visible(e) &&
    /^(log ?in|sign ?in|login with .*|sign in with .*|log in to .*)$/i.test((e.innerText || e.value || '').trim()));
  const body = document.body;
  const text = body ? (body.innerText || '').replace(/ /g, ' ').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim() : '';
  return { title: (document.title || '').slice(0, 300), text: text.slice(0, 200000), textLength: text.length,
    passwordVisible: password.some(visible), passwordAny: password.length > 0, userVisible: user.some(visible),
    loginForm: forms.length > 0, loginPrompt: prompts.length > 0,
    challenge: !!document.querySelector('#challenge-form, #cf-challenge-running, .g-recaptcha, iframe[src*="captcha" i], iframe[title*="challenge" i]') };
}"""


def _host(url: str) -> str:
    """The site a URL belongs to: its host name, plus the port when it is not the usual one."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        return f"{host}:{p.port}" if host and p.port and p.port != {"http": 80, "https": 443}.get(p.scheme) else host
    except ValueError:
        return ""


def _origin(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), "", "", ""))


def _display_url(url: str | None) -> str | None:
    """Where the browser ended up, without the query: sign-in redirects carry tickets."""
    if not url:
        return None
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))[:500]


def _network_reason(error: str) -> str:
    table = (("ERR_NAME_NOT_RESOLVED", "dns"), ("ERR_INTERNET_DISCONNECTED", "offline"), ("ERR_NETWORK_CHANGED", "offline"),
             ("ERR_ADDRESS_UNREACHABLE", "offline"), ("ERR_PROXY", "offline"), ("ERR_TUNNEL", "offline"),
             ("ERR_CONNECTION_TIMED_OUT", "timeout"), ("ERR_TIMED_OUT", "timeout"), ("Timeout", "timeout"),
             ("ERR_CERT", "tls"), ("ERR_SSL", "tls"), ("ERR_TOO_MANY_REDIRECTS", "redirect_loop"),
             ("ERR_CONNECTION", "refused"), ("ERR_EMPTY_RESPONSE", "refused"), ("ERR_UNSAFE_PORT", "refused"))
    return next((reason for needle, reason in table if needle in error), "network_error")


def classify(target: str, final_url: str, status: int | None, challenge: str | None, facts: dict, *, sent_basic: bool = False) -> dict:
    """Decide what a loaded page is. Pure, so it can be tested without a browser."""
    target_host, final_host = _host(target), _host(final_url)
    path = urlsplit(final_url).path + ("?" + urlsplit(final_url).query if urlsplit(final_url).query else "")
    base = {"http_status": status, "final_url": _display_url(final_url), "page_title": facts.get("title") or None}
    text_len = int(facts.get("textLength") or 0)
    password, user = bool(facts.get("passwordVisible")), bool(facts.get("userVisible"))

    def login(method, reason, host=None):
        return {**base, "outcome": "login_required", "reason": reason, "login_method": method,
                "login_host": host or final_host or target_host}

    if status == 401 and challenge:
        if re.search(r"(?:^|,)\s*Basic(?:\s|$)", challenge, re.I):
            return login("basic", "credentials_rejected" if sent_basic else "http_basic")
        # Digest, NTLM, Negotiate: only the browser's own prompt can answer these.
        return login("form", "http_auth_other")
    if status in (401, 403):
        if password or (user and facts.get("loginForm")):
            return login("form" if final_host == target_host else "sso", "login_form")
        return {**base, "outcome": "forbidden", "reason": f"http_{status}"}
    if status in (404, 410):
        return {**base, "outcome": "not_found", "reason": f"http_{status}"}
    if status == 429:
        return {**base, "outcome": "site_error", "reason": "rate_limited"}
    if status == 407:
        return {**base, "outcome": "network", "reason": "proxy_auth"}
    if status is not None and status >= 400:
        return {**base, "outcome": "site_error", "reason": f"http_{status}"}
    if facts.get("challenge") or BOT_TITLE.search(facts.get("title") or ""):
        return {**base, "outcome": "blocked", "reason": "bot_challenge"}
    login_path = bool(LOGIN_PATH.search(path))
    if final_host != target_host and (IDP_HOST.search(final_host.split(':')[0]) or login_path or password or (user and text_len < 1500)):
        return login("sso", "sso_redirect")
    if password and (login_path or facts.get("loginForm") or text_len < 1500):
        return login("form", "login_form")
    if login_path and (user or facts.get("loginForm")):
        return login("form", "login_form")
    if facts.get("loginPrompt") and text_len < 700 and JOIN_TEXT.search(facts.get("text") or ""):
        # A landing page that only offers "Log in" / "Join class": not course content.
        return login("form", "login_prompt")
    if len((facts.get("text") or "").strip()) < 40:
        return {**base, "outcome": "empty", "reason": "no_text"}
    return {**base, "outcome": "ok", "reason": "sign_in_optional" if password else None, "content": facts["text"][:MAX_TEXT]}


def explain(result: dict, url: str) -> str:
    """One plain sentence for the user: what happened and what to do next."""
    host = _host(url) or "the site"
    outcome, reason = result.get("outcome"), result.get("reason")
    login_host = result.get("login_host") or host
    if outcome == "ok":
        note = " The page also offers an optional sign-in; sign in if something is missing." if reason == "sign_in_optional" else ""
        return f"Read and saved ({result.get('content_chars') or 0:,} characters).{note}"
    if outcome == "login_required":
        if reason == "credentials_rejected":
            return f"{login_host} did not accept that username and password. Check them and try again."
        if reason == "insecure_basic":
            return (f"{login_host} asks for a password over an unencrypted (http) connection. Canvas Hub will not send "
                    "a password that way; open the https address of the site instead.")
        if result.get("login_method") == "basic":
            return f"{login_host} asks for a username and password (HTTP Basic sign-in). Enter them below to continue."
        if result.get("login_method") == "sso":
            return (f"This page sends you to {login_host} to sign in (single sign-on). Open the sign-in window, "
                    "sign in there as usual, and Canvas Hub continues by itself.")
        if reason == "login_prompt":
            return f"{host} only shows a log-in or join page. Open the sign-in window and sign in to see the course."
        return f"{host} shows its own sign-in form. Open the sign-in window, sign in there, and Canvas Hub continues by itself."
    if outcome == "network":
        return {
            "offline": "This computer could not reach Canvas either, so it looks offline (or a VPN/proxy is blocking it). "
                       "Check the internet connection and try again.",
            "dns": f"The address {host} could not be found. Check the link; some university sites only resolve on the campus network or VPN.",
            "timeout": f"{host} did not answer in time. It may be down, or only reachable on the campus network or VPN.",
            "refused": f"{host} refused the connection. The site may be down or blocking this network.",
            "tls": f"{host} has a certificate problem, so a secure connection could not be made. Nothing was sent.",
            "redirect_loop": f"{host} kept redirecting in a loop. Signing in to the site usually fixes this.",
            "proxy_auth": "A network proxy asked for a sign-in before anything could be reached.",
        }.get(reason, f"{host} could not be reached because of a network problem.")
    if outcome == "not_found":
        return "The page does not exist any more (404). The link in Canvas may be out of date."
    if outcome == "forbidden":
        return (f"{host} refused access ({result.get('http_status') or 403}). If you have not signed in to this site yet, "
                "open the sign-in window; otherwise your account may not have access to this page.")
    if outcome == "site_error":
        if reason == "rate_limited":
            return f"{host} is limiting requests right now. Try again in a few minutes."
        return f"{host} reported an error of its own ({result.get('http_status') or 'server error'}). Try again later."
    if outcome == "blocked":
        return (f"{host} is showing a robot check and blocks automatic reading. Open the sign-in window, pass the check "
                "yourself, then choose Continue.")
    if outcome == "empty":
        return "The page loaded but showed no readable text. It may need a sign-in, or it only works interactively."
    if outcome == "unsupported":
        return f"This link is a file Canvas Hub cannot read as text ({result.get('content_type') or 'unknown type'}). Open it in your browser."
    if outcome == "unsafe":
        return ("Canvas Hub does not open this address automatically: it points at a private network address, or it looks "
                "like an action (log out, submit, delete...) rather than a page.")
    return "The inspection stopped unexpectedly. Try again."


class LinkInspector:
    def __init__(self, hub, *, headless: bool | None = None, allow_local: bool = False):
        self.hub, self.db = hub, hub.db
        self.headless = (os.environ.get("CANVAS_HUB_HEADLESS", "").lower() in ("1", "true", "yes")) if headless is None else headless
        # Loopback targets are refused in the app. Only tests, which serve their
        # synthetic sites on 127.0.0.1, construct an inspector that allows them.
        self.allow_local = allow_local
        self.online = self._canvas_reachable
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._changed = threading.Condition()
        self._pw = None
        self._ctx = None
        self._ctx_owner: str | None = None
        self._ctx_headed = False
        self._routes: set[str] = set()
        self._login: dict | None = None
        self._deferred: list = []
        self._carry: dict[str, list] = {}       # session cookies kept across a window -> background switch
        self._basic: dict[tuple[str, str], str] = {}
        self._stopping = False
        # Work interrupted by a restart is not replayed; the user asks again.
        with self.db.tx() as c:
            c.execute("UPDATE link_inspections SET state='idle', message=CASE WHEN outcome IS NULL THEN "
                      "'Interrupted when Canvas Hub closed. Inspect again.' ELSE message END WHERE state!='idle'")

    # ---- identity, storage -------------------------------------------------
    def _owner(self, aid: int) -> str:
        account = self.hub.identity.get_account(aid)
        return sha256_text(account["origin"] + "\0" + account["canvas_user_id"])

    def _profile(self, owner: str):
        return self.hub.paths.browser_profiles / "external-sites" / f"account-{owner[:20]}"

    def _credentials_file(self):
        return self.hub.paths.runtime / "site-logins.json"

    def _saved_credentials(self) -> dict:
        try:
            data = json.loads(self._credentials_file().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_credentials(self, data: dict) -> None:
        path = self._credentials_file()
        if not data:
            path.unlink(missing_ok=True)
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _basic_for(self, owner: str, origin: str) -> str | None:
        with self._lock:
            value = self._basic.get((owner, origin))
        return value or self._saved_credentials().get(owner, {}).get(origin)

    def _forget_basic(self, owner: str, origin: str) -> None:
        with self._lock:
            self._basic.pop((owner, origin), None)
        saved = self._saved_credentials()
        if saved.get(owner, {}).pop(origin, None) is not None:
            self._write_credentials({k: v for k, v in saved.items() if v})

    def _source(self, aid: int, sid: int) -> dict:
        return self.hub.external_sources._source(aid, int(sid))

    def _row(self, sid: int) -> dict:
        row = self.db.one("SELECT * FROM link_inspections WHERE source_id=?", (sid,))
        return dict(row) if row else {"source_id": sid, "state": "idle", "outcome": None}

    def _set(self, aid: int, sid: int, **fields) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO link_inspections(source_id, account_id) VALUES (?,?)", (sid, aid))
            if fields:
                c.execute(f"UPDATE link_inspections SET {','.join(k + '=?' for k in fields)} WHERE source_id=?",
                          (*fields.values(), sid))
        with self._changed:
            self._changed.notify_all()

    # ---- what the UI and the assistant see ----------------------------------
    def view(self, source: dict, row: dict | None = None) -> dict:
        row = row if row is not None else self._row(source["id"])
        state, outcome = row.get("state") or "idle", row.get("outcome")
        if state == "waiting_login":
            actions = ["continue", "cancel"]
        elif state in ("queued", "running"):
            actions = ["cancel"] if state == "queued" else []
        elif outcome == "login_required" and row.get("login_method") == "basic" and row.get("reason") != "insecure_basic":
            actions = ["credentials", "inspect"]
        elif outcome in ("login_required", "blocked", "forbidden", "empty"):
            actions = ["sign_in", "inspect"]
        elif outcome == "ok":
            actions = ["inspect", "sign_in"]
        elif outcome == "unsafe":
            actions = []
        else:
            actions = ["inspect"]
        label = {"queued": "Waiting to be inspected", "running": "Inspecting…", "waiting_login": "Waiting for your sign-in"}.get(state) or {
            None: "Not inspected yet", "ok": "Read and saved", "login_required": "Sign-in needed", "network": "Network problem",
            "not_found": "Page not found", "forbidden": "Access refused", "site_error": "Site error", "blocked": "Blocked by the site",
            "empty": "Nothing readable", "unsupported": "Unsupported file", "unsafe": "Not opened", "error": "Inspection failed"}[outcome]
        return {"state": state, "outcome": outcome, "label": label, "reason": row.get("reason"),
                "login_method": row.get("login_method") if outcome == "login_required" or state == "waiting_login" else None,
                "login_host": row.get("login_host") if outcome == "login_required" or state == "waiting_login" else None,
                "http_status": row.get("http_status"), "final_url": row.get("final_url"), "page_title": row.get("page_title"),
                "content_chars": row.get("content_chars"), "item_id": row.get("item_id"), "message": row.get("message"),
                "checked_at": row.get("checked_at"), "succeeded_at": row.get("succeeded_at"),
                "notice_dismissed": bool(row.get("notice_dismissed_at")), "actions": actions}

    def sources(self, aid: int, course_id=None, item_id=None) -> list[dict]:
        """Compact rows: enough to list, act on and explain every link."""
        self.hub.identity.get_account(aid)
        sql = ("SELECT s.id, s.course_id, s.url, s.title, s.last_seen_at, c.title AS course_title, li.* "
               "FROM external_sources s LEFT JOIN link_inspections li ON li.source_id=s.id "
               "LEFT JOIN items c ON c.account_id=s.account_id AND c.item_type='course' AND c.source_key=s.course_id "
               "WHERE s.account_id=?")
        args: list = [aid]
        if course_id is not None:
            sql += " AND s.course_id=?"
            args.append(str(course_id))
        if item_id is not None:
            sql += " AND s.id IN (SELECT source_id FROM external_source_origins WHERE item_id=?)"
            args.append(int(item_id))
        out = []
        for r in self.db.query(sql + " ORDER BY s.course_id, s.id LIMIT 500", args):
            r = dict(r)
            source = {"id": r["id"], "course_id": r["course_id"], "course_title": r["course_title"], "url": r["url"],
                      "title": r["title"], "host": _host(r["url"]), "last_seen_at": r["last_seen_at"], "earlier": None}
            source["inspection"] = self.view(source, {**r, "state": r["state"] or "idle"})
            out.append(source)
        # Text kept from before link inspection existed (an agent's or adapter's read) stays visible.
        without_page = {s["id"]: s for s in out if not s["inspection"]["item_id"]}
        if without_page:
            for read in self.db.query("SELECT source_id, read_at, evidence_json FROM external_reads WHERE id IN "
                                      "(SELECT MAX(r.id) FROM external_reads r JOIN external_sources s ON s.id=r.source_id "
                                      "WHERE s.account_id=? GROUP BY r.source_id)", (aid,)):
                source = without_page.get(read["source_id"])
                text = "\n\n".join(p.get("content") or "" for p in json.loads(read["evidence_json"]) if isinstance(p, dict)).strip()
                if source is not None and text:
                    source["earlier"] = {"read_at": read["read_at"], "text": text[:3000]}
        return out

    def summary(self, aid: int) -> dict:
        rows = self.sources(aid)
        new = [s for s in rows if s["inspection"]["outcome"] is None and s["inspection"]["state"] == "idle"]
        notice = [s for s in new if not s["inspection"]["notice_dismissed"]]
        waiting = next((s for s in rows if s["inspection"]["state"] == "waiting_login"), None)
        return {"total": len(rows),
                "not_inspected": len(new),
                "needs_login": sum(s["inspection"]["outcome"] == "login_required" and s["inspection"]["state"] == "idle" for s in rows),
                "failed": sum(s["inspection"]["outcome"] not in (None, "ok", "login_required") and s["inspection"]["state"] == "idle" for s in rows),
                "read": sum(s["inspection"]["outcome"] == "ok" for s in rows),
                "active": sum(s["inspection"]["state"] in ("queued", "running") for s in rows),
                "waiting_login": {"source_id": waiting["id"], "title": waiting["title"],
                                  "host": waiting["inspection"]["login_host"] or waiting["host"]} if waiting else None,
                "notice": {"count": len(notice), "source_ids": [s["id"] for s in notice],
                           "examples": [{"title": s["title"], "host": s["host"], "course_title": s["course_title"]} for s in notice[:4]]}}

    # ---- commands (any thread) ----------------------------------------------
    def _start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stopping = False
                self._thread = threading.Thread(target=self._run, name="canvas-hub-link-inspection", daemon=True)
                self._thread.start()

    def stop(self, timeout: float = 15) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._stopping = True
        self._q.put(None)
        thread.join(timeout)

    def inspect(self, aid: int, source_ids=None, *, course_id=None, item_id=None, only_new: bool = False) -> dict:
        """Queue reads. With no ids: every link in the scope that has not been read successfully."""
        self.hub.identity.get_account(aid)
        if source_ids is None:
            chosen = [s for s in self.sources(aid, course_id, item_id) if s["inspection"]["state"] == "idle" and (
                s["inspection"]["outcome"] is None if only_new else
                s["inspection"]["outcome"] not in ("ok", "login_required", "unsafe"))]
            ids = [s["id"] for s in chosen]
        else:
            if not isinstance(source_ids, list) or len(source_ids) > 200 or any(type(x) is not int for x in source_ids):
                raise InvalidInput("source_ids must be a list of link ids")
            ids = list(dict.fromkeys(source_ids))
        queued = []
        for sid in ids:
            self._source(aid, sid)
            state = self._row(sid)["state"]
            if state == "waiting_login":
                self.continue_sign_in(aid, sid)
                continue
            if state != "idle":
                continue
            self._set(aid, sid, state="queued", requested_at=now_iso(), notice_dismissed_at=now_iso())
            queued.append(sid)
            self._q.put(lambda aid=aid, sid=sid: self._job_inspect(aid, sid))
        if queued:
            self._start()
        return {"queued": queued}

    def submit_credentials(self, aid: int, sid: int, username, password, *, remember: bool = False) -> dict:
        source = self._source(aid, sid)
        row = self._row(sid)
        if row.get("outcome") != "login_required" or row.get("login_method") != "basic" or row.get("state") != "idle":
            raise Conflict("This link is not waiting for a username and password. Inspect it again first.")
        if (not isinstance(username, str) or not isinstance(password, str) or not 1 <= len(username) <= 1024
                or not 1 <= len(password) <= 4096 or ":" in username
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in username + password)):
            raise InvalidInput("Enter the username and password for this site")
        final = row.get("final_url") or source["url"]
        origin = _origin(final)
        if not origin.startswith("https://") and not self.allow_local:
            raise Conflict("Canvas Hub only sends a password over https")
        try:
            value = "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        except UnicodeError:
            raise InvalidInput("These characters cannot be used in a site password") from None
        owner = self._owner(aid)
        with self._lock:
            self._basic[(owner, origin)] = value
        saved = self._saved_credentials()
        if remember is True:
            saved.setdefault(owner, {})[origin] = value
            self._write_credentials(saved)
        elif saved.get(owner, {}).pop(origin, None) is not None:
            self._write_credentials({k: v for k, v in saved.items() if v})
        self._set(aid, sid, state="queued", requested_at=now_iso())
        # The read carries the sign-in it was started for: another link on the same site may be
        # read first, be refused, and clear a wrong password before this one gets its turn.
        self._q.put(lambda: self._job_inspect(aid, sid, basic=(owner, origin, value)))
        self._start()
        return self.view(source)

    def sign_in(self, aid: int, sid: int) -> dict:
        source = self._source(aid, sid)
        row = self._row(sid)
        if row["state"] == "waiting_login":
            return self.view(source)
        if row["state"] != "idle":
            raise Conflict("This link is being inspected. Try again in a moment.")
        waiting = self.db.one("SELECT s.title FROM link_inspections li JOIN external_sources s ON s.id=li.source_id "
                              "WHERE li.state='waiting_login'")
        if waiting:
            raise Conflict(f"A sign-in window is already open for “{waiting['title']}”. Finish or cancel that one first.",
                           code="sign_in_window_open")
        self._check_target(source["url"], resolve=False)
        self._set(aid, sid, state="waiting_login", requested_at=now_iso(), notice_dismissed_at=now_iso(),
                  message="Opening the sign-in window…")
        self._q.put(lambda: self._job_open_login(aid, sid))
        self._start()
        return self.view(source)

    def continue_sign_in(self, aid: int, sid: int) -> dict:
        source = self._source(aid, sid)
        if self._row(sid)["state"] != "waiting_login":
            raise Conflict("No sign-in window is open for this link.")
        self._q.put(lambda: self._job_continue(aid, sid))
        return self.view(source)

    def cancel(self, aid: int, sid: int) -> dict:
        source = self._source(aid, sid)
        state = self._row(sid)["state"]
        if state == "queued":
            self._set(aid, sid, state="idle")
        elif state == "waiting_login":
            self._q.put(lambda: self._job_cancel_login(aid, sid))
        return self.view(source)

    def dismiss_notice(self, aid: int, source_ids=None) -> dict:
        self.hub.identity.get_account(aid)
        ids = self.summary(aid)["notice"]["source_ids"] if source_ids is None else source_ids
        if not isinstance(ids, list) or any(type(x) is not int for x in ids):
            raise InvalidInput("source_ids must be a list of link ids")
        for sid in ids[:500]:
            self._source(aid, sid)
            self._set(aid, sid, notice_dismissed_at=now_iso())
        return self.summary(aid)

    def forget_sign_ins(self, aid: int) -> dict:
        """Delete this account's external-site browser profile and saved site passwords. Saved pages stay."""
        owner = self._owner(aid)
        if self.db.one("SELECT 1 FROM link_inspections WHERE account_id=? AND state!='idle'", (aid,)):
            raise Conflict("Wait for the current inspection or sign-in to finish first.")
        done = threading.Event()

        def job():
            try:
                if self._ctx_owner == owner:
                    self._close_context(keep_session=False)
                self._carry.pop(owner, None)
                shutil.rmtree(self._profile(owner), ignore_errors=True)
            finally:
                done.set()
        if self._thread is not None and self._thread.is_alive():
            self._q.put(job)
            done.wait(20)
        else:
            job()
        with self._lock:
            for key in [k for k in self._basic if k[0] == owner]:
                del self._basic[key]
        saved = self._saved_credentials()
        if saved.pop(owner, None) is not None:
            self._write_credentials(saved)
        return {"forgotten": True}

    def wait(self, aid: int, sid: int, timeout: float = 60) -> dict:
        """Block until the link is no longer queued or running (it may be waiting for a sign-in)."""
        source = self._source(aid, sid)
        deadline = time.monotonic() + timeout
        with self._changed:
            while self._row(sid)["state"] in ("queued", "running") and time.monotonic() < deadline:
                self._changed.wait(0.25)
        return self.view(source)

    # ---- worker thread -------------------------------------------------------
    def _run(self) -> None:
        while True:
            try:
                if self._login is None:
                    job = self._q.get()
                else:
                    try:
                        job = self._q.get_nowait()
                    except queue.Empty:
                        self._login_tick()
                        continue
                if job is None:
                    break
                job()
            except Exception:  # noqa: BLE001
                log.exception("link inspection worker error")
        try:
            if self._login is not None:
                self._end_login(self._login["aid"], self._login["sid"], None, "Canvas Hub closed before sign-in finished.")
            self._close_context(keep_session=False)
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = None

    def _alive(self) -> bool:
        if self._ctx is None:
            return False
        try:
            self._ctx.cookies()
            return True
        except Exception:  # noqa: BLE001
            self._ctx = None
            return False

    def _context(self, owner: str, *, visible: bool = False):
        want_headed = visible and not self.headless
        if self._alive():
            if self._ctx_owner == owner and (self._ctx_headed or not want_headed):
                self._sync_routes(owner)
                return self._ctx
            self._close_context()
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        profile = self._profile(owner)
        for directory in (profile.parent, profile):
            if directory.is_symlink():
                raise InvalidInput("The external-site browser profile cannot be a symbolic link")
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        options = {"headless": not want_headed, "accept_downloads": False,
                   "viewport": None if want_headed else {"width": 1280, "height": 900}}
        if os.environ.get("CANVAS_HUB_CHROMIUM"):
            options["executable_path"] = os.environ["CANVAS_HUB_CHROMIUM"]
        elif os.environ.get("CANVAS_HUB_BROWSER_CHANNEL"):
            options["channel"] = os.environ["CANVAS_HUB_BROWSER_CHANNEL"]
        ctx = self._pw.chromium.launch_persistent_context(str(profile), **options)
        carried = self._carry.pop(owner, None)
        if carried:
            try:
                ctx.add_cookies(carried)
            except Exception:  # noqa: BLE001
                log.warning("could not restore session cookies for the external-site browser")
        if not self.allow_local:
            ctx.route(PRIVATE_URL, lambda route: route.abort("blockedbyclient"))
        self._ctx, self._ctx_owner, self._ctx_headed, self._routes = ctx, owner, want_headed, set()
        self._sync_routes(owner)
        return ctx

    def _sync_routes(self, owner: str) -> None:
        """Attach a saved Basic sign-in to requests for its own origin, and to nothing else."""
        with self._lock:
            origins = {origin for (who, origin) in self._basic if who == owner}
        origins |= set(self._saved_credentials().get(owner, {}))
        for origin in origins - self._routes:
            self._ctx.route(origin + "/**", self._basic_handler(owner, origin))
            self._routes.add(origin)

    def _basic_handler(self, owner: str, origin: str):
        # Playwright passes (route, request) to handlers that accept two arguments,
        # so the origin is bound in a closure rather than as a default argument.
        def handler(route):
            try:
                value = self._basic_for(owner, origin)
                request = route.request
                if value and _origin(request.url) == origin:
                    route.continue_(headers={**request.headers, "authorization": value})
                else:
                    route.continue_()
            except Exception:  # noqa: BLE001
                try:
                    route.abort()
                except Exception:  # noqa: BLE001
                    pass
        return handler

    def _close_context(self, *, keep_session: bool = True) -> None:
        ctx, self._ctx = self._ctx, None
        if ctx is None:
            return
        try:
            if keep_session and self._ctx_owner:
                # Session cookies end with the browser process. Keep them in memory so a
                # sign-in made in the visible window still works for background reads.
                self._carry[self._ctx_owner] = [c for c in ctx.cookies() if c.get("expires", -1) in (-1, 0, None)]
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass

    def _canvas_reachable(self, aid: int) -> bool:
        try:
            p = urlsplit(self.hub.identity.get_account(aid)["origin"])
            socket.create_connection((p.hostname, p.port or (443 if p.scheme == "https" else 80)), timeout=4).close()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _check_target(self, url: str, *, resolve: bool = True) -> None:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise InvalidInput("Only http(s) links can be inspected")
        if self.allow_local:
            return
        clean_url(url)                           # public host, usual port, not an action such as log out or submit
        try:
            ipaddress.ip_address(p.hostname)
            return                               # literal addresses were checked by clean_url
        except ValueError:
            pass
        if not resolve:
            return
        try:
            addresses = socket.getaddrinfo(p.hostname, None, type=socket.SOCK_STREAM)
        except OSError:
            return                               # let the browser report the DNS failure
        if any(not public_address(a[4][0]) for a in addresses):
            raise InvalidInput("This address resolves to a private network address")

    def _private_destination(self, url: str) -> bool:
        if self.allow_local:
            return False
        host = (urlsplit(url).hostname or "").lower()
        try:
            return not public_address(host)
        except ValueError:
            return host == "localhost" or host.endswith((".local", ".internal", ".localhost"))

    def _read(self, ctx, aid: int, url: str) -> dict:
        """Load one page in a new tab and classify it. Never clicks, types or submits."""
        owner = self._owner(aid)
        try:
            self._check_target(url)
        except InvalidInput:
            return {"outcome": "unsafe", "reason": "refused_target"}
        page = ctx.new_page()
        chain: list[dict] = []

        def on_response(response):
            try:
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    chain.append({"status": response.status, "url": response.url,
                                  "challenge": response.headers.get("www-authenticate"),
                                  "type": (response.headers.get("content-type") or "").split(";")[0].strip().lower()})
            except Exception:  # noqa: BLE001
                pass
        page.on("response", on_response)
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_MS)
            except Exception as e:  # noqa: BLE001
                last = chain[-1] if chain else None
                if last and last["status"] < 400 and last["type"] and last["type"] not in HTML_TYPES:
                    return self._read_file(ctx, owner, last)
                if "ERR_BLOCKED_BY_CLIENT" in str(e) or any(self._private_destination(c["url"]) for c in chain):
                    return {"outcome": "unsafe", "reason": "private_destination"}
                reason = _network_reason(str(e))
                if reason in ("dns", "timeout", "refused", "network_error") and not self.online(aid):
                    reason = "offline"
                return {"outcome": "network", "reason": reason, "final_url": _display_url(chain[-1]["url"]) if chain else None}
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
            except Exception:  # noqa: BLE001
                pass
            final_url = page.url
            if self._private_destination(final_url) or any(self._private_destination(c["url"]) for c in chain):
                return {"outcome": "unsafe", "reason": "private_destination"}
            last = next((c for c in reversed(chain) if c["url"].split("#")[0] == final_url.split("#")[0]), chain[-1] if chain else None)
            if last and last["status"] < 400 and last["type"] and last["type"] not in HTML_TYPES:
                return self._read_file(ctx, owner, last)
            facts = page.evaluate(FACTS_JS)
            origin = _origin(final_url)
            result = classify(url, final_url, last["status"] if last else None, last["challenge"] if last else None, facts,
                              sent_basic=bool(self._basic_for(owner, origin)))
            if result.get("login_method") == "basic" and not origin.startswith("https://") and not self.allow_local:
                result["reason"] = "insecure_basic"
            if result.get("reason") == "credentials_rejected":
                self._forget_basic(owner, origin)            # never keep sending a rejected password
            return result
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def _read_file(self, ctx, owner: str, hop: dict) -> dict:
        base = {"http_status": hop["status"], "final_url": _display_url(hop["url"]), "content_type": hop["type"]}
        if hop["type"] != "application/pdf" and hop["type"] not in TEXT_TYPES:
            return {**base, "outcome": "unsupported", "reason": "file_type"}
        headers = {}
        basic = self._basic_for(owner, _origin(hop["url"]))
        if basic:
            headers["authorization"] = basic
        try:
            response = ctx.request.get(hop["url"], headers=headers, timeout=NAVIGATION_MS, max_redirects=0)
            if response.status != 200:
                return {**base, "outcome": "site_error", "reason": f"http_{response.status}", "http_status": response.status}
            body = response.body()
        except Exception as e:  # noqa: BLE001
            return {**base, "outcome": "network", "reason": _network_reason(str(e))}
        if len(body) > MAX_FILE_BYTES:
            return {**base, "outcome": "unsupported", "reason": "file_too_large"}
        if hop["type"] == "application/pdf":
            try:
                parsed = subprocess.run([sys.executable, "-m", "canvas_hub.services.external_pdf"], input=body,
                                        capture_output=True, timeout=20, check=True)
                text = parsed.stdout.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return {**base, "outcome": "unsupported", "reason": "pdf_unreadable"}
        else:
            text = body.decode("utf-8", errors="replace")
        text = text.strip()[:MAX_TEXT]
        if len(text) < 20:
            return {**base, "outcome": "empty", "reason": "no_text"}
        return {**base, "outcome": "ok", "reason": None, "content": text,
                "page_title": urlsplit(hop["url"]).path.rsplit("/", 1)[-1][:300] or None}

    # ---- recording a result ---------------------------------------------------
    def _record(self, aid: int, sid: int, result: dict, *, state: str = "idle") -> None:
        source = self._source(aid, sid)
        when = now_iso()
        outcome = result.get("outcome") if result.get("outcome") in OUTCOMES else "error"
        fields = {"state": state, "outcome": outcome, "reason": result.get("reason"),
                  "login_method": result.get("login_method"), "login_host": result.get("login_host"),
                  "http_status": result.get("http_status"), "final_url": result.get("final_url"),
                  "page_title": result.get("page_title"), "checked_at": when}
        sources = self.hub.external_sources
        if outcome == "ok":
            content = result["content"]
            page_url = source["url"]
            with sources._lock, self.db.tx() as c:
                sources._publish(aid, source, [{"url": page_url, "content": content, "records": []}], 0, when,
                                 snapshot=PROVIDED_BY)
                c.execute("INSERT INTO external_reads(source_id,read_at,status,evidence_json,detail) VALUES (?,?,?,?,?)",
                          (sid, when, "inspection_read", canonical_json([{"url": page_url, "content": content,
                                                                           "provided_by": PROVIDED_BY}]), None))
                c.execute("UPDATE external_sources SET status='enabled', last_attempt_at=?, last_success_at=?, error=NULL "
                          "WHERE id=? AND enabled_revision IS NULL", (when, when, sid))
                item = c.execute("SELECT id FROM items WHERE account_id=? AND item_type='external_link' AND source_key=?",
                                 (aid, f"external:{sid}:inspected-page:{sha256_text(page_url)}")).fetchone()
            fields.update(content_chars=len(content), item_id=item["id"] if item else None, succeeded_at=when)
            result = {**result, "content_chars": len(content)}
        else:
            status = "needs_login" if outcome == "login_required" else "stale" if source["last_success_at"] else "needs_attention"
            with self.db.tx() as c:
                c.execute("UPDATE external_sources SET status=?, last_attempt_at=?, error=? WHERE id=? AND enabled_revision IS NULL",
                          (status, when, explain(result, source["url"])[:2000], sid))
        fields["message"] = explain(result, source["url"])
        self._set(aid, sid, **fields)
        try:
            self.hub.components.sync_sources(aid)
        except Exception:  # noqa: BLE001
            log.exception("could not refresh tracker source status after a link inspection")

    # ---- jobs (worker thread) -------------------------------------------------
    def _job_inspect(self, aid: int, sid: int, basic: tuple | None = None) -> None:
        try:
            if self._row(sid)["state"] != "queued":
                return                           # cancelled, or the account/source was deleted
            if self._login is not None:
                self._deferred.append(lambda: self._job_inspect(aid, sid, basic))   # do not open tabs in the user's sign-in window
                return
            if basic is not None:
                with self._lock:
                    self._basic[basic[:2]] = basic[2]
            source = self._source(aid, sid)
            before = self._row(sid)
            self._set(aid, sid, state="running")
            result = self._read(self._context(self._owner(aid)), aid, source["url"])
            self._record(aid, sid, result)
            if result.get("outcome") == "ok" and before.get("outcome") == "login_required":
                self._retry_related(aid, sid, before.get("login_host"))
        except NotFound:
            return
        except Exception:  # noqa: BLE001
            log.exception("link inspection failed")
            try:
                self._record(aid, sid, {"outcome": "error", "reason": "internal"})
            except Exception:  # noqa: BLE001
                pass

    def _job_open_login(self, aid: int, sid: int) -> None:
        try:
            if self._row(sid)["state"] != "waiting_login" or self._login is not None:
                return
            source = self._source(aid, sid)
            try:
                self._check_target(source["url"])
            except InvalidInput:
                self._record(aid, sid, {"outcome": "unsafe", "reason": "refused_target"})
                return
            ctx = self._context(self._owner(aid), visible=True)
            blank = [p for p in ctx.pages if p.url in ("about:blank", "chrome://newtab/")]
            page = blank[0] if blank else ctx.new_page()
            try:
                page.goto(source["url"], wait_until="domcontentloaded", timeout=NAVIGATION_MS)
            except Exception:  # noqa: BLE001
                pass                             # the user can still retry or type an address in the window
            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            self._login = {"aid": aid, "sid": sid, "page": page, "url": source["url"], "deadline": time.monotonic() + LOGIN_SECONDS,
                           "seen_url": None, "seen_at": 0.0, "tried_url": None, "auto_left": AUTO_CHECKS}
            self._set(aid, sid, message="The sign-in window is open. Sign in there as you normally would; "
                                        "Canvas Hub continues by itself when the page is readable.")
        except Exception:  # noqa: BLE001
            log.exception("could not open the sign-in window")
            self._login = None
            try:
                self._set(aid, sid, state="idle", message="The sign-in window could not be opened. Check that the Canvas Hub "
                                                          "browser is installed (uv run playwright install chromium), then try again.")
            except Exception:  # noqa: BLE001
                pass

    def _login_page(self):
        """The tab the user is working in; follows pop-ups and new tabs."""
        login = self._login
        try:
            if not login["page"].is_closed():
                return login["page"]
            pages = [p for p in self._ctx.pages if not p.is_closed()] if self._alive() else []
        except Exception:  # noqa: BLE001
            pages = []
        if pages:
            login["page"] = pages[-1]
            return pages[-1]
        return None

    def _login_tick(self) -> None:
        login = self._login
        aid, sid = login["aid"], login["sid"]
        try:
            if self._row(sid)["state"] != "waiting_login":
                self._end_login(aid, sid, None, None)
                return
            page = self._login_page()
            if page is None:
                # The user closed the window. A persistent sign-in may still have been saved.
                self._ctx = self._ctx if self._alive() else None
                result = self._read(self._context(self._owner(aid)), aid, login["url"])
                self._end_login(aid, sid, result, None if result.get("outcome") == "ok" else
                                "The sign-in window was closed before the page became readable.")
                return
            if time.monotonic() > login["deadline"]:
                self._end_login(aid, sid, None, "The sign-in window was open for 15 minutes without finishing. Try again when you are ready.")
                return
            page.wait_for_timeout(400)           # also lets the browser's own events run
            url = page.url
            now = time.monotonic()
            if url != login["seen_url"]:
                login["seen_url"], login["seen_at"] = url, now
                return
            if (login["auto_left"] <= 0 or url == login["tried_url"] or now - login["seen_at"] < 1.2
                    or _host(url) != _host(login["url"]) or LOGIN_PATH.search(urlsplit(url).path)):
                return
            quick = page.evaluate("() => !![...document.querySelectorAll('input[type=password]')].find(i => i.offsetParent !== null)")
            if quick:
                return
            # The user's tab is back on the site and shows no sign-in form: try the real read.
            login["tried_url"], login["auto_left"] = url, login["auto_left"] - 1
            result = self._read(self._ctx, aid, login["url"])
            if result.get("outcome") == "ok":
                self._end_login(aid, sid, result, None)
        except NotFound:
            self._end_login(aid, sid, None, None)
        except Exception:  # noqa: BLE001
            log.exception("sign-in window check failed")
            time.sleep(0.4)

    def _job_continue(self, aid: int, sid: int) -> None:
        login = self._login
        if not login or (login["aid"], login["sid"]) != (aid, sid):
            return
        try:
            ctx = self._ctx if self._alive() else self._context(self._owner(aid))
            result = self._read(ctx, aid, login["url"])
        except Exception:  # noqa: BLE001
            log.exception("continue after sign-in failed")
            return
        if result.get("outcome") in ("login_required", "blocked") and self._login_page() is not None:
            self._set(aid, sid, message="The page still asks for a sign-in. Finish signing in in the window, then choose Continue again.")
            return
        self._end_login(aid, sid, result, None)

    def _job_cancel_login(self, aid: int, sid: int) -> None:
        if self._login and (self._login["aid"], self._login["sid"]) == (aid, sid):
            self._end_login(aid, sid, None, "Sign-in cancelled. Nothing was changed.")
        elif self._row(sid)["state"] == "waiting_login":
            self._set(aid, sid, state="idle")

    def _end_login(self, aid: int, sid: int, result: dict | None, message: str | None) -> None:
        login, self._login = self._login, None
        try:
            login_host = self._row(sid).get("login_host")
            if login is not None:
                try:
                    if not login["page"].is_closed() and not self._ctx_headed:
                        login["page"].close()
                except Exception:  # noqa: BLE001
                    pass
            if self._ctx_headed:
                self._close_context()            # the window disappears: a clear "done"
            try:
                if result is not None:
                    self._record(aid, sid, result)
                    if message and result.get("outcome") != "ok":
                        self._set(aid, sid, message=message)
                else:
                    self._set(aid, sid, state="idle", **({"message": message} if message else {}))
            except NotFound:
                pass
            if result is not None and result.get("outcome") == "ok":
                self._retry_related(aid, sid, login_host)
        finally:
            deferred, self._deferred = self._deferred, []
            for job in deferred:
                self._q.put(job)

    def _retry_related(self, aid: int, sid: int, login_host: str | None) -> None:
        """One sign-in often unlocks several links (same site, or the same single sign-on)."""
        hosts = {_host(self._source(aid, sid)["url"]), _host(self._row(sid).get("final_url") or "")} - {""}
        for other in self.db.query("SELECT li.source_id, li.login_host, s.url FROM link_inspections li JOIN external_sources s "
                                   "ON s.id=li.source_id WHERE li.account_id=? AND li.state='idle' AND li.outcome='login_required' "
                                   "AND li.source_id!=?", (aid, sid)):
            if _host(other["url"]) in hosts or (login_host and other["login_host"] == login_host):
                self._set(aid, other["source_id"], state="queued", requested_at=now_iso())
                self._q.put(lambda other_id=other["source_id"]: self._job_inspect(aid, other_id))
