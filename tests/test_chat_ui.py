"""The assistant chat in a real browser: replies appear word by word, and the focus mode."""

import time

import pytest

from .test_components_ui import serve

pytestmark = pytest.mark.browser

ANSWER = ("Your proposal is due on Friday at 23:59. Start with the outline today, read chapter four tomorrow, "
          "and leave Thursday evening for the final check.")


@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole-reply"])
def test_a_reply_appears_word_by_word(hub, account, streaming):
    # Streamed replies flow in as they are written; a provider that sends the whole reply at once is typed out too.
    from playwright.sync_api import expect, sync_playwright
    opts = {"delay": 0.06} if streaming else {"openai_streaming": False}
    with serve(hub, [], answer=ANSWER, **opts) as (base, tok, _), sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        pg.locator(".composer textarea").fill("When is the proposal due?")
        pg.get_by_role("button", name="Send").click()
        reply = pg.locator(".msg.assistant").last
        seen = []
        for _ in range(200):
            if reply.count():
                t = reply.inner_text().strip()
                if t and (not seen or t != seen[-1]):
                    seen.append(t)
                if t == ANSWER and not pg.locator(".msg.assistant.live").count():
                    break
            pg.wait_for_timeout(25)
        expect(pg.locator(".msg.assistant").last).to_have_text(ANSWER)
        b.close()
    assert not errors, errors
    partial = [t for t in seen if t != ANSWER]
    assert len(partial) >= 4, seen                               # it was written out in steps
    assert all(ANSWER.startswith(t) for t in partial)            # always a growing prefix, never jumbled
    lengths = [len(t) for t in seen]
    assert lengths == sorted(lengths)                            # never jumps back


def test_focus_mode_shows_just_the_chat(hub, account):
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, [], answer=ANSWER) as (base, tok, _), sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        chat = pg.locator(".chat")
        expect(chat).to_be_visible()
        before = chat.bounding_box()
        pg.get_by_role("button", name="⤢ Focus").click()
        for hidden in ("#sidebar", "#topbar", ".main > .page-head"):
            expect(pg.locator(hidden)).to_be_hidden()
        for shown in (".composer textarea", ".chat-pick", ".tabs", ".conv-list"):
            expect(pg.locator(shown).first).to_be_visible()
        new_chat = pg.locator(".conv-list").get_by_role("link", name="+ New chat")      # on top of the chat list
        expect(new_chat).to_be_visible()
        after = chat.bounding_box()
        assert after["height"] > before["height"] + 80 and after["width"] > before["width"] + 200, (before, after)
        pg.locator(".composer textarea").fill("Quick question")      # still works as a chat
        pg.get_by_role("button", name="Send").click()
        expect(pg.locator(".msg.assistant").last).to_have_text(ANSWER, timeout=15000)
        conv = pg.locator(".conv-list .conv")
        expect(conv).to_have_count(1)                                   # the chat list switches conversations
        new_chat.click()
        expect(pg.locator(".msg")).to_have_count(0)
        expect(pg.locator("#sidebar")).to_be_hidden()                   # still in focus mode
        conv.click()
        expect(pg.locator(".msg.assistant").last).to_have_text(ANSWER)
        pg.reload()                                                     # remembered in this browser
        expect(pg.locator("#sidebar")).to_be_hidden()
        pg.keyboard.press("Escape")                                     # Esc leaves it
        expect(pg.locator("#sidebar")).to_be_visible()
        pg.get_by_role("button", name="⤢ Focus").click()
        pg.goto(f"{base}/#/deadlines")                                  # other pages are never in focus mode
        expect(pg.locator("#sidebar")).to_be_visible()
        b.close()
    assert not errors, errors


@pytest.mark.parametrize("width,height", [(1280, 800), (390, 844)], ids=["desktop", "phone"])
def test_scrolling_the_page_grows_the_chat_under_the_tabs(hub, account, width, height):
    # The message box stays at the bottom of the window; scrolling moves the heading away and the chat grows
    # into its space, up to the tabs, which stay on screen.
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, [], answer=ANSWER) as (base, tok, _), sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": height})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        pg.locator(".composer textarea").fill("When is the proposal due?")
        pg.get_by_role("button", name="Send").click()
        expect(pg.locator(".msg.assistant").last).to_have_text(ANSWER, timeout=15000)

        def box(sel):
            return pg.locator(sel).first.bounding_box()

        def at(y):
            pg.evaluate("y => window.scrollTo(0, y)", y)
            pg.wait_for_timeout(150)
            return {"composer": box(".composer"), "chat": box(".chat"), "tabs": box(".main > .tabs"),
                    "head": box(".main > .page-head"), "y": pg.evaluate("window.scrollY")}

        start = at(0)
        assert height - 120 < start["composer"]["y"] + start["composer"]["height"] < height, start   # near the bottom
        mid, end = at(40), at(10_000)
        assert end["y"] > 40
        for s in (mid, end):
            assert abs(s["composer"]["y"] - start["composer"]["y"]) < 2, s       # the message box did not move
        assert mid["chat"]["height"] > start["chat"]["height"] + 30           # the chat grew into the space
        assert end["chat"]["height"] > mid["chat"]["height"]
        assert end["head"]["y"] + end["head"]["height"] <= end["tabs"]["y"] + 1   # the heading went out of view
        assert end["tabs"]["y"] + end["tabs"]["height"] <= end["chat"]["y"]
        expect(pg.get_by_role("button", name="⤢ Focus")).to_be_in_viewport()
        at(0)
        pg.mouse.move(start["chat"]["x"] + 100, start["chat"]["y"] + 60)       # scrolling over the chat works too
        pg.mouse.wheel(0, 300)
        pg.wait_for_timeout(300)
        assert pg.evaluate("window.scrollY") > 0
        b.close()
    assert not errors, errors


def test_switching_chats_keeps_the_page_where_it_is(hub, account):
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, [], answer=ANSWER) as (base, tok, api), sync_playwright() as p:
        for i in range(16):                                         # enough chats for the list to scroll
            r = api.assistant.send(account["id"], f"Question {i}")
            api.assistant.wait(r["conversation_id"], 30)
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        expect(pg.locator(".conv-list .conv")).to_have_count(16)
        pg.evaluate("window.scrollTo(0, 10000)")
        pg.locator(".conv-scroll").evaluate("el => { el.scrollTop = el.scrollHeight; }")
        pg.wait_for_timeout(200)
        y, list_top = pg.evaluate("window.scrollY"), pg.locator(".conv-scroll").evaluate("el => el.scrollTop")
        composer = pg.locator(".composer").bounding_box()
        assert y > 40 and list_top > 100
        seen = []
        for name in ("Question 0", "Question 1"):                   # the oldest chats, at the end of the list
            pg.locator(".conv").filter(has=pg.get_by_text(name, exact=True)).click()
            expect(pg.locator(".msg.user .bubble").first).to_have_text(name)
            for _ in range(10):                                     # it stays put, not just ends up there
                seen.append(pg.evaluate("window.scrollY"))
                pg.wait_for_timeout(30)
            assert pg.locator(".conv-scroll").evaluate("el => el.scrollTop") == list_top
            assert abs(pg.locator(".composer").bounding_box()["y"] - composer["y"]) < 2
        assert set(seen) == {y}, seen
        pg.get_by_role("button", name="Settings").click()           # the tabs keep it too
        expect(pg.get_by_role("button", name="Save & test")).to_be_visible()
        pg.get_by_role("button", name="Chat").click()
        expect(pg.locator(".composer")).to_be_visible()
        assert pg.evaluate("window.scrollY") == y
        # While the next chat loads, the old one stays on screen but cannot be used: nothing goes to the old chat.
        pg.route("**/api/assistant/config", lambda route: (time.sleep(1.0), route.continue_()))
        pg.locator(".conv").filter(has=pg.get_by_text("Question 2", exact=True)).click()
        pg.wait_for_timeout(300)
        assert pg.locator(".composer textarea").evaluate("el => !!el.closest('[inert]')")
        expect(pg.locator(".msg.user .bubble").first).to_have_text("Question 2")
        assert not pg.locator(".composer textarea").evaluate("el => !!el.closest('[inert]')")
        pg.unroute("**/api/assistant/config")
        # Another page, then back: an ordinary visit starts at the top again.
        pg.goto(f"{base}/#/deadlines")
        expect(pg.locator(".main > .page-head")).to_be_visible()
        pg.goto(f"{base}/#/assistant")
        expect(pg.locator(".composer")).to_be_visible()
        pg.wait_for_timeout(200)
        assert pg.evaluate("window.scrollY") == 0
        b.close()
    assert not errors, errors


def test_a_long_chat_with_a_table_never_stretches_the_page(hub, account, fixture_pkg):
    # A dashboard deep in a long chat has a screen-reader caption placed absolutely. It must stay inside the
    # message list: before, it sat thousands of pixels down and the page scrolled into blank space.
    from playwright.sync_api import expect, sync_playwright

    from .fake_llm import FakeLLM
    hub.importer.import_package(fixture_pkg)                           # courses for the dashboard
    long = "\n\n".join(f"Paragraph {i}: " + "word " * 60 for i in range(12))
    with serve(hub, [], answer=long) as (base, tok, api), sync_playwright() as p:
        r = api.assistant.send(account["id"], "Question 0")
        cid = r["conversation_id"]
        api.assistant.wait(cid, 30)
        for i in range(1, 3):
            api.assistant.send(account["id"], f"Question {i}", conversation_id=cid)
            api.assistant.wait(cid, 30)
        dash = FakeLLM([("show_dashboard", {})], answer="Here it is.")
        try:
            api.assistant.update_config({"preset": "custom", "base_url": dash.url + "/v1", "model": "m"})
            api.assistant.send(account["id"], "Show my dashboard", conversation_id=cid)
            api.assistant.wait(cid, 30)
        finally:
            dash.stop()
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1000, "height": 560})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant/{cid}")
        expect(pg.locator(".dash-t")).to_have_count(1)
        page_h = "document.documentElement.scrollHeight"
        assert pg.evaluate(page_h) == pg.evaluate("document.querySelector('.shell').offsetHeight")
        composer = pg.locator(".composer").bounding_box()
        pg.mouse.move(300, 120)
        for _ in range(20):                                             # scroll well past the end
            pg.mouse.wheel(0, 150)
            pg.wait_for_timeout(40)
        pg.wait_for_timeout(200)
        assert pg.evaluate("window.scrollY + window.innerHeight") <= pg.evaluate(page_h) + 1
        assert abs(pg.locator(".composer").bounding_box()["y"] - composer["y"]) < 2      # nothing jumped
        expect(pg.get_by_role("button", name="⤢ Focus")).to_be_in_viewport()
        expect(pg.locator("#sidebar")).to_be_in_viewport()
        b.close()
    assert not errors, errors
