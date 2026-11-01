"""Acceptance tests for the interactive components in a real browser:
score-model toggles with live totals that persist and read back (#1), and
the artifact block's Copy and Download (#8/#9)."""

import socket
import threading
import time
from contextlib import contextmanager

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.boundary import HubAPI
from canvas_hub.web.app import create_app

from .fake_llm import FakeLLM
from .test_components import HMAW
from .test_dates_courses import FORTNIGHTLY_EVENT, ICS

pytestmark = pytest.mark.browser

SPEC_TEXT = "SPEC: Canvas Hub components to add\n" + "\n".join(f"{i}. item {i} — details" for i in range(1, 60))


@contextmanager
def serve(hub, plan, **fake_options):
    """The app on a free port, with a scripted model that calls the tools in ``plan``."""
    import uvicorn
    api = HubAPI(hub)
    fake = FakeLLM(plan, **fake_options)
    api.assistant = AssistantService(hub, api)
    api.assistant.update_config({"preset": "custom", "base_url": fake.url + "/v1", "model": "m"})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tok = "t" * 40
    srv = uvicorn.Server(uvicorn.Config(create_app(api, token=tok, port=port), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{port}", tok, api
    finally:
        srv.should_exit = True
        fake.stop()


@pytest.fixture
def app_server(hub, account):
    with serve(hub, [("show_score_model", {**HMAW, "pin": True}),
                     ("show_artifact", {"title": "Component spec", "content": SPEC_TEXT, "language": "markdown",
                                        "filename": "canvas-hub-spec.md"})]) as s:
        yield s


def test_score_model_and_artifact_in_the_browser(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        pg.locator(".composer textarea").fill("Track HMAW1905B and give me the spec")
        pg.get_by_role("button", name="Send").click()
        score = pg.locator(".cmp-score")
        score.wait_for(timeout=20000)
        big = score.locator(".sl-big .num")
        assert big.inner_text() == "95"
        assert "Community Meeting 1" in score.inner_text() and score.locator("tr", has_text="Community Meeting 1").locator("td:nth-child(3) .pill").inner_text() == "Lost"
        dl = score.locator(".sl-kv")
        assert "Booked15" in dl.inner_text().replace("\n", "") and "Ceiling95" in dl.inner_text().replace("\n", "")

        score.get_by_label("Include Nest Posts").uncheck()
        expect(big).to_have_text("85")
        assert "PASS" in score.locator(".sl-side .pill").inner_text()
        score.get_by_label("Include FYES").uncheck()
        expect(big).to_have_text("80")
        assert score.locator(".sl-side .pill").inner_text() == "PASS · +0"
        expect(score.locator(".save-state")).to_contain_text("Saved", timeout=10000)

        # survives a reload, and the assistant reads back which lines are ticked
        pg.reload()
        score = pg.locator(".cmp-score")
        expect(score.locator(".sl-big .num")).to_have_text("80")
        assert not score.get_by_label("Include Nest Posts").is_checked()
        cid = hub.db.one("SELECT id FROM components WHERE account_id=?", (account["id"],))["id"]
        back = hub.components.describe(account["id"], cid)
        assert {x["id"] for x in back["lines"] if x["status"] == "pending" and not x["included"]} == {"nest", "fyes"}
        assert back["totals"]["projected"] == 80 and back["totals"]["margin"] == 0

        # copy the model as CSV through the universal toolbar
        own = score.locator(":scope > .cmp-bar")
        own.get_by_role("button", name="Copy ▾").click()
        own.get_by_role("menuitem", name="Copy as CSV").click()
        assert "Nest Posts,pending,no" in pg.evaluate("navigator.clipboard.readText()")

        # artifact block: full text, Copy and Download
        art = pg.locator(".cmp-artifact")
        assert "60 lines" in art.locator(".art-meta").inner_text()
        assert art.locator(".art-lines li").count() == 24                 # collapsed preview
        art.get_by_role("button", name="Expand full (60 lines)").click()
        assert art.locator(".art-lines li").count() == 60
        art.get_by_role("button", name="Copy", exact=True).click()
        clipboard = pg.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n")
        assert clipboard == SPEC_TEXT
        with pg.expect_download() as d:
            art.get_by_role("button", name="Download", exact=True).click()
        assert d.value.suggested_filename == "canvas-hub-spec.md"
        with open(d.value.path(), encoding="utf-8") as fh:
            assert fh.read() == SPEC_TEXT

        # the pinned model is on the Trackers page with the same state
        pg.goto(f"{base}/#/trackers")
        expect(pg.locator(".cmp-score .sl-big .num")).to_have_text("80")
        assert not errors, errors
        b.close()


def test_edits_made_while_a_save_is_in_flight_are_kept(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    v = hub.components.create(account["id"], "score_model", HMAW)
    hub.components.pin(account["id"], v["id"], True)
    real, calls = hub.components.set_state, []

    def slow(*a, **k):                  # the first save answers late
        calls.append(1)
        if len(calls) == 1:
            time.sleep(1.5)
        return real(*a, **k)
    hub.components.set_state = slow
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/trackers")
        score = pg.locator(".cmp-score")
        expect(score.locator(".sl-big .num")).to_have_text("95")
        score.get_by_label("Include Nest Posts").uncheck()
        pg.wait_for_timeout(600)                      # debounce fired; the first save is now in flight
        score.get_by_label("Include FYES").uncheck()
        expect(score.locator(".sl-big .num")).to_have_text("80")
        expect(score.locator(".save-state")).to_contain_text("Saved", timeout=10000)
        expect(score.locator(".sl-big .num")).to_have_text("80")
        b.close()
    hub.components.set_state = real
    st = hub.components.get(account["id"], v["id"])["state"]
    assert st["include"] == {"nest": False, "fyes": False}


def test_quick_replies_after_leaving_and_reopening_a_chat(hub, account):
    # Cached components are reused across chat views; their buttons must send into the view on screen.
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, [("show_choices", {"question": "Which week?", "options": ["Week 9", "Week 10"]})]) as (base, tok, _), \
            sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        pg.locator(".composer textarea").fill("Plan a week")
        pg.get_by_role("button", name="Send").click()
        expect(pg.locator(".cmp-choices")).to_be_visible(timeout=20000)
        expect(pg.get_by_role("button", name="Send")).to_be_enabled(timeout=20000)
        chat = pg.url
        pg.goto(f"{base}/#/deadlines")
        pg.goto(chat)
        pg.locator(".cmp-choices").get_by_role("button", name="Week 10").click()
        expect(pg.locator(".msg.user .bubble", has_text="Week 10")).to_be_visible(timeout=10000)
        b.close()


def test_a_page_that_loads_late_does_not_replace_the_next_one(hub, account):
    # Leaving a page before its data arrives must not let it draw over the page opened after it.
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, []) as (base, tok, api), sync_playwright() as p:
        real = api.deadlines

        def slow(*a, **k):
            time.sleep(1.5)
            return real(*a, **k)
        api.deadlines = slow
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(f"{base}/?token={tok}")
        expect(pg.locator("h1")).to_have_text("Overall")    # the app has loaded the account
        with pg.expect_request(lambda r: "/deadlines?" in r.url):
            pg.goto(f"{base}/#/deadlines")
        pg.goto(f"{base}/#/notes")
        expect(pg.locator("h1")).to_have_text("Notes & tasks")
        pg.wait_for_timeout(2500)                        # the deadlines answer has arrived by now
        expect(pg.locator("h1")).to_have_text("Notes & tasks")
        assert pg.locator("#main").count() == 1
        b.close()


def test_a_second_tick_during_a_save_keeps_todos_finished_elsewhere(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    aid = account["id"]
    v = hub.components.create(aid, "checklist", {"title": "Week", "sync_todos": True, "sections": [
        {"id": "s", "rows": [{"id": "r1", "label": "One"}, {"id": "r2", "label": "Two"}, {"id": "r3", "label": "Three"}]}]})
    hub.components.pin(aid, v["id"], True)
    links = v["state"]["links"]
    real, calls = hub.components.set_state, []

    def slow(*a, **k):                  # the first save answers late
        calls.append(1)
        if len(calls) == 1:
            time.sleep(1.5)
        return real(*a, **k)
    hub.components.set_state = slow
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/trackers")
        cl = pg.locator(".cmp-checklist")
        expect(cl.get_by_text("0/3 done")).to_be_visible()
        hub.annotations.update(aid, links["r1"], task_state="done")      # finished under Notes & tasks meanwhile
        cl.get_by_role("checkbox", name="Two").check()
        pg.wait_for_timeout(600)                                         # the first save is now in flight
        cl.get_by_role("checkbox", name="Three").check()
        expect(cl.locator(".save-state")).to_contain_text("Saved", timeout=10000)
        b.close()
    hub.components.set_state = real
    assert hub.annotations.get(aid, links["r1"])["task_state"] == "done"    # not reopened by the queued save
    assert hub.components.get(aid, v["id"])["state"]["done"] == {"r1": True, "r2": True, "r3": True}


def test_a_chat_shows_changes_made_on_trackers_when_reopened(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        pg.locator(".composer textarea").fill("Track HMAW1905B")
        pg.get_by_role("button", name="Send").click()
        expect(pg.locator(".cmp-score .sl-big .num")).to_have_text("95", timeout=20000)
        expect(pg.locator(".cmp-artifact")).to_be_visible(timeout=20000)
        chat = pg.url
        pg.goto(f"{base}/#/trackers")
        score = pg.locator(".cmp-score")
        score.get_by_label("Include Nest Posts").uncheck()
        expect(score.locator(".save-state")).to_contain_text("Saved", timeout=10000)
        pg.goto(chat)                                   # the chat's copy of the model is cached from before
        expect(pg.locator(".cmp-score .sl-big .num")).to_have_text("85", timeout=10000)
        b.close()


def test_timetable_import_shows_skipped_recurrences(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    ics = ICS.replace("END:VCALENDAR", FORTNIGHTLY_EVENT + "END:VCALENDAR")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/planner")
        upload = pg.locator('input[type="file"][accept=".ics,text/calendar"]')
        upload.set_input_files({"name": "classes.ics", "mimeType": "text/calendar", "buffer": ics.encode("utf-8")})
        expect(pg.locator("#toasts")).to_contain_text("Found 2 weekly class time(s), added 2. Skipped 1 event(s): Fortnightly lab")
        expect(pg.get_by_text("COMP2711H Lecture").first).to_be_visible()
        assert {c["title"] for c in hub.planner.list_classes(account["id"])} == {"COMP2711H Lecture"}

        only_fortnightly = "BEGIN:VCALENDAR\n" + FORTNIGHTLY_EVENT + "END:VCALENDAR\n"
        upload.set_input_files({"name": "labs.ics", "mimeType": "text/calendar", "buffer": only_fortnightly.encode("utf-8")})
        expect(pg.locator("#toasts")).to_contain_text("no class times imported: only events that repeat every week are supported")
        assert len(hub.planner.list_classes(account["id"])) == 2
        b.close()


def test_an_edit_made_while_undo_is_in_flight_is_kept(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    aid = account["id"]
    v = hub.components.create(aid, "checklist", {"title": "Week", "sections": [
        {"id": "s", "rows": [{"id": "r1", "label": "One"}, {"id": "r2", "label": "Two"}]}]})
    hub.components.pin(aid, v["id"], True)
    hub.components.set_state(aid, v["id"], {"done": {"r1": True}})            # something to undo
    real = hub.components.undo

    def slow_undo(*a, **k):
        time.sleep(1.5)
        return real(*a, **k)
    hub.components.undo = slow_undo
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/trackers")
        cl = pg.locator(".cmp-checklist")
        expect(cl.get_by_text("1/2 done")).to_be_visible()
        cl.get_by_role("button", name="↶ Undo").click()
        pg.wait_for_timeout(300)                                         # the undo is in flight
        cl.get_by_role("checkbox", name="Two").check()
        expect(cl.locator(".save-state")).to_contain_text("Saved", timeout=10000)
        b.close()
    hub.components.undo = real
    assert hub.components.get(aid, v["id"])["state"]["done"] == {"r2": True}   # r1 undone, r2 kept


def test_two_quick_undos_leave_the_page_matching_the_server(app_server, hub, account):
    from playwright.sync_api import expect, sync_playwright
    base, tok, _ = app_server
    aid = account["id"]
    v = hub.components.create(aid, "checklist", {"title": "Week", "sections": [
        {"id": "s", "rows": [{"id": "r1", "label": "One"}, {"id": "r2", "label": "Two"}]}]})
    hub.components.pin(aid, v["id"], True)
    v = hub.components.set_state(aid, v["id"], {"done": {"r1": True}})       # tick, then untick
    hub.components.set_state(aid, v["id"], {"done": {}})
    real = hub.components.undo

    def slow_undo(*a, **k):
        time.sleep(0.8)
        return real(*a, **k)
    hub.components.undo = slow_undo
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/trackers")
        cl = pg.locator(".cmp-checklist")
        expect(cl.get_by_text("0/2 done")).to_be_visible()
        undo = cl.get_by_role("button", name="↶ Undo")
        undo.click()
        undo.click()                                                     # before the first answer arrives
        expect(cl.locator(".save-state")).to_have_text("Undone", timeout=10000)
        pg.wait_for_timeout(1500)                                        # both answers are in
        shown = cl.get_by_role("checkbox", name="One").is_checked()
        b.close()
    hub.components.undo = real
    saved = hub.components.get(aid, v["id"])["state"]["done"]
    assert shown == bool(saved.get("r1")), (shown, saved)                # the page shows what is saved
