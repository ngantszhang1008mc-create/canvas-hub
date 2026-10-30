"""Planner, external-site view requests, and interactive assistant components."""

import json
import sys
from datetime import datetime, timedelta, timezone

import anyio
import pytest

from canvas_hub.agent_tools import ToolContext, run_tool
from canvas_hub.assistant import AssistantService
from canvas_hub.boundary import HubAPI
from canvas_hub.util import InvalidInput

from .conftest import variant
from .fake_llm import FakeLLM


def _future_fixture(fixture_pkg, days=4):
    """Fixture with the proposal due a few days from now, so study suggestions apply."""
    due = (datetime.now(timezone.utc) + timedelta(days=days)).replace(hour=15, minute=59, second=0, microsecond=0)
    iso = due.strftime("%Y-%m-%dT%H:%M:%SZ")

    def mut(p):
        for it in p["items"]:
            if it.get("canvas_id") == "7001" and it["type"] == "assignment":
                it["fields"]["due_at"] = {"raw": iso, "iso": iso, "source": "canvas_api_iso8601", "precision": "instant"}
    return variant(fixture_pkg, "future", "2026-10-30T02:00:00Z", mut), due


def test_planner_week_classes_suggestions_and_ics(hub, account, fixture_pkg):
    pkg, due = _future_fixture(fixture_pkg)
    hub.importer.import_package(pkg)
    aid = account["id"]
    p = hub.planner
    c = p.add_class(aid, title="SAMPLE1010 Lecture", weekday=1, start="14:00", end="15:20", location="Room 1")
    with pytest.raises(InvalidInput):
        p.add_class(aid, title="x", weekday=9, start="14:00", end="15:00")
    with pytest.raises(InvalidInput):
        p.add_class(aid, title="x", weekday=1, start="15:00", end="14:00")
    due_local = due.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Hong_Kong"))
    w = p.week(aid, week_of=due_local.date().isoformat(), suggestions=True, study_hours=3)
    evs = [e for d in w["days"] for e in d["events"]]
    now_week = p.week(aid, suggestions=True, study_hours=3)
    if now_week["week_start"] != w["week_start"]:
        evs += [e for d in now_week["days"] for e in d["events"]]
    assert any(e["kind"] == "class" and e["title"] == "SAMPLE1010 Lecture" for e in evs)
    assert any(e["kind"] == "deadline" and e["title"] == "Project proposal" and not e["done"] for e in evs)
    study = [e for e in evs if e["kind"] == "study"]
    assert len(study) >= 2 and all(e["suggested"] and e["start"] < due_local.isoformat() for e in study)
    assert len({e["start"][:10] for e in study}) == len(study)      # at most one block per day
    classes = [e for e in evs if e["kind"] == "class"]
    for s in study:        # suggestions never overlap classes
        assert not any(s["start"] < c_["end"] and c_["start"] < s["end"] for c_ in classes)
    assert hub.annotations.list(aid) == []    # suggestions are not stored
    ics = p.ics(aid, weeks=4)
    assert ics.startswith("BEGIN:VCALENDAR") and "RRULE:FREQ=WEEKLY" in ics and "SUMMARY:Due: Project proposal" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    p.delete_class(aid, c["id"])
    assert p.list_classes(aid) == []


def test_view_request_lifecycle(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    rq = hub.requests
    r = rq.create(aid, url="https://comp1023.example.edu/schedule", title="COMP1023 site", note="Use Chrome profile ngan",
                  course_id="4242")
    assert r["status"] == "open"
    assert rq.create(aid, url="https://comp1023.example.edu/schedule")["id"] == r["id"]   # no duplicates
    with pytest.raises(InvalidInput):
        rq.create(aid, url="file:///etc/passwd")
    rq.claim(aid, r["id"], "Codex")
    with pytest.raises(InvalidInput):
        rq.claim(aid, r["id"], "Another agent")            # claimed by someone else
    with pytest.raises(InvalidInput):
        rq.submit(aid, r["id"], "Another agent", content="x")
    done = rq.submit(aid, r["id"], "Codex", content="Week 5: Lab 3 due Friday 23:59. Midterm on Nov 20 in LTA.",
                     title="COMP1023 schedule")
    assert done["status"] == "done" and done["result_item_id"]
    item = hub.query.get_item(aid, done["result_item_id"])
    f = {x["field"]: x["value"] for x in item["fields"]}
    assert f["provided_by"] == "connected agent: Codex" and "Lab 3" in f["content"]
    hits = hub.search.search(aid, "midterm LTA")["results"]
    assert hits and hits[0]["item_id"] == done["result_item_id"]
    with pytest.raises(InvalidInput):
        rq.submit(aid, r["id"], "Codex", content="again")    # closed
    r2 = rq.create(aid, url="https://private.example.edu/")
    assert rq.decline(aid, r2["id"], "Codex", "login required")["status"] == "declined"
    r3 = rq.create(aid, url="https://another.example.edu/")
    assert rq.cancel(aid, r3["id"])["status"] == "cancelled"
    other = hub.identity.upsert_verified_account(account["instance_id"], {"canvas_user_id": "777"})
    assert rq.list(other["id"]) == []


def test_mcp_agent_fulfils_request(hub, account, fixture_pkg):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    hub.importer.import_package(fixture_pkg)
    req = hub.requests.create(account["id"], url="https://comp1023.example.edu/", title="COMP1023 site")
    home = str(hub.paths.home)

    async def main(flags):
        params = StdioServerParameters(command=sys.executable, args=["-m", "canvas_hub", "--home", home, "mcp", *flags])
        async with stdio_client(params) as streams:
            async with ClientSession(*streams[:2]) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                if "submit_view_result" not in names:
                    return names, None
                listed = json.loads((await s.call_tool("list_view_requests", {"status": "open"})).content[0].text)
                rid = listed["requests"][0]["id"]
                waited = json.loads((await s.call_tool("wait_for_view_requests", {"timeout_seconds": 5})).content[0].text)
                assert waited["requests"][0]["request_id"] == rid
                await s.call_tool("claim_view_request", {"request_id": rid, "agent_name": "Codex"})
                r = await s.call_tool("submit_view_result", {"request_id": rid, "agent_name": "Codex",
                                                             "content": "Assignment 2 is due 22 Nov."})
                return names, json.loads(r.content[0].text)
    names, _ = anyio.run(main, [])
    assert "list_view_requests" in names and "claim_view_request" not in names and "show_timetable" not in names
    names, res = anyio.run(main, ["--allow-fulfil"])
    assert res["request"]["status"] == "done"
    seen = HubAPI(hub).connected_agents()["agents"]
    assert seen and seen[0]["can_fulfil"] and seen[0]["last_seen"]
    assert hub.requests.view(account["id"], req["id"])["claimed_by"] == "Codex"


def test_assistant_components_and_planner_tools(hub, account, fixture_pkg):
    pkg, _ = _future_fixture(fixture_pkg)
    hub.importer.import_package(pkg)
    ctx = ToolContext(api=HubAPI(hub), account_id=account["id"])
    text, err = run_tool(ctx, "add_class_time", {"title": "Lecture", "weekday": "Tue", "start": "10:30", "end": "11:50"},
                         {"read", "notes"})
    assert not err and json.loads(text)["added"]["weekday"] == "Tuesday"
    fake = FakeLLM([("show_timetable", {"suggest_study": True}), ("show_deadlines", {"days_ahead": 30}),
                    ("show_chart", {"labels": ["A", "B"], "values": [80, 65], "unit": "%"})])
    try:
        svc = AssistantService(hub, HubAPI(hub))
        svc.update_config({"preset": "custom", "base_url": fake.url + "/v1", "model": "m", "api_key": "sk-x-123456789"})
        r = svc.send(account["id"], "Plan my week")
        svc.wait(r["conversation_id"], 30)
        msgs = svc.get_conversation(account["id"], r["conversation_id"])["messages"]
        displays = [res["display"] for m in msgs if m["role"] == "tool" for res in m["results"] if res.get("display")]
        assert [d["component"] for d in displays] == ["timetable", "deadlines", "chart"]
        assert displays[1]["deadlines"][0]["title"] == "Project proposal"
        # the model received a compact summary, not the component payload
        sent_tool_msgs = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
        assert all("display" not in json.loads(m["content"]) for m in sent_tool_msgs)
        assert json.loads(sent_tool_msgs[0]["content"])["shown_to_user"] is True
        # interactive tools are offered in-app
        assert {"show_timetable", "show_choices", "get_timetable"} <= {t["function"]["name"] for t in fake.requests[0]["tools"]}
    finally:
        fake.stop()


def test_study_task_does_not_complete_deadline(hub, account, fixture_pkg):
    pkg, due = _future_fixture(fixture_pkg)
    hub.importer.import_package(pkg)
    aid = account["id"]
    week_of = due.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Hong_Kong")).date().isoformat()

    def proposal():
        evs = [e for d in hub.planner.week(aid, week_of=week_of)["days"] for e in d["events"]]
        return next(e for e in evs if e["kind"] == "deadline" and e["title"] == "Project proposal")
    item_id = proposal()["item_id"]
    study = hub.annotations.create(aid, kind="task", title="Study: Project proposal", item_id=item_id,
                                   local_due=(due - timedelta(days=2)).isoformat())
    hub.annotations.update(aid, study["id"], task_state="done")
    assert proposal()["done"] is False                         # a finished study block is only a step
    marker = hub.annotations.create(aid, kind="task", title="Project proposal", item_id=item_id, task_state="done")
    assert proposal()["done"] is True                          # the undated "Mark done locally" marker
    hub.annotations.delete(aid, marker["id"])


def test_large_component_is_not_truncated():
    from canvas_hub.agent_tools import MAX_RESULT_CHARS, run_tool_display
    cols = [f"c{i}" for i in range(8)]
    rows = [["x" * 100] * 8 for _ in range(40)]
    text, err, display = run_tool_display(None, "show_table", {"columns": cols, "rows": rows}, {"ui"})
    assert not err and display["component"] == "table" and len(display["rows"]) == 40
    assert len(text) <= MAX_RESULT_CHARS + 100 and "display" not in text


def test_accepted_study_block_counts_towards_plan(hub, account, fixture_pkg):
    pkg, _ = _future_fixture(fixture_pkg, days=5)
    hub.importer.import_package(pkg)
    aid = account["id"]
    p = hub.planner

    def suggestions():
        weeks = {p.week(aid, suggestions=True, study_hours=4.5)["week_start"]: None}
        nxt = (datetime.fromisoformat(next(iter(weeks))) + timedelta(days=7)).date().isoformat()
        out = []
        for w in (None, nxt):
            out += [e for d in p.week(aid, week_of=w, suggestions=True, study_hours=4.5)["days"] for e in d["events"]
                    if e["kind"] == "study" and e["title"] == "Study: Project proposal"]
        return out
    before = suggestions()
    assert len(before) == 3                                     # 4.5 h = three 90-minute blocks
    first = before[0]
    hub.annotations.create(aid, kind="task", title=first["title"], item_id=first["for_item_id"], local_due=first["start"])
    after = suggestions()
    assert len(after) == 2 and first["start"][:10] not in {e["start"][:10] for e in after}
