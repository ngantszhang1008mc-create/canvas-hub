"""The external-links pages in a real browser, against synthetic sites."""
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from canvas_hub.boundary import HubAPI
from canvas_hub.testing import fake_sites as fs
from canvas_hub.testing.link_demo import add_link, add_links
from canvas_hub.web.app import create_app

from .conftest import in_sign_in_window
from .test_link_inspection import sso_sign_in

pytestmark = pytest.mark.browser


@contextmanager
def app(hub, inspector):
    import uvicorn
    api = HubAPI(hub)
    api.link_inspector = inspector
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = "t" * 40
    server = uvicorn.Server(uvicorn.Config(create_app(api, token=token, port=port), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", token
    finally:
        server.should_exit = True


@contextmanager
def browser_page(base, token, width=1280):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        page.problems, page.opened = [], []
        # This test app has no Canvas browser, so the Canvas sign-in status request answers 503; that is unrelated.
        page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" and "503" not in m.text else None)
        page.on("pageerror", lambda e: page.problems.append(str(e)))
        context.on("page", lambda p: page.opened.append(p.url) if p is not page else None)
        page.goto(f"{base}/?token={token}")
        try:
            yield page
        finally:
            browser.close()


def row(page, title):
    return page.locator(".link-row", has=page.get_by_role("link", name=title, exact=True))


def test_found_links_are_announced_and_one_press_inspects_them_all(hub, account, inspector, fake_sites):
    from playwright.sync_api import expect
    ids = add_links(hub, account["id"], fake_sites)
    with app(hub, inspector) as (base, token), browser_page(base, token) as page:
        notice = page.get_by_role("complementary", name="External links", exact=True)
        expect(notice).to_contain_text("7 new external links found")
        expect(notice).to_contain_text("COMP course website")
        expect(page.locator('#nav a[data-view="links"] .nav-count')).to_have_text("7")
        assert not fake_sites.seen()                     # announcing is not opening
        notice.get_by_role("button", name="Inspect all 7", exact=True).click()
        expect(page).to_have_url(f"{base}/#/links")
        expect(row(page, "Public syllabus").get_by_role("status")).to_have_text("Read and saved", timeout=30000)
        expect(row(page, "Old tutorial page").get_by_role("status")).to_have_text("Page not found", timeout=30000)
        expect(row(page, "Old tutorial page")).to_contain_text("does not exist any more")
        expect(row(page, "COMP course website").get_by_role("status")).to_have_text("Sign-in needed", timeout=30000)
        expect(row(page, "Grader: grades").get_by_role("status")).to_have_text("Sign-in needed", timeout=30000)
        expect(row(page, "Class forum").get_by_role("status")).to_have_text("Sign-in needed", timeout=30000)
        expect(notice).to_be_hidden()
        # Each kind of sign-in gets its own, single entrance.
        expect(row(page, "COMP course website").get_by_label("Password", exact=True)).to_be_visible()
        expect(row(page, "Lecture notes (PDF)")).to_contain_text("Needs the same sign-in as “COMP course website”")
        expect(row(page, "Lecture notes (PDF)").get_by_label("Password", exact=True)).to_have_count(0)
        expect(row(page, "Grader: assignments").get_by_role("button", name="Open sign-in window", exact=True)).to_be_visible()
        expect(row(page, "Grader: assignments")).to_contain_text("single sign-on")
        expect(row(page, "Grader: grades").get_by_role("button", name="Open sign-in window", exact=True)).to_have_count(0)
        expect(row(page, "Class forum").get_by_role("button", name="Open sign-in window", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Try failed links again (1)", exact=True)).to_be_visible()
        # Nothing opened another tab or window of the app, and the page logged no errors.
        assert page.opened == [] and page.problems == []
        # Every control has a name a screen reader can announce.
        unnamed = page.evaluate("""() => [...document.querySelectorAll('#main button, #main input, #main select, #main a')].filter(e =>
            !(e.innerText || '').trim() && !e.getAttribute('aria-label') && !e.labels?.length && !e.title).map(e => e.outerHTML.slice(0, 80))""")
        assert unnamed == []
        page.reload()
        expect(row(page, "Public syllabus").get_by_role("status")).to_have_text("Read and saved")
        expect(notice).to_be_hidden()
    assert ids


def test_inspect_all_sends_a_long_list_in_requests_the_server_accepts(hub, account, inspector, fake_sites):
    import json
    from playwright.sync_api import expect
    ids = [add_link(hub, account["id"], fake_sites.url("course", f"/missing?page={n}"), f"Page {n}") for n in range(250)]
    with app(hub, inspector) as (base, token), browser_page(base, token) as page:
        sent = []

        def record(route):
            sent.append(json.loads(route.request.post_data)["source_ids"])
            route.fulfill(json={"queued": sent[-1]})
        page.route("**/link-inspection/inspect", record)
        notice = page.get_by_role("complementary", name="External links", exact=True)
        notice.get_by_role("button", name="Inspect all 250", exact=True).click()
        expect(page).to_have_url(f"{base}/#/links")
        assert [len(batch) for batch in sent] == [200, 50] and sorted(sum(sent, [])) == sorted(ids)


def test_not_now_keeps_quiet_until_another_link_is_found(hub, account, inspector, fake_sites):
    from playwright.sync_api import expect
    add_links(hub, account["id"], fake_sites, ["syllabus"])
    with app(hub, inspector) as (base, token), browser_page(base, token) as page:
        notice = page.get_by_role("complementary", name="External links", exact=True)
        expect(notice).to_contain_text("New external link found")
        notice.get_by_role("button", name="Not now", exact=True).click()
        expect(notice).to_be_hidden()
        page.reload()
        page.goto(f"{base}/#/links")
        expect(row(page, "Public syllabus").get_by_role("status")).to_have_text("Not inspected yet")
        expect(notice).to_be_hidden()
        assert not fake_sites.seen()
        add_links(hub, account["id"], fake_sites, ["missing"])
        page.evaluate("window.dispatchEvent(new Event('links-updated'))")
        expect(notice).to_contain_text("New external link found")
        expect(notice).to_contain_text("Old tutorial page")


def test_username_and_password_sign_in_from_the_page(hub, account, inspector, fake_sites):
    from playwright.sync_api import expect
    add_links(hub, account["id"], fake_sites, ["course", "notes"])
    with app(hub, inspector) as (base, token), browser_page(base, token) as page:
        page.goto(f"{base}/#/course/4242")
        card = page.get_by_role("region", name="External links", exact=True)
        card.get_by_role("button", name="Inspect all (2)", exact=True).click()
        course = row(page, "COMP course website")
        expect(course.get_by_role("status")).to_have_text("Sign-in needed", timeout=30000)
        site = fake_sites.origin("course").split("//")[1]
        expect(course).to_contain_text(f"{site} asks for a username and password")
        form = course.get_by_role("form", name=f"Sign in to {site}", exact=True)
        form.get_by_label(f"Username for {site}", exact=True).fill(fs.BASIC_USER)
        form.get_by_label("Password", exact=True).fill("wrong")
        form.get_by_role("button", name="Sign in and continue", exact=True).click()
        expect(course).to_contain_text("did not accept that username and password", timeout=30000)
        # A background refresh must not wipe what is being typed.
        form.get_by_label(f"Username for {site}", exact=True).fill(fs.BASIC_USER)
        page.evaluate("window.dispatchEvent(new Event('links-updated'))")
        page.wait_for_timeout(1500)
        expect(form.get_by_label(f"Username for {site}", exact=True)).to_have_value(fs.BASIC_USER)
        form.get_by_label("Password", exact=True).fill(fs.BASIC_PASSWORD)
        form.get_by_role("button", name="Sign in and continue", exact=True).click()
        expect(course.get_by_role("status")).to_have_text("Read and saved", timeout=30000)
        expect(row(page, "Lecture notes (PDF)").get_by_role("status")).to_have_text("Read and saved", timeout=30000)
        expect(course.get_by_label("Password", exact=True)).to_have_count(0)
        assert fs.BASIC_PASSWORD not in page.content()
        course.get_by_role("link", name="View saved page", exact=True).click()
        expect(page.get_by_role("heading", name="Page text saved by Canvas Hub", exact=True)).to_be_visible()
        expect(page.locator("#main")).to_contain_text("Lecture 7 covers recursion")
        assert page.opened == [] and page.problems == []


def test_sign_in_window_flow_with_clear_steps(hub, account, inspector, fake_sites):
    from playwright.sync_api import expect
    add_links(hub, account["id"], fake_sites, ["grader", "grades"])
    with app(hub, inspector) as (base, token), browser_page(base, token) as page:
        page.goto(f"{base}/#/links")
        page.get_by_role("button", name="Inspect all (2)", exact=True).click()
        grader = row(page, "Grader: assignments")
        grader.get_by_role("button", name="Open sign-in window", exact=True).click(timeout=30000)
        expect(grader.get_by_role("status")).to_have_text("Waiting for your sign-in")
        expect(grader.locator(".link-steps li")).to_have_count(3)
        expect(grader).to_contain_text("Canvas Hub cannot see what you type")
        expect(grader.get_by_role("button", name="I've signed in — continue", exact=True)).to_be_visible()
        expect(grader.get_by_role("button", name="Cancel", exact=True)).to_be_visible()
        notice = page.get_by_role("complementary", name="External links", exact=True)
        expect(notice).to_contain_text("Sign-in window open", timeout=10000)
        in_sign_in_window(inspector, sso_sign_in)        # the user signs in; nothing is pressed in the app
        expect(grader.get_by_role("status")).to_have_text("Read and saved", timeout=30000)
        expect(row(page, "Grader: grades").get_by_role("status")).to_have_text("Read and saved", timeout=30000)
        expect(notice).to_be_hidden(timeout=10000)
        expect(page.locator(".link-counts")).to_contain_text("2 read and saved")
        assert page.opened == [] and page.problems == []


def test_cancel_and_narrow_screens(hub, account, inspector, fake_sites):
    from playwright.sync_api import expect
    add_links(hub, account["id"], fake_sites, ["forum", "course"])
    with app(hub, inspector) as (base, token), browser_page(base, token, width=390) as page:
        page.goto(f"{base}/#/links")
        page.get_by_role("button", name="Inspect all (2)", exact=True).click()
        forum = row(page, "Class forum")
        forum.get_by_role("button", name="Open sign-in window", exact=True).click(timeout=30000)
        expect(forum.get_by_role("status")).to_have_text("Waiting for your sign-in")
        # A second sign-in window is refused with a plain explanation, not opened on top.
        forum.get_by_role("button", name="Cancel", exact=True).click()
        expect(forum.get_by_role("status")).to_have_text("Sign-in needed", timeout=15000)
        expect(forum).to_contain_text("Sign-in cancelled")
        expect(row(page, "COMP course website").get_by_label("Password", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.opened == [] and page.problems == []


def test_adding_a_link_by_hand(hub, account, inspector):
    from playwright.sync_api import expect
    with app(hub, inspector) as (base, token), browser_page(base, token) as page:
        page.goto(f"{base}/#/links")
        expect(page.locator("#main")).to_contain_text("No external links yet")
        page.get_by_label("Link address", exact=True).fill("http://192.168.1.1/admin")
        page.get_by_role("button", name="Add link", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("Private/internal external targets are forbidden")
        page.get_by_label("Link address", exact=True).fill("https://course.example.edu/comp1023/")
        page.get_by_label("Link name", exact=True).fill("COMP1023 course website")
        page.get_by_label("Course", exact=True).select_option("4242")
        page.get_by_role("button", name="Add link", exact=True).click()
        added = row(page, "COMP1023 course website")
        expect(added.get_by_role("status")).to_have_text("Not inspected yet")
        expect(added.get_by_role("button", name="Inspect", exact=True)).to_be_visible()
        # The only logged error is the browser noting the refused private address.
        assert len(page.problems) == 1 and "400" in page.problems[0]
