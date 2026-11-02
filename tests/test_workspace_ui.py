"""Daily work, explicit Agent actions and the same saved workspace on a phone."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from canvas_hub.boundary import HubAPI
from .test_components_ui import serve

pytestmark = pytest.mark.browser


@pytest.fixture
def daily_work(hub, account, fixture_pkg):
    package = copy.deepcopy(fixture_pkg)
    now = datetime.now(timezone.utc)
    proposal = next(i for i in package["items"] if i["type"] == "assignment" and i["canvas_id"] == "7001")
    proposal["fields"]["due_at"].update(iso=(now + timedelta(days=2)).isoformat(), raw="In two days")
    second_course = copy.deepcopy(package["items"][0])
    second_course.update(canvas_id="5252", course_id="5252", title="OTHER2020 Other course")
    second_course["fields"].update(title="OTHER2020 Other course", course_code="OTHER2020")
    package["items"].append(second_course)
    for canvas_id, title, course, offset in [
        ("7010", "Earlier unfinished essay", "4242", -200),
        ("7011", "Other course report", "5252", 3),
        ("7012", "Personally finished draft", "4242", 3),
    ]:
        item = copy.deepcopy(proposal)
        item.update(canvas_id=canvas_id, title=title, course_id=course)
        item["fields"]["title"] = title
        item["fields"]["due_at"].update(iso=(now + timedelta(days=offset)).isoformat(), raw=title)
        package["items"].append(item)
    hub.importer.import_package(package)
    aid = account["id"]
    ids = {r["canvas_id"]: r["id"] for r in hub.db.query("SELECT id,canvas_id FROM items WHERE account_id=? AND item_type='assignment'", (aid,))}
    checklist = hub.components.create(aid, "checklist", {
        "title": "Proposal checklist", "sync_todos": True,
        "sections": [{"id": "work", "rows": [
            {"id": "read", "label": "Read the brief", "item_id": ids["7001"]},
            {"id": "draft", "label": "Draft the proposal", "item_id": ids["7001"]},
        ]}],
    }, item_id=ids["7001"])
    hub.components.set_state(aid, checklist["id"], {"done": {"read": True}})
    HubAPI(hub).set_deadline_local_completion(aid, ids["7012"], True)
    task = hub.annotations.create(aid, kind="task", title="Book a study room", course_id="4242")
    return aid, ids, checklist, task


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 380], ids=["desktop", "phone"])
def test_daily_work_filters_and_completion_are_distinct(hub, account, daily_work, theme, width):
    from playwright.sync_api import expect, sync_playwright
    aid, ids, checklist, task = daily_work
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/?token={token}")
        page.get_by_role("button", name=theme.title(), exact=True).click()
        expect(page.locator(".page-head h1")).to_have_text("Overall")
        expect(page.locator(".workspace-continue")).to_be_hidden()
        proposal = page.locator(f'.workspace-work-row[data-item-id="{ids["7001"]}"]')
        expect(proposal).to_contain_text("1/2 steps")
        expect(proposal).to_contain_text("Not submitted")
        expect(proposal).not_to_contain_text("Submitted")
        expect(proposal).not_to_contain_text("Done locally")
        expect(page.get_by_role("link", name="Book a study room", exact=True)).to_be_visible()
        # Synced checklist rows and whole-assignment completion markers are not duplicate tasks.
        expect(page.get_by_role("link", name="Read the brief", exact=True)).to_have_count(0)
        expect(page.get_by_role("link", name="Other course report", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Personally finished draft", exact=True)).to_have_count(0)
        page.get_by_label("Filter by course").select_option("4242")
        expect(page.get_by_role("link", name="Other course report", exact=True)).to_have_count(0)
        page.get_by_label("Filter by course").select_option("")
        page.get_by_role("button", name="Overdue", exact=True).click()
        expect(page.get_by_role("link", name="Earlier unfinished essay", exact=True)).to_be_visible()
        page.get_by_role("button", name="All", exact=True).click()
        locally_done = page.locator(f'.workspace-work-row[data-item-id="{ids["7012"]}"]')
        expect(locally_done).to_contain_text("Done locally")
        expect(locally_done).not_to_contain_text("Submitted")
        expect(page.locator(".workspace-overall")).not_to_contain_text("GPA")
        expect(page.locator(".workspace-overall")).not_to_contain_text("Grade planning")
        page.get_by_role("button", name="Complete local task: Book a study room", exact=True).click()
        expect(page.locator(f'.workspace-work-row[data-task-id="{task["id"]}"]')).to_contain_text("Done locally")
        assert hub.annotations.get(aid, task["id"])["task_state"] == "done"
        assert not HubAPI(hub).deadlines(aid, start="1960-01-01T00:00:00Z", end="2100-01-01T00:00:00Z")["deadlines"][0]["canvas_completed"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if width == 380:
            expect(page.locator("#sidebar")).to_be_hidden()
            page.get_by_role("button", name="Toggle navigation", exact=True).click()
            expect(page.locator("#sidebar")).to_be_visible()
            page.get_by_role("button", name="Close navigation", exact=True).click()
            expect(page.locator("#sidebar")).to_be_hidden()
        page.screenshot(path=f"/tmp/canvas-workspace-overall-{theme}-{width}.png", full_page=True)
        assert errors == []
        browser.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 380], ids=["desktop", "phone"])
def test_resume_step_and_action_launcher_keep_the_same_workspace(hub, account, daily_work, theme, width):
    from playwright.sync_api import expect, sync_playwright
    aid, ids, checklist, _ = daily_work
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=width == 380, is_mobile=width == 380)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/?token={token}#/item/{ids['7001']}")
        page.get_by_role("button", name=theme.title(), exact=True).click()
        expect(page.locator(".workspace-linked .cmp-checklist")).to_be_visible()
        expect(page.locator(".workspace-agent")).to_be_hidden()
        expect(page.get_by_text("One page describing your dataset and question.", exact=True)).to_be_visible()
        step = page.get_by_label("Active step in Proposal checklist")
        step.select_option("draft")
        expect(page.get_by_text("Next step saved", exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_label("Active step in Proposal checklist")).to_have_value("draft")
        page.goto(f"{base}/#/overall")
        expect(page.locator(".workspace-continue")).to_contain_text("Draft the proposal")
        page.get_by_role("link", name="Resume", exact=True).click()
        expect(page.get_by_label("Active step in Proposal checklist")).to_have_value("draft")
        toggle = page.get_by_role("button", name="Workspace actions", exact=True)
        expect(page.locator(".workspace-action-popover")).to_be_hidden()
        toggle.click()
        expect(page.get_by_role("button", name="Interpret grading", exact=True)).to_have_count(0)
        ask = page.get_by_role("button", name="Ask Agent", exact=True)
        expect(ask).to_have_attribute("title", "Ask Agent: Open the conversation for this workspace. Your question is sent only when you choose Send.")
        if width == 380:
            ask.tap()
            expect(page.locator(".workspace-action-description")).to_contain_text("Your question is sent only")
            expect(page.locator(".workspace-agent")).to_be_hidden()
            page.get_by_role("button", name="Open Agent", exact=True).click()
        else:
            ask.focus()
            page.keyboard.press("Enter")
        expect(page.locator(".workspace-agent")).to_be_visible()
        expect(page.get_by_label("Ask contextual Agent")).to_be_visible()
        # Opening and reading context never invokes the model.
        assert hub.db.one("SELECT COUNT(*) AS n FROM assistant_messages m JOIN assistant_conversations c ON c.id=m.conversation_id WHERE c.account_id=?", (aid,))["n"] == 0
        active = hub.workspace.get(aid)["active"]
        assert active["item_id"] == ids["7001"] and active["step_id"] == "draft"
        expanded_width = page.locator(".workspace-center").bounding_box()["width"]
        page.get_by_role("button", name="Close Agent", exact=True).click()
        expect(page.locator(".workspace-agent")).to_be_hidden()
        closed_width = page.locator(".workspace-center").bounding_box()["width"]
        assert closed_width > expanded_width if width == 1280 else closed_width == expanded_width
        assignment_status = page.get_by_label("Assignment status", exact=True)
        expect(assignment_status).to_contain_text("Not submitted")
        expect(assignment_status).to_contain_text("Due")
        page.get_by_role("button", name="Mark done locally", exact=True).click()
        expect(assignment_status.get_by_text("Done locally", exact=True)).to_be_visible()
        expect(assignment_status).to_contain_text("Not submitted")
        expect(page.locator(".workspace-linked .cmp-checklist")).to_contain_text("1/2 done")
        page.get_by_role("button", name="Reopen locally", exact=True).click()
        expect(assignment_status.get_by_text("Done locally", exact=True)).to_be_hidden()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=f"/tmp/canvas-workspace-assignment-{theme}-{width}.png", full_page=True)
        assert errors == []
        browser.close()


def test_source_updates_are_reviewed_and_local_overrides_can_follow_the_source(hub, account, daily_work):
    from playwright.sync_api import expect, sync_playwright
    from .test_component_sync import field, score_sources, score_spec
    aid, ids, _, _ = daily_work
    iid = ids["7001"]
    checklist = hub.components.create(aid, "checklist", {
        "title": "Source-backed steps", "sections": [{"id": "work", "rows": [{"id": "read", "label": "Read instructions"}]}],
    }, item_id=iid, sources={"review_sources": [{"item_id": iid, "fields": ["description"]}]})
    hub.components.set_state(aid, checklist["id"], {"done": {"read": True}})
    field(hub, iid, "description", "<p>New requirement: include citations.</p>")
    hub.components.sync_sources(aid)
    proposed = copy.deepcopy(checklist["spec"])
    proposed["sections"][0]["rows"].append({"id": "citations", "label": "Draft with citations"})
    hub.components.update_spec(aid, checklist["id"], proposed)
    score = hub.components.create(aid, "score_model", score_spec(), course_id="4242", sources=score_sources(iid))
    hub.components.set_source_override(aid, score["id"], "buckets/proposal/max", 15)
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}/?token={token}#/item/{iid}")
        source = page.locator(".workspace-source").filter(has_text="Source changes need review")
        source.locator("summary").click()
        expect(source).to_contain_text("Draft with citations")
        current = page.locator(f'.workspace-component[data-component-id="{checklist["id"]}"]')
        expect(current.get_by_role("checkbox", name="Read instructions")).to_be_checked()
        expect(current.get_by_role("checkbox", name="Draft with citations")).to_have_count(0)
        source.get_by_role("button", name="Approve source update", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("Draft with citations")
        dialog.get_by_role("button", name="Approve changes", exact=True).click()
        expect(current.get_by_role("checkbox", name="Draft with citations")).to_be_visible()
        expect(current.get_by_role("checkbox", name="Read instructions")).to_be_checked()
        page.goto(f"{base}/#/course/4242")
        expect(page.locator(".workspace-course-checklists").get_by_role("link", name="Proposal checklist", exact=True)).to_have_attribute("href", f"#/item/{iid}")
        planning = page.locator("details.workspace-evidence").filter(has=page.locator(".cmp-score"))
        expect(planning).not_to_have_attribute("open", "")
        source = page.locator(".workspace-source").filter(has_text="Source linked · local edits")
        source.locator("summary").click()
        expect(source).not_to_contain_text("Source unavailable")
        source.get_by_role("button", name="Follow source", exact=True).click()
        expect(page.locator(".workspace-context-sources")).not_to_contain_text("edited locally")
        assert hub.components.get(aid, score["id"])["spec"]["buckets"][0]["max"] == 10
        assert not hub.components.get(aid, score["id"])["source_overrides"]
        browser.close()


def test_important_changes_show_readable_evidence_and_optional_failures_are_visible(hub, account, daily_work, fixture_pkg):
    from playwright.sync_api import expect, sync_playwright
    from .conftest import variant
    now = datetime.now(timezone.utc)
    package = variant(fixture_pkg, "workspace-updated-requirements", now.isoformat())
    assignment = next(i for i in package["items"] if i["type"] == "assignment" and i["canvas_id"] == "7001")
    assignment["fields"]["description"] = "<p>Include a dataset citation and an evaluation plan.</p>"
    assignment["fields"]["due_at"].update(iso=(now + timedelta(days=5)).isoformat(), raw="New deadline")
    hub.importer.import_package(package)
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 380, "height": 844})
        page.goto(f"{base}/?token={token}#/overall")
        change = page.locator(".workspace-change").filter(has_text="Requirements changed")
        expect(change).to_contain_text("One page describing your dataset and question.")
        expect(change).to_contain_text("Include a dataset citation and an evaluation plan.")
        expect(change).not_to_contain_text("<p>")
        expect(page.locator(".workspace-attention")).to_contain_text("Due date changed")
        page.route("**/changes?*", lambda route: route.fulfill(status=503, json={"message": "Temporarily unavailable"}))
        page.reload()
        expect(page.locator(".workspace-notice")).to_contain_text("Recent changes unavailable: Temporarily unavailable")
        expect(page.get_by_role("link", name="Project proposal", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()


def test_empty_optional_zones_are_hidden(hub, account):
    from playwright.sync_api import expect, sync_playwright
    with serve(hub, []) as (base, token, _), sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 380, "height": 844})
        page.goto(f"{base}/?token={token}")
        expect(page.locator(".page-head h1")).to_have_text("Overall")
        expect(page.locator(".workspace-continue")).to_be_hidden()
        expect(page.locator(".workspace-attention")).to_be_hidden()
        expect(page.locator(".workspace-empty")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()
