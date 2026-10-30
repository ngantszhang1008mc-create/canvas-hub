"""The assistant chat in a real browser: replies appear word by word, and the focus mode."""

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
