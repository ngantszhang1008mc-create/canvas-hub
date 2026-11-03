"""Synthetic browser regression for sorting/filtering row-bound table links."""
import json

import pytest

from .test_components_ui import serve
from .test_table_links import show, table_items  # noqa: F401 (shared synthetic fixture)

pytestmark = pytest.mark.browser


def test_table_links_follow_title_rows_through_sort_filter_details_and_reload(hub, account, table_items, tmp_path):
    from playwright.sync_api import expect, sync_playwright

    items = table_items[:6]
    rows = [{"cells": ["Group " + ("B" if i % 2 else "A"), item["title"], i + 1],
             "item_id": item["id"], "detail": item["title"] + " detail"} for i, item in reversed(list(enumerate(items)))]
    new_args = {"columns": ["Course", "Item", "Due"], "rows": rows, "group_by": "Course",
                "title": "Bound rows", "totals": ["Due"], "column_types": ["text", "text", "number"]}
    _, error, display = show(hub, account, **new_args)
    assert not error
    alpha, beta = items[:2]
    legacy_args = {"title": "Legacy rows", "columns": ["Course", "Item"],
                   "rows": [["Group", beta["title"]], ["Group", alpha["title"]]],
                   "item_ids": [alpha["id"], alpha["id"]], "details": ["Beta legacy detail", "Alpha legacy detail"]}
    legacy = {"component": "table", **legacy_args}
    with serve(hub, []) as (base, token, api), sync_playwright() as playwright:
        with hub.db.tx() as conn:
            cid = conn.execute("INSERT INTO assistant_conversations(account_id,title,created_at,updated_at) VALUES (?,?,?,?)",
                               (account["id"], "Table links", "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z")).lastrowid
        # Displays belong to the assistant calls that requested them, rather
        # than standalone tool-result messages.
        api.assistant._append(cid, "assistant", {"tool_calls": [
            {"id": "new", "name": "show_table", "input": new_args},
            {"id": "old", "name": "show_table", "input": legacy_args}]})
        api.assistant._append(cid, "tool", {"results": [
            {"id": "new", "name": "show_table", "content": "{}", "display": display},
            {"id": "old", "name": "show_table", "content": "{}", "display": legacy}]})
        def stored_messages():
            return [(row["role"], json.loads(row["content_json"])) for row in hub.db.query(
                "SELECT role,content_json FROM assistant_messages WHERE conversation_id=? ORDER BY id", (cid,))]
        stored_before = stored_messages()
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 960})
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/?token={token}#/assistant/{cid}")
        bound, old = page.locator(".cmp-table").nth(0), page.locator(".cmp-table").nth(1)
        expect(bound).to_be_visible()
        expect(old).to_be_visible()
        page.on("request", lambda request: requests.append(request))

        def check_links():
            links = bound.locator("a.cmp-link")
            assert links.count() == 6
            expected = {item["title"]: f"#/item/{item['id']}" for item in items}
            assert {link.inner_text(): link.get_attribute("href") for link in links.all()} == expected
            assert bound.locator("tbody td:first-child a").count() == 0
            assert bound.locator(".t-group").count() == 2
        check_links()
        for _ in range(2):
            bound.get_by_role("button", name="Item", exact=False).click()
            check_links()
        bound.get_by_role("button", name="Due", exact=False).click()
        check_links()
        bound.get_by_label("Filter rows").fill("Beta")
        expect(bound.locator("a.cmp-link")).to_have_text(beta["title"])
        expect(bound.locator("a.cmp-link")).to_have_attribute("href", f"#/item/{beta['id']}")
        bound.get_by_role("button", name="Details for Beta task").click()
        expect(bound.locator(".t-detail")).to_have_text("Beta task detail")
        bound.get_by_role("button", name="Due", exact=False).click()
        expect(bound.locator(".t-detail")).to_have_text("Beta task detail")
        bound.locator("a.cmp-link").click()
        drawer = page.get_by_role("dialog", name="Item details")
        expect(drawer.locator(".drawer-head h2")).to_have_text(beta["title"])
        expect(drawer.get_by_role("link", name="Open in Canvas")).to_have_attribute("href", beta["source_url"])
        drawer.get_by_role("button", name="Close", exact=True).click()
        expect(page.locator(".drawer.open")).to_have_count(0)
        bound.get_by_role("button", name="Copy ▾").click()
        bound.get_by_role("menuitem", name="Copy as CSV").click()
        assert page.evaluate("navigator.clipboard.readText()") == "Course,Item,Due\nGroup B,Beta task,2"
        bound.get_by_label("Filter rows").fill("")
        check_links()
        # Legacy content is preserved, but the mismatched Beta -> Alpha link is
        # absent, with a visible notice. The valid Alpha link still works.
        expect(old.locator(".cmp-table-notices summary")).to_contain_text("could not be verified")
        expect(old.locator("a.cmp-link")).to_have_text(alpha["title"])
        expect(old.locator("a.cmp-link")).to_have_attribute("href", f"#/item/{alpha['id']}")
        assert "Beta task" in old.inner_text()
        page.screenshot(path=str(tmp_path / "table-link-binding.png"), full_page=True)
        page.reload()
        expect(bound.locator("a.cmp-link")).to_have_count(6)
        check_links()
        expect(old.locator("a.cmp-link")).to_have_text(alpha["title"])
        # The app shell polls due reminders on reload independently of tables.
        # Every other request must still be read-only, including component/chat APIs.
        reminder_poll = f"{base}/api/accounts/{account['id']}/reminders/due"
        writes = [(request.method, request.url) for request in requests
                  if request.method != "GET" and not (request.method == "POST" and request.url == reminder_poll)]
        assert not writes, writes
        assert not errors, errors
        browser.close()
        assert stored_messages() == stored_before
