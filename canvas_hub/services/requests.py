"""Requests for connected agents to read sites Canvas Hub cannot reach.

Some course material lives outside Canvas (for example a separate course
website that needs its own login or a particular browser profile). Canvas Hub
does not open such sites itself. Instead the user - or the assistant, when
allowed - leaves a *view request*. An agent connected over MCP with the
``fulfil`` permission can list open requests, claim one, read the site with
its own access, and submit what it read. The result is stored as an ordinary,
searchable item marked as agent-provided (not Canvas-reported), with the
agent's name and the time it read the page.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .. import __version__
from ..db import Database
from ..urls import canonical_url
from ..util import InvalidInput, NotFound, now_iso, parse_iso

_AGENT_NAME = re.compile(r"^[\w .:@()/-]{1,80}$")
MAX_CONTENT = 200_000
CLAIM_TIMEOUT = timedelta(hours=2)


def _check_url(url: str) -> str:
    u = (url or "").strip()
    if not re.match(r"^https?://[^\s/]+", u, re.I) or len(u) > 2000:
        raise InvalidInput("the address must start with http:// or https://", code="invalid_url")
    return u


def _check_agent(name: str) -> str:
    n = (name or "").strip()
    if not _AGENT_NAME.match(n):
        raise InvalidInput("agent_name is required (letters, digits, spaces and .:@()/- only)", code="invalid_agent_name")
    return n


class RequestService:
    def __init__(self, db: Database, identity, importer):
        self.db = db
        self.identity = identity
        self.importer = importer

    def _get(self, account_id: int, request_id: int) -> dict:
        row = self.db.one("SELECT * FROM view_requests WHERE id=? AND account_id=?", (int(request_id), account_id))
        if not row:
            raise NotFound("request not found", code="request_not_found")
        return dict(row)

    def view(self, account_id: int, request_id: int) -> dict:
        r = self._get(account_id, request_id)
        if r["result_item_id"]:
            item = self.db.one("SELECT title, last_observed_at FROM items WHERE id=?", (r["result_item_id"],))
            r["result_title"] = item["title"] if item else None
        return r

    def create(self, account_id: int, *, url: str, title: str | None = None, note: str | None = None,
               course_id: str | None = None, requested_by: str = "user", external_source_id: int | None = None) -> dict:
        u = _check_url(url)
        if requested_by not in ("user", "assistant"):
            raise InvalidInput("requested_by must be user or assistant")
        if len(note or "") > 2000 or len(title or "") > 300:
            raise InvalidInput("title or note too long")
        dup = self.db.one("SELECT id FROM view_requests WHERE account_id=? AND url=? AND external_source_id IS ? AND status IN ('open','claimed')",
                          (account_id, u, external_source_id))
        if dup:
            return self.view(account_id, dup["id"])
        if self.db.one("SELECT COUNT(*) n FROM view_requests WHERE account_id=? AND status IN ('open','claimed')",
                       (account_id,))["n"] >= 200:
            raise InvalidInput("too many open requests; cancel some first", code="too_many_requests")
        now = now_iso()
        with self.db.tx() as c:
            rid = c.execute(
                """INSERT INTO view_requests(account_id, url, title, note, course_canvas_id, requested_by, created_at, updated_at, external_source_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (account_id, u, (title or "").strip() or None, (note or "").strip() or None, course_id, requested_by, now, now, external_source_id)).lastrowid
        return self.view(account_id, rid)

    def list(self, account_id: int, status: str | None = None) -> list[dict]:
        sql, params = "SELECT id FROM view_requests WHERE account_id=?", [account_id]
        if status:
            if status not in ("open", "claimed", "done", "declined", "cancelled", "active"):
                raise InvalidInput("unknown status")
            if status == "active":
                sql += " AND status IN ('open','claimed')"
            else:
                sql += " AND status=?"
                params.append(status)
        return [self.view(account_id, r["id"]) for r in self.db.query(sql + " ORDER BY id DESC LIMIT 200", params)]

    def cancel(self, account_id: int, request_id: int) -> dict:
        r = self._get(account_id, request_id)
        if r["status"] in ("done", "declined", "cancelled"):
            return r
        self._set(r["id"], status="cancelled")
        return self.view(account_id, request_id)

    def _set(self, rid: int, **fields) -> None:
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.db.tx() as c:
            c.execute(f"UPDATE view_requests SET {sets} WHERE id=?", (*fields.values(), rid))

    # ---- agent side -------------------------------------------------------------------
    def claim(self, account_id: int, request_id: int, agent_name: str) -> dict:
        agent = _check_agent(agent_name)
        r = self._get(account_id, request_id)
        stale = False
        if r["status"] == "claimed" and r["claimed_by"] != agent:
            claimed = parse_iso(r["claimed_at"])
            stale = claimed is not None and datetime.now(timezone.utc) - claimed > CLAIM_TIMEOUT
            if not stale:
                raise InvalidInput(f"already claimed by {r['claimed_by']}", code="request_claimed")
        if r["status"] not in ("open", "claimed"):
            raise InvalidInput(f"request is {r['status']}", code="request_closed")
        self._set(r["id"], status="claimed", claimed_by=agent, claimed_at=now_iso())
        return self.view(account_id, request_id)

    def decline(self, account_id: int, request_id: int, agent_name: str, reason: str) -> dict:
        agent = _check_agent(agent_name)
        r = self._get(account_id, request_id)
        if r["status"] not in ("open", "claimed"):
            raise InvalidInput(f"request is {r['status']}", code="request_closed")
        self._set(r["id"], status="declined", claimed_by=agent, result_note=(reason or "")[:2000] or "declined",
                  completed_at=now_iso())
        return self.view(account_id, request_id)

    def submit(self, account_id: int, request_id: int, agent_name: str, *, content: str, title: str | None = None,
               source_url: str | None = None, read_at: str | None = None) -> dict:
        """Store what an agent read. Content is plain text/markdown written by
        the agent; it is stored as untrusted, agent-provided data."""
        agent = _check_agent(agent_name)
        r = self._get(account_id, request_id)
        if r["status"] not in ("open", "claimed"):
            raise InvalidInput(f"request is {r['status']}", code="request_closed")
        if r["status"] == "claimed" and r["claimed_by"] != agent:
            raise InvalidInput(f"claimed by {r['claimed_by']}", code="request_claimed")
        if r.get("external_source_id"):
            raise InvalidInput("Use submit_source_analysis for classified source requests")
        text = content or ""
        if not text.strip():
            raise InvalidInput("content is empty")
        if len(text) > MAX_CONTENT:
            raise InvalidInput(f"content too long (max {MAX_CONTENT} characters); split it into several requests")
        src = _check_url(source_url) if source_url else r["url"]
        when = read_at if read_at and parse_iso(read_at) else now_iso()
        acct = self.identity.get_account(account_id)
        url = canonical_url(src)
        pkg = {
            "format": "canvas-hub.observations", "format_version": 1,
            "package_id": f"view-request-{r['id']}-{int(datetime.now(timezone.utc).timestamp())}",
            "generated_at": now_iso(),
            "collector": {"name": f"agent:{agent}"[:100], "version": __version__, "method": "connected_agent_view_request"},
            "identity": {"origin": acct["origin"], "canvas_user_id": acct["canvas_user_id"]},
            "run": {"mode": "import", "scope": {"view_request": r["id"]}},
            "items": [{
                "type": "external_link", "course_id": r["course_canvas_id"], "url": url,
                "title": (title or r["title"] or url)[:300], "observed_at": when, "method": f"agent:{agent}", "depth": "body",
                "fields": {"title": (title or r["title"] or url)[:300], "url": url, "content": text,
                           "provided_by": f"connected agent: {agent}", "read_at": when, "view_request_id": r["id"],
                           "launched": False},
            }],
        }
        res = self.importer.import_package(pkg, expected_account_id=account_id)
        item = self.db.one("SELECT id FROM items WHERE account_id=? AND item_type='external_link' AND source_key=?",
                           (account_id, url))
        self._set(r["id"], status="done", claimed_by=agent, result_item_id=item["id"] if item else None,
                  completed_at=now_iso(), result_note=f"{len(text)} characters stored")
        out = self.view(account_id, request_id)
        out["import"] = res["stats"]
        return out
