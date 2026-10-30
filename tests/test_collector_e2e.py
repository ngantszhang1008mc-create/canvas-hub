"""End-to-end collector tests: real Chromium + Playwright against the synthetic
Canvas server (canvas_hub/testing/fake_canvas.py). The sign-in form is filled
by the test, standing in for the user typing into the app-managed window.

These do not prove behaviour against a real institution; see
CODEX_HANDOFF.md section 7 for the user-assisted checks.
"""

import time

import pytest

from canvas_hub.collector.browser import BrowserWorker
from canvas_hub.collector.runner import JobRunner
from canvas_hub.collector.traversal import CollectionRun
from canvas_hub.paths import get_paths
from canvas_hub.services import Hub
from canvas_hub.testing.fake_canvas import MFA_CODE, PASSWORD, FakeCanvas

pytestmark = pytest.mark.browser


class Env:
    def __init__(self, tmp_path, entry="/courses/101"):
        self.fc = FakeCanvas(async_courses_ms=300).start()
        self.hub = Hub(get_paths(tmp_path / "home"))
        self.worker = BrowserWorker(self.hub, headless=True, min_interval=0.0)
        self.worker.start()
        self.inst = self.hub.identity.add_instance(self.fc.origin + entry)
        self.runner = None

    def login(self, username="astudent"):
        state = self.worker.call(self.worker.open_login, self.inst["id"])
        assert state["status"] == "login_open"   # a window is open, but nothing is verified yet

        def fill():
            page = self.worker._contexts[self.inst["id"]].pages[0]
            page.goto(self.fc.origin + "/login")
            page.fill("#username", username)
            page.fill("#password", PASSWORD)
            page.click("#submit")
            page.fill("#code", MFA_CODE)
            page.click("#verify")
            page.wait_for_load_state()
        self.worker.call(fill)
        state = self.worker.call(self.worker.check_session, self.inst["id"])
        assert state["status"] == "signed_in"
        return state

    def run(self, account_id, mode="baseline", scope=None):
        if self.runner is None:
            self.runner = JobRunner(self.hub, self.worker)   # not started: run synchronously below
        job = self.runner.start_collection(account_id, mode, scope)
        return self.run_job(job["id"])

    def run_job(self, job_id):
        def work():
            run = CollectionRun(self.hub, None, job_id)
            run.client = self.worker.client_for(self.inst["id"], should_stop=run.should_stop)
            return run.run()
        self.worker.call(work, timeout=600)
        return self.hub.jobs.get(job_id)

    def close(self):
        if self.runner:
            self.runner.stop()
        self.worker.stop()
        self.fc.stop()
        self.hub.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def item(hub, aid, type_, key):
    return hub.db.one("SELECT * FROM items WHERE account_id=? AND item_type=? AND source_key=?", (aid, type_, str(key)))


def test_login_verification_and_baseline(env):
    # Before sign-in nothing is verified and the API refuses.
    s0 = env.worker.call(env.worker.check_session, env.inst["id"])
    assert s0["status"] != "signed_in"
    st = env.login()
    assert st["display_name"] == "Alex Student" and st["time_zone"] == "Asia/Hong_Kong"
    assert st["entry_course_accessible"] is True
    aid = st["account_id"]
    job = env.run(aid, "baseline")
    assert job["state"] in ("completed", "completed_with_gaps"), job
    hub, fc = env.hub, env.fc

    # Read-only: no write requests at all; no view-to-complete content opened.
    assert fc.writes == []
    opened = [p for p in fc.paths_requested() if "integrity-pledge" in p or "/files/9302" in p]
    assert opened == []
    assert not any("week-2-reading" in p for p in fc.paths_requested())   # locked module item not opened

    # Discovery from All Courses: includes unpublished/inaccessible entry, past course not traversed by default.
    courses = {c["title"]: c for c in hub.query.list_courses(aid)}
    unpub = courses["DEMO3999 Unpublished Lab"]
    assert unpub["access"] == "not_accessible" and unpub["published"] is False
    assert courses["DEMO0900 Bridging Course (Summer 2026)"]["listing_section"] == "past"
    assert not any(p.startswith("/api/v1/courses/105/") for p in fc.paths_requested())

    # Paginated, asynchronously loaded announcements: all 35 bodies.
    assert hub.db.one("SELECT COUNT(*) n FROM items WHERE account_id=? AND item_type='announcement' AND course_canvas_id='101'",
                      (aid,))["n"] == 35
    # Nested folders reconcile with displayed counts.
    cov = {(r["course_id"], r["category"]): r["latest"] for r in hub.query.coverage(aid)["records"]}
    assert cov[("101", "files")]["status"] == "complete" and cov[("101", "files")]["collected_count"] == 41
    assert item(hub, aid, "file", "8201")["current_revision"] > 0   # course files/Lectures/Old/old-notes.md

    # Hidden Pages/Files menus, but Home links are followed (COMP2711H-like course).
    assert cov[("102", "pages")]["status"] == "not_exposed"
    notes_page = hub.db.one("SELECT * FROM items WHERE account_id=? AND item_type='page' AND title='Lecture notes'", (aid,))
    assert notes_page and notes_page["current_revision"] > 0
    assert hub.db.one("SELECT extraction_status FROM resources r JOIN item_resources ir ON ir.resource_id=r.id "
                      "JOIN items i ON i.id=ir.item_id WHERE i.source_key='9001'")["extraction_status"] == "ok"
    assert not any(p.startswith("/api/v1/courses/102/pages?") or p == "/api/v1/courses/102/pages" for p in fc.paths_requested())

    # Collapsed Home content is captured.
    assert hub.search.search(aid, "Thursdays")["results"]

    # Modules: view-to-complete gap recorded, completed must-view item read, external items unlaunched.
    assert "must view" in (cov[("103", "modules")]["reason"] or "")
    assert item(hub, aid, "page", "6302")["current_revision"] > 0       # completed must_view -> safe to read
    pledge = hub.db.one("SELECT * FROM items WHERE account_id=? AND item_type='page' AND title='Integrity pledge'", (aid,))
    assert pledge is None or pledge["current_revision"] == 0
    assert cov[("103", "external_tools")]["status"] == "partial"
    assert not any("video.example.org" in p for p in fc.paths_requested())

    # Assignment details with evidence; hidden grade is not a zero; locked assignment is partial body.
    ps1 = hub.query.get_item(aid, item(hub, aid, "assignment", "1001")["id"])
    due = next(f for f in ps1["fields"] if f["field"] == "due_at")["value"]
    assert due["raw"] == "2026-10-31T15:59:00Z" and due["source"] == "canvas_api_iso8601"
    mid = hub.query.get_item(aid, item(hub, aid, "submission", "1003")["id"])
    fields = {f["field"]: f["value"] for f in mid["fields"]}
    assert fields["grade_visible"] is False and fields["score"] is None and "not visible" in fields["grade_note"]
    assert cov[("101", "assignments")]["status"] == "partial"
    hw = hub.query.get_item(aid, item(hub, aid, "submission", "2001")["id"])
    assert any(f["field"] == "comments" and f["value"][0]["comment"].startswith("Good work") for f in hw["fields"])

    # Discussions: staff reply kept, classmates' replies not copied; require-initial-post noted.
    disc = hub.query.get_item(aid, item(hub, aid, "discussion", "7501")["id"])
    staff = next(f for f in disc["fields"] if f["field"] == "staff_replies")["value"]
    assert len(staff) == 1 and "kernels" in staff[0]["message"]
    assert "Classmate" not in str(disc["fields"])
    assert cov[("101", "discussions")]["status"] == "partial"

    # Announcement vs assignment date discrepancy flagged with both dates kept.
    d = hub.query.discrepancies(aid)
    assert d and d[0]["other_title"] == "Problem Set 1" and d[0]["detail"]["announcement_mentions"] == ["Oct 30 at 11:59pm"]

    # Documents: extraction states are distinguishable.
    states = {r["extraction_status"] for r in hub.db.query("SELECT extraction_status FROM resources")}
    assert {"ok", "needs_ocr", "image_only", "unsupported_media"} <= states
    hits = hub.search.search(aid, "spectral theorem")["results"]
    assert hits and hits[0]["resource_id"] and hits[0]["source_url"]

    # Everything from the first baseline is flagged baseline; nothing shows as a "change".
    assert hub.query.changes(aid)["events"] == []


def test_second_run_no_false_updates_then_real_changes(env):
    aid = env.login()["account_id"]
    env.run(aid, "baseline")
    n_items = env.hub.db.one("SELECT COUNT(*) n FROM items")["n"]
    j2 = env.run(aid, "thorough")
    assert j2["stats"]["events"] == 0, env.hub.query.changes(aid)["events"][:5]
    assert env.hub.db.one("SELECT COUNT(*) n FROM items")["n"] == n_items

    # Instructor edits: deadline moved, page text changed, new announcement, file removed.
    D = env.fc.data
    D["assignments"]["101"][1]["due_at"] = "2026-11-21T15:59:00Z"
    D["pages"]["101"][0]["body"] = "<p>Late work loses 20% per day.</p>"
    D["pages"]["101"][0]["updated_at"] = "2026-10-29T03:00:00Z"
    D["announcements"]["101"].append({"id": "7999", "title": "Room change", "posted_at": "2026-10-29T01:00:00Z",
                                      "message": "<p>Tutorial moves to Room 4000.</p>", "author": {"id": "9001"},
                                      "html_url": "/courses/101/discussion_topics/7999", "attachments": []})
    del D["files"]["8021"]

    quick = env.run(aid, "quick")
    assert quick["state"] in ("completed", "completed_with_gaps")
    ev = env.hub.query.changes(aid)["events"]
    kinds = {(e["kind"], e["field"], e["title"]) for e in ev}
    assert ("field_changed", "due_at", "Problem Set 2") in kinds
    assert ("field_changed", "body", "Course policies") in kinds          # updated_at moved, so quick re-read it
    assert ("first_observed", None, "Room change") in kinds
    assert ("not_in_latest_index", None, "handout-21.txt") in kinds
    # Quick mode does not claim deletion; a thorough run re-checks the source.
    f = item(env.hub, aid, "file", "8021")
    assert f["presence"] == "not_in_latest_index"
    env.run(aid, "thorough")
    assert item(env.hub, aid, "file", "8021")["presence"] == "source_unavailable"
    assert item(env.hub, aid, "file", "8021")["current_revision"] > 0      # old evidence kept
    # The quick run did not replace the thorough baseline for pages.
    rec = next(r for r in env.hub.query.coverage(aid, course_id="101")["records"] if r["category"] == "pages")
    assert rec["last_thorough"] is not None


def test_session_expiry_pauses_and_resumes(env):
    aid = env.login()["account_id"]
    env.runner = JobRunner(env.hub, env.worker, poll_seconds=0.2)
    env.runner.start()
    env.fc.expire_after_api_requests = 40
    job = env.runner.start_collection(aid, "baseline")
    deadline = time.time() + 300
    while time.time() < deadline:
        j = env.hub.jobs.get(job["id"])
        if j["state"] == "needs_login":
            break
        time.sleep(0.3)
    assert j["state"] == "needs_login", j
    done_before = j["tasks"].get("done", 0)
    assert done_before > 0 and j["tasks"].get("pending", 0) > 0
    items_before = env.hub.db.one("SELECT COUNT(*) n FROM items WHERE current_revision>0")["n"]
    assert items_before > 0   # verified partial results are kept
    s = env.worker.call(env.worker.check_session, env.inst["id"])
    assert s["status"] == "expired"
    env.login()                      # user signs in again -> job auto-resumes
    while time.time() < deadline:
        j = env.hub.jobs.get(job["id"])
        if j["state"] in ("completed", "completed_with_gaps", "failed"):
            break
        time.sleep(0.3)
    assert j["state"] in ("completed", "completed_with_gaps"), j
    assert env.hub.db.one("SELECT COUNT(*) n FROM items WHERE account_id=? AND item_type='announcement'", (aid,))["n"] == 36
    assert env.hub.db.one("PRAGMA integrity_check")[0] == "ok"


def test_cancel_and_restart_resume(env):
    aid = env.login()["account_id"]
    runner = JobRunner(env.hub, env.worker)
    job = runner.start_collection(aid, "baseline")
    # Simulate an app crash in the middle: run a few tasks, then leave it 'running'.
    def partial():
        run = CollectionRun(env.hub, None, job["id"])
        run.client = env.worker.client_for(env.inst["id"], should_stop=run.should_stop)
        jobs = env.hub.jobs
        run.enqueue("identity", "identity")
        run.enqueue("courses", "courses", category="courses")
        jobs.set_state(job["id"], "running")
        for _ in range(4):
            t = jobs.next_task(job["id"])
            getattr(run, run.HANDLERS[t["kind"]])(t)
            jobs.finish_task(t["id"], "done")
    env.worker.call(partial, timeout=120)
    assert env.hub.jobs.get(job["id"])["state"] == "running"
    # App restarts: running jobs become interrupted, checkpoint intact.
    assert env.hub.jobs.recover_after_restart() == [job["id"]]
    j = env.hub.jobs.get(job["id"])
    assert j["state"] == "interrupted" and j["resumable"] and j["tasks"]["done"] == 4
    env.hub.jobs.resume(job["id"])
    j = env.run_job(job["id"])
    assert j["state"] in ("completed", "completed_with_gaps")
    assert env.hub.db.one("SELECT COUNT(*) n FROM job_tasks WHERE job_id=? AND state='pending'", (job["id"],))["n"] == 0

    # Cancel: a new job cancelled while queued keeps prior data and cannot be resumed.
    job2 = runner.start_collection(aid, "quick")
    c = env.hub.jobs.request_cancel(job2["id"])
    assert c["state"] == "cancelled" and not c["resumable"]
    assert env.hub.db.one("SELECT COUNT(*) n FROM items WHERE current_revision>0")["n"] > 100


def test_different_signed_in_account_is_never_mixed(env):
    a1 = env.login()["account_id"]
    env.run(a1, "baseline", scope={"categories": ["courses", "assignments"]})
    n1 = env.hub.db.one("SELECT COUNT(*) n FROM items WHERE account_id=?", (a1,))["n"]
    # Another person signs in to the same Canvas in the app browser.
    env.fc.user = {"id": "5002", "name": "Other Student"}
    env.fc.profile = {"id": "5002", "name": "Other Student", "time_zone": "Asia/Hong_Kong"}
    s = env.worker.call(env.worker.check_session, env.inst["id"])
    assert s["canvas_user_id"] == "5002" and s["account_id"] != a1
    # A job for the first account refuses to collect as the other user.
    runner = JobRunner(env.hub, env.worker)
    job = runner.start_collection(a1, "quick", {"categories": ["courses", "assignments"]})
    runner._run_one(env.hub.jobs.get(job["id"]))
    j = env.hub.jobs.get(job["id"])
    assert j["state"] == "needs_login" and "different Canvas user" in j["message"]
    # Even if forced to run, the identity task stops it before any import.
    env.hub.jobs.resume(job["id"])
    j = env.run_job(job["id"])
    assert j["state"] == "failed"
    assert env.hub.db.one("SELECT COUNT(*) n FROM items WHERE account_id=?", (a1,))["n"] == n1
    assert env.hub.db.one("SELECT COUNT(*) n FROM items WHERE account_id=?", (s["account_id"],))["n"] == 0


@pytest.mark.parametrize("expired", [False, True], ids=["persistent-session", "expired-session"])
def test_background_checks_do_not_open_browser_and_reopened_sessions_are_verified(env, expired):
    iid = env.inst["id"]
    env.login()

    def keep_session():
        # Choose a persistent cookie explicitly: Chromium versions differ in
        # whether they retain a session-only cookie in a persistent profile.
        ctx = env.worker._contexts[iid]
        cookies = ctx.cookies()
        for cookie in cookies:
            if cookie["name"] == "fake_session":
                cookie["expires"] = time.time() + 3600
        ctx.add_cookies(cookies)

    env.worker.call(keep_session)
    env.worker.call(env.worker.close_browser, iid)
    requests_before = env.fc.paths_requested().count("/api/v1/users/self")
    polled = env.worker.call(lambda: env.worker.check_session(iid, launch=False))
    assert polled["status"] != "signed_in" and iid not in env.worker._contexts   # nothing launched
    assert env.fc.paths_requested().count("/api/v1/users/self") == requests_before
    if expired:
        env.fc.expire_sessions()
    # Reopening checks with Canvas even if the profile retains its cookie.
    checked = env.worker.call(lambda: env.worker.check_session(iid, launch=True))
    assert checked["status"] == ("expired" if expired else "signed_in")
    assert env.worker._modes[iid] == "headless"
    assert env.fc.paths_requested().count("/api/v1/users/self") > requests_before
