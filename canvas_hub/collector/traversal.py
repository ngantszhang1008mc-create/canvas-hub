"""Collection traversal.

A collection job is a persistent queue of tasks (job_tasks). Each task reads
one Canvas surface, builds an observation package and imports it in one
transaction; only then is the task marked done. Restarting re-runs at most
the task that was in flight, and the import is idempotent.

Traversal policy (brief sections 4 and 5):

* Discovery starts from Courses -> All Courses (rendered DOM).
* A course's categories are read only when its own navigation exposes them
  (course tabs), or when Home/syllabus/module/body content links to them.
  Hidden index routes are never guessed.
* Module items whose requirement is "must view" (and not yet completed) are
  inventoried but their content is not opened unless the account scope has
  ``allow_view_completion`` - the job records an explicit gap instead.
* Only GET requests are made. No quiz attempts are started, nothing is
  submitted/posted/sent, invitations are not accepted, favorites unchanged.
  Inbox reads use auto_mark_as_read=false.
* quick mode re-reads indexes (lists, dates, submission states, grades) but
  does not claim unchanged bodies: bodies are re-read only when the index
  shows a newer update time; coverage says so.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .. import __version__, dates
from ..htmltools import links as html_links
from ..htmltools import sanitize
from ..observations import new_package
from ..urls import canonical_url, classify_link
from ..util import IdentityMismatch, now_iso
from .client import (AccessDenied, AuthRequired, Cancelled, CanvasClient, CollectorError, NotFoundError,
                     RateLimited, TransientError)
from .discovery import ALL_COURSES_JS, parse_all_courses, published_flag

log = logging.getLogger("canvas_hub.collector")

_PKG_ID_OK = re.compile(r"[^0-9A-Za-z\-_.~:]")
_MAX_LINK_DEPTH = 6

# Course navigation tab id -> categories it exposes.
_TAB_CATEGORIES = {
    "home": ["home"], "announcements": ["announcements"], "assignments": ["assignments"],
    "discussions": ["discussions"], "grades": ["grades"], "pages": ["pages"], "files": ["files"],
    "syllabus": ["home"], "quizzes": ["quizzes"], "modules": ["modules"],
}
# A link to /courses/:id/<segment> is an explicit pointer to that index.
_NAV_SEGMENT_CATEGORY = {
    "announcements": "announcements", "assignments": "assignments", "discussion_topics": "discussions",
    "grades": "grades", "pages": "pages", "wiki": "pages", "files": "files", "quizzes": "quizzes", "modules": "modules",
}


def _d(v):
    return dates.from_api(v)


def _html(v):
    return sanitize(v) if v else ("" if v == "" else None)


def _pick(src: dict, spec: dict) -> dict:
    """Build fields from a Canvas object. spec maps field -> source key or
    (source key, transform). A key absent from the source is *not collected*
    and is omitted - it is never recorded as a verified None."""
    out = {}
    for field, how in spec.items():
        key, fn = (how, None) if isinstance(how, str) else how
        if key in src:
            v = src[key]
            out[field] = fn(v) if fn else v
    return out


def _sid(v):
    return str(v) if v not in (None, "") else None


def _pid(s: str) -> str:
    s = _PKG_ID_OK.sub("_", s)
    return s if len(s) <= 180 else s[:120] + "_" + str(abs(hash(s)))


class Package:
    """Builds one observation package for a task."""

    def __init__(self, run: "CollectionRun", task_key: str):
        self.run = run
        self.task_key = task_key
        self.data = new_package(
            package_id=_pid(f"job{run.job_id}-{task_key}"), generated_at=now_iso(), identity=run.identity_payload,
            mode=run.mode, scope=run.scope, collector_version=__version__,
        )
        self._n = 0

    def item(self, type_: str, *, course_id=None, canvas_id=None, source_key=None, url=None, title=None,
             fields: dict | None = None, depth="index", observed_at=None, source_updated_at=None,
             links: list | None = None, resources: list | None = None, replace_links: list | None = None,
             method="canvas_api_session") -> dict:
        self._n += 1
        key = canvas_id or source_key or url
        obs = {
            "type": type_, "course_id": str(course_id) if course_id else None,
            "canvas_id": str(canvas_id) if canvas_id else None, "source_key": source_key,
            "url": url, "title": title, "observed_at": observed_at or now_iso(),
            "observation_id": _pid(f"job{self.run.job_id}:{self.task_key}:{type_}:{key}:{self._n}"),
            "method": method, "depth": depth, "source_updated_at": source_updated_at,
            "fields": {k: v for k, v in (fields or {}).items()},
            "links": links or [], "resources": resources or [], "replace_links": replace_links or [],
        }
        self.data["items"].append(obs)
        return obs

    def coverage(self, category: str, status: str, *, course_id=None, index="not_checked", body="not_checked",
                 attachments="not_checked", expected=None, collected=None, reason=None, detail=None,
                 absence_types: list | None = None) -> None:
        cov = {
            "course_id": str(course_id) if course_id else None, "category": category, "status": status,
            "index_depth": index, "body_depth": body, "attachment_depth": attachments,
            "expected_count": expected, "collected_count": collected, "reason": reason,
            "detail": detail or {}, "observed_at": now_iso(),
        }
        if absence_types:
            cov["absence_inference"] = {"item_types": absence_types}
        self.data["coverage"].append(cov)

    def presence(self, ref: dict, result: str, status: int | None) -> None:
        self.data["presence_checks"].append({"item": ref, "result": result, "http_status": status, "checked_at": now_iso()})

    def commit(self) -> dict | None:
        if not (self.data["items"] or self.data["coverage"] or self.data["presence_checks"]):
            return None
        return self.run.hub.importer.import_package(self.data, job_id=self.run.job_id,
                                                    expected_account_id=self.run.account["id"])


def ref(type_: str, course_id=None, canvas_id=None, source_key=None, url=None, title=None) -> dict:
    return {"type": type_, "course_id": str(course_id) if course_id else None,
            "canvas_id": str(canvas_id) if canvas_id else None, "source_key": source_key, "url": url,
            "title": (title or None) and str(title)[:300]}


class CollectionRun:
    def __init__(self, hub, client: CanvasClient, job_id: int):
        self.hub = hub
        self.client = client
        self.job_id = job_id
        job = hub.jobs.get(job_id)
        self.job = job
        self.mode = job["mode"]
        self.thorough = self.mode in ("baseline", "thorough")
        self.scope = job["scope"]
        self.cats = set(self.scope["categories"])
        self.account = hub.identity.get_account(job["account_id"])
        self.origin = self.account["origin"]
        self.identity_payload = {k: self.account.get(k) for k in (
            "origin", "canvas_user_id", "display_name", "login_id", "primary_email", "time_zone", "time_zone_source", "locale")}
        st = job["stats"]
        self.must_view: dict[str, dict] = dict(st.get("must_view") or {})
        self.scoped_courses: list[str] = list(st.get("scoped_courses") or [])
        self.staff: dict[str, list[str]] = dict(st.get("staff") or {})
        self._stop_cache = (0.0, False)

    # ---- control -------------------------------------------------------------
    def should_stop(self) -> bool:
        t, v = self._stop_cache
        if time.monotonic() - t < 1.0:
            return v
        row = self.hub.db.one("SELECT state FROM jobs WHERE id=?", (self.job_id,))
        v = row is None or row["state"] in ("cancel_requested", "paused", "cancelled")
        self._stop_cache = (time.monotonic(), v)
        return v

    def enqueue(self, key: str, kind: str, *, course_id=None, category=None, payload=None) -> bool:
        return self.hub.jobs.add_task(self.job_id, key, kind, course_id=str(course_id) if course_id else None,
                                      category=category, payload=payload)

    def enqueue_with_referrer(self, key: str, kind: str, referrer: dict | None, *, course_id=None, category=None,
                              payload=None) -> None:
        """Enqueue a fetch task, remembering who linked to the target so the
        relationship can be recorded once the target's identity is known."""
        payload = dict(payload or {})
        if referrer:
            payload["referrers"] = [referrer]
        if self.enqueue(key, kind, course_id=course_id, category=category, payload=payload) or not referrer:
            return
        with self.hub.db.tx() as c:
            row = c.execute("SELECT id, state, payload_json FROM job_tasks WHERE job_id=? AND task_key=?",
                            (self.job_id, key)).fetchone()
            p = json.loads(row["payload_json"])
            if row["state"] == "done" and p.get("resolved"):
                # Target already collected in this job: record the link now.
                pkg = Package(self, f"{key}-ref-{len(p.get('referrers', []))}")
                pkg.item(**self._link_only(referrer, p["resolved"]))
                pkg.commit()
            refs = p.setdefault("referrers", [])
            if referrer not in refs:
                refs.append(referrer)
                c.execute("UPDATE job_tasks SET payload_json=? WHERE id=?", (json.dumps(p), row["id"]))

    def _link_only(self, referrer: dict, target: dict) -> dict:
        frm = referrer["from"]
        return dict(type_=frm["type"], course_id=frm.get("course_id"), canvas_id=frm.get("canvas_id"),
                    source_key=frm.get("source_key"), url=frm.get("url"), fields={},
                    links=[{"relation": referrer["relation"], "label": referrer.get("label") or "", "to": target,
                            "position": referrer.get("position"), "attrs": referrer.get("attrs") or {}}])

    def resolve_referrers(self, task: dict, target: dict, pkg: Package) -> None:
        for r in task["payload"].get("referrers", []):
            pkg.item(**self._link_only(r, target))
        with self.hub.db.tx() as c:
            row = c.execute("SELECT payload_json FROM job_tasks WHERE id=?", (task["id"],)).fetchone()
            p = json.loads(row["payload_json"])
            p["resolved"] = target
            c.execute("UPDATE job_tasks SET payload_json=? WHERE id=?", (json.dumps(p), task["id"]))

    def save_state(self) -> None:
        self.hub.jobs.update_stats(self.job_id, must_view=self.must_view, scoped_courses=self.scoped_courses,
                                   staff=self.staff)

    def observed_in_job(self, type_: str, canvas_id) -> bool:
        row = self.hub.db.one(
            """SELECT 1 FROM items i JOIN item_observations o ON o.item_id=i.id
               WHERE i.account_id=? AND i.item_type=? AND i.source_key=? AND o.job_id=? AND o.fields_json!='[]' LIMIT 1""",
            (self.account["id"], type_, str(canvas_id), self.job_id))
        return row is not None

    def stored_field(self, type_: str, key: str, field: str):
        row = self.hub.db.one(
            """SELECT f.value_json FROM items i JOIN item_fields f ON f.item_id=i.id
               WHERE i.account_id=? AND i.item_type=? AND i.source_key=? AND f.field=?""",
            (self.account["id"], type_, str(key), field))
        return json.loads(row["value_json"]) if row else None

    def has_resource(self, type_: str, key: str) -> bool:
        return self.hub.db.one(
            """SELECT 1 FROM items i JOIN item_resources r ON r.item_id=i.id
               WHERE i.account_id=? AND i.item_type=? AND i.source_key=?""",
            (self.account["id"], type_, str(key))) is not None

    def view_blocked(self, key: str) -> dict | None:
        """Opening this target would satisfy an uncompleted 'must view'
        module requirement."""
        mv = self.must_view.get(key)
        if mv and not mv.get("completed") and not self.scope.get("allow_view_completion"):
            return mv
        return None

    # ---- links -----------------------------------------------------------------
    def process_body(self, html: str | None, course_id, from_ref: dict, depth: int = 0) -> list[dict]:
        """Classify links in Canvas HTML. Returns link observations for the
        source item and enqueues fetches for linked Canvas content."""
        out: list[dict] = []
        if not html:
            return out
        seen = set()
        for href, text in html_links(html):
            ln = classify_link(href, self.origin)
            if ln is None:
                continue
            label = (text or "")[:200]
            sig = (ln.kind, ln.url, label)
            if sig in seen:
                continue
            seen.add(sig)
            course = ln.course_id or (str(course_id) if course_id else None)
            in_scope = course is None or course in self.scoped_courses
            nd = depth + 1
            referrer = {"from": from_ref, "relation": "body_link", "label": label}
            if ln.kind == "external":
                out.append({"relation": "body_link", "label": label,
                            "to": ref("external_link", course_id, url=ln.url, title=label or ln.url)})
            elif ln.kind == "external_tool":
                out.append({"relation": "body_link", "label": label,
                            "to": ref("external_tool", course, source_key=ln.url, url=ln.url, title=label)})
            elif ln.kind == "page":
                if in_scope and nd <= _MAX_LINK_DEPTH:
                    self.enqueue_with_referrer(f"fetch:page:{course}:{ln.ref}", "fetch_page", referrer,
                                               course_id=course, category="linked_content",
                                               payload={"slug": ln.ref, "depth": nd})
            elif ln.kind in ("assignment", "quiz", "discussion", "announcement"):
                out.append({"relation": "body_link", "label": label, "to": ref(ln.kind, course, ln.ref, url=ln.url)})
                if in_scope and nd <= _MAX_LINK_DEPTH:
                    self.enqueue(f"fetch:{ln.kind}:{course}:{ln.ref}", f"fetch_{ln.kind}", course_id=course,
                                 category="linked_content", payload={"id": ln.ref, "depth": nd})
            elif ln.kind == "file":
                out.append({"relation": "body_link", "label": label, "to": ref("file", course, ln.ref, url=ln.url)})
                if in_scope:
                    self.enqueue(f"fetch:file:{ln.ref}", "fetch_file", course_id=course, category="linked_content",
                                 payload={"id": ln.ref})
            elif ln.kind == "folder":
                if in_scope and course:
                    self.enqueue(f"fetch:folder:{course}:{ln.ref}", "fetch_folder", course_id=course,
                                 category="linked_content", payload={"path": ln.ref})
            elif ln.kind == "module_item":
                out.append({"relation": "body_link", "label": label, "to": ref("module_item", course, ln.ref, url=ln.url)})
            elif ln.kind == "module":
                out.append({"relation": "body_link", "label": label, "to": ref("module", course, ln.ref, url=ln.url)})
            elif ln.kind == "course_nav" and in_scope and course:
                cat = _NAV_SEGMENT_CATEGORY.get(ln.ref or "")
                if cat and cat in self.cats:
                    self.enqueue(f"{cat}:{course}", cat, course_id=course, category=cat,
                                 payload={"reason": f"linked from content ({label or ln.url})"})
        return out

    def external_items(self, pkg: Package, links: list[dict]) -> None:
        """Metadata-only items for external links/tools: never launched."""
        for ln in links:
            to = ln["to"]
            if to["type"] in ("external_link", "external_tool"):
                pkg.item(to["type"], course_id=to.get("course_id"), source_key=to.get("source_key"), url=to["url"],
                         title=to.get("title"), fields={"url": to["url"], "title": to.get("title") or to["url"],
                                                        "launched": False})

    # ---- main loop --------------------------------------------------------------
    HANDLERS = {
        "identity": "t_identity", "courses": "t_courses", "course": "t_course", "home": "t_home",
        "modules": "t_modules", "announcements": "t_announcements", "assignments": "t_assignments",
        "quizzes": "t_quizzes", "discussions": "t_discussions", "pages": "t_pages", "files": "t_files",
        "grades": "t_grades", "calendar": "t_calendar", "groups": "t_groups", "inbox": "t_inbox",
        "fetch_page": "t_fetch_page", "fetch_assignment": "t_fetch_assignment", "fetch_quiz": "t_fetch_quiz",
        "fetch_discussion": "t_fetch_discussion", "fetch_announcement": "t_fetch_discussion",
        "fetch_file": "t_fetch_file", "fetch_folder": "t_fetch_folder", "download": "t_download",
    }

    def run(self) -> str:
        jobs = self.hub.jobs
        if not jobs.tasks(self.job_id, limit=1):
            self.enqueue("identity", "identity")
            self.enqueue("courses", "courses", category="courses")
        jobs.set_state(self.job_id, "running", message="Collecting from Canvas")
        while True:
            if self.should_stop():
                return self._stopped()
            task = jobs.next_task(self.job_id)
            if task is None:
                break
            jobs.update_stats(self.job_id, current_task=task["task_key"])
            try:
                getattr(self, self.HANDLERS[task["kind"]])(task)
                jobs.finish_task(task["id"], "done")
            except AuthRequired as e:
                jobs.defer_task(task["id"], f"sign-in required: {e}")
                jobs.set_state(self.job_id, "needs_login",
                               message="Canvas sign-in expired or is required. Sign in again; the job resumes from its checkpoint.",
                               error=str(e))
                return "needs_login"
            except Cancelled:
                jobs.defer_task(task["id"], "stopped before completion")
                return self._stopped()
            except IdentityMismatch as e:
                jobs.finish_task(task["id"], "failed", str(e))
                jobs.set_state(self.job_id, "failed", error=str(e),
                               message="The signed-in Canvas account is not this job's account; nothing was mixed.")
                return "failed"
            except (AccessDenied, NotFoundError) as e:
                jobs.finish_task(task["id"], "blocked", str(e))
                self._task_gap(task, "blocked", str(e))
            except (TransientError, RateLimited, CollectorError) as e:
                jobs.finish_task(task["id"], "failed", str(e))
                self._task_gap(task, "partial", f"{type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001 - one task must not sink the job
                log.exception("task %s failed", task["task_key"])
                jobs.finish_task(task["id"], "failed", f"{type(e).__name__}: {e}")
                self._task_gap(task, "partial", f"internal error: {type(e).__name__}: {e}")
            jobs.heartbeat(self.job_id)
        return self._finish()

    def _stopped(self) -> str:
        row = self.hub.db.one("SELECT state FROM jobs WHERE id=?", (self.job_id,))
        if row and row["state"] == "cancel_requested":
            self.hub.jobs.set_state(self.job_id, "cancelled", message="Cancelled. Observations collected so far were kept.")
            return "cancelled"
        return row["state"] if row else "cancelled"

    def _linked_coverage(self) -> None:
        """Record linked-content coverage for courses where every followed link
        was read (gaps were already recorded per task)."""
        rows = self.hub.db.query(
            """SELECT course_canvas_id, COUNT(*) n, SUM(state='done') ok FROM job_tasks
               WHERE job_id=? AND category='linked_content' AND course_canvas_id IS NOT NULL GROUP BY course_canvas_id""",
            (self.job_id,))
        pkg = Package(self, "linked-coverage")
        for r in rows:
            if r["n"] == r["ok"] and not self.hub.db.one(
                    "SELECT 1 FROM coverage WHERE job_id=? AND course_canvas_id=? AND category='linked_content'",
                    (self.job_id, r["course_canvas_id"])):
                pkg.coverage("linked_content", "complete", course_id=r["course_canvas_id"], index="complete",
                             body="complete", attachments="complete" if self.scope.get("download_files") else "not_checked",
                             collected=r["n"], detail={"note": "Every destination linked from Home, syllabus, modules, "
                                                               "pages or announcements was followed."})
        pkg.commit()

    def _finish(self) -> str:
        self._linked_coverage()
        gaps = self.hub.db.one("SELECT COUNT(*) n FROM job_tasks WHERE job_id=? AND state IN ('failed','blocked','skipped')",
                               (self.job_id,))["n"]
        partial = self.hub.db.one("SELECT COUNT(*) n FROM coverage WHERE job_id=? AND status IN ('partial','blocked')",
                                  (self.job_id,))["n"]
        state = "completed_with_gaps" if gaps or partial else "completed"
        msg = ("Finished. Some content could not be read; see Coverage for the specific gaps."
               if state == "completed_with_gaps" else "Finished; every in-scope surface was read.")
        self.hub.jobs.update_stats(self.job_id, current_task=None)
        self.hub.jobs.set_state(self.job_id, state, message=msg)
        return state

    def _task_gap(self, task: dict, status: str, reason: str) -> None:
        cat = task.get("category")
        if not cat:
            return
        try:
            pkg = Package(self, f"{task['task_key']}-gap")
            pkg.coverage(cat, status, course_id=task.get("course_canvas_id"), reason=reason[:500],
                         index="blocked" if status == "blocked" else "partial",
                         detail={"task": task["task_key"]})
            pkg.commit()
        except Exception:  # noqa: BLE001
            log.exception("could not record gap")

    # ---- identity & discovery ------------------------------------------------------
    def t_identity(self, task):
        user = self.client.api_get("/api/v1/users/self")
        if str(user.get("id")) != str(self.account["canvas_user_id"]):
            raise IdentityMismatch(
                f"browser is signed in as Canvas user {user.get('id')}, but this job belongs to user {self.account['canvas_user_id']}")
        ident = {"canvas_user_id": str(user["id"]), "display_name": user.get("name")}
        try:
            prof = self.client.api_get("/api/v1/users/self/profile")
            if prof.get("time_zone"):
                ident.update(time_zone=prof["time_zone"], time_zone_source="canvas_api:users/self/profile")
            ident.update(login_id=prof.get("login_id"), primary_email=prof.get("primary_email"), locale=prof.get("locale"))
        except (AccessDenied, NotFoundError):
            pass
        self.account = self.hub.identity.upsert_verified_account(self.account["instance_id"], ident)
        self.identity_payload.update({k: self.account.get(k) for k in self.identity_payload})

    def _course_selected(self, row: dict) -> bool:
        sel = self.scope["courses"]
        if isinstance(sel, list):
            return row.get("course_id") in sel
        if sel == "all":
            return True
        return row.get("section") in ("current", "unknown")

    def t_courses(self, task):
        pkg = Package(self, task["task_key"])
        problems: list[str] = []
        rows: list[dict] = []
        dom_ok = False
        try:
            _url, snap = self.client.page_snapshot("/courses", ALL_COURSES_JS, settle_selector="table, #content")
            parsed = parse_all_courses(snap or {})
            rows = parsed["rows"]
            problems += parsed["problems"]
            dom_ok = parsed["tables_seen"] > 0
        except AuthRequired:
            raise
        except Exception as e:  # noqa: BLE001
            problems.append(f"All Courses page could not be read: {e}")
        api: dict[str, dict] = {}
        api_complete = False
        try:
            res = self.client.api_list("/api/v1/courses", [("include[]", "term"), ("state[]", "available"),
                                                           ("state[]", "completed"), ("state[]", "unpublished")])
            api = {str(c["id"]): c for c in res.items if isinstance(c, dict) and c.get("id")}
            api_complete = res.complete
        except (AccessDenied, NotFoundError, CollectorError) as e:
            problems.append(f"enrollment API list unavailable: {e}")
        t = now_iso()
        seen_ids = set()
        if not dom_ok:
            # Fall back to the API list, clearly marked partial.
            for cid, c in api.items():
                rows.append({"course_id": cid, "name": c.get("name"), "nickname": None,
                             "term": (c.get("term") or {}).get("name"), "enrolled_as": None, "published_text": None,
                             "section": "unknown", "has_link": True, "href": f"/courses/{cid}", "invitation": False})
        selected: list[str] = []
        for r in rows:
            cid = r.get("course_id")
            a = api.get(cid or "", {})
            accessible = bool(r.get("has_link")) and not r.get("invitation")
            fields = {
                "name": r.get("name"), "title": r.get("nickname") or r.get("name"), "nickname": r.get("nickname"),
                "term": r.get("term") or (a.get("term") or {}).get("name"), "enrollment_role": r.get("enrolled_as"),
                "listing_section": r.get("section"), "published": published_flag(r.get("published_text")),
                "published_text": r.get("published_text"),
                "access": "accessible" if accessible else ("invitation_pending" if r.get("invitation") else "not_accessible"),
            }
            if a:
                fields.update({
                    "course_code": a.get("course_code"), "workflow_state": a.get("workflow_state"),
                    "start_at": _d(a.get("start_at")), "end_at": _d(a.get("end_at")),
                    "course_time_zone": a.get("time_zone"), "default_view": a.get("default_view"),
                    "enrollments": [{"type": e.get("type"), "role": e.get("role"), "state": e.get("enrollment_state")}
                                    for e in a.get("enrollments") or []],
                })
            if cid:
                seen_ids.add(cid)
                pkg.item("course", course_id=cid, canvas_id=cid, url=f"{self.origin}/courses/{cid}",
                         title=fields["title"], fields=fields, observed_at=t, method="dom:all_courses+canvas_api_session")
            else:
                pkg.item("course", source_key=r["unlinked_key"], title=fields["title"], fields=fields, observed_at=t,
                         method="dom:all_courses")
            if cid and accessible and self._course_selected(r):
                selected.append(cid)
            elif cid and self._course_selected(r) and not accessible:
                pkg.coverage("home", "blocked", course_id=cid, index="blocked", body="blocked", attachments="blocked",
                             reason=f"Listed in All Courses but not accessible ({fields['access']}); not opened")
        api_only = sorted(set(api) - seen_ids) if dom_ok else []
        status = "complete" if dom_ok and not problems else "partial"
        pkg.coverage("courses", status, index="complete" if dom_ok else "partial", body="not_applicable",
                     attachments="not_applicable", collected=len(rows),
                     reason="; ".join(problems) or None,
                     detail={"source": "Courses -> All Courses" if dom_ok else "enrollment API fallback",
                             "by_section": {s: sum(1 for r in rows if r.get("section") == s) for s in
                                            {r.get("section") for r in rows}},
                             "api_courses": len(api), "api_list_complete": api_complete,
                             "in_api_not_on_all_courses": api_only, "selected_for_traversal": selected,
                             "scope_courses": self.scope["courses"]},
                     absence_types=["course"] if dom_ok and not problems else None)
        pkg.commit()
        self.scoped_courses = selected
        self.save_state()
        for cid in selected:
            self.enqueue(f"course:{cid}", "course", course_id=cid, category="home")
        if "calendar" in self.cats:
            self.enqueue("calendar", "calendar", category="calendar")
        if "groups" in self.cats:
            self.enqueue("groups", "groups", category="groups")
        if "inbox" in self.cats:
            self.enqueue("inbox", "inbox", category="inbox")

    # ---- per course ------------------------------------------------------------------
    def t_course(self, task):
        cid = task["course_canvas_id"]
        t = now_iso()
        c = self.client.api_get(f"/api/v1/courses/{cid}", [("include[]", "syllabus_body"), ("include[]", "term"),
                                                          ("include[]", "teachers")])
        tabs_res = self.client.api_list(f"/api/v1/courses/{cid}/tabs")
        tabs = [x for x in tabs_res.items if isinstance(x, dict) and not x.get("hidden")]
        nav = [{"id": x.get("id"), "label": x.get("label"), "type": x.get("type"), "position": x.get("position")}
               for x in tabs]
        tab_ids = {x.get("id") for x in tabs}
        exposed: set[str] = set()
        for tid in tab_ids:
            exposed.update(_TAB_CATEGORIES.get(tid, []))
        default_view = c.get("default_view")
        if default_view == "modules":
            exposed.add("modules")
        teachers = [str(x.get("id")) for x in c.get("teachers") or [] if x.get("id")]
        self.staff[cid] = sorted(set(self.staff.get(cid, [])) | set(teachers))
        self.save_state()

        pkg = Package(self, task["task_key"])
        course_fields = {
            "name": c.get("name"), "course_code": c.get("course_code"), "default_view": default_view,
            "navigation": nav, "term": (c.get("term") or {}).get("name"), "workflow_state": c.get("workflow_state"),
            "start_at": _d(c.get("start_at")), "end_at": _d(c.get("end_at")), "course_time_zone": c.get("time_zone"),
            "apply_assignment_group_weights": c.get("apply_assignment_group_weights"),
            "hide_final_grades": c.get("hide_final_grades"),
            "teachers": [{"name": x.get("display_name")} for x in c.get("teachers") or []],
        }
        pkg.item("course", course_id=cid, canvas_id=cid, url=f"{self.origin}/courses/{cid}", title=None,
                 fields=course_fields, observed_at=t, depth="detail")
        syllabus = c.get("syllabus_body")
        syllabus_visible = "syllabus" in tab_ids or default_view == "syllabus"
        if "home" in self.cats and (syllabus or syllabus_visible):
            sref = ref("syllabus", cid, source_key=f"syllabus:{cid}")
            links = self.process_body(syllabus, cid, sref)
            self.external_items(pkg, links)
            pkg.item("syllabus", course_id=cid, source_key=f"syllabus:{cid}",
                     url=f"{self.origin}/courses/{cid}/assignments/syllabus",
                     title=f"Syllabus: {c.get('name')}", fields={"title": f"Syllabus: {c.get('name')}",
                                                                 "body": _html(syllabus) if syllabus is not None else None},
                     observed_at=t, depth="body", links=links, replace_links=["body_link"])
        # External tools in navigation: metadata only, never launched.
        ext = [x for x in tabs if x.get("type") == "external"]
        if "external_tools" in self.cats:
            for x in ext:
                url = x.get("full_url") or (self.origin + x["html_url"] if x.get("html_url") else None)
                if not url:
                    continue
                pkg.item("external_tool", course_id=cid, source_key=canonical_url(url), url=canonical_url(url),
                         title=x.get("label"), fields={"title": x.get("label"), "url": canonical_url(url),
                                                       "placement": "course_navigation", "launched": False},
                         observed_at=t)
            pkg.coverage("external_tools", "partial" if ext else "complete", course_id=cid,
                         index="complete", body="not_applicable" if not ext else "blocked", attachments="not_applicable",
                         collected=len(ext),
                         reason=("Linked tools are recorded as metadata only; their content needs a separate launch/login "
                                 "and was not opened") if ext else None)
        pkg.coverage("home", "complete", course_id=cid, index="complete", body="complete" if syllabus is not None else "not_applicable",
                     attachments="not_applicable", detail={"default_view": default_view, "navigation": [n["id"] for n in nav]})
        # Categories the navigation does not expose, or that this run's scope excludes.
        for cat in ("announcements", "assignments", "quizzes", "discussions", "pages", "files", "grades", "modules"):
            if cat not in self.cats:
                pkg.coverage(cat, "not_checked", course_id=cid, reason="Not in this run's scope")
                continue
            exposed_here = cat in exposed or (cat == "assignments" and ({"grades", "syllabus"} & tab_ids))
            if not exposed_here:
                pkg.coverage(cat, "not_exposed", course_id=cid, index="not_applicable", body="not_applicable",
                             attachments="not_applicable",
                             reason="Course navigation does not show this section; content linked from Home, "
                                    "syllabus, modules or pages is still collected")
        pkg.commit()

        order = ["home", "modules", "announcements", "assignments", "quizzes", "discussions", "pages", "files", "grades"]
        for cat in order:
            if cat not in self.cats:
                continue
            if cat == "home":
                if default_view == "wiki":
                    self.enqueue(f"home:{cid}", "home", course_id=cid, category="home", payload={"default_view": default_view})
                continue
            if cat in exposed or (cat == "assignments" and ({"grades", "syllabus"} & tab_ids)):
                self.enqueue(f"{cat}:{cid}", cat, course_id=cid, category=cat)

    def t_home(self, task):
        cid = task["course_canvas_id"]
        t = now_iso()
        fp = self.client.api_get(f"/api/v1/courses/{cid}/front_page")
        pkg = Package(self, task["task_key"])
        self._page_item(pkg, cid, fp, t, depth=0)
        pkg.item("course", course_id=cid, canvas_id=cid, fields={},
                 links=[{"relation": "home_page", "label": "", "to": ref("page", cid, fp.get("page_id"), title=fp.get("title"))}],
                 replace_links=["home_page"], observed_at=t)
        pkg.coverage("home", "complete", course_id=cid, index="complete", body="complete", attachments="not_applicable",
                     detail={"default_view": "wiki", "front_page": fp.get("title"),
                             "note": "Full page HTML read, including collapsed/expandable sections."})
        pkg.commit()

    def _page_item(self, pkg: Package, cid, p: dict, t: str, depth: int) -> dict:
        page_id = p.get("page_id")
        slug = p.get("url")
        pref = ref("page", cid, page_id, title=p.get("title"))
        body = p.get("body")
        links = self.process_body(body, cid, pref, depth)
        self.external_items(pkg, links)
        fields = {
            "title": p.get("title"), "slug": slug, "published": p.get("published"), "front_page": p.get("front_page"),
            "source_updated_at": _d(p.get("updated_at")), "locked_for_user": p.get("locked_for_user"),
        }
        if p.get("lock_explanation"):
            fields["lock_explanation"] = p.get("lock_explanation")
        depth_kind = "index"
        if "body" in p:
            fields["body"] = _html(body)
            depth_kind = "body"
        pkg.item("page", course_id=cid, canvas_id=page_id, url=f"{self.origin}/courses/{cid}/pages/{slug}",
                 title=p.get("title"), fields=fields, observed_at=t, depth=depth_kind,
                 source_updated_at=dates.from_api(p.get("updated_at"))["iso"] if p.get("updated_at") else None,
                 links=links, replace_links=["body_link"] if "body" in p else None)
        return pref

    # ---- modules -----------------------------------------------------------------------
    def t_modules(self, task):
        cid = task["course_canvas_id"]
        res = self.client.api_list(f"/api/v1/courses/{cid}/modules", [("include[]", "items"), ("include[]", "content_details")])
        t = now_iso()
        pkg = Package(self, task["task_key"])
        complete = res.complete
        problems = [] if res.complete else [res.reason or "module list incomplete"]
        n_items = 0
        external = 0
        gated = []
        locked_items: list = []
        for m in res.items:
            items = m.get("items")
            if items is None:
                try:
                    r2 = self.client.api_list(f"/api/v1/courses/{cid}/modules/{m['id']}/items",
                                              [("include[]", "content_details")])
                    items = r2.items
                    if not r2.complete:
                        complete = False
                        problems.append(f"module {m.get('name')}: {r2.reason}")
                except AccessDenied as e:
                    items = []
                    complete = False
                    problems.append(f"module {m.get('name')}: items not accessible ({e})")
            member_links = []
            for it in items:
                n_items += 1
                itype = it.get("type")
                cd = it.get("content_details") or {}
                req = it.get("completion_requirement")
                iref = ref("module_item", cid, it["id"], title=it.get("title"))
                member_links.append({"relation": "module_contains", "label": "", "position": it.get("position"), "to": iref})
                fields = {
                    "title": it.get("title"), "item_kind": itype, "position": it.get("position"), "indent": it.get("indent"),
                    "module_id": str(m["id"]), "module_name": m.get("name"), "completion_requirement": req,
                    "published": it.get("published"), "locked_for_user": cd.get("locked_for_user"),
                }
                for k in ("due_at", "unlock_at", "lock_at"):
                    if k in cd:
                        fields[k] = _d(cd.get(k))
                if "points_possible" in cd:
                    fields["points_possible"] = cd.get("points_possible")
                if cd.get("lock_explanation"):
                    fields["lock_explanation"] = cd.get("lock_explanation")
                links = []
                target_key = None
                if cd.get("locked_for_user"):
                    # Locked by date/prerequisite: inventory the item, do not try to open it.
                    locked_items.append(it.get("title"))
                elif itype == "Page" and it.get("page_url"):
                    target_key = f"page:{cid}:{it['page_url']}"
                    self.enqueue_with_referrer(
                        f"fetch:page:{cid}:{it['page_url']}", "fetch_page",
                        {"from": iref, "relation": "module_item_content", "label": ""},
                        course_id=cid, category="linked_content", payload={"slug": it["page_url"], "depth": 1})
                elif itype in ("Assignment", "Quiz", "Discussion", "File") and it.get("content_id"):
                    kind = {"Assignment": "assignment", "Quiz": "quiz", "Discussion": "discussion", "File": "file"}[itype]
                    links.append({"relation": "module_item_content", "label": "", "to": ref(kind, cid, it["content_id"])})
                    target_key = f"{kind}:{it['content_id']}"
                    if kind == "file":
                        self.enqueue(f"fetch:file:{it['content_id']}", "fetch_file", course_id=cid,
                                     category="linked_content", payload={"id": str(it["content_id"])})
                    else:
                        self.enqueue(f"fetch:{kind}:{cid}:{it['content_id']}", f"fetch_{kind}", course_id=cid,
                                     category="linked_content", payload={"id": str(it["content_id"]), "depth": 1})
                elif itype == "ExternalUrl" and it.get("external_url"):
                    url = canonical_url(it["external_url"])
                    fields["external_url"] = it["external_url"]
                    links.append({"relation": "module_item_content", "label": "",
                                  "to": ref("external_link", cid, url=url, title=it.get("title"))})
                    pkg.item("external_link", course_id=cid, url=url, title=it.get("title"),
                             fields={"url": url, "title": it.get("title"), "launched": False}, observed_at=t)
                    external += 1
                elif itype == "ExternalTool":
                    url = canonical_url(it.get("external_url") or it.get("html_url") or f"{self.origin}/courses/{cid}/modules/items/{it['id']}")
                    links.append({"relation": "module_item_content", "label": "",
                                  "to": ref("external_tool", cid, source_key=url, url=url, title=it.get("title"))})
                    pkg.item("external_tool", course_id=cid, source_key=url, url=url, title=it.get("title"),
                             fields={"url": url, "title": it.get("title"), "placement": "module_item", "launched": False},
                             observed_at=t)
                    external += 1
                if req and req.get("type") == "must_view" and target_key:
                    self.must_view[target_key] = {"module_item_id": str(it["id"]), "module": m.get("name"),
                                                  "title": it.get("title"), "completed": bool(req.get("completed"))}
                    if not req.get("completed"):
                        gated.append(it.get("title"))
                pkg.item("module_item", course_id=cid, canvas_id=it["id"], url=it.get("html_url"), title=it.get("title"),
                         fields=fields, observed_at=t, links=links, replace_links=["module_item_content"])
            pkg.item("module", course_id=cid, canvas_id=m["id"], url=f"{self.origin}/courses/{cid}/modules#module_{m['id']}",
                     title=m.get("name"), observed_at=t, links=member_links, replace_links=["module_contains"],
                     fields={
                         "title": m.get("name"), "position": m.get("position"), "unlock_at": _d(m.get("unlock_at")),
                         "prerequisite_module_ids": [str(x) for x in m.get("prerequisite_module_ids") or []],
                         "require_sequential_progress": m.get("require_sequential_progress"),
                         "progress_state": m.get("state"), "published": m.get("published"),
                         "items": [{"id": str(i.get("id")), "title": i.get("title"), "type": i.get("type"),
                                    "position": i.get("position"), "indent": i.get("indent")} for i in items],
                     })
        self.save_state()
        reason = list(problems)
        if gated:
            reason.append(f"{len(gated)} item(s) complete a 'must view' requirement when opened; inventoried but not opened "
                          f"(enable 'allow view completion' in scope to read them)")
        if locked_items:
            reason.append(f"{len(locked_items)} item(s) locked by date or prerequisite; listed but not opened")
        pkg.coverage("modules", "complete" if complete else "partial", course_id=cid,
                     index="complete" if complete else "partial", body="not_applicable",
                     attachments="not_applicable", collected=len(res.items),
                     reason="; ".join(reason) or None,
                     detail={"module_items": n_items, "external_items": external, "view_to_complete_unopened": gated,
                             "locked_items": locked_items},
                     absence_types=["module", "module_item"] if complete else None)
        pkg.commit()
        self._presence_checks(cid, ["module"])

    # ---- announcements & discussions ---------------------------------------------------------
    def _attachments(self, pkg: Package, cid, atts: list | None, t: str) -> list[dict]:
        out = []
        for a in atts or []:
            if not a.get("id"):
                continue
            fid = str(a["id"])
            # The attachment record embedded in a topic is an abbreviated view;
            # only its name is taken here. Full metadata comes from the file itself.
            pkg.item("file", course_id=cid, canvas_id=fid, url=f"{self.origin}/files/{fid}", title=a.get("display_name"),
                     fields=_pick(a, {"title": "display_name", "display_name": "display_name"}), observed_at=t)
            if not self.scope.get("download_files"):
                self.enqueue(f"fetch:file:{fid}", "fetch_file", course_id=cid, category="linked_content",
                             payload={"id": fid})
            out.append({"relation": "attachment", "label": a.get("display_name") or "", "to": ref("file", cid, fid)})
            self._maybe_download(cid, a)
        return out

    def _topic_fields(self, d: dict) -> dict:
        f = _pick(d, {
            "title": "title", "message": ("message", _html), "posted_at": ("posted_at", _d),
            "delayed_post_at": ("delayed_post_at", _d), "last_reply_at": ("last_reply_at", _d),
            "read_state": "read_state", "unread_count": "unread_count", "reply_count": "discussion_subentry_count",
            "locked": "locked", "locked_for_user": "locked_for_user", "require_initial_post": "require_initial_post",
            "discussion_type": "discussion_type", "assignment_id": ("assignment_id", _sid),
            "todo_date": ("todo_date", _d), "subscribed": "subscribed",
        })
        author = d.get("author") or {}
        if author or "user_name" in d:
            f["author"] = author.get("display_name") or d.get("user_name")
            f["author_id"] = _sid(author.get("id") or d.get("user_id"))
        if d.get("lock_explanation"):
            f["lock_explanation"] = d["lock_explanation"]
        if not d.get("message") and d.get("locked_for_user"):
            f.pop("message", None)  # body not visible; not collected rather than "empty"
        return f

    def t_announcements(self, task):
        cid = task["course_canvas_id"]
        res = self.client.api_list(f"/api/v1/courses/{cid}/discussion_topics", [("only_announcements", "true")])
        t = now_iso()
        pkg = Package(self, task["task_key"])
        authors = set()
        for d in res.items:
            aref = ref("announcement", cid, d["id"], title=d.get("title"))
            links = self.process_body(d.get("message"), cid, aref)
            self.external_items(pkg, links)
            links += self._attachments(pkg, cid, d.get("attachments"), t)
            fields = self._topic_fields(d)
            if fields.get("author_id"):
                authors.add(fields["author_id"])
            pkg.item("announcement", course_id=cid, canvas_id=d["id"], url=d.get("html_url"), title=d.get("title"),
                     fields=fields, observed_at=t, depth="body", links=links, replace_links=["body_link", "attachment"])
        self.staff[cid] = sorted(set(self.staff.get(cid, [])) | authors)
        self.save_state()
        pkg.coverage("announcements", "complete" if res.complete else "partial", course_id=cid,
                     index="complete" if res.complete else "partial", body="complete",
                     attachments="complete" if self.scope.get("download_files") else "not_checked",
                     collected=len(res.items), reason=res.reason,
                     detail={"pages_read": res.pages, "note": "Full announcement bodies from the topic list (all pages)."},
                     absence_types=["announcement"] if res.complete else None)
        pkg.commit()
        self._presence_checks(cid, ["announcement"])

    def t_discussions(self, task):
        cid = task["course_canvas_id"]
        res = self.client.api_list(f"/api/v1/courses/{cid}/discussion_topics")
        t = now_iso()
        pkg = Package(self, task["task_key"])
        hidden = 0
        replies_read = 0
        staff = set(self.staff.get(cid, []))
        for d in res.items:
            dref = ref("discussion", cid, d["id"], title=d.get("title"))
            links = self.process_body(d.get("message"), cid, dref)
            self.external_items(pkg, links)
            links += self._attachments(pkg, cid, d.get("attachments"), t)
            fields = self._topic_fields(d)
            if self.thorough and self.scope.get("discussion_replies") == "staff_only" and d.get("discussion_subentry_count"):
                try:
                    view = self.client.api_get(f"/api/v1/courses/{cid}/discussion_topics/{d['id']}/view")
                    fields["staff_replies"] = self._staff_replies(view, staff)
                    fields["replies_access"] = "read"
                    replies_read += 1
                except AccessDenied:
                    fields["replies_access"] = "hidden_until_initial_post" if d.get("require_initial_post") else "denied"
                    hidden += 1
            pkg.item("discussion", course_id=cid, canvas_id=d["id"], url=d.get("html_url"), title=d.get("title"),
                     fields=fields, observed_at=t, depth="body", links=links, replace_links=["body_link", "attachment"])
        reasons = []
        if hidden:
            reasons.append(f"{hidden} discussion(s) hide replies until you post; nothing was posted")
        if not self.thorough:
            reasons.append("quick check: replies not re-read")
        pkg.coverage("discussions", "complete" if res.complete and not hidden else "partial", course_id=cid,
                     index="complete" if res.complete else "partial",
                     body="complete" if self.thorough and not hidden else "partial",
                     attachments="complete" if self.scope.get("download_files") else "not_checked",
                     collected=len(res.items), reason="; ".join(reasons) or res.reason,
                     detail={"reply_policy": self.scope.get("discussion_replies"), "threads_with_replies_read": replies_read,
                             "note": "Only staff replies are stored; other participants' replies are counted, not copied."},
                     absence_types=["discussion"] if res.complete else None)
        pkg.commit()
        self._presence_checks(cid, ["discussion"])

    def _staff_replies(self, view: dict, staff: set[str]) -> list[dict]:
        names = {str(p.get("id")): p.get("display_name") for p in view.get("participants") or []}
        out = []

        def walk(entries):
            for e in entries or []:
                uid = str(e.get("user_id") or "")
                if uid and uid in staff and not e.get("deleted"):
                    out.append({"author": names.get(uid), "posted_at": _d(e.get("created_at")),
                                "updated_at": _d(e.get("updated_at")), "message": _html(e.get("message"))})
                walk(e.get("replies"))

        walk(view.get("view"))
        return out

    # ---- assignments, quizzes, grades ---------------------------------------------------------
    def _assignment_fields(self, a: dict, groups: dict) -> dict:
        f = _pick(a, {
            "title": "name", "points_possible": "points_possible", "grading_type": "grading_type",
            "submission_types": "submission_types", "due_at": ("due_at", _d), "unlock_at": ("unlock_at", _d),
            "lock_at": ("lock_at", _d), "allowed_attempts": "allowed_attempts", "published": "published",
            "locked_for_user": "locked_for_user", "omit_from_final_grade": "omit_from_final_grade",
            "is_quiz_assignment": "is_quiz_assignment", "quiz_id": ("quiz_id", _sid), "peer_reviews": "peer_reviews",
            "discussion_topic_id": ("discussion_topic", lambda t: _sid((t or {}).get("id"))),
            "external_tool_url": ("external_tool_tag_attributes", lambda t: (t or {}).get("url")),
            "rubric": ("rubric", lambda rs: [
                {"description": r.get("description"), "long_description": r.get("long_description"),
                 "points": r.get("points"),
                 "ratings": [{"description": x.get("description"), "points": x.get("points")} for x in r.get("ratings") or []]}
                for r in rs or []] or None),
        })
        if "assignment_group_id" in a:
            g = groups.get(str(a.get("assignment_group_id")), {})
            if g:
                f["assignment_group"] = g.get("name")
                f["assignment_group_weight"] = g.get("group_weight")
        if a.get("lock_explanation"):
            f["lock_explanation"] = a["lock_explanation"]
        if "description" in a and (a.get("description") is not None or not a.get("locked_for_user")):
            f["description"] = _html(a.get("description"))
        return f

    def _submission_fields(self, s: dict) -> dict:
        f = _pick(s, {
            "workflow_state": "workflow_state", "submitted_at": ("submitted_at", _d), "late": "late",
            "missing": "missing", "excused": "excused", "score": "score", "grade": "grade", "attempt": "attempt",
            "seconds_late": "seconds_late", "late_policy_status": "late_policy_status",
            "points_deducted": "points_deducted", "posted_at": ("posted_at", _d), "graded_at": ("graded_at", _d),
            "submission_type": "submission_type",
            "grade_matches_current_submission": "grade_matches_current_submission",
        })
        if "score" in s or "grade" in s:
            f["grade_visible"] = s.get("score") is not None or s.get("grade") is not None
        if s.get("workflow_state") == "graded" and s.get("score") is None and s.get("grade") is None:
            f["grade_note"] = "graded but score not visible to you (hidden or not posted)"
        if "submission_comments" in s:
            f["comments"] = [{"author": c.get("author_name"), "comment": c.get("comment"),
                              "created_at": _d(c.get("created_at")),
                              "attachments": [x.get("display_name") for x in c.get("attachments") or []]}
                             for c in s.get("submission_comments") or []]
        if "rubric_assessment" in s:
            f["rubric_assessment"] = s.get("rubric_assessment")
        return f

    def _groups(self, cid) -> dict:
        try:
            res = self.client.api_list(f"/api/v1/courses/{cid}/assignment_groups")
            return {str(g["id"]): g for g in res.items}
        except (AccessDenied, NotFoundError):
            return {}

    def _emit_assignment(self, pkg: Package, cid, a: dict, groups: dict, t: str) -> None:
        aref = ref("assignment", cid, a["id"], title=a.get("name"))
        fields = self._assignment_fields(a, groups)
        links = self.process_body(a.get("description"), cid, aref)
        self.external_items(pkg, links)
        pkg.item("assignment", course_id=cid, canvas_id=a["id"], url=a.get("html_url"), title=a.get("name"),
                 fields=fields, observed_at=t, depth="detail", links=links, replace_links=["body_link"])
        if isinstance(a.get("submission"), dict):
            s = a["submission"]
            pkg.item("submission", course_id=cid, canvas_id=a["id"], url=f"{a.get('html_url') or ''}/submissions/self",
                     title=f"Submission: {a.get('name')}", fields=self._submission_fields(s), observed_at=t,
                     links=[{"relation": "submission_for", "label": "", "to": aref}])

    def t_assignments(self, task):
        cid = task["course_canvas_id"]
        groups = self._groups(cid)
        res = self.client.api_list(f"/api/v1/courses/{cid}/assignments", [("include[]", "submission"), ("order_by", "due_at")])
        t = now_iso()
        pkg = Package(self, task["task_key"])
        locked = 0
        for a in res.items:
            if a.get("locked_for_user") and a.get("description") is None:
                locked += 1
            self._emit_assignment(pkg, cid, a, groups, t)
        reason = [res.reason] if res.reason else []
        if locked:
            reason.append(f"{locked} assignment(s) locked; instructions not visible until unlocked")
        if task["payload"].get("reason"):
            reason.append(task["payload"]["reason"])
        pkg.coverage("assignments", "complete" if res.complete and not locked else "partial", course_id=cid,
                     index="complete" if res.complete else "partial", body="complete" if not locked else "partial",
                     attachments="not_applicable", collected=len(res.items), reason="; ".join(reason) or None,
                     detail={"includes": "instructions, dates (user-specific as returned for you), points, rubric, "
                                         "submission state", "assignment_groups": len(groups)},
                     absence_types=["assignment"] if res.complete else None)
        pkg.commit()
        self._presence_checks(cid, ["assignment"])

    def t_fetch_assignment(self, task):
        cid, aid = task["course_canvas_id"], task["payload"]["id"]
        if self.observed_in_job("assignment", aid):
            return
        blocked = self.view_blocked(f"assignment:{aid}")
        if blocked:
            self._skip_view(task, blocked)
            return
        a = self.client.api_get(f"/api/v1/courses/{cid}/assignments/{aid}", [("include[]", "submission")])
        pkg = Package(self, task["task_key"])
        self._emit_assignment(pkg, cid, a, self._groups(cid), now_iso())
        pkg.commit()

    def _skip_view(self, task, mv: dict) -> None:
        self.hub.jobs.update_stats(self.job_id, view_completion_skipped=1)
        pkg = Package(self, task["task_key"] + "-viewgate")
        pkg.coverage("linked_content", "partial", course_id=task.get("course_canvas_id"), index="complete",
                     body="partial", reason=f"Not opened: '{mv.get('title')}' in module '{mv.get('module')}' is a "
                                            f"'must view' requirement and opening it would mark it complete",
                     detail={"task": task["task_key"], "module_item_id": mv.get("module_item_id")})
        pkg.commit()

    def _quiz_fields(self, q: dict) -> dict:
        f = _pick(q, {
            "title": "title", "description": ("description", _html), "quiz_type": "quiz_type",
            "due_at": ("due_at", _d), "unlock_at": ("unlock_at", _d), "lock_at": ("lock_at", _d),
            "time_limit_minutes": "time_limit", "allowed_attempts": "allowed_attempts",
            "points_possible": "points_possible", "question_count": "question_count", "published": "published",
            "locked_for_user": "locked_for_user", "assignment_id": ("assignment_id", _sid),
            "one_question_at_a_time": "one_question_at_a_time", "access_code_required": ("has_access_code", bool),
        })
        if q.get("lock_explanation"):
            f["lock_explanation"] = q["lock_explanation"]
        return f

    def t_quizzes(self, task):
        cid = task["course_canvas_id"]
        res = self.client.api_list(f"/api/v1/courses/{cid}/quizzes")
        t = now_iso()
        pkg = Package(self, task["task_key"])
        for q in res.items:
            qref = ref("quiz", cid, q["id"], title=q.get("title"))
            links = self.process_body(q.get("description"), cid, qref)
            self.external_items(pkg, links)
            pkg.item("quiz", course_id=cid, canvas_id=q["id"], url=q.get("html_url"), title=q.get("title"),
                     fields=self._quiz_fields(q), observed_at=t, depth="detail", links=links, replace_links=["body_link"])
        pkg.coverage("quizzes", "complete" if res.complete else "partial", course_id=cid,
                     index="complete" if res.complete else "partial", body="complete", attachments="not_applicable",
                     collected=len(res.items), reason=res.reason,
                     detail={"boundary": "Landing information only. No attempt was started or resumed; questions are not read. "
                                         "New Quizzes appear as assignments backed by an external tool."},
                     absence_types=["quiz"] if res.complete else None)
        pkg.commit()

    def t_fetch_quiz(self, task):
        cid, qid = task["course_canvas_id"], task["payload"]["id"]
        if self.observed_in_job("quiz", qid):
            return
        blocked = self.view_blocked(f"quiz:{qid}")
        if blocked:
            self._skip_view(task, blocked)
            return
        q = self.client.api_get(f"/api/v1/courses/{cid}/quizzes/{qid}")
        pkg = Package(self, task["task_key"])
        pkg.item("quiz", course_id=cid, canvas_id=qid, url=q.get("html_url"), title=q.get("title"),
                 fields=self._quiz_fields(q), observed_at=now_iso(), depth="detail")
        pkg.commit()

    def t_fetch_discussion(self, task):
        cid, did = task["course_canvas_id"], task["payload"]["id"]
        kind = "announcement" if task["kind"] == "fetch_announcement" else "discussion"
        if self.observed_in_job("discussion", did) or self.observed_in_job("announcement", did):
            return
        known_ann = self.hub.db.one("SELECT 1 FROM items WHERE account_id=? AND item_type='announcement' AND source_key=?",
                                    (self.account["id"], str(did)))
        if known_ann:
            kind = "announcement"
        blocked = self.view_blocked(f"discussion:{did}")
        if blocked:
            self._skip_view(task, blocked)
            return
        d = self.client.api_get(f"/api/v1/courses/{cid}/discussion_topics/{did}")
        t = now_iso()
        pkg = Package(self, task["task_key"])
        dref = ref(kind, cid, did, title=d.get("title"))
        links = self.process_body(d.get("message"), cid, dref, task["payload"].get("depth", 1))
        self.external_items(pkg, links)
        links += self._attachments(pkg, cid, d.get("attachments"), t)
        pkg.item(kind, course_id=cid, canvas_id=did, url=d.get("html_url"), title=d.get("title"),
                 fields=self._topic_fields(d), observed_at=t, depth="body", links=links,
                 replace_links=["body_link", "attachment"])
        pkg.commit()

    def t_grades(self, task):
        cid = task["course_canvas_id"]
        t = now_iso()
        pkg = Package(self, task["task_key"])
        problems = []
        enr = self.client.api_list(f"/api/v1/courses/{cid}/enrollments", [("user_id", "self")])
        groups = self._groups(cid)
        course = self.hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='course' AND source_key=?",
                                 (self.account["id"], str(cid)))
        hide = self.stored_field("course", cid, "hide_final_grades") if course else None
        weights = self.stored_field("course", cid, "apply_assignment_group_weights") if course else None
        grades = None
        for e in enr.items:
            if e.get("type") == "StudentEnrollment" and isinstance(e.get("grades"), dict):
                grades = e["grades"]
                break
        fields = {
            "title": "Grade summary", "hide_final_grades": hide, "weighted_by_group": weights,
            "assignment_groups": [{"name": g.get("name"), "weight": g.get("group_weight")} for g in groups.values()],
        }
        if grades is not None:
            fields.update({"current_score": grades.get("current_score"), "final_score": grades.get("final_score"),
                           "current_grade": grades.get("current_grade"), "final_grade": grades.get("final_grade"),
                           "totals_visible": grades.get("current_score") is not None or grades.get("current_grade") is not None})
        else:
            problems.append("no student enrollment grades returned (totals may be hidden or you are not a student here)")
        pkg.item("grade_summary", course_id=cid, source_key=f"grades:{cid}", url=f"{self.origin}/courses/{cid}/grades",
                 title="Grade summary", fields=fields, observed_at=t)
        subs = self.client.api_list(f"/api/v1/courses/{cid}/students/submissions",
                                    [("student_ids[]", "self"), ("include[]", "submission_comments"),
                                     ("include[]", "rubric_assessment")])
        for s in subs.items:
            aid = s.get("assignment_id")
            if not aid:
                continue
            pkg.item("submission", course_id=cid, canvas_id=aid, url=f"{self.origin}/courses/{cid}/assignments/{aid}/submissions/self",
                     fields=self._submission_fields(s), observed_at=t,
                     links=[{"relation": "submission_for", "label": "", "to": ref("assignment", cid, aid)}])
        if not subs.complete:
            problems.append(subs.reason or "submission list incomplete")
        pkg.coverage("grades", "complete" if not problems else "partial", course_id=cid,
                     index="complete" if subs.complete else "partial", body="complete",
                     attachments="not_applicable", collected=len(subs.items), reason="; ".join(problems) or None,
                     detail={"note": "Scores, statuses, comments and rubric assessments as visible to you. "
                                     "Hidden/unposted grades are recorded as not visible, not as zero."})
        pkg.commit()

    # ---- pages ------------------------------------------------------------------------------------
    def t_pages(self, task):
        cid = task["course_canvas_id"]
        res = self.client.api_list(f"/api/v1/courses/{cid}/pages", [("sort", "title")])
        t = now_iso()
        pkg = Package(self, task["task_key"])
        to_fetch = []
        for p in res.items:
            prev = self.stored_field("page", p.get("page_id"), "source_updated_at")
            has_body = self.stored_field("page", p.get("page_id"), "body") is not None
            self._page_item(pkg, cid, {k: v for k, v in p.items() if k != "body"}, t, depth=0)
            changed = (prev or {}).get("iso") != (dates.from_api(p.get("updated_at")) or {}).get("iso")
            if self.thorough or changed or not has_body:
                to_fetch.append(p.get("url"))
        pkg.coverage("pages", "complete" if res.complete else "partial", course_id=cid,
                     index="complete" if res.complete else "partial",
                     body="complete" if self.thorough else "partial", attachments="not_applicable",
                     collected=len(res.items),
                     reason=res.reason or (None if self.thorough else
                                           "quick check: only new or updated pages were re-read; other bodies keep their earlier observation time"),
                     detail={"bodies_queued": len(to_fetch)},
                     absence_types=["page"] if res.complete else None)
        pkg.commit()
        for slug in to_fetch:
            if slug:
                self.enqueue(f"fetch:page:{cid}:{slug}", "fetch_page", course_id=cid, category="pages",
                             payload={"slug": slug, "depth": 1})
        self._presence_checks(cid, ["page"])

    def t_fetch_page(self, task):
        cid, slug = task["course_canvas_id"], task["payload"]["slug"]
        blocked = self.view_blocked(f"page:{cid}:{slug}")
        if blocked:
            self._skip_view(task, blocked)
            return
        p = self.client.api_get(f"/api/v1/courses/{cid}/pages/{quote(slug, safe='')}")
        pkg = Package(self, task["task_key"])
        target = self._page_item(pkg, cid, p, now_iso(), task["payload"].get("depth", 1))
        self.resolve_referrers(task, target, pkg)
        pkg.commit()

    # ---- files ------------------------------------------------------------------------------------
    def _file_fields(self, f: dict) -> dict:
        out = _pick(f, {
            "title": "display_name", "display_name": "display_name", "filename": "filename",
            "content_type": "content-type", "size": "size", "created_at": ("created_at", _d),
            "source_updated_at": ("updated_at", _d), "modified_at": ("modified_at", _d), "locked": "locked",
            "hidden": "hidden", "locked_for_user": "locked_for_user", "unlock_at": ("unlock_at", _d),
            "lock_at": ("lock_at", _d), "folder_id": ("folder_id", _sid),
        })
        if "content_type" not in out and "content_type" in f:
            out["content_type"] = f["content_type"]
        if f.get("lock_explanation"):
            out["lock_explanation"] = f["lock_explanation"]
        return out

    def _maybe_download(self, cid, f: dict) -> None:
        if not self.scope.get("download_files"):
            return
        fid = str(f["id"])
        size = f.get("size") or 0
        if size > self.scope.get("max_file_mb", 100) * 1024 * 1024:
            return
        if f.get("locked_for_user"):
            return
        prev_mod = self.stored_field("file", fid, "modified_at")
        new_mod = dates.from_api(f.get("modified_at") or f.get("updated_at"))
        changed = (prev_mod or {}).get("iso") != (new_mod or {}).get("iso")
        if not self.has_resource("file", fid) or changed or self.thorough:
            self.enqueue(f"download:{fid}", "download", course_id=cid, category="files", payload={"id": fid})

    def t_files(self, task):
        cid = task["course_canvas_id"]
        folders = self.client.api_list(f"/api/v1/courses/{cid}/folders")
        t = now_iso()
        pkg = Package(self, task["task_key"])
        complete = folders.complete
        mismatches = []
        n_files = 0
        for fo in folders.items:
            foid = str(fo["id"])
            files: list = []
            if (fo.get("files_count") or 0) > 0:
                try:
                    r = self.client.api_list(f"/api/v1/folders/{foid}/files")
                    files = r.items
                    if not r.complete:
                        complete = False
                except AccessDenied:
                    complete = False
                    mismatches.append({"folder": fo.get("full_name"), "problem": "files not accessible"})
                    files = []
            if fo.get("files_count") is not None and len(files) != fo.get("files_count"):
                mismatches.append({"folder": fo.get("full_name"), "displayed_count": fo.get("files_count"),
                                   "collected": len(files)})
            members = []
            for f in files:
                n_files += 1
                fid = str(f["id"])
                pkg.item("file", course_id=cid, canvas_id=fid, url=f"{self.origin}/courses/{cid}/files/{fid}",
                         title=f.get("display_name"), fields={**self._file_fields(f), "folder_path": fo.get("full_name")},
                         observed_at=t)
                members.append({"relation": "folder_contains", "label": "", "to": ref("file", cid, fid)})
                if not self.view_blocked(f"file:{fid}"):
                    self._maybe_download(cid, f)
            pkg.item("folder", course_id=cid, canvas_id=foid, url=f"{self.origin}/courses/{cid}/files/folder/{quote(fo.get('full_name') or '')}",
                     title=fo.get("full_name"), observed_at=t, links=members, replace_links=["folder_contains"],
                     fields={"title": fo.get("full_name"), "name": fo.get("name"), "files_count": fo.get("files_count"),
                             "folders_count": fo.get("folders_count"), "source_updated_at": _d(fo.get("updated_at")),
                             "locked": fo.get("locked"), "hidden": fo.get("hidden"),
                             "locked_for_user": fo.get("locked_for_user"),
                             "parent_folder_id": str(fo["parent_folder_id"]) if fo.get("parent_folder_id") else None})
        reconciled = complete and not mismatches
        pkg.coverage("files", "complete" if reconciled else "partial", course_id=cid,
                     index="complete" if reconciled else "partial", body="not_applicable",
                     attachments="complete" if self.scope.get("download_files") else "not_checked",
                     expected=sum((fo.get("files_count") or 0) for fo in folders.items), collected=n_files,
                     reason=(f"folder counts did not reconcile: {mismatches[:5]}" if mismatches else folders.reason),
                     detail={"folders": len(folders.items), "mismatches": mismatches,
                             "downloads": "queued separately; see per-file extraction status"},
                     absence_types=["file", "folder"] if reconciled else None)
        pkg.commit()
        self._presence_checks(cid, ["file"])

    def t_fetch_file(self, task):
        fid = task["payload"]["id"]
        cid = task.get("course_canvas_id")
        if self.observed_in_job("file", fid) and self.has_resource("file", fid):
            return
        blocked = self.view_blocked(f"file:{fid}")
        if blocked:
            self._skip_view(task, blocked)
            return
        f = self.client.api_get(f"/api/v1/files/{fid}")
        pkg = Package(self, task["task_key"])
        pkg.item("file", course_id=cid, canvas_id=fid, url=f"{self.origin}/files/{fid}", title=f.get("display_name"),
                 fields=self._file_fields(f), observed_at=now_iso())
        pkg.commit()
        self._maybe_download(cid, f)

    def t_fetch_folder(self, task):
        cid, path = task["course_canvas_id"], task["payload"]["path"]
        chain = self.client.api_get(f"/api/v1/courses/{cid}/folders/by_path/{path}")
        if not chain:
            raise NotFoundError("folder path not found")
        target = chain[-1] if isinstance(chain, list) else chain
        t = now_iso()
        pkg = Package(self, task["task_key"])
        stack = [target]
        seen = 0
        while stack and seen < 50:
            fo = stack.pop()
            seen += 1
            foid = str(fo["id"])
            files = self.client.api_list(f"/api/v1/folders/{foid}/files").items if (fo.get("files_count") or 0) else []
            members = []
            for f in files:
                fid = str(f["id"])
                pkg.item("file", course_id=cid, canvas_id=fid, url=f"{self.origin}/courses/{cid}/files/{fid}",
                         title=f.get("display_name"), fields={**self._file_fields(f), "folder_path": fo.get("full_name")},
                         observed_at=t)
                members.append({"relation": "folder_contains", "label": "", "to": ref("file", cid, fid)})
                if not self.view_blocked(f"file:{fid}"):
                    self._maybe_download(cid, f)
            pkg.item("folder", course_id=cid, canvas_id=foid, title=fo.get("full_name"), observed_at=t, links=members,
                     replace_links=["folder_contains"],
                     fields={"title": fo.get("full_name"), "files_count": fo.get("files_count"),
                             "folders_count": fo.get("folders_count")})
            if fo.get("folders_count"):
                stack.extend(self.client.api_list(f"/api/v1/folders/{foid}/folders").items)
        pkg.commit()

    def t_download(self, task):
        fid = task["payload"]["id"]
        cid = task.get("course_canvas_id")
        blocked = self.view_blocked(f"file:{fid}")
        if blocked:
            self._skip_view(task, blocked)
            return
        meta = self.client.api_get(f"/api/v1/files/{fid}")
        url = meta.get("url")
        if not url:
            raise AccessDenied("no download URL offered (file locked or restricted)")
        max_bytes = self.scope.get("max_file_mb", 100) * 1024 * 1024
        body, ctype, _final = self.client.download(url, max_bytes)
        sha = self.hub.resources.store_bytes(body, ctype or meta.get("content-type"))
        name = meta.get("display_name") or meta.get("filename") or f"file-{fid}"
        ext = self.hub.resources.extract(sha, name)
        pkg = Package(self, task["task_key"])
        pkg.item("file", course_id=cid, canvas_id=fid, url=f"{self.origin}/files/{fid}", title=name,
                 fields={**self._file_fields(meta), "content_sha256": sha, "downloaded_size": len(body),
                         "extraction_status": ext["extraction_status"]},
                 resources=[{"sha256": sha, "filename": name}], observed_at=now_iso(), depth="detail")
        pkg.commit()
        if ext["extraction_status"] not in ("ok",):
            self.hub.jobs.update_stats(self.job_id, documents_without_text=1)

    # ---- account level --------------------------------------------------------------------------------
    def t_calendar(self, task):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=self.scope.get("calendar_days_back", 14))).strftime("%Y-%m-%dT00:00:00Z")
        end = (now + timedelta(days=self.scope.get("calendar_days_ahead", 120))).strftime("%Y-%m-%dT00:00:00Z")
        codes = [f"course_{c}" for c in self.scoped_courses] + [f"user_{self.account['canvas_user_id']}"]
        pkg = Package(self, task["task_key"])
        total, complete = 0, True
        for i in range(0, len(codes), 10):
            chunk = codes[i:i + 10]
            for typ in ("event", "assignment"):
                params = [("type", typ), ("start_date", start), ("end_date", end)] + [("context_codes[]", c) for c in chunk]
                res = self.client.api_list("/api/v1/calendar_events", params)
                complete = complete and res.complete
                t = now_iso()
                for e in res.items:
                    total += 1
                    ctx = e.get("context_code") or ""
                    cid = ctx.split("_", 1)[1] if ctx.startswith("course_") else None
                    eid = str(e.get("id"))
                    asg = e.get("assignment") or {}
                    pkg.item("calendar_event", course_id=cid, canvas_id=eid, url=e.get("html_url"), title=e.get("title"),
                             observed_at=t, fields={
                                 "title": e.get("title"), "description": _html(e.get("description")),
                                 "event_type": typ, "start_at": _d(e.get("start_at")), "end_at": _d(e.get("end_at")),
                                 "all_day": e.get("all_day"), "all_day_date": e.get("all_day_date"),
                                 "location_name": e.get("location_name"), "location_address": e.get("location_address"),
                                 "context_code": ctx, "assignment_id": str(asg["id"]) if asg.get("id") else None,
                             },
                             links=[{"relation": "calendar_for", "label": "", "to": ref("assignment", cid, asg["id"])}]
                             if asg.get("id") else [])
        pkg.coverage("calendar", "complete" if complete else "partial", index="complete" if complete else "partial",
                     body="complete", attachments="not_applicable", collected=total,
                     detail={"start": start, "end": end, "calendars": codes,
                             "note": "Calendar entries for this date range only; the assignment inventory comes from each course."})
        pkg.commit()

    def t_groups(self, task):
        res = self.client.api_list("/api/v1/users/self/groups")
        t = now_iso()
        pkg = Package(self, task["task_key"])
        for g in res.items:
            cid = str(g["course_id"]) if g.get("course_id") else None
            pkg.item("group", course_id=cid, canvas_id=g["id"], url=f"{self.origin}/groups/{g['id']}", title=g.get("name"),
                     observed_at=t, fields={"title": g.get("name"), "description": _html(g.get("description")),
                                            "members_count": g.get("members_count"), "context_type": g.get("context_type")})
        pkg.coverage("groups", "partial" if res.items else "complete", index="complete" if res.complete else "partial",
                     body="not_checked", attachments="not_checked", collected=len(res.items),
                     reason="Group memberships recorded; group pages/files are not traversed in this version" if res.items else None,
                     absence_types=["group"] if res.complete else None)
        pkg.commit()

    def t_inbox(self, task):
        res = self.client.api_list("/api/v1/conversations", [("scope", "inbox")])
        pkg = Package(self, task["task_key"])
        scoped = {f"course_{c}" for c in self.scoped_courses}
        kept = 0
        for cv in res.items:
            ctx = cv.get("context_code") or ""
            if ctx not in scoped:
                continue
            full = self.client.api_get(f"/api/v1/conversations/{cv['id']}", [("auto_mark_as_read", "false")])
            t = now_iso()
            names = {str(p.get("id")): p.get("name") for p in full.get("participants") or []}
            kept += 1
            pkg.item("conversation", course_id=ctx.split("_", 1)[1], canvas_id=cv["id"],
                     url=f"{self.origin}/conversations#filter=type=inbox", title=cv.get("subject") or "(no subject)",
                     observed_at=t, depth="body", fields={
                         "title": cv.get("subject") or "(no subject)", "message_count": cv.get("message_count"),
                         "last_message_at": _d(cv.get("last_message_at")), "workflow_state": cv.get("workflow_state"),
                         "participants": [p.get("name") for p in full.get("participants") or []],
                         "messages": [{"author": names.get(str(m.get("author_id"))), "created_at": _d(m.get("created_at")),
                                       "body": m.get("body")} for m in full.get("messages") or []],
                     })
        pkg.coverage("inbox", "complete" if res.complete else "partial", index="complete" if res.complete else "partial",
                     body="complete", attachments="not_checked", collected=kept,
                     detail={"filter": "conversations whose context is an in-scope course",
                             "note": "Read with auto_mark_as_read=false; nothing is ever sent."})
        pkg.commit()

    # ---- presence checks ----------------------------------------------------------------------------------
    _PRESENCE_PATHS = {
        "assignment": "/api/v1/courses/{c}/assignments/{i}", "announcement": "/api/v1/courses/{c}/discussion_topics/{i}",
        "discussion": "/api/v1/courses/{c}/discussion_topics/{i}", "page": "/api/v1/courses/{c}/pages/page_id:{i}",
        "file": "/api/v1/files/{i}", "quiz": "/api/v1/courses/{c}/quizzes/{i}", "module": "/api/v1/courses/{c}/modules/{i}",
    }

    def _presence_checks(self, cid, types: list[str]) -> None:
        """Thorough runs re-check the source of items that disappeared from an
        exhaustive index before calling them unavailable."""
        if not self.thorough:
            return
        rows = self.hub.db.query(
            f"""SELECT i.item_type, i.canvas_id, i.course_canvas_id FROM items i
                WHERE i.account_id=? AND i.course_canvas_id=? AND i.presence='not_in_latest_index'
                AND i.item_type IN ({','.join('?'*len(types))}) ORDER BY i.presence_changed_at DESC LIMIT 30""",
            (self.account["id"], str(cid), *types))
        if not rows:
            return
        pkg = Package(self, f"presence:{cid}:{'-'.join(types)}")
        for r in rows:
            path = self._PRESENCE_PATHS.get(r["item_type"])
            if not path or not r["canvas_id"]:
                continue
            rf = ref(r["item_type"], r["course_canvas_id"], r["canvas_id"])
            try:
                self.client.api_get(path.format(c=r["course_canvas_id"], i=r["canvas_id"]))
                pkg.presence(rf, "available", 200)
            except NotFoundError:
                pkg.presence(rf, "not_found", 404)
            except AccessDenied as e:
                pkg.presence(rf, "forbidden", e.status)
        pkg.commit()

