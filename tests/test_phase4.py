"""Phase 4: formulas in score models, local grading overrides, the sandboxed HTML component, the dashboard and OCR."""

import json
import os
import sys

import pytest

from canvas_hub import scoring
from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput

from .test_components_ui import serve
from .test_planner_requests import _future_fixture

SDE = "if(hours >= 10, 20 * min(1, hours / 20), 0)"
MODEL = {"title": "HMAW", "threshold": 30, "buckets": [
    {"id": "sde", "label": "SDE hours", "max": 20, "unit": "hours", "full_at": 20, "formula": SDE},
    {"id": "att", "label": "Attendance", "max": 10, "formula": "round(value / 12 * 10, 1)", "full_at": 12},
    {"id": "hw", "label": "Homework", "max": 10, "earned": 9}]}


def test_formula_lines():
    m = scoring.normalize_model(MODEL)
    full = scoring.evaluate(m, {})
    assert full["totals"]["projected"] == 39                                  # 20 + 10 + 9 at the defaults
    r = scoring.evaluate(m, {"values": {"sde": 8.5, "att": 9}})
    line = {x["id"]: x for x in r["lines"]}
    assert line["sde"]["points"] == 0 and line["att"]["points"] == 7.5 and r["totals"]["projected"] == 16.5
    assert scoring.evaluate(m, {"values": {"sde": 15}})["totals"]["projected"] == 15 + 10 + 9
    assert scoring.evaluate(m, {"values": {"att": 50}})["lines"][1]["points"] == 10      # kept within the maximum
    with pytest.raises(InvalidInput, match="not \\['secret'\\]"):
        scoring.normalize_model({"buckets": [{"label": "x", "max": 1, "formula": "secret * 2"}]})
    with pytest.raises(InvalidInput, match="x: formula"):
        scoring.normalize_model({"buckets": [{"id": "x", "label": "x", "max": 1, "formula": "1 +"}]})
    bad = scoring.normalize_model({"buckets": [{"id": "d", "label": "d", "max": 5, "formula": "max / value"}]})
    line = scoring.evaluate(bad, {"values": {"d": 0}})["lines"][0]
    assert line["points"] == 0 and line["gated"] and "division by zero" in line["note"]
    # an all-or-nothing hour line without a minimum no longer breaks the alerts
    aon = scoring.normalize_model({"buckets": [{"id": "h", "label": "h", "max": 5, "unit": "hours", "full_at": 4,
                                                "gating": "all_or_nothing"}]})
    assert scoring.evaluate(aon, {"values": {"h": 1}})["alerts"]


@pytest.mark.browser
def test_formula_lines_compute_the_same_in_the_page(hub, account):
    from playwright.sync_api import expect, sync_playwright
    v = hub.components.create(account["id"], "score_model", MODEL)
    hub.components.pin(account["id"], v["id"], True)
    with serve(hub, []) as (base, tok, _), sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/trackers")
        score = pg.locator(".cmp-score")
        expect(score.locator(".sl-big .num")).to_have_text("39")
        score.get_by_label("Hours for SDE hours", exact=True).fill("8.5")
        score.get_by_label("Hours for SDE hours", exact=True).dispatch_event("change")
        score.get_by_label("Value for Attendance", exact=True).fill("9")
        score.get_by_label("Value for Attendance", exact=True).dispatch_event("change")
        expect(score.locator(".sl-big .num")).to_have_text("16.5")
        expect(score.locator(".save-state")).to_contain_text("Saved", timeout=10000)
        assert score.locator(".formula-text", has_text="hours >= 10").count() == 1
        b.close()
    assert not errors, errors
    back = hub.components.describe(account["id"], v["id"])
    assert back["totals"]["projected"] == 16.5


def test_local_grading_rules_apply_to_drafts(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    rules = {"groups": {"Assignments": {"weight": 40, "drop_lowest": 1}},
             "items": {"7001": {"unit": "hours", "full_at": 10, "gating": "min_threshold", "min_value": 5,
                                "formula": "if(hours >= 5, max * min(1, hours / 10), 0)", "note": "syllabus p.2"}},
             "threshold": 6, "scale": [{"letter": "P", "min": 50}, {"letter": "F", "min": 0}], "source": "syllabus"}
    text, err, _ = run_tool_display(ctx, "set_grading_rules", {"course_id": "4242", **rules}, {"read"})
    assert err                                                         # changing local data needs the notes permission
    text, err, _ = run_tool_display(ctx, "set_grading_rules", {"course_id": "4242", **rules}, {"read", "notes"})
    assert not err and json.loads(text)["saved"]["groups"]["Assignments"]["weight"] == 40
    lines = json.loads(run_tool_display(ctx, "course_score_lines", {"course_id": "4242"}, {"read"})[0])
    b = lines["buckets"][0]
    assert b["unit"] == "hours" and b["min_value"] == 5 and b["formula"].startswith("if(") and b["note"] == "syllabus p.2"
    assert lines["group_weights"] == {"Assignments": 40} and lines["grading_rules"]["threshold"] == 6
    model = scoring.normalize_model({"buckets": lines["buckets"], "threshold": 6})
    assert scoring.evaluate(model, {"values": {b["id"]: 4}})["lines"][0]["points"] == 0
    assert scoring.evaluate(model, {"values": {b["id"]: 8}})["lines"][0]["points"] == 8
    text, err, display = run_tool_display(ctx, "show_grade_projector", {"course_id": "4242"}, {"ui"})
    spec = display["view"]["spec"]
    assert spec["categories"][0]["weight"] == 40 and spec["categories"][0]["drop_lowest"] == 1
    assert [x["letter"] for x in spec["scale"]] == ["P", "F"]
    # excluded lines drop out; bad rules are refused; the web API round-trips
    hub.grading.set(aid, "4242", {"items": {"7001": {"exclude": True}}})
    assert json.loads(run_tool_display(ctx, "course_score_lines", {"course_id": "4242"}, {"read"})[0])["buckets"] == []
    for bad in ({"items": {"7001": {"formula": "secret"}}}, {"groups": {"A": {"weight": 140}}},
                {"items": {"7001": {"gating": "min_threshold"}}}, {"items": {"../x": {}}}):
        with pytest.raises(InvalidInput):
            hub.grading.set(aid, "4242", bad)
    from fastapi.testclient import TestClient

    from canvas_hub.web.app import create_app
    c = TestClient(create_app(HubAPI(hub), token="t" * 40, port=8765), base_url="http://127.0.0.1:8765")
    c.cookies.set("canvas_hub_token", "t" * 40)
    r = c.put(f"/api/accounts/{aid}/courses/4242/grading", json={"groups": {"Exams": {"weight": 60}}},
              headers={"X-Canvas-Hub": "1"})
    assert r.status_code == 200, r.text
    assert c.get(f"/api/accounts/{aid}/courses/4242/grading").json()["groups"] == {"Exams": {"weight": 60.0}}


PROBE = """<div id="out">waiting</div>
<script>
const r = {};
r.data = window.hubData.total;
try { window.hubData.total = 0; r.frozen = window.hubData.total; } catch (e) { r.frozen = window.hubData.total; }
try { r.cookie = document.cookie; } catch (e) { r.cookie = "blocked"; }
try { r.parent = parent.document.title; } catch (e) { r.parent = "blocked"; }
try { localStorage.setItem("x", "1"); r.storage = "open"; } catch (e) { r.storage = "blocked"; }
const img = new Image(); img.src = "http://127.0.0.1:9/leak.png";
fetch("/api/info").then(() => { r.fetch = "open"; }, () => { r.fetch = "blocked"; })
  .finally(() => { document.getElementById("out").textContent = JSON.stringify(r); });
</script>"""


@pytest.mark.browser
def test_custom_html_runs_isolated(hub, account):
    from playwright.sync_api import expect, sync_playwright
    aid = account["id"]
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    text, err, _ = run_tool_display(ctx, "show_html", {"html": "<p>x</p>"}, {"ui"})
    assert err and json.loads(text)["error"] == "custom_html_off"            # off by default
    hub.settings.set(aid, "custom_html", {"enabled": True})
    with serve(hub, [("show_html", {"title": "Probe", "html": PROBE, "data": {"total": 42}})]) as (base, tok, _), \
            sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1100, "height": 900})
        errors, frame_requests, failed = [], [], {}
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("requestfinished", lambda r: r.frame != pg.main_frame and frame_requests.append(r.url))
        pg.on("requestfailed", lambda r: failed.setdefault(r.url, r.failure))
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        pg.locator(".composer textarea").fill("Show a custom block")
        pg.get_by_role("button", name="Send").click()
        block = pg.locator(".cmp-html")
        expect(block.get_by_role("button", name="Run")).to_be_visible(timeout=20000)
        assert pg.locator("iframe").count() == 0                            # nothing runs before Run
        block.get_by_role("button", name="Run").click()
        out = pg.frame_locator("iframe.html-frame").locator("#out")
        expect(out).not_to_have_text("waiting", timeout=10000)
        assert json.loads(out.inner_text()) == {"data": 42, "frozen": 42, "cookie": "blocked", "parent": "blocked",
                                                "storage": "blocked", "fetch": "blocked"}
        assert [u for u in frame_requests if not u.endswith("/sandbox")] == [], frame_requests   # nothing else got out
        assert failed.get("http://127.0.0.1:9/leak.png") == "csp", failed                     # the image was refused
        assert block.locator("iframe").get_attribute("sandbox") == "allow-scripts"
        b.close()
    assert not errors, errors
    # the shell is served with its own CSP and can only be framed by Canvas Hub
    from fastapi.testclient import TestClient

    from canvas_hub.web.app import create_app
    c = TestClient(create_app(HubAPI(hub), token="t" * 40, port=8765), base_url="http://127.0.0.1:8765")
    h = c.get("/sandbox").headers
    assert "connect-src 'none'" in h["content-security-policy"] and "frame-ancestors 'self'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in c.get("/").headers["content-security-policy"]


def test_dashboard_across_courses(hub, account, fixture_pkg):
    pkg, _ = _future_fixture(fixture_pkg, days=3)
    hub.importer.import_package(pkg)
    aid = account["id"]
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    text, err, display = run_tool_display(ctx, "show_dashboard", {"days": 7}, {"ui"})
    assert not err and display["component"] == "dashboard"
    row = next(c for c in display["courses"] if c["course_id"] == "4242")
    assert row["assignments"] == 1 and row["done"] == 0 and row["lost"] == 0 and row["due_soon"] == 1
    assert row["grade_percent"] is None and row["weighted"] is False                   # nothing graded yet
    assert row["next_due"]["title"] == "Project proposal" and "1 due in 7 d" in json.loads(text)["summary"]
    hub.grading.set(aid, "4242", {"groups": {"Assignments": {"weight": 100}}})
    row = next(c for c in HubAPI(hub).dashboard(aid, 2)["courses"] if c["course_id"] == "4242")
    assert row["weighted"] is True and row["due_soon"] == 0                            # due in 3 days, outside 2


# Controls, meters and graphics with no accessible name.
UNNAMED = r"""() => {
  const out = [];
  const nameOf = (el) => {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) return lb.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || "").join(" ").trim();
    return (el.getAttribute("aria-label") || (el.labels && [...el.labels].map((l) => l.textContent).join(" ")) || el.textContent || el.getAttribute("title") || el.getAttribute("alt") || el.getAttribute("placeholder") || (el.type === "submit" ? el.value : "")).trim();
  };
  for (const el of document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=menuitem], [role=meter], [role=progressbar], [role=img], svg, canvas, iframe")) {
    if (el.closest("[hidden]") || el.closest("svg") && el.tagName.toLowerCase() !== "svg") continue;
    const tag = el.tagName.toLowerCase();
    if (tag === "svg" && (el.getAttribute("aria-hidden") === "true" || el.closest("[aria-hidden=true]"))) continue;
    if (!nameOf(el)) out.push(`${tag}${el.className && typeof el.className === "string" ? "." + el.className.split(" ").join(".") : ""} in ${(el.closest("section,.card,nav,header") || {}).className || "?"}`);
  }
  return [...new Set(out)];
}"""


@pytest.mark.browser
def test_every_control_has_an_accessible_name(hub, account, fixture_pkg):
    from playwright.sync_api import expect, sync_playwright

    from .test_components import HMAW
    hub.importer.import_package(fixture_pkg)
    hub.annotations.create(account["id"], kind="task", title="Draft", task_state="in_progress")
    plan = [("show_score_model", {**HMAW, "pin": True}),
            ("show_checklist", {"title": "Week", "sections": [{"title": "A", "rows": [{"label": "One", "points": 2}]}]}),
            ("show_chart", {"labels": ["A", "B"], "kind": "stacked", "series": [{"name": "Earned", "values": [3, 4]}]}),
            ("show_heatmap", {"weeks": 4}), ("show_kanban", {}), ("show_dashboard", {}),
            ("show_table", {"columns": ["A", "B"], "rows": [["x", "1"]]})]
    with serve(hub, plan) as (base, tok, _), sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/assistant")
        pg.locator(".composer textarea").fill("everything")
        pg.get_by_role("button", name="Send").click()
        expect(pg.locator(".cmp-table")).to_be_visible(timeout=30000)
        found = {"assistant": pg.evaluate(UNNAMED)}
        for view in ("trackers", "notes", "notes?filter=board", "course/4242", "settings", "overview"):
            pg.goto(f"{base}/#/{view}")
            pg.wait_for_load_state("networkidle")
            pg.wait_for_timeout(500)                      # views render after their API calls return
            found[view] = pg.evaluate(UNNAMED)
        b.close()
    assert not any(found.values()), found


# ---- optional OCR ------------------------------------------------------------------------------------
STUB_TESSERACT = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "tesseract 5.3.4 (stub)"; exit 0; fi
if grep -q UNREADABLE "$1"; then echo "Error: cannot read image" >&2; exit 1; fi
CONF="${STUB_CONF:-91}"
printf 'level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext\\n'
printf '1\\t1\\t0\\t0\\t0\\t0\\t0\\t0\\t100\\t100\\t-1\\t\\n'
printf '5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t10\\t10\\t%s\\tPhotosynthesis\\n' "$CONF"
printf '5\\t1\\t1\\t1\\t1\\t2\\t0\\t0\\t10\\t10\\t%s\\tworksheet\\n' "$CONF"
printf '5\\t1\\t1\\t1\\t2\\t1\\t0\\t0\\t10\\t10\\t%s\\tQ1\\n' "$CONF"
printf '5\\t1\\t2\\t1\\t1\\t1\\t0\\t0\\t10\\t10\\t%s\\t%s\\n' "$CONF" "$(basename "$1")"
"""
STUB_PDFTOPPM = """#!/bin/sh
for last; do :; done
touch "$last-1.png" "$last-2.png"
"""

STUB_TESSERACT_WINDOWS = r"""
import os
import sys
from pathlib import Path

if sys.argv[1] == "--version":
    print("tesseract 5.3.4 (stub)")
    raise SystemExit(0)
if b"UNREADABLE" in Path(sys.argv[1]).read_bytes():
    print("Error: cannot read image", file=sys.stderr)
    raise SystemExit(1)
conf = os.environ.get("STUB_CONF", "91")
print("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext")
print("1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t")
for line, word, text in ((1, 1, "Photosynthesis"), (1, 2, "worksheet"), (2, 1, "Q1")):
    print(f"5\t1\t1\t1\t{line}\t{word}\t0\t0\t10\t10\t{conf}\t{text}")
print(f"5\t1\t1\t2\t1\t1\t0\t0\t10\t10\t{conf}\t{Path(sys.argv[1]).name}")
"""

STUB_PDFTOPPM_WINDOWS = r"""
import sys
from pathlib import Path

prefix = Path(sys.argv[-1])
prefix.with_name(prefix.name + "-1.png").touch()
prefix.with_name(prefix.name + "-2.png").touch()
"""


@pytest.fixture
def ocr_stubs(tmp_path, monkeypatch):
    if os.name == "nt":
        programs = (("tesseract", STUB_TESSERACT_WINDOWS), ("pdftoppm", STUB_PDFTOPPM_WINDOWS))
        for name, body in programs:
            script = tmp_path / f"{name}.py"
            script.write_text(body, encoding="utf-8")
            p = tmp_path / f"{name}.cmd"
            p.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
            monkeypatch.setenv(f"CANVAS_HUB_{name.upper()}", str(p))
    else:
        for name, body in (("tesseract", STUB_TESSERACT), ("pdftoppm", STUB_PDFTOPPM)):
            p = tmp_path / name
            p.write_text(body)
            p.chmod(0o755)
            monkeypatch.setenv(f"CANVAS_HUB_{name.upper()}", str(p))
    return tmp_path


def test_ocr_reads_tsv_into_lines_and_paragraphs():
    from canvas_hub import ocr
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\tHello\n5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t80\tworld\n"
           "5\t1\t1\t1\t2\t1\t0\t0\t1\t1\t70\tagain\n5\t1\t2\t1\t1\t1\t0\t0\t1\t1\t-1\t\n5\t1\t2\t1\t1\t2\t0\t0\t1\t1\t60\tNew\n")
    text, confs = ocr._parse_tsv(tsv)
    assert text == "Hello world\nagain\n\nNew" and confs == [90, 80, 70, 60]
    with pytest.raises(ValueError):
        ocr.valid_lang("eng; rm -rf /")


def test_ocr_is_optional_flags_confidence_and_makes_text_searchable(hub, account, fixture_pkg, ocr_stubs, monkeypatch):
    from canvas_hub import ocr
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    item = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1", (aid,))["id"]
    sha = hub.resources.store_bytes(b"\\x89PNG fake photo of a worksheet", "image/png")
    with hub.db.tx() as c:
        c.execute("INSERT INTO item_resources(item_id, resource_id, filename, observed_at) VALUES (?,?,?,?)",
                  (item, sha, "worksheet.png", "2026-10-30T00:00:00Z"))
    r = hub.resources.extract(sha, "worksheet.png")
    assert r["extraction_status"] == "image_only" and "OCR is off" in r["extraction_detail"]     # off by default
    assert hub.resources.ocr_pending() == 1
    with pytest.raises(InvalidInput):
        hub.run_ocr()
    api = HubAPI(hub)
    st = api.set_ocr({"enabled": True, "lang": "eng"})
    assert st["tesseract"] and st["version"] == "tesseract 5.3.4 (stub)" and st["pending"] == 1
    out = hub.run_ocr()
    assert out["pending"] == 0 and out["processed"][0]["status"] == "ocr" and out["processed"][0]["confidence"] == 91
    doc = hub.resources.describe(aid, sha)["extraction"]
    assert doc["ocr_confidence"] == 91 and doc["low_confidence"] is False and doc["tool"].startswith("tesseract")
    assert hub.resources.read_text(aid, sha)["text"].startswith("Photosynthesis worksheet\nQ1")
    hits = hub.search.search(aid, "photosynthesis")["results"]
    assert any(h.get("resource_id") == sha or (h.get("document") or {}).get("resource_id") == sha for h in hits), hits
    ctx = ToolContext(api=api, account_id=aid)
    got = json.loads(run_tool_display(ctx, "get_item", {"item_id": item}, {"read"})[0])
    assert any(d.get("ocr_confidence") == 91 and "caution" in d for d in got["documents"])
    # a poor scan is kept but flagged
    monkeypatch.setenv("STUB_CONF", "42")
    sha2 = hub.resources.store_bytes(b"\\x89PNG blurry", "image/png")
    r = hub.resources.extract(sha2, "blurry.jpg")
    assert r["extraction_status"] == "ocr_low_confidence" and r["ocr_confidence"] == 42
    assert "check it against the original" in r["extraction_detail"]
    # scanned PDFs go page by page through pdftoppm
    pdf = ocr_stubs / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 scanned")
    res = ocr.ocr_file(pdf, ".pdf", "eng")
    assert res.pages == 2 and "<!-- page 2 -->" in res.text and "p-2.png" in res.text and res.tool.endswith("pdftoppm")
    with pytest.raises(InvalidInput):
        api.set_ocr({"enabled": True, "lang": "eng;rm"})


@pytest.mark.skipif(not __import__("shutil").which("tesseract"), reason="tesseract is not installed")
def test_real_tesseract_reads_a_rendered_line(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    from canvas_hub import ocr
    monkeypatch.delenv("CANVAS_HUB_TESSERACT", raising=False)
    img = Image.new("L", (900, 120), 255)
    ImageDraw.Draw(img).text((20, 30), "Midterm on Friday in Room 2503", fill=0, font=ImageFont.load_default(size=40))
    p = tmp_path / "line.png"
    img.save(p)
    r = ocr.ocr_file(p, ".png", "eng")
    assert "Midterm" in r.text and r.confidence and r.confidence > 50


def test_documents_ocr_cannot_read_do_not_block_the_rest(hub, ocr_stubs):
    hub.resources.set_ocr_settings(True)
    bad = [hub.resources.store_bytes(f"UNREADABLE scan {i}".encode(), "image/png") for i in range(11)]
    good = hub.resources.store_bytes(b"a readable photo", "image/png")
    for sha in [*bad, good]:
        hub.resources.extract(sha, None)       # stored while OCR could not run yet: pending
    with hub.db.tx() as c:
        c.execute("UPDATE resources SET extraction_tool=NULL, extraction_status='image_only'")
    first = hub.run_ocr(limit=10)
    assert [x["status"] for x in first["processed"]] == ["image_only"] * 10 and first["failed"] == 10
    second = hub.run_ocr(limit=10)                                    # the queue moves on past the failures
    assert {x["resource_id"]: x["status"] for x in second["processed"]}[good] == "ocr"
    assert second["pending"] == 0 and second["failed"] == 11
    retry = hub.run_ocr(limit=50, retry_failed=True)                  # failures are retried only on request
    assert {x["resource_id"] for x in retry["processed"]} == set(bad) and retry["failed"] == 11
    assert "OCR failed" in retry["processed"][0]["detail"]


# ---- follow-ups from review ------------------------------------------------------------------
@pytest.mark.browser
def test_formula_inputs_cover_the_raw_range_and_bonus_points_count_in_the_page(hub, account):
    from playwright.sync_api import expect, sync_playwright

    from canvas_hub import projections
    aid = account["id"]
    v = hub.components.create(aid, "score_model", MODEL)
    hub.components.pin(aid, v["id"], True)
    bonus = {"title": "With bonus", "categories": [{"name": "All", "items": [
        {"id": "exam", "label": "Exam", "max": 100, "score": 80}, {"id": "extra", "label": "Bonus", "max": 0, "score": 5}]}]}
    assert projections.project(projections.normalize_projector(bonus), {})["percent"] == 85      # not 80
    p = hub.components.create(aid, "grade_projector", bonus)
    hub.components.pin(aid, p["id"], True)
    with serve(hub, []) as (base, tok, _), sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(f"{base}/?token={tok}")
        pg.goto(f"{base}/#/trackers")
        score = pg.locator(".cmp-score")
        slider = score.get_by_label("Value for Attendance (slider)", exact=True)
        assert slider.get_attribute("max") == "12"                   # attendances, not the 10 points
        expect(score.locator(".sl-big .num")).to_have_text("39")
        expect(pg.locator(".cmp-projector .sl-big")).to_contain_text("85")
        b.close()


def test_checklist_sync_keeps_in_progress_and_blocked_todos(hub, account):
    aid = account["id"]
    svc = hub.components
    v = svc.create(aid, "checklist", {"title": "T", "sync_todos": True, "sections": [
        {"id": "s", "rows": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}]}]})
    links = v["state"]["links"]
    hub.annotations.update(aid, links["a"], task_state="in_progress")         # moved on the board
    hub.annotations.update(aid, links["c"], task_state="blocked")
    v = svc.set_state(aid, v["id"], {**svc.get(aid, v["id"])["state"], "done": {"b": True}})
    states = {k: hub.annotations.get(aid, i)["task_state"] for k, i in links.items()}
    assert states == {"a": "in_progress", "b": "done", "c": "blocked"}
    svc.set_state(aid, v["id"], {**v["state"], "done": {"b": True, "c": True}})     # ticking still completes it
    assert hub.annotations.get(aid, links["c"])["task_state"] == "done"
