"""Synthetic external course sites for tests and offline demos.

Each site mimics one way a real course site can ask for sign-in. Everything
here is made up; no real institution, account or course content is involved.

* ``course``  - a course website behind HTTP Basic authentication (a 401 with
  a ``WWW-Authenticate: Basic`` challenge), plus a legacy path that redirects
  to it, a PDF, and public/missing/broken/empty pages.
* ``grader``  - a submission platform that redirects to a separate identity
  provider (single sign-on with a second verification step) and keeps its own
  session in a *session* cookie.
* ``idp``     - that identity provider, on its own origin.
* ``forum``   - a discussion forum with its own e-mail/password form whose
  class page is rendered by a script after a POST to its API.

Every request is recorded so tests can assert where credentials went.

Run standalone:  python -m canvas_hub.testing.fake_sites
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from .fake_canvas import make_pdf

BASIC_USER, BASIC_PASSWORD = "student", "basic pass"
SSO_USER, SSO_PASSWORD, SSO_CODE = "astudent", "sso pass", "135790"
FORUM_EMAIL, FORUM_PASSWORD = "astudent@example.edu", "forum pass"

COURSE_TEXT = ("Course website. Lecture 7 covers recursion and the call stack. Lab 5 is released on Friday and "
               "is due the following Thursday at 23:59. The midterm covers lectures 1 to 6 and is held in LT-A. "
               "Programming Assignment 2 is submitted through the grader, not through Canvas.")
GRADER_TEXT = ("Assignments on the grader. Programming Assignment 2: Maze Solver, due 21 November 23:59, "
               "three submissions remaining. Lab 5: Linked lists, due 14 November 23:59, graded 9 out of 10. "
               "Late submissions lose ten percent per day.")
FORUM_POSTS = ["Midterm logistics: bring your student card and a pen. Seating is posted on Thursday.",
               "PA2 clarification: the maze input always has exactly one exit.",
               "Lab 5 hint: draw the list before writing the pointer updates."]
PUBLIC_TEXT = ("Public syllabus. Grading: labs 20 percent, programming assignments 30 percent, midterm 20 percent, "
               "final examination 30 percent. Office hours are on Tuesday afternoons in room 4210.")


def _page(title: str, body: str, script: str = "") -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
            f"<body>{body}{'<script>' + script + '</script>' if script else ''}</body></html>").encode()


class FakeSites:
    SITES = ("course", "grader", "idp", "forum")

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._servers: dict[str, ThreadingHTTPServer] = {}
        self._threads: list[threading.Thread] = []
        self.idp_sessions: set[str] = set()
        self.idp_pending: set[str] = set()
        self.tickets: set[str] = set()
        self.grader_sessions: set[str] = set()
        self.forum_sessions: set[str] = set()
        self.down: set[str] = set()      # sites that answer 503 (simulated outage)

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeSites":
        for name in self.SITES:
            sites = self

            class Handler(BaseHTTPRequestHandler):
                site = name
                protocol_version = "HTTP/1.1"

                def log_message(self, *_a):
                    pass

                def do_GET(self):
                    sites._handle(self, "GET")

                def do_POST(self):
                    sites._handle(self, "POST")

            server = ThreadingHTTPServer((self.host, 0), Handler)
            server.daemon_threads = True
            self._servers[name] = server
            thread = threading.Thread(target=server.serve_forever, name=f"fake-site-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        for server in self._servers.values():
            server.shutdown()
            server.server_close()
        self._servers.clear()

    def origin(self, name: str) -> str:
        return f"http://{self.host}:{self._servers[name].server_address[1]}"

    def url(self, name: str, path: str = "/") -> str:
        return self.origin(name) + path

    def seen(self, site: str | None = None, method: str | None = None) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if (site is None or r["site"] == site) and (method is None or r["method"] == method)]

    def expire_sessions(self) -> None:
        """Server-side sign-out everywhere (the browser may still hold cookies)."""
        self.idp_sessions.clear()
        self.grader_sessions.clear()
        self.forum_sessions.clear()

    # ---- plumbing ----------------------------------------------------------
    def _send(self, h, status: int, body: bytes | str = b"", ctype: str = "text/html; charset=utf-8", headers=None) -> None:
        data = body.encode() if isinstance(body, str) else body
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        for key, value in (headers or []):
            h.send_header(key, value)
        h.end_headers()
        h.wfile.write(data)

    def _redirect(self, h, location: str, cookies=()) -> None:
        self._send(h, 302, b"", headers=[("Location", location), *[("Set-Cookie", c) for c in cookies]])

    @staticmethod
    def _cookie(h, name: str) -> str | None:
        jar = SimpleCookie(h.headers.get("Cookie", ""))
        return jar[name].value if name in jar else None

    @staticmethod
    def _form(h) -> dict:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length).decode("utf-8", "replace") if length else ""
        if h.headers.get("Content-Type", "").startswith("application/json"):
            try:
                return json.loads(raw or "{}")
            except ValueError:
                return {}
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _handle(self, h, method: str) -> None:
        parts = urlsplit(h.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        form = self._form(h) if method == "POST" else {}
        with self._lock:
            self.requests.append({"site": h.site, "method": method, "path": parts.path, "query": dict(query),
                                  "authorization": h.headers.get("Authorization"), "cookie": h.headers.get("Cookie")})
        if h.site in self.down:
            self._send(h, 503, _page("Unavailable", "<p>Service unavailable.</p>"))
            return
        getattr(self, "_" + h.site)(h, method, parts.path, query, form)

    # ---- course website: HTTP Basic ----------------------------------------
    def _course(self, h, method, path, query, form) -> None:
        if path == "/old/":
            self._redirect(h, "/comp/")                        # the legacy link Canvas still points at
        elif path == "/public/syllabus":
            self._send(h, 200, _page("Public syllabus", f"<main><h1>Syllabus</h1><p>{PUBLIC_TEXT}</p></main>"))
        elif path == "/public/app":
            # Like a script-built grader page: the visible text says little; the details come from its own API.
            self._send(h, 200, _page("Assignments", "<main><h1>Assignments</h1><ul id='list'></ul></main>",
                                     "fetch('/public/api/session').then(r=>r.json());"
                                     "fetch('/public/api/assignments').then(r=>r.json()).then(d=>{const l=document.getElementById('list');"
                                     "d.data.assignments.forEach(a=>{const li=document.createElement('li');li.textContent=a.title;l.append(li);});});"))
        elif path == "/public/api/assignments":
            self._send(h, 200, json.dumps({"data": {"assignments": [
                {"__typename": "Assignment", "title": "PA2 Maze Solver", "dueAt": "2026-11-21T23:59:00+08:00", "attemptsLeft": 3},
                {"__typename": "Assignment", "title": "Lab 5 Linked lists", "dueAt": "2026-11-14T23:59:00+08:00",
                 "grade": {"score": 9, "outOf": 10}, "apiToken": "do-not-keep"}]}}), "application/json")
        elif path == "/public/api/session":
            self._send(h, 200, json.dumps({"user": "astudent", "sessionKey": "secret-session-value"}), "application/json")
        elif path == "/missing":
            self._send(h, 404, _page("Not found", "<h1>404</h1><p>Nothing here.</p>"))
        elif path == "/broken":
            self._send(h, 500, _page("Error", "<h1>500</h1><p>Internal error.</p>"))
        elif path == "/empty":
            self._send(h, 200, _page("Empty", "<div id='app'></div>"))
        elif path == "/archive.zip":
            self._send(h, 200, b"PK\x05\x06" + b"\0" * 18, "application/zip")
        elif path == "/denied":
            self._send(h, 403, _page("Forbidden", "<h1>403</h1><p>You do not have access to this page.</p>"))
        elif path.startswith("/comp/"):
            expected = "Basic " + base64.b64encode(f"{BASIC_USER}:{BASIC_PASSWORD}".encode()).decode()
            if h.headers.get("Authorization") != expected:
                self._send(h, 401, _page("Authorization required", "<h1>401 Authorization Required</h1>"),
                           headers=[("WWW-Authenticate", 'Basic realm="Course materials"')])
            elif path == "/comp/moved":
                self._redirect(h, self.origin("forum") + "/public")     # moved to another site
            elif path == "/comp/notes.pdf":
                self._send(h, 200, make_pdf(["Lecture notes", "Recursion needs a base case."]), "application/pdf")
            else:
                self._send(h, 200, _page("COMP course website",
                                         f"<nav><a href='/comp/notes.pdf'>Notes</a></nav><main><h1>Course website</h1><p>{COURSE_TEXT}</p></main>"))
        else:
            self._send(h, 404, _page("Not found", "<h1>404</h1>"))

    # ---- grader: redirects to the identity provider -------------------------
    def _grader(self, h, method, path, query, form) -> None:
        if path == "/callback":
            ticket = query.get("ticket", "")
            if ticket in self.tickets:
                self.tickets.discard(ticket)
                session = secrets.token_hex(12)
                self.grader_sessions.add(session)
                # A session cookie (no Max-Age): it ends when the browser closes.
                self._redirect(h, query.get("next") or "/assignments", [f"grader_session={session}; Path=/; HttpOnly"])
            else:
                self._send(h, 403, _page("Sign-in failed", "<p>Invalid ticket.</p>"))
            return
        if self._cookie(h, "grader_session") not in self.grader_sessions:
            service = self.origin("grader") + "/callback?next=" + quote(path, safe="")
            self._redirect(h, self.origin("idp") + "/cas/login?service=" + quote(service, safe=""))
            return
        if path == "/assignments":
            self._send(h, 200, _page("Grader", f"<main><h1>Assignments</h1><p>{GRADER_TEXT}</p></main>"))
        elif path == "/grades":
            self._send(h, 200, _page("Grader grades", "<main><h1>Grades</h1><p>Lab 5: 9 out of 10. Lab 4: 10 out of 10. "
                                     "Programming Assignment 1: 87 out of 100, feedback released.</p></main>"))
        else:
            self._send(h, 404, _page("Not found", "<h1>404</h1>"))

    # ---- identity provider: two-step sign-in --------------------------------
    def _idp(self, h, method, path, query, form) -> None:
        if path != "/cas/login":
            self._send(h, 404, _page("Not found", "<h1>404</h1>"))
            return
        service = query.get("service") or form.get("service") or ""

        def issue(cookies=()):
            if not service.startswith(self.origin("grader") + "/"):
                self._send(h, 200, _page("Signed in", "<p>You are signed in.</p>"), headers=[("Set-Cookie", c) for c in cookies])
                return
            ticket = "ST-" + secrets.token_hex(8)
            self.tickets.add(ticket)
            self._redirect(h, service + ("&" if "?" in service else "?") + "ticket=" + ticket, cookies)

        if self._cookie(h, "idp_session") in self.idp_sessions:
            issue()
            return
        hidden = f"<input type='hidden' name='service' value='{html.escape(service, quote=True)}'>"
        if method == "POST" and form.get("code") is not None:
            pending = self._cookie(h, "idp_pending")
            if pending in self.idp_pending and form.get("code") == SSO_CODE:
                self.idp_pending.discard(pending)
                session = secrets.token_hex(12)
                self.idp_sessions.add(session)
                issue([f"idp_session={session}; Path=/; HttpOnly"])
            else:
                self._send(h, 200, _page("Verify", f"<h1>Verify your identity</h1><p role='alert'>Wrong code.</p><form method='post'>{hidden}"
                                         "<label>Verification code <input name='code' autocomplete='one-time-code'></label><button>Verify</button></form>"))
        elif method == "POST":
            if form.get("username") == SSO_USER and form.get("password") == SSO_PASSWORD:
                pending = secrets.token_hex(8)
                self.idp_pending.add(pending)
                self._send(h, 200, _page("Verify", f"<h1>Verify your identity</h1><form method='post'>{hidden}"
                                         "<label>Verification code <input name='code' autocomplete='one-time-code'></label><button>Verify</button></form>"),
                           headers=[("Set-Cookie", f"idp_pending={pending}; Path=/; HttpOnly")])
            else:
                self._send(h, 200, _page("University sign-in", f"<h1>University sign-in</h1><p role='alert'>Wrong username or password.</p>"
                                         f"<form method='post'>{hidden}<label>Username <input name='username'></label>"
                                         "<label>Password <input name='password' type='password'></label><button>Sign in</button></form>"))
        else:
            self._send(h, 200, _page("University sign-in", f"<h1>University sign-in</h1><form method='post'>{hidden}"
                                     "<label>Username <input name='username'></label>"
                                     "<label>Password <input name='password' type='password'></label><button>Sign in</button></form>"))

    # ---- forum: its own form, script-rendered class page ---------------------
    def _forum(self, h, method, path, query, form) -> None:
        signed_in = self._cookie(h, "forum_session") in self.forum_sessions
        if path == "/account/login":
            target = query.get("next") or form.get("next") or "/class/abc123"
            if method == "POST":
                if form.get("email") == FORUM_EMAIL and form.get("password") == FORUM_PASSWORD:
                    session = secrets.token_hex(12)
                    self.forum_sessions.add(session)
                    self._redirect(h, target, [f"forum_session={session}; Path=/; HttpOnly; Max-Age=86400"])
                    return
                note = "<p role='alert'>Wrong e-mail or password.</p>"
            else:
                note = ""
            self._send(h, 200, _page("Forum - log in", f"<h1>Log in to the forum</h1>{note}<form method='post' action='/account/login'>"
                                     f"<input type='hidden' name='next' value='{html.escape(target, quote=True)}'>"
                                     "<label>E-mail <input name='email' type='email'></label>"
                                     "<label>Password <input name='password' type='password'></label><button>Log in</button></form>"))
        elif path == "/to-course":
            # Stands in for http://site/ -> https://site/: another origin that redirects into the Basic site.
            self._redirect(h, self.origin("course") + "/comp/")
        elif path == "/api/feed":
            if method != "POST" or not signed_in:
                self._send(h, 401, json.dumps({"error": "sign in"}), "application/json")
            else:
                self._send(h, 200, json.dumps({"posts": FORUM_POSTS}), "application/json")
        elif path.startswith("/class/"):
            if not signed_in:
                self._redirect(h, "/account/login?next=" + quote(path, safe=""))
                return
            # Like a real single-page forum: an empty shell, filled by a script after an API call.
            self._send(h, 200, _page("Class forum", "<main><h1>Class forum</h1><div id='feed'>Loading…</div></main>",
                                     "fetch('/api/feed',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})"
                                     ".then(r=>r.json()).then(d=>{const f=document.getElementById('feed');f.textContent='';"
                                     "for(const p of d.posts){const e=document.createElement('article');e.textContent=p;f.append(e);}});"))
        else:
            self._send(h, 404, _page("Not found", "<h1>404</h1>"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic external course sites (made-up data only).")
    parser.parse_args()
    sites = FakeSites().start()
    print("Synthetic external sites (Ctrl-C stops them):")
    print(f"  course website (HTTP Basic)  {sites.url('course', '/old/')}   user {BASIC_USER!r}, password {BASIC_PASSWORD!r}")
    print(f"  grader (single sign-on)      {sites.url('grader', '/assignments')}   user {SSO_USER!r}, password {SSO_PASSWORD!r}, code {SSO_CODE}")
    print(f"  forum (its own form)         {sites.url('forum', '/class/abc123')}   e-mail {FORUM_EMAIL!r}, password {FORUM_PASSWORD!r}")
    print(f"  public page                  {sites.url('course', '/public/syllabus')}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        sites.stop()


if __name__ == "__main__":
    main()
