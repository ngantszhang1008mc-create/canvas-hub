"""Appearance selection, isolation of the original style, and real navigation."""
import re
import pytest

from .test_components_ui import serve

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1280, 800), (390, 844)], ids=["desktop", "phone"])
def test_switch_during_a_response_keeps_chat_and_scrolling_working(hub, account, theme, width, height):
    from playwright.sync_api import expect, sync_playwright
    from .test_chat_ui import ANSWER
    with serve(hub, [], answer=ANSWER, delay=0.03) as (base, tok, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"{base}/?token={tok}#/assistant")
        page.get_by_role("button", name="Dark" if theme == "light" else "Light", exact=True).click()
        page.locator(".composer textarea").fill("When is the proposal due?")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator(".msg.assistant").last).to_contain_text("Your")
        page.get_by_role("button", name=theme.title(), exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        expect(page.locator(".msg.assistant").last).to_have_text(ANSWER, timeout=15000)
        expect(page.get_by_role("button", name="Stop", exact=True)).to_be_hidden()
        composer = page.locator(".composer").bounding_box()
        assert height - 120 < composer["y"] + composer["height"] < height
        page.evaluate("window.scrollTo(0,10000)")
        expect(page.get_by_role("button", name="⤢ Focus", exact=True)).to_be_in_viewport()
        page.evaluate("window.scrollTo(0,0)")
        page.wait_for_timeout(150)
        # Font metrics change the size of the message viewport. Target that
        # viewport, not a fixed offset that can land on the composer, and let
        # its remaining inner scroll finish before expecting page scroll.
        messages = page.locator(".chat-scroll")
        area = messages.bounding_box()
        assert area and area["height"] > 0
        messages.evaluate("el => el.scrollTop = el.scrollHeight")
        page.mouse.move(area["x"] + area["width"] / 2, area["y"] + area["height"] / 2)
        for _ in range(5):
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(200)
            if page.evaluate("window.scrollY") > 0:
                break
        assert page.evaluate("window.scrollY") > 0, page.evaluate("""() => ({
            pageScroll: scrollY, documentHeight: document.documentElement.scrollHeight,
            messageHeight: document.querySelector('.chat-scroll').clientHeight,
            messageScroll: document.querySelector('.chat-scroll').scrollTop,
            messageScrollHeight: document.querySelector('.chat-scroll').scrollHeight
        })""")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()


@pytest.mark.parametrize("system_theme", ["light", "dark"])
def test_switch_preserves_draft_and_layout_and_remembers_selection(hub, account, system_theme):
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, []) as (base, tok, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=system_theme)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={tok}#/assistant")
        expect(page.locator(".composer textarea")).to_be_visible()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        expect(page.get_by_role("button", name="Dark", exact=True)).to_have_attribute("aria-pressed", "true")
        assert page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(21, 19, 15)"
        dark = page.evaluate("""() => ({
            sidebar: getComputedStyle(document.querySelector('.sidebar')).width,
            columns: getComputedStyle(document.querySelector('.assistant-layout')).gridTemplateColumns,
            padding: getComputedStyle(document.querySelector('.main')).paddingInline,
            heading: getComputedStyle(document.querySelector('h1')).fontFamily
        })""")
        draft = page.locator(".composer textarea")
        draft.fill("Keep this unsent draft")
        page.get_by_role("button", name="Light", exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        expect(draft).to_have_value("Keep this unsent draft")
        assert page.evaluate("localStorage.getItem('ch.theme')") == "light"
        assert page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(249, 251, 254)"
        assert page.locator(".sidebar").evaluate("el => getComputedStyle(el).width") == dark["sidebar"]
        assert page.locator(".assistant-layout").evaluate("el => getComputedStyle(el).gridTemplateColumns") == dark["columns"]
        assert page.locator(".main").evaluate("el => getComputedStyle(el).paddingInline") == dark["padding"]
        assert page.locator("h1").evaluate("el => getComputedStyle(el).fontFamily") != dark["heading"]
        page.get_by_role("button", name="Dark", exact=True).focus()
        page.keyboard.press("Enter")
        expect(draft).to_have_value("Keep this unsent draft")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        # Loading the optional Light stylesheet must have no effect on Dark.
        snapshot = """() => ['body','.sidebar','.page-head','h1','.conv-list','.chat','.composer','.chat-pick','.nav a.active']
          .map(s => {const c=getComputedStyle(document.querySelector(s));return [c.color,c.backgroundColor,c.fontFamily,
            c.fontSize,c.borderRadius,c.padding,c.display,c.gridTemplateColumns]})"""
        before = page.evaluate(snapshot)
        page.evaluate("document.querySelector('link[href$=\"light-theme.css\"]').disabled = true")
        assert page.evaluate(snapshot) == before
        page.evaluate("document.querySelector('link[href$=\"light-theme.css\"]').disabled = false")
        page.get_by_role("button", name="Light", exact=True).click()
        page.reload()
        expect(page.get_by_role("button", name="Light", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        assert errors == []
        browser.close()


def test_light_style_covers_every_app_section(hub, account, fixture_pkg):
    from playwright.sync_api import expect, sync_playwright
    hub.importer.import_package(fixture_pkg)
    routes = [("overview", "Overall"), ("deadlines", "Deadlines"), ("planner", "Planner"),
              ("trackers", "Trackers"), ("courses", "Courses"), ("search", "Search"),
              ("changes", "Changes"), ("coverage", "Coverage"), ("notes", "Notes & tasks"),
              ("collect", "Collection"), ("requests", "Agent requests"), ("settings", "Data & settings"),
              ("assistant/settings", "Assistant")]
    with serve(hub, []) as (base, tok, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={tok}")
        page.get_by_role("button", name="Light", exact=True).click()
        for route, title in routes:
            page.goto(f"{base}/#/{route}")
            expect(page.locator(".page-head h1")).to_have_text(title)
            expect(page.get_by_role("button", name="Light", exact=True)).to_have_attribute("aria-pressed", "true")
            assert page.locator("body").evaluate("el => getComputedStyle(el).getPropertyValue('--accent').trim()") == "#006bff"
            assert "Source Serif" not in page.locator("h1").evaluate("el => getComputedStyle(el).fontFamily")
        page.get_by_role("button", name="Dark", exact=True).click()
        assert "Source Serif" in page.locator("h1").evaluate("el => getComputedStyle(el).fontFamily")
        assert errors == []
        browser.close()


def test_theme_syncs_between_tabs(hub, account):
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, []) as (base, tok, _), sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        first, second = context.new_page(), context.new_page()
        for page in (first, second):
            page.goto(f"{base}/?token={tok}#/assistant")
            expect(page.locator(".composer textarea")).to_be_visible()
        second.locator(".composer textarea").fill("A draft in another tab")
        first.get_by_role("button", name="Light", exact=True).click()
        expect(second.locator("html")).to_have_attribute("data-theme", "light")
        expect(second.locator(".composer textarea")).to_have_value("A draft in another tab")
        second.get_by_role("button", name="Dark", exact=True).click()
        expect(first.locator("html")).to_have_attribute("data-theme", "dark")
        second.get_by_role("button", name="⤢ Focus", exact=True).click()
        second.goto(f"{base}/#/overview")
        expect(second.locator(".page-head h1")).to_have_text("Overall")
        assert second.evaluate("localStorage.getItem('ch.chatFocus')") == "1"
        first.get_by_role("button", name="Light", exact=True).click()
        expect(second.locator("html")).to_have_attribute("data-theme", "light")
        assert second.evaluate("localStorage.getItem('ch.chatFocus')") == "1"
        second.goto(f"{base}/#/assistant")
        expect(second.locator("body")).to_have_class(re.compile(r"\bchat-focus\b"))
        browser.close()


def test_switch_works_when_browser_storage_is_disabled(hub, account):
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, []) as (base, tok, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('Blocked', 'SecurityError')}})")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/?token={tok}#/assistant")
        page.get_by_role("button", name="Light", exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        page.get_by_role("button", name="Dark", exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
        browser.close()
