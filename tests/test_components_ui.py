"""Acceptance tests for the interactive components in a real browser:
score-model toggles with live totals that persist and read back (#1), and
the artifact block's Copy and Download (#8/#9)."""

import socket
import threading
import time

import pytest

from canvas_hub.assistant import AssistantService
from canvas_hub.boundary import HubAPI
from canvas_hub.web.app import create_app

from .fake_llm import FakeLLM
from .test_components import HMAW

pytestmark = pytest.mark.browser

SPEC_TEXT = "SPEC: Canvas Hub components to add\n" + "\n".join(f"{i}. item {i} — details" for i in range(1, 60))


@pytest.fixture
def app_server(hub, account):
    import uvicorn
    api = HubAPI(hub)
    fake = FakeLLM([("show_score_model", {**HMAW, "pin": True}),
                    ("show_artifact", {"title": "Component spec", "content": SPEC_TEXT, "language": "markdown",
                                       "filename": "canvas-hub-spec.md"})])
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
    yield f"http://127.0.0.1:{port}", tok, api
    srv.should_exit = True
    fake.stop()


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
        assert pg.evaluate("navigator.clipboard.readText()") == SPEC_TEXT
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
