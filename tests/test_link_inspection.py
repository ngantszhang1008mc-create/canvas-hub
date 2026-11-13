"""External-link inspection against synthetic sites: reading, diagnosing failures, and the three kinds of sign-in."""
import json
import os
import stat
import time

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool
from canvas_hub.boundary import HubAPI
from canvas_hub.services.link_inspection import PRIVATE_URL, LinkInspector, classify, explain
from canvas_hub.testing import fake_sites as fs
from canvas_hub.testing.link_demo import add_link, add_links

from .conftest import in_sign_in_window, settled

def field(hub, aid, item_id, name="content"):
    return {f["field"]: f["value"] for f in hub.query.get_item(aid, item_id)["fields"]}[name]


PAGE = {"title": "Course", "text": "Lecture 3 covers loops. " * 20, "textLength": 480, "passwordVisible": False, "passwordAny": False,
        "userVisible": False, "loginForm": False, "loginPrompt": False, "challenge": False}


def facts(**changes):
    return {**PAGE, **changes}


# ---- deciding what a loaded page is (no browser) ---------------------------------------
@pytest.mark.parametrize("target,final,status,challenge,page,outcome,method,login_host", [
    # A course website behind a browser username/password prompt.
    ("http://course.example.edu/c/", "https://course.example.edu/c/", 401, 'Basic realm="Course"', facts(textLength=30), "login_required", "basic", "course.example.edu"),
    # A grader that sends the browser to the university's sign-in service.
    ("https://grader.example.edu/a", "https://cas.example.edu/cas/login?service=x", 200, None,
     facts(passwordVisible=True, userVisible=True, textLength=80), "login_required", "sso", "cas.example.edu"),
    # A cloud identity provider whose first screen asks for an e-mail address only.
    ("https://grader.example.edu/a", "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?x=1", 200, None,
     facts(userVisible=True, textLength=60), "login_required", "sso", "login.microsoftonline.com"),
    # A forum with its own log-in page on the same site.
    ("https://forum.example.com/class/abc", "https://forum.example.com/account/login?next=x", 200, None,
     facts(passwordVisible=True, userVisible=True, loginForm=True, textLength=90), "login_required", "form", "forum.example.com"),
    # A landing page that only offers "Log in" and "Join class".
    ("https://forum.example.com/class/abc", "https://forum.example.com/class/abc", 200, None,
     facts(loginPrompt=True, text="Log in. Join class to see posts. Add another class.", textLength=52), "login_required", "form", "forum.example.com"),
    # Other HTTP sign-in schemes can only be answered in a browser window.
    ("https://site.example.edu/", "https://site.example.edu/", 401, "Negotiate", facts(textLength=10), "login_required", "form", "site.example.edu"),
    ("https://site.example.edu/x", "https://site.example.edu/x", 404, None, facts(), "not_found", None, None),
    ("https://site.example.edu/x", "https://site.example.edu/x", 403, None, facts(textLength=40), "forbidden", None, None),
    ("https://site.example.edu/x", "https://site.example.edu/x", 503, None, facts(), "site_error", None, None),
    ("https://site.example.edu/x", "https://site.example.edu/x", 429, None, facts(), "site_error", None, None),
    ("https://site.example.edu/x", "https://site.example.edu/x", 200, None, facts(title="Just a moment...", textLength=30), "blocked", None, None),
    ("https://site.example.edu/x", "https://site.example.edu/x", 200, None, facts(text="", textLength=0), "empty", None, None),
    # Readable course content with an optional sign-in box beside it is still content.
    ("https://site.example.edu/x", "https://site.example.edu/x", 200, None,
     facts(passwordVisible=True, text="Syllabus. " * 400, textLength=4000), "ok", None, None),
    # A redirect to another host that is simply the page's new home is content too.
    ("http://old.example.edu/x", "https://new.example.edu/x", 200, None, facts(), "ok", None, None),
])
def test_pages_are_classified_by_what_they_show(target, final, status, challenge, page, outcome, method, login_host):
    result = classify(target, final, status, challenge, page)
    assert result["outcome"] == outcome
    assert result.get("login_method") == method and result.get("login_host") == login_host
    message = explain({**result, "content_chars": 10}, target)
    assert message and "None" not in message


def test_a_rejected_password_is_reported_as_such_and_never_as_course_content():
    result = classify("https://c.example.edu/", "https://c.example.edu/", 401, "Basic realm=x", facts(), sent_basic=True)
    assert result["reason"] == "credentials_rejected" and "did not accept" in explain(result, "https://c.example.edu/")
    assert "content" not in result


def test_the_final_address_is_stored_without_sign_in_tickets():
    result = classify("https://g.example.edu/a", "https://cas.example.edu/cas/login?service=x&ticket=ST-secret", 200, None,
                      facts(passwordVisible=True, textLength=50))
    assert result["final_url"] == "https://cas.example.edu/cas/login" and "ST-secret" not in json.dumps(result)


def test_requests_to_this_computer_or_a_private_network_are_dropped():
    for url in ("http://127.0.0.1:8730/api/info", "http://localhost/", "https://10.1.2.3/x", "http://192.168.0.1",
                "http://172.20.4.4:8080/a", "http://169.254.169.254/latest/meta-data", "http://[::1]:9000/",
                "http://printer.local/status", "http://user@127.0.0.1/"):
        assert PRIVATE_URL.match(url), url
    for url in ("https://course.example.edu/127.0.0.1/", "https://10.example.edu/", "https://172.99.1.1/",
                "https://localhost.example.edu/", "https://piazza.com/class/abc?next=http://127.0.0.1/"):
        assert not PRIVATE_URL.match(url), url


# ---- newly found links are announced, never opened by themselves ------------------------
def test_a_collected_link_is_announced_until_inspected_or_dismissed(hub, account, fixture_pkg):
    pkg = json.loads(json.dumps(fixture_pkg))
    pkg["package_id"] = "with-external-link"
    pkg["items"][0].setdefault("links", []).append({"relation": "body_link", "label": "Course website",
        "to": {"type": "external_link", "course_id": "4242", "url": "https://course.example.edu/comp/", "title": "Course website"}})
    hub.importer.import_package(pkg)
    service = LinkInspector(hub)
    aid = account["id"]
    summary = service.summary(aid)
    assert summary["notice"]["count"] == 1 and summary["not_inspected"] == 1
    assert summary["notice"]["examples"][0]["title"] == "Course website"
    assert service._thread is None                       # finding a link starts no browser
    sid = summary["notice"]["source_ids"][0]
    assert service.sources(aid)[0]["inspection"]["actions"] == ["inspect"]
    assert service.dismiss_notice(aid, [sid])["notice"]["count"] == 0
    assert service.summary(aid)["not_inspected"] == 1    # dismissed, but still listed as not inspected


def test_interrupted_work_is_not_replayed_after_a_restart(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    sid = add_link(hub, account["id"], "https://course.example.edu/a", "A")
    first = LinkInspector(hub)
    first._set(account["id"], sid, state="running")
    again = LinkInspector(hub)
    view = again.view(again._source(account["id"], sid))
    assert view["state"] == "idle" and view["outcome"] is None and "Interrupted" in view["message"]
    assert again._thread is None


# ---- reading with a real browser --------------------------------------------------------
@pytest.mark.browser
def test_a_public_page_is_read_saved_and_searchable(hub, account, inspector, fake_sites):
    aid = account["id"]
    sid = add_links(hub, aid, fake_sites, ["syllabus"])["syllabus"]
    assert inspector.inspect(aid, [sid]) == {"queued": [sid]}
    view = inspector.wait(aid, sid)
    assert view["outcome"] == "ok" and view["state"] == "idle" and view["content_chars"] > 100
    assert view["message"].startswith("Read and saved")
    assert hub.query.get_item(aid, view["item_id"])["item_type"] == "external_link"
    assert "Office hours" in field(hub, aid, view["item_id"])
    assert field(hub, aid, view["item_id"], "provided_by") == "Canvas Hub link inspection"
    assert hub.search.search(aid, "office hours")["results"][0]["item_id"] == view["item_id"]
    source = hub.external_sources.get(aid, sid)
    assert source["status"] == "enabled" and source["last_success_at"] and source["reads"][0]["status"] == "inspection_read"
    assert not fake_sites.seen(method="POST")            # reading never submits anything
    # Reading the same unchanged page again creates no second item and no change event.
    events = hub.db.one("SELECT COUNT(*) n FROM change_events")["n"]
    inspector.inspect(aid, [sid])
    assert inspector.wait(aid, sid)["item_id"] == view["item_id"]
    assert hub.db.one("SELECT COUNT(*) n FROM change_events")["n"] == events


@pytest.mark.browser
def test_failures_say_what_went_wrong(hub, account, inspector, fake_sites):
    aid = account["id"]
    ids = {name: add_link(hub, aid, url, name) for name, url in {
        "missing": fake_sites.url("course", "/missing"), "broken": fake_sites.url("course", "/broken"),
        "denied": fake_sites.url("course", "/denied"), "empty": fake_sites.url("course", "/empty"),
        "zip": fake_sites.url("course", "/archive.zip"), "dns": "http://no-such-host.invalid/page",
        "refused": "http://127.0.0.1:9/"}.items()}
    assert sorted(inspector.inspect(aid)["queued"]) == sorted(ids.values())
    got = {name: inspector.wait(aid, sid) for name, sid in ids.items()}
    assert {name: (v["outcome"], v["reason"]) for name, v in got.items()} == {
        "missing": ("not_found", "http_404"), "broken": ("site_error", "http_500"), "denied": ("forbidden", "http_403"),
        "empty": ("empty", "no_text"), "zip": ("unsupported", "file_type"), "dns": ("network", "dns"),
        "refused": ("network", "refused")}
    assert "does not exist any more" in got["missing"]["message"] and "could not be found" in got["dns"]["message"]
    assert all(v["item_id"] is None and v["state"] == "idle" for v in got.values())
    assert hub.external_sources.get(aid, ids["missing"])["status"] == "needs_attention"
    # When Canvas cannot be reached either, the problem is this computer's connection, not the site.
    inspector.online = lambda _aid: False
    inspector.inspect(aid, [ids["dns"]])
    offline = inspector.wait(aid, ids["dns"])
    assert offline["reason"] == "offline" and "internet connection" in offline["message"]
    summary = inspector.summary(aid)
    assert summary["failed"] == 7 and summary["not_inspected"] == 0 and summary["notice"]["count"] == 0


@pytest.mark.browser
def test_a_site_outage_keeps_the_page_saved_earlier(hub, account, inspector, fake_sites):
    aid = account["id"]
    sid = add_links(hub, aid, fake_sites, ["syllabus"])["syllabus"]
    inspector.inspect(aid, [sid])
    saved = inspector.wait(aid, sid)
    fake_sites.down.add("course")
    inspector.inspect(aid, [sid])
    view = inspector.wait(aid, sid)
    assert view["outcome"] == "site_error" and view["item_id"] == saved["item_id"] and view["succeeded_at"] == saved["succeeded_at"]
    assert "Office hours" in field(hub, aid, saved["item_id"])
    assert hub.external_sources.get(aid, sid)["status"] == "stale"


@pytest.mark.browser
def test_the_app_never_opens_private_addresses(hub, account, fixture_pkg, fake_sites):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    sid = add_links(hub, aid, fake_sites, ["syllabus"])["syllabus"]
    service = LinkInspector(hub)                         # as the app builds it: no local addresses
    try:
        service.inspect(aid, [sid])
        view = service.wait(aid, sid)
        assert view["outcome"] == "unsafe" and view["actions"] == [] and not fake_sites.seen()
        with pytest.raises(Exception, match="standard|Private|forbidden"):
            service.sign_in(aid, sid)
    finally:
        service.stop()


# ---- sign-in kind 1: a username/password prompt (HTTP Basic) ----------------------------
@pytest.mark.browser
def test_basic_sign_in_from_the_local_form_then_everything_behind_it(hub, account, inspector, fake_sites):
    aid = account["id"]
    ids = add_links(hub, aid, fake_sites, ["course", "notes", "grader"])
    inspector.inspect(aid)
    course = inspector.wait(aid, ids["course"])
    assert (course["outcome"], course["login_method"], course["reason"]) == ("login_required", "basic", "http_basic")
    assert course["actions"][0] == "credentials" and "username and password" in course["message"]
    assert inspector.wait(aid, ids["notes"])["login_method"] == "basic"
    inspector.wait(aid, ids["grader"])
    assert hub.external_sources.get(aid, ids["course"])["status"] == "needs_login"

    # Another link on the same site is read at the same moment; the wrong password is still
    # reported on the link it was typed for.
    inspector.inspect(aid, [ids["notes"]])
    inspector.submit_credentials(aid, ids["course"], fs.BASIC_USER, "not the password")
    wrong = inspector.wait(aid, ids["course"])
    inspector.wait(aid, ids["notes"])
    assert wrong["reason"] == "credentials_rejected" and "did not accept" in wrong["message"]
    assert not inspector._basic                          # a rejected password is not kept or re-sent
    before = len(fake_sites.seen("course"))
    inspector.inspect(aid, [ids["course"]])
    assert inspector.wait(aid, ids["course"])["reason"] == "http_basic"
    assert all(r["authorization"] is None for r in fake_sites.seen("course")[before:])

    inspector.submit_credentials(aid, ids["course"], fs.BASIC_USER, fs.BASIC_PASSWORD)
    done = inspector.wait(aid, ids["course"])
    assert done["outcome"] == "ok" and "recursion" in field(hub, aid, done["item_id"])
    # The PDF behind the same sign-in is read without asking again.
    notes = settled(inspector, aid, ids["notes"])
    assert notes["outcome"] == "ok" and "base case" in field(hub, aid, notes["item_id"])
    # The password went to that one site only, and was not written anywhere.
    assert not [r for r in fake_sites.seen() if r["site"] != "course" and r["authorization"]]
    assert not inspector._credentials_file().exists()
    stored = json.dumps([dict(r) for r in hub.db.query("SELECT * FROM link_inspections")] +
                        [dict(r) for r in hub.db.query("SELECT url,title,error FROM external_sources")])
    assert fs.BASIC_PASSWORD not in stored and "c3R1ZGVudDpiYXNpYyBwYXNz" not in stored


@pytest.mark.browser
def test_a_remembered_basic_sign_in_survives_a_restart_and_can_be_forgotten(hub, account, inspector, fake_sites):
    aid = account["id"]
    sid = add_links(hub, aid, fake_sites, ["course"])["course"]
    inspector.inspect(aid, [sid])
    inspector.wait(aid, sid)
    inspector.submit_credentials(aid, sid, fs.BASIC_USER, fs.BASIC_PASSWORD, remember=True)
    assert inspector.wait(aid, sid)["outcome"] == "ok"
    path = inspector._credentials_file()
    assert path.parent == hub.paths.runtime
    if os.name != "nt":                                  # Windows has no owner-only mode bits
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not str(path).startswith(str(hub.paths.data))  # outside the folder that backups and exports read
    inspector.stop()
    again = LinkInspector(hub, allow_local=True)
    try:
        again.inspect(aid, [sid])
        assert again.wait(aid, sid)["outcome"] == "ok"
        # If the site stops accepting the remembered password, it is deleted rather than sent again and again.
        fs_password, fs.BASIC_PASSWORD = fs.BASIC_PASSWORD, "changed on the site"
        try:
            again.inspect(aid, [sid])
            assert again.wait(aid, sid)["reason"] == "credentials_rejected" and not path.exists()
            again.inspect(aid, [sid])
            assert again.wait(aid, sid)["reason"] == "http_basic"
        finally:
            fs.BASIC_PASSWORD = fs_password
        again.submit_credentials(aid, sid, fs.BASIC_USER, fs.BASIC_PASSWORD, remember=True)
        assert again.wait(aid, sid)["outcome"] == "ok" and path.exists()
        assert again.forget_sign_ins(aid) == {"forgotten": True}
        assert not path.exists() and not again._profile(again._owner(aid)).exists()
        again.inspect(aid, [sid])
        assert again.wait(aid, sid)["reason"] == "http_basic"
        # The page saved earlier is kept.
        assert again.view(again._source(aid, sid))["item_id"]
    finally:
        again.stop()


@pytest.mark.browser
def test_a_password_is_only_accepted_for_a_link_that_asked_for_one(hub, account, inspector, fake_sites):
    aid = account["id"]
    ids = add_links(hub, aid, fake_sites, ["syllabus", "course"])
    with pytest.raises(Exception, match="not waiting for a username"):
        inspector.submit_credentials(aid, ids["syllabus"], "a", "b")
    inspector.inspect(aid)
    inspector.wait(aid, ids["syllabus"])
    inspector.wait(aid, ids["course"])
    with pytest.raises(Exception, match="not waiting for a username"):
        inspector.submit_credentials(aid, ids["syllabus"], "a", "b")
    for user, password in (("", "x"), ("a:b", "x"), ("a", ""), ("a\n", "x")):
        with pytest.raises(Exception, match="Enter the username"):
            inspector.submit_credentials(aid, ids["course"], user, password)
    # Outside tests a password is never sent over plain http.
    inspector.allow_local = False
    with pytest.raises(Exception, match="only sends a password over https"):
        inspector.submit_credentials(aid, ids["course"], fs.BASIC_USER, fs.BASIC_PASSWORD)


# ---- sign-in kind 2: redirect to a separate identity provider ---------------------------
def sso_sign_in(page):
    page.fill("input[name=username]", fs.SSO_USER)
    page.fill("input[name=password]", fs.SSO_PASSWORD)
    page.click("button")
    page.fill("input[name=code]", fs.SSO_CODE)
    page.click("button")
    page.wait_for_load_state("domcontentloaded")


@pytest.mark.browser
def test_single_sign_on_in_the_window_then_the_read_continues_by_itself(hub, account, inspector, fake_sites):
    aid = account["id"]
    ids = add_links(hub, aid, fake_sites, ["grader", "grades", "forum"])
    inspector.inspect(aid)
    grader = inspector.wait(aid, ids["grader"])
    idp = fake_sites.origin("idp").split("//")[1]
    assert (grader["outcome"], grader["login_method"], grader["login_host"]) == ("login_required", "sso", idp)
    assert grader["actions"][0] == "sign_in" and "single sign-on" in grader["message"]
    assert "ticket" not in (grader["final_url"] or "") and "service=" not in (grader["final_url"] or "")
    inspector.wait(aid, ids["grades"])
    inspector.wait(aid, ids["forum"])

    opened = inspector.sign_in(aid, ids["grader"])
    assert opened["state"] == "waiting_login" and opened["actions"] == ["continue", "cancel"]
    with pytest.raises(Exception, match="already open"):
        inspector.sign_in(aid, ids["forum"])             # one sign-in window at a time
    assert inspector.summary(aid)["waiting_login"]["source_id"] == ids["grader"]
    in_sign_in_window(inspector, sso_sign_in)            # the user signs in; nothing else is pressed
    done = settled(inspector, aid, ids["grader"])
    assert done["outcome"] == "ok" and "Maze Solver" in field(hub, aid, done["item_id"])
    assert inspector._login is None
    # The other link behind the same sign-in is read too; the unrelated forum still needs its own.
    grades = settled(inspector, aid, ids["grades"])
    assert grades["outcome"] == "ok" and "87 out of 100" in field(hub, aid, grades["item_id"])
    assert settled(inspector, aid, ids["forum"])["outcome"] == "login_required"
    # The grader keeps its session in a session cookie. It must survive the browser being relaunched
    # (as happens when the visible sign-in window is swapped for the background browser).
    relaunched = []
    inspector._q.put(lambda: (inspector._close_context(), relaunched.append(True)))
    inspector.inspect(aid, [ids["grader"]])
    assert inspector.wait(aid, ids["grader"])["outcome"] == "ok" and relaunched
    # When the site signs the user out, the link says so again instead of saving a sign-in page.
    fake_sites.expire_sessions()
    inspector.inspect(aid, [ids["grader"]])
    expired = inspector.wait(aid, ids["grader"])
    assert expired["outcome"] == "login_required" and expired["item_id"] == done["item_id"]
    assert "Maze Solver" in field(hub, aid, done["item_id"])


@pytest.mark.browser
def test_continue_and_cancel_in_the_sign_in_window(hub, account, inspector, fake_sites):
    aid = account["id"]
    sid = add_links(hub, aid, fake_sites, ["grader"])["grader"]
    other = add_links(hub, aid, fake_sites, ["syllabus"])["syllabus"]
    inspector.inspect(aid, [sid])
    inspector.wait(aid, sid)
    inspector.sign_in(aid, sid)
    in_sign_in_window(inspector, lambda page: page.url)
    # Pressing Continue too early changes nothing except the message.
    inspector.continue_sign_in(aid, sid)
    deadline = time.monotonic() + 15
    while "still asks for a sign-in" not in (inspector.view(inspector._source(aid, sid))["message"] or ""):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert inspector.view(inspector._source(aid, sid))["state"] == "waiting_login"
    # Other links wait instead of opening tabs in the window the user is typing in.
    inspector.inspect(aid, [other])
    time.sleep(1)
    assert inspector.view(inspector._source(aid, other))["state"] == "queued"
    inspector.cancel(aid, sid)
    cancelled = settled(inspector, aid, sid)
    assert cancelled["outcome"] == "login_required" and "cancelled" in cancelled["message"].lower()
    assert inspector.wait(aid, other)["outcome"] == "ok"
    with pytest.raises(Exception, match="No sign-in window"):
        inspector.continue_sign_in(aid, sid)


@pytest.mark.browser
def test_closing_the_sign_in_window_early_is_explained(hub, account, inspector, fake_sites):
    aid = account["id"]
    sid = add_links(hub, aid, fake_sites, ["grader"])["grader"]
    inspector.inspect(aid, [sid])
    inspector.wait(aid, sid)
    inspector.sign_in(aid, sid)
    in_sign_in_window(inspector, lambda page: page.close())
    view = settled(inspector, aid, sid)
    assert view["outcome"] == "login_required" and "closed before" in view["message"]


# ---- sign-in kind 3: the site's own form, page drawn by a script ------------------------
@pytest.mark.browser
def test_a_site_with_its_own_form_and_a_script_rendered_page(hub, account, inspector, fake_sites):
    aid = account["id"]
    sid = add_links(hub, aid, fake_sites, ["forum"])["forum"]
    inspector.inspect(aid, [sid])
    view = inspector.wait(aid, sid)
    assert (view["outcome"], view["login_method"]) == ("login_required", "form") and "own sign-in form" in view["message"]
    inspector.sign_in(aid, sid)

    def log_in(page):
        page.fill("input[name=email]", fs.FORUM_EMAIL)
        page.fill("input[name=password]", fs.FORUM_PASSWORD)
        page.click("button")
    in_sign_in_window(inspector, log_in)
    done = settled(inspector, aid, sid)
    text = field(hub, aid, done["item_id"])
    assert done["outcome"] == "ok" and "Midterm logistics" in text and "Loading" not in text
    # Canvas Hub itself typed nothing: the only form post is the user's own.
    posts = [r["path"] for r in fake_sites.seen("forum", "POST")]
    assert posts.count("/account/login") == 1 and set(posts) <= {"/account/login", "/api/feed"}
    assert fs.FORUM_PASSWORD not in json.dumps([dict(r) for r in hub.db.query("SELECT * FROM link_inspections")])


# ---- the assistant ------------------------------------------------------------------------
@pytest.mark.browser
def test_the_assistant_can_inspect_but_only_the_user_can_sign_in(hub, account, inspector, fake_sites):
    aid = account["id"]
    ids = add_links(hub, aid, fake_sites, ["syllabus", "grader"])
    api = HubAPI(hub)
    api.link_inspector = inspector
    ctx = ToolContext(api=api, account_id=aid)
    text, error = run_tool(ctx, "inspect_external_link", {"source_id": ids["syllabus"]}, {"read"})
    assert error and "not available" in text             # opening a site needs the collect permission
    text, error = run_tool(ctx, "inspect_external_link", {"source_id": ids["syllabus"]}, {"read", "collect"})
    result = json.loads(text)
    assert not error and result["outcome"] == "ok" and result["item_id"] and "get_item" in result["next_step"]
    result = json.loads(run_tool(ctx, "inspect_external_link", {"source_id": ids["grader"]}, {"read", "collect"})[0])
    assert result["outcome"] == "login_required" and result["login_method"] == "sso"
    assert "Never ask for a password" in result["user_action_needed"]
    listed = json.loads(run_tool(ctx, "list_external_sources", {}, {"read"})[0])
    assert {s["id"]: s["inspection"]["outcome"] for s in listed["sources"]} == {ids["syllabus"]: "ok", ids["grader"]: "login_required"}
    # Another process (an MCP client) has no browser of its own.
    text, error = run_tool(ToolContext(api=HubAPI(hub), account_id=aid), "inspect_external_link",
                           {"source_id": ids["grader"]}, {"read", "collect"})
    assert error and json.loads(text)["error"] == "app_required"
