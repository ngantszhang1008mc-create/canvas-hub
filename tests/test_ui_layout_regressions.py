"""Synthetic browser regressions for tracker bounds, dates, links and launchers."""

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from canvas_hub.util import now_iso
from .test_components_ui import serve

pytestmark = pytest.mark.browser


@pytest.fixture
def layout_examples(hub, account, fixture_pkg):
    package = copy.deepcopy(fixture_pkg)
    now = datetime.now(timezone.utc)
    assignment = next(i for i in package["items"] if i["type"] == "assignment")
    assignment["title"] = "Synthetic overdue assignment with a long title " * 3
    assignment["fields"]["title"] = assignment["title"]
    assignment["fields"]["due_at"]["iso"] = (now - timedelta(hours=22, minutes=24)).isoformat()
    second = copy.deepcopy(assignment)
    second.update(canvas_id="ui-layout-future", title="Synthetic deadline about to pass")
    second["fields"]["title"] = second["title"]
    second["fields"]["due_at"]["iso"] = (now + timedelta(seconds=15)).isoformat()
    package["items"].append(second)
    course = next(i for i in package["items"] if i["type"] == "course")
    course["title"] = "SYNTHETIC101 " + "VeryLongCourseNameWithoutSpaces" * 8
    course["fields"]["title"] = course["title"]
    hub.importer.import_package(package)
    aid = account["id"]
    # Insert the original persisted schema directly, without today's create /
    # normalize path. Existing scores, manual values and checks must survive a
    # layout upgrade without rewriting any saved component data.
    score_spec = {
        "title": "Synthetic score tracker", "threshold": 80, "unit_label": "pts", "scenarios": [],
        "buckets": [
            {"id": f"line-{n}", "label": f"Activity {n:02}", "max": 10, "earned": None, "status": "pending",
             "gating": "none", "min_value": None, "unit": "points", "full_at": None,
             "due": (now - timedelta(hours=22) if n == 0 else now + timedelta(days=n + 1)).isoformat(),
             "note": "Synthetic notes for scrolling and score controls."}
            for n in range(18)
        ],
    }
    checklist_spec = {
        "title": "Synthetic checklist", "sync_todos": False, "sections": [{"id": "steps", "title": "Steps", "rows": [
            {"id": f"step-{n}", "label": f"Synthetic step {n:02}", "due": (now - timedelta(days=2)).isoformat()}
            for n in range(24)
        ]}],
    }
    ids = []
    with hub.db.tx() as connection:
        for kind, spec, state in (
            ("score_model", score_spec, {"include": {"line-3": False}, "values": {"line-2": 4.5}, "scenario": None}),
            ("checklist", checklist_spec, {"done": {"step-1": True}, "links": {}, "collapsed": {}}),
        ):
            ids.append(connection.execute("""INSERT INTO components
                (account_id,kind,title,spec_json,state_json,version,pinned,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,7,1,'assistant','2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')""",
                (aid, kind, spec["title"], json.dumps(spec), json.dumps(state))).lastrowid)
    for component_id in ids:
        hub.widgets.save(aid, f"component:{component_id}", {"mode": "pinned"}, expected_version=0)
    urls = ["https://study.example.edu/short", "https://study.example.edu/" + "long-path-segment" * 20,
            "https://study.example.edu/materials?chapter=" + "synthetic" * 25]
    for url in urls:
        sid = hub.external_sources.register(aid, url, course_id="4242")
        request = hub.external_sources.analyze(aid, sid)
        rid = request["analysis_request_id"]
        hub.requests.claim(aid, rid, "Synthetic fixture")
        hub.external_sources.submit_analysis(aid, rid, "Synthetic fixture", {
            "roles": ["material"], "confidence": 1, "evidence_url": url, "read_at": now_iso(),
            "summary": "Synthetic cached link", "content": "Synthetic content; no remote page was read.",
        })
    return {"aid": aid, "score": ids[0], "checklist": ids[1]}


def screenshot(page, tmp_path, name):
    directory = Path(os.environ.get("CANVAS_HUB_UI_SCREENSHOTS", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / name))


def assert_no_horizontal_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), page.evaluate("""() =>
        [...document.querySelectorAll('body *')].filter(el => {
            const r=el.getBoundingClientRect(); return r.width && r.right > innerWidth + 1;
        }).slice(0, 12).map(el => ({tag:el.tagName, class:el.className, width:el.getBoundingClientRect().width}))
    """)


@pytest.mark.parametrize("theme,width", [("light", 1120), ("dark", 390)], ids=["desktop-light", "phone-dark"])
def test_tracker_resize_internal_scroll_and_saved_controls(hub, layout_examples, theme, width, tmp_path):
    data = layout_examples
    def saved_data():
        return [tuple(row) for row in hub.db.query(
            "SELECT spec_json,state_json,version FROM components WHERE account_id=? ORDER BY id", (data["aid"],))]
    original_data = saved_data()
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 960})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/?token={token}#/trackers")
        page.get_by_role("button", name=theme.title(), exact=True).click()
        score = page.locator(".cmp-score")
        content = score.locator(".cmp-resizable-content")
        expect(content).to_be_visible()
        original_height = content.bounding_box()["height"]
        # Legacy components are bounded immediately, before the first resize.
        assert 180 < original_height < 800
        scroll = score.locator(".sl-main")
        assert scroll.evaluate("el => el.scrollHeight > el.clientHeight + 20")
        assert score.locator(".sl-side").evaluate("el => getComputedStyle(el).position") not in ("sticky", "fixed")
        expect(score.get_by_label("Points for Activity 02", exact=True)).to_have_value("4.5")
        expect(score.get_by_label("Include Activity 03", exact=True)).not_to_be_checked()
        if width == 1120:
            side_box, main_box = score.locator(".sl-side").bounding_box(), scroll.bounding_box()
            assert side_box["x"] + side_box["width"] < main_box["x"]
        else:
            assert scroll.bounding_box()["y"] > score.locator(".sl-side").bounding_box()["y"]
        side_before = score.locator(".sl-side").bounding_box()["y"]
        viewport_before = page.evaluate("scrollY")
        scroll.evaluate("el => el.scrollTop = 150")
        assert scroll.evaluate("el => el.scrollTop") > 0
        assert abs(score.locator(".sl-side").bounding_box()["y"] - side_before) < 1
        assert page.evaluate("scrollY") == viewport_before
        checklist = page.locator(".cmp-checklist")
        expect(checklist.get_by_role("checkbox", name="Synthetic step 01", exact=True)).to_be_checked()
        check_scroll = checklist.locator(".cl-main")
        header_y = checklist.locator(".cl-top").bounding_box()["y"]
        check_scroll.evaluate("el => el.scrollTop = 100")
        assert check_scroll.evaluate("el => el.scrollTop") > 0
        assert checklist.locator(".cl-top").bounding_box()["y"] == header_y
        resize = score.get_by_role("separator", name="Resize Synthetic score tracker tracker", exact=True)
        resize.focus()
        for _ in range(40):
            page.keyboard.press("ArrowUp")
        compact_height = content.bounding_box()["height"]
        assert compact_height < original_height - 100
        if width == 1120:
            resize.scroll_into_view_if_needed()
            grip = resize.bounding_box()
            page.mouse.move(grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2)
            page.mouse.down()
            page.mouse.move(grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2 + 40)
            page.mouse.up()
            assert abs(content.bounding_box()["height"] - compact_height - 40) < 2
            compact_height = content.bounding_box()["height"]
        assert scroll.evaluate("el => el.scrollHeight > el.clientHeight + 20")
        scroll.evaluate("el => el.scrollTop = 150")
        before_scroll = scroll.evaluate("el => el.scrollTop")
        assert before_scroll > 0
        if width == 1120:
            score.scroll_into_view_if_needed()
            side_before = score.locator(".sl-side").bounding_box()["y"]
            viewport_before = page.evaluate("scrollY")
            scroll.evaluate("el => el.scrollTop += 120")
            assert abs(score.locator(".sl-side").bounding_box()["y"] - side_before) < 1
            assert page.evaluate("scrollY") == viewport_before
        assert saved_data() == original_data
        slider = score.get_by_label("Points for Activity 04 (slider)", exact=True)
        slider.focus()
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        expect(score.get_by_label("Points for Activity 04", exact=True)).to_have_value("9")
        expect(score.locator(".save-state")).to_contain_text("Saved")
        assert scroll.evaluate("el => el.scrollTop") > 0
        assert abs(content.bounding_box()["height"] - compact_height) < 2
        score.get_by_label("Include Activity 05", exact=True).uncheck()
        expect(score.locator(".save-state")).to_contain_text("Saved")
        checklist.get_by_role("checkbox", name="Synthetic step 00", exact=True).check()
        expect(checklist.locator(".save-state")).to_contain_text("Saved")
        page.reload()
        expect(score.get_by_label("Points for Activity 04", exact=True)).to_have_value("9")
        expect(score.get_by_label("Include Activity 05", exact=True)).not_to_be_checked()
        expect(checklist.get_by_role("checkbox", name="Synthetic step 00", exact=True)).to_be_checked()
        expect(checklist.get_by_role("checkbox", name="Synthetic step 01", exact=True)).to_be_checked()
        expect(score.get_by_label("Points for Activity 02", exact=True)).to_have_value("4.5")
        expect(score.get_by_label("Include Activity 03", exact=True)).not_to_be_checked()
        assert abs(content.bounding_box()["height"] - compact_height) < 2
        score.evaluate("el => el.scrollIntoView({block: 'start'})")
        screenshot(page, tmp_path, f"tracker-{theme}-{width}.png")
        resize.focus()
        page.keyboard.press("Home")
        assert content.bounding_box()["height"] > compact_height + 100
        assert content.bounding_box()["height"] < 800
        assert scroll.evaluate("el => el.scrollHeight > el.clientHeight + 20")
        expect(resize).to_have_attribute("aria-valuetext", f"Auto height, {round(content.bounding_box()['height'])} pixels")
        assert_no_horizontal_overflow(page)
        assert hub.components.get(data["aid"], data["score"])["state"]["values"]["line-4"] == 9
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 390)], ids=["desktop-light", "phone-dark"])
def test_overdue_colors_and_wrapping_after_render_and_time_change(hub, layout_examples, theme, width, tmp_path):
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 960})
        page.clock.install()
        page.goto(f"{base}/?token={token}#/deadlines?range=all")
        page.get_by_role("button", name=theme.title(), exact=True).click()
        overdue = page.locator(".deadline").filter(has_text="Synthetic overdue assignment")
        expect(overdue.locator(".countdown")).to_have_class("countdown overdue")
        future = page.locator(".deadline").filter(has_text="Synthetic deadline about to pass")
        expect(future.locator(".countdown")).to_have_class("countdown urgent")
        colors = overdue.locator(".countdown").evaluate("""el => {
            const expected = document.createElement('span'); expected.style.color='var(--bad)'; el.append(expected);
            const colors = [getComputedStyle(el).color, getComputedStyle(expected).color]; expected.remove(); return colors;
        }""")
        assert colors[0] == colors[1]
        assert overdue.locator(".time").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert overdue.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        if width == 1440:
            timing = overdue.locator(".time").bounding_box()
            title = overdue.locator("a.strong").bounding_box()
            assert timing["x"] + timing["width"] < title["x"]
            assert overdue.locator(".countdown").bounding_box()["x"] + overdue.locator(".countdown").bounding_box()["width"] <= title["x"]
        page.clock.fast_forward(31000)
        expect(future.locator(".countdown")).to_have_class("countdown overdue")
        expect(future.locator(".countdown")).to_contain_text("overdue")
        assert_no_horizontal_overflow(page)
        screenshot(page, tmp_path, f"deadlines-{theme}-{width}.png")
        browser.close()


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 390)], ids=["desktop-light", "phone-dark"])
def test_external_sources_padding_and_actions_align_for_long_urls(hub, layout_examples, theme, width, tmp_path):
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 960})
        page.goto(f"{base}/?token={token}#/course/4242")
        page.get_by_role("button", name=theme.title(), exact=True).click()
        listing = page.locator(".external-source-list")
        expect(listing).to_be_visible()
        listing.evaluate("el => el.open = true")
        rows = listing.locator(".external-source-row")
        expect(rows).to_have_count(3)
        list_box = listing.bounding_box()
        reinspection = []
        launchers = []
        for row in rows.all():
            link = row.locator("a").first
            assert link.bounding_box()["x"] >= list_box["x"] + 12
            assert row.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            reinspection.append(row.get_by_role("button", name="Re-inspect", exact=True).bounding_box()["x"])
            launchers.append(row.locator(".workspace-launcher-toggle").bounding_box()["x"])
        assert max(reinspection) - min(reinspection) < 2
        assert max(launchers) - min(launchers) < 2
        assert_no_horizontal_overflow(page)
        listing.scroll_into_view_if_needed()
        screenshot(page, tmp_path, f"external-links-{theme}-{width}.png")
        browser.close()


@pytest.mark.parametrize("width,reduced", [(1440, False), (320, True)], ids=["desktop", "narrow-reduced-motion"])
def test_launchers_open_left_tooltips_dismiss_and_activate_once(hub, layout_examples, width, reduced, tmp_path):
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 960}, has_touch=width < 500,
                                reduced_motion="reduce" if reduced else "no-preference")
        page.goto(f"{base}/?token={token}#/overall")
        page.get_by_role("button", name="Light", exact=True).click()
        expect(page.locator(".overall-widget-toolbar")).to_be_visible()
        for route in ("overall", "course/4242"):
            page.goto(f"{base}/#/{route}")
            expect(page.locator(".workspace-launcher-toggle").first).to_be_visible()
            for launcher in page.locator(".workspace-launcher").all():
                toggle = launcher.locator(".workspace-launcher-toggle")
                if not toggle.is_visible():
                    continue
                for _ in range(2):
                    toggle.click()
                    expect(toggle).to_have_attribute("aria-expanded", "true")
                    tray = launcher.locator(".workspace-action-tray")
                    expect(tray).to_be_visible()
                    # Wait for layout animation to finish before comparing final bounds.
                    page.wait_for_timeout(300 if not reduced else 20)
                    toggle_box = toggle.bounding_box()
                    for action in launcher.locator(".workspace-action-icon").all():
                        box = action.bounding_box()
                        assert abs(box["width"] - toggle_box["width"]) < 1
                        assert abs(box["height"] - toggle_box["height"]) < 1
                        assert box["x"] >= 0
                        assert box["x"] + box["width"] <= toggle_box["x"] + 1
                        action.focus()
                        described_by = action.get_attribute("aria-describedby")
                        assert described_by
                        tooltip = page.locator(f"[id='{described_by}']")
                        expect(tooltip).to_be_visible()
                        expect(tooltip).to_have_attribute("role", "tooltip")
                        assert tooltip.evaluate("el => getComputedStyle(el).whiteSpace") == "nowrap"
                        if width == 1440:
                            toggle.focus()
                            action.hover()
                            expect(tooltip).to_be_visible()
                    assert_no_horizontal_overflow(page)
                    page.keyboard.press("Escape")
                    expect(toggle).to_be_focused()
                    expect(tray).to_be_hidden()
                toggle.click()
                page.locator(".page-head h1").click()
                expect(toggle).to_have_attribute("aria-expanded", "false")
        page.goto(f"{base}/#/overall")
        toolbar = page.locator(".overall-widget-toolbar")
        toolbar.locator(".workspace-launcher-toggle").click()
        screenshot(page, tmp_path, f"launcher-light-{width}.png")
        manage = toolbar.get_by_role("button", name="Manage widgets", exact=True)
        manage.tap() if width < 500 else manage.click()
        expect(page.get_by_role("dialog", name="Manage widgets", exact=True)).to_be_visible()
        assert hub.db.one("SELECT COUNT(*) n FROM assistant_messages")["n"] == 0
        assert_no_horizontal_overflow(page)
        browser.close()
