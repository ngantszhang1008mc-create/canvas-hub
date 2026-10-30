"""Agent tool registry, shared by the MCP server and the in-app assistant.

Each tool is a thin, validated wrapper over ``HubAPI``. Tools are grouped by
permission so a client only gets what the user enabled:

    read     - query the local databank (always available)
    notes    - create/update/delete the user's local notes and to-dos
    collect  - start/cancel collection jobs (the app's own browser reads Canvas
               under the same scope, read-only and view-to-complete rules as
               the UI)
    settings - change the remembered collection scope
    ui       - show_* tools that render interactive components in the app's
               assistant chat (in-app only; not offered over MCP)
    fulfil   - claim and complete requests to read external sites (MCP agents
               with their own browser access, e.g. a course website)

No tool takes SQL, a filesystem path, a shell command, browser control or a
credential. Collected Canvas content is returned as data; it is untrusted and
must not be followed as instructions.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import dates
from .htmltools import to_text
from .model import CATEGORIES, ITEM_TYPES
from .util import HubError, InvalidInput

PERMISSIONS = ("read", "notes", "collect", "settings", "ui", "fulfil")
MAX_RESULT_CHARS = 24000


@dataclass
class ToolContext:
    api: Any                       # canvas_hub.boundary.HubAPI
    account_id: int | None = None  # fixed account (in-app assistant); None = take from args / single account
    worker: Any = None             # BrowserWorker when running inside the app
    conversation_id: int | None = None   # in-app chat the call belongs to (components link back to it)
    permissions: frozenset[str] | None = None   # set per call by run_tool_display; None = not restricted


@dataclass
class AgentTool:
    name: str
    description: str
    permission: str
    properties: dict = field(default_factory=dict)
    required: list = field(default_factory=list)
    handler: Callable[[ToolContext, dict], Any] = None
    account_scoped: bool = True

    def schema(self, *, with_account: bool = False) -> dict:
        props = dict(self.properties)
        if with_account and self.account_scoped:
            props["account_id"] = {"type": "integer",
                                   "description": "Canvas Hub account id (optional when only one account exists)"}
        return {"type": "object", "properties": props, "required": list(self.required), "additionalProperties": False}


# ---------------------------------------------------------------- validation
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate_args(tool: AgentTool, args: Any, *, with_account: bool = False) -> dict:
    """Strict validation of tool input (also guards against partial or
    malformed input from streamed tool calls)."""
    if not isinstance(args, dict):
        raise InvalidInput("tool input must be a JSON object", code="invalid_tool_input")
    schema = tool.schema(with_account=with_account)
    props = schema["properties"]
    extra = set(args) - set(props)
    if extra:
        raise InvalidInput(f"unknown argument(s): {sorted(extra)}", code="invalid_tool_input")
    for r in schema["required"]:
        if r not in args or args[r] is None:
            raise InvalidInput(f"missing required argument '{r}'", code="invalid_tool_input")
    for k, v in args.items():
        if v is None:
            continue
        spec = props[k]
        t = _TYPES[spec["type"]]
        if isinstance(v, bool) and spec["type"] in ("integer", "number"):
            raise InvalidInput(f"'{k}' must be {spec['type']}", code="invalid_tool_input")
        if not isinstance(v, t):
            raise InvalidInput(f"'{k}' must be {spec['type']}", code="invalid_tool_input")
        if "enum" in spec and v not in spec["enum"]:
            raise InvalidInput(f"'{k}' must be one of {spec['enum']}", code="invalid_tool_input")
        if spec["type"] == "array" and "items" in spec and "enum" in spec["items"]:
            bad = [x for x in v if x not in spec["items"]["enum"]]
            if bad:
                raise InvalidInput(f"'{k}' has invalid values {bad}", code="invalid_tool_input")
        if spec["type"] == "integer" and ("minimum" in spec and v < spec["minimum"] or "maximum" in spec and v > spec["maximum"]):
            raise InvalidInput(f"'{k}' out of range", code="invalid_tool_input")
    return args


def _account(ctx: ToolContext, args: dict) -> int:
    if ctx.account_id is not None:
        return ctx.account_id
    if args.get("account_id") is not None:
        return int(args["account_id"])
    accts = [a for i in ctx.api.list_identities()["instances"] for a in i["accounts"]]
    if len(accts) == 1:
        return accts[0]["id"]
    if not accts:
        raise InvalidInput("No Canvas account yet. Sign in and collect in the Canvas Hub app first.", code="no_account")
    raise InvalidInput(f"Several accounts exist; pass account_id (one of {[a['id'] for a in accts]})",
                       code="account_required")


def _clip(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    return s if len(s) <= n else s[:n] + f"… [truncated, {len(s) - n} more characters]"


def _date_view(v, tz: str | None):
    if not isinstance(v, dict) or "raw" not in v:
        return v
    out = {"local": dates.format_local(v, tz) if v.get("iso") else None, "raw": v.get("raw")}
    if not v.get("iso"):
        out["note"] = v.get("note") or "time not established; shown as written"
    return out


# ---------------------------------------------------------------- handlers
def h_list_accounts(ctx, a):
    out = []
    for inst in ctx.api.list_identities()["instances"]:
        for acc in inst["accounts"]:
            if ctx.account_id is not None and acc["id"] != ctx.account_id:
                continue
            out.append({"account_id": acc["id"], "canvas": inst["origin"], "name": acc["display_name"],
                        "canvas_user_id": acc["canvas_user_id"], "time_zone": acc["time_zone"],
                        "last_verified_at": acc["last_verified_at"]})
    return {"accounts": out}


def h_list_courses(ctx, a):
    aid = _account(ctx, a)
    cs = ctx.api.list_courses(aid)["courses"]
    return {"courses": [{"course_id": c["course_id"], "title": c["title"], "code": c["course_code"], "term": c["term"],
                         "section": c["listing_section"], "access": c["access"], "item_counts": c["item_counts"],
                         "last_read": c["last_observed_at"]} for c in cs]}


def h_search(ctx, a):
    aid = _account(ctx, a)
    r = ctx.api.search(aid, a["query"], course_id=a.get("course_id"), types=a.get("types"),
                       include_documents=a.get("include_documents", True), limit=a.get("limit", 10),
                       cursor=a.get("cursor"))
    return {"results": [{"item_id": x["item_id"], "type": x["item_type"], "title": x["title"],
                         "course": x["course_title"], "snippet": x["snippet"].replace("[[", "«").replace("]]", "»"),
                         "document_resource_id": x["resource_id"] or None, "source_url": x["source_url"],
                         "last_read": x["last_observed_at"], "limitations": x["limitations"]} for x in r["results"]],
            "next_cursor": r["next_cursor"]}


def _item_view(ctx, aid, item_id, body_chars=8000):
    it = ctx.api.read_item(aid, item_id)
    tz = it.get("time_zone")
    fields = {}
    for f in it["fields"]:
        if f.get("volatile"):
            continue
        v = f["value"]
        if f["kind"] == "html":
            v = _clip(to_text(v) if v else v, body_chars)
        elif f["kind"] == "date":
            v = _date_view(v, tz)
        elif isinstance(v, (list, dict)):
            s = json.dumps(v, ensure_ascii=False)
            v = json.loads(s) if len(s) <= 4000 else _clip(s, 4000)
        fields[f["field"]] = {"value": v, "last_read": f["last_observed_at"]}
    return {
        "item_id": it["id"], "type": it["item_type"], "title": it["title"], "course_id": it["course_canvas_id"],
        "course": it.get("course_title"), "source_url": it["source_url"], "presence": it["presence"],
        "presence_detail": it["presence_detail"], "first_seen": it["first_observed_at"], "last_read": it["last_observed_at"],
        "freshness": it["freshness"], "fields": fields,
        "documents": [{"resource_id": r["resource_id"], "filename": r["filename"], "extraction": r["extraction_status"],
                       "chars": r["extracted_chars"]} for r in it["resources"]],
        "links": [{"relation": ln["relation"], "item_id": ln["item_id"], "type": ln["item_type"], "title": ln["title"],
                   "read": ln["collected"]} for ln in it["links_out"][:40]],
        "referenced_from": [{"relation": ln["relation"], "item_id": ln["item_id"], "title": ln["title"]}
                            for ln in it["links_in"][:20]],
        "date_conflicts": [d["detail"] for d in it["discrepancies"]],
        "submission_item_id": it.get("submission_item_id"),
        "my_notes": [{"note_id": n["id"], "kind": n["kind"], "title": n["title"], "body": n["body"],
                      "task_state": n["task_state"]} for n in it["annotations"]],
    }


def h_get_item(ctx, a):
    return _item_view(ctx, _account(ctx, a), a["item_id"])


def h_item_history(ctx, a):
    aid = _account(ctx, a)
    h = ctx.api.item_history(aid, a["item_id"])
    evs = []
    for e in h["events"][:60]:
        evs.append({"kind": e["kind"], "field": e["field"], "observed_at": e["observed_at"], "baseline": e["baseline"],
                    "old": _clip(json.dumps(e["old"], ensure_ascii=False), 600) if e["old"] is not None else None,
                    "new": _clip(json.dumps(e["new"], ensure_ascii=False), 600) if e["new"] is not None else None})
    return {"item_id": h["item_id"], "title": h["title"], "revisions": len(h["revisions"]), "events": evs}


def h_list_items(ctx, a):
    aid = _account(ctx, a)
    r = ctx.api.list_items(aid, course_id=a.get("course_id"), item_type=a.get("type"), limit=a.get("limit", 50),
                           cursor=a.get("cursor"))
    return {"items": [{"item_id": i["id"], "type": i["item_type"], "title": i["title"], "course_id": i["course_canvas_id"],
                       "presence": i["presence"], "last_read": i["last_observed_at"]} for i in r["items"]],
            "next_cursor": r["next_cursor"]}


def h_announcements(ctx, a):
    aid = _account(ctx, a)
    hub = ctx.api.hub
    params: list = [aid]
    sql = "SELECT id FROM items WHERE account_id=? AND item_type='announcement' AND current_revision>0"
    if a.get("course_id"):
        sql += " AND course_canvas_id=?"
        params.append(str(a["course_id"]))
    rows = hub.db.query(sql, params)
    anns = []
    for r in rows:
        it = ctx.api.read_item(aid, r["id"])
        f = {x["field"]: x for x in it["fields"]}
        posted = (f.get("posted_at") or {}).get("value")
        anns.append((dates.compare_key(posted) or "", it, f, posted))
    anns.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, it, f, posted in anns[: a.get("limit", 10)]:
        msg = (f.get("message") or {}).get("value")
        out.append({"item_id": it["id"], "title": it["title"], "course": it.get("course_title"),
                    "posted": _date_view(posted, it.get("time_zone")), "author": (f.get("author") or {}).get("value"),
                    "text": _clip(to_text(msg) if msg else None, a.get("body_chars", 1500)),
                    "source_url": it["source_url"], "last_read": it["last_observed_at"]})
    return {"announcements": out, "total_stored": len(anns)}


def h_deadlines(ctx, a):
    aid = _account(ctx, a)
    now = datetime.now(timezone.utc)
    start = a.get("start") or (now - timedelta(days=1)).isoformat()
    end = a.get("end") or (now + timedelta(days=a.get("days_ahead", 14))).isoformat()
    r = ctx.api.deadlines(aid, start=start, end=end, time_zone=a.get("time_zone"), course_id=a.get("course_id"),
                          include_calendar=a.get("include_calendar", True), limit=100)
    out = []
    for d in r["deadlines"]:
        s = d.get("submission") or {}
        out.append({"item_id": d["item_id"], "type": d["item_type"], "title": d["title"], "course": d["course_title"],
                    "due_local": d["date_local"], "due_raw": d["date"].get("raw"), "date_last_read": d["date_observed_at"],
                    "points": d.get("points_possible"), "location": d.get("location_name"),
                    "submission": {k: s.get(k) for k in ("workflow_state", "submitted_at", "late", "missing", "excused",
                                                         "score", "grade", "grade_visible", "observed_at", "state") if k in s} or None,
                    "marked_done_locally": d.get("done_locally"), "my_local_tasks": d.get("local_tasks"),
                    "source_url": d["source_url"]})
    return {"time_zone": r["time_zone"], "range": r["range"], "deadlines": out,
            "unresolved_dates": [{"item_id": u["item_id"], "title": u["title"], "as_written": u["date"].get("raw")}
                                 for u in r["unresolved_dates"]],
            "note": "Submission states are Canvas-reported as of date_last_read/observed_at. Local tasks are the user's own markers."}


def h_changes(ctx, a):
    aid = _account(ctx, a)
    r = ctx.api.changes(aid, cursor=a.get("cursor"), since=a.get("since"),
                        include_baseline=a.get("include_baseline", False), limit=a.get("limit", 50))
    return {"events": [{"kind": e["kind"], "field": e["field"], "item_id": e["item_id"], "type": e["item_type"],
                        "title": e["title"], "course": e["course_title"], "observed_at": e["observed_at"],
                        "old": _clip(json.dumps(e["old"], ensure_ascii=False), 400) if e["old"] is not None else None,
                        "new": _clip(json.dumps(e["new"], ensure_ascii=False), 400) if e["new"] is not None else None}
                       for e in r["events"]],
            "next_cursor": r["next_cursor"], "has_more": r["has_more"],
            "legend": {"first_observed": "seen for the first time (not necessarily newly published)",
                       "field_changed": "an actual edit", "field_added": "a field read for the first time, not an edit",
                       "not_in_latest_index": "missing from a complete list; not proven deleted",
                       "source_unavailable": "its own page returned not-found/forbidden"}}


def h_coverage(ctx, a):
    aid = _account(ctx, a)
    c = ctx.api.coverage(aid, course_id=a.get("course_id"))
    recs = [{"course": r["course_title"] or r["course_id"] or "account", "category": r["category"],
             "status": r["latest"]["status"], "bodies": r["latest"]["body_depth"], "reason": r["latest"]["reason"],
             "as_of": r["latest"]["observed_at"], "last_complete": r["last_complete_at"]} for r in c["records"]]
    return {"records": recs, "unfinished_jobs": [{"job_id": j["id"], "state": j["state"], "message": j["message"]}
                                                 for j in c["unfinished_jobs"]]}


def h_freshness(ctx, a):
    return ctx.api.freshness(_account(ctx, a))


def h_read_document(ctx, a):
    aid = _account(ctx, a)
    r = ctx.api.read_resource_text(aid, a["resource_id"], offset=a.get("offset", 0), limit=min(a.get("limit", 12000), 20000))
    return {"resource_id": r["resource_id"], "extraction": r["extraction"], "text": r["text"], "offset": r["offset"],
            "next_offset": r["next_offset"], "total_chars": r["total_chars"],
            "items": [{"item_id": i["item_id"], "title": i["title"], "source_url": i["source_url"]} for i in r["items"]]}


def h_signin(ctx, a):
    aid = _account(ctx, a)
    acct = ctx.api.get_account(aid)
    out = {"account_id": aid, "canvas": acct["origin"], "last_verified_at": acct["last_verified_at"]}
    if ctx.worker is not None:
        s = ctx.worker.session(acct["instance_id"])
        out.update(status=s.get("status"), message=s.get("message"))
    else:
        out["status"] = "unknown_outside_app"
        out["message"] = "Live sign-in state is only known inside the running Canvas Hub app."
    waiting = ctx.api.hub.db.query("SELECT id FROM jobs WHERE account_id=? AND state='needs_login'", (aid,))
    if waiting:
        out["jobs_waiting_for_sign_in"] = [r["id"] for r in waiting]
    return out


def _job_view(j):
    t = j.get("tasks") or {}
    return {"job_id": j["id"], "mode": j["mode"], "state": j["state"], "message": j["message"], "error": j["error"],
            "tasks": t, "created_at": j["created_at"], "finished_at": j["finished_at"],
            "changes_recorded": (j.get("stats") or {}).get("events"),
            "note": (j.get("stats") or {}).get("note")}


def h_list_jobs(ctx, a):
    aid = _account(ctx, a)
    return {"jobs": [_job_view(j) for j in ctx.api.list_jobs(aid, limit=a.get("limit", 5))["jobs"]]}


def h_get_job(ctx, a):
    return _job_view(ctx.api.job_status(_account(ctx, a), a["job_id"]))


def h_start_collection(ctx, a):
    aid = _account(ctx, a)
    scope = {}
    if a.get("course_ids"):
        scope["courses"] = [str(x) for x in a["course_ids"]]
    if a.get("categories"):
        scope["categories"] = ["courses", *a["categories"]]
    job = ctx.api.request_collection(aid, mode=a.get("mode", "quick"), scope=scope or None)
    v = _job_view(job)
    v["next_step"] = "Use wait_for_job to follow it. Canvas is read by the app's own signed-in browser."
    return v


def h_wait_job(ctx, a):
    aid = _account(ctx, a)
    deadline = time.monotonic() + min(a.get("timeout_seconds", 120), 300)
    while True:
        j = ctx.api.job_status(aid, a["job_id"])
        if j["state"] not in ("queued", "running", "cancel_requested") or time.monotonic() >= deadline:
            v = _job_view(j)
            if j["state"] == "queued":
                v["hint"] = "Still queued: the Canvas Hub app must be running (it owns the browser) and signed in."
            if j["state"] == "needs_login":
                v["hint"] = "The user must sign in to Canvas in the app; the job then resumes by itself."
            return v
        time.sleep(1.5)


def h_cancel_job(ctx, a):
    return _job_view(ctx.api.cancel_job(_account(ctx, a), a["job_id"]))


def h_list_notes(ctx, a):
    aid = _account(ctx, a)
    r = ctx.api.list_annotations(aid, item_id=a.get("item_id"), kind=a.get("kind"), task_state=a.get("task_state"))
    return {"notes": [{"note_id": n["id"], "kind": n["kind"], "title": n["title"], "body": _clip(n["body"], 2000),
                       "task_state": n["task_state"], "local_due": n["local_due"], "item_id": n["item_id"],
                       "item_title": n["item_title"], "updated_at": n["updated_at"]} for n in r["annotations"]]}


def h_create_note(ctx, a):
    aid = _account(ctx, a)
    data = {k: a[k] for k in ("kind", "title", "body", "item_id", "local_due", "task_state") if a.get(k) is not None}
    n = ctx.api.create_annotation(aid, data)
    return {"created": {"note_id": n["id"], "kind": n["kind"], "title": n["title"], "task_state": n["task_state"]},
            "note": "Stored locally only; nothing was sent to Canvas."}


def h_update_note(ctx, a):
    aid = _account(ctx, a)
    data = {k: a[k] for k in ("title", "body", "task_state", "local_due") if k in a}
    n = ctx.api.update_annotation(aid, a["note_id"], data)
    return {"updated": {"note_id": n["id"], "title": n["title"], "task_state": n["task_state"], "body": _clip(n["body"], 500)}}


def h_delete_note(ctx, a):
    return ctx.api.delete_annotation(_account(ctx, a), a["note_id"])


def h_update_scope(ctx, a):
    aid = _account(ctx, a)
    scope = {}
    if "courses" in a:
        c = a["courses"]
        scope["courses"] = c if c in ("current", "all") else [x.strip() for x in c.split(",") if x.strip()]
    if "categories" in a:
        scope["categories"] = a["categories"]
    for k in ("download_files", "calendar_days_ahead", "calendar_days_back"):
        if k in a:
            scope[k] = a[k]
    return {"scope": ctx.api.set_scope(aid, scope),
            "note": "Opening view-to-complete module items can only be allowed by the user in the app."}


def h_timetable(ctx, a):
    aid = _account(ctx, a)
    return ctx.api.hub.planner.week(aid, week_of=a.get("week_of"), suggestions=a.get("suggest_study", False),
                                    study_hours=a.get("study_hours", 3))


def h_list_classes(ctx, a):
    return {"class_times": [{k: c[k] for k in ("id", "title", "weekday_name", "start_time", "end_time", "location",
                                               "course_canvas_id", "valid_from", "valid_until")}
                            for c in ctx.api.hub.planner.list_classes(_account(ctx, a))]}


_WEEKDAY = {n.lower(): i for i, n in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}


def h_add_class(ctx, a):
    wd = a["weekday"].strip().lower()
    wd = _WEEKDAY.get(wd) if wd in _WEEKDAY else next((i for n, i in _WEEKDAY.items() if n.startswith(wd[:3])), None)
    if wd is None:
        raise InvalidInput("weekday must be a day name, e.g. Tuesday")
    c = ctx.api.hub.planner.add_class(_account(ctx, a), title=a["title"], weekday=wd, start=a["start"], end=a["end"],
                                      course_id=a.get("course_id"), location=a.get("location"),
                                      valid_from=a.get("valid_from"), valid_until=a.get("valid_until"))
    return {"added": {"class_id": c["id"], "title": c["title"], "weekday": c["weekday_name"], "start": c["start_time"],
                      "end": c["end_time"]}, "note": "Saved in your local timetable only."}


def h_remove_class(ctx, a):
    return ctx.api.hub.planner.delete_class(_account(ctx, a), a["class_id"])


def _req_view(r):
    return {k: r.get(k) for k in ("id", "url", "title", "note", "course_canvas_id", "status", "requested_by", "claimed_by",
                                  "claimed_at", "result_item_id", "result_note", "created_at", "completed_at")}


def h_request_view(ctx, a):
    r = ctx.api.hub.requests.create(_account(ctx, a), url=a["url"], title=a.get("title"), note=a.get("note"),
                                    course_id=a.get("course_id"), requested_by="assistant" if ctx.account_id else "user")
    return {"request": _req_view(r), "note": "A connected agent with its own access can now read this site and send back "
                                             "its content. Canvas Hub itself does not open it."}


def h_list_requests(ctx, a):
    return {"requests": [_req_view(r) for r in ctx.api.hub.requests.list(_account(ctx, a), a.get("status"))]}


def h_claim_request(ctx, a):
    return {"request": _req_view(ctx.api.hub.requests.claim(_account(ctx, a), a["request_id"], a["agent_name"])),
            "next_step": "Read the page with your own access, then call submit_view_result with the text you read "
                         "(or decline_view_request if you cannot access it)."}


def h_submit_request(ctx, a):
    r = ctx.api.hub.requests.submit(_account(ctx, a), a["request_id"], a["agent_name"], content=a["content"],
                                    title=a.get("title"), source_url=a.get("source_url"), read_at=a.get("read_at"))
    return {"request": _req_view(r), "stored_item_id": r["result_item_id"]}


def h_decline_request(ctx, a):
    return {"request": _req_view(ctx.api.hub.requests.decline(_account(ctx, a), a["request_id"], a["agent_name"], a["reason"]))}


# ---- interactive components (rendered by the app's chat UI) --------------------------
def _display(component: str, data: dict, summary: str) -> dict:
    return {"display": {"component": component, **data}, "summary": summary}


def h_show_deadlines(ctx, a):
    d = h_deadlines(ctx, {k: v for k, v in a.items() if k in ("days_ahead", "course_id")} | {"include_calendar": False})
    return _display("deadlines", {"deadlines": d["deadlines"], "time_zone": d["time_zone"]},
                    f"Showed {len(d['deadlines'])} deadline(s) as cards: "
                    + "; ".join(f"{x['title']} ({x['due_local']}, {(x['submission'] or {}).get('workflow_state') or 'no status'})"
                                for x in d["deadlines"][:15]))


def h_show_timetable(ctx, a):
    w = h_timetable(ctx, a)
    n = sum(len(d["events"]) for d in w["days"])
    sugg = [e for d in w["days"] for e in d["events"] if e["kind"] == "study"]
    return _display("timetable", {"week": w}, f"Showed the week of {w['week_start']} with {n} entries"
                    + (f", including {len(sugg)} suggested study block(s): " + "; ".join(f"{e['title']} {e['start']}" for e in sugg)
                       if sugg else "") + (". No class times are defined yet." if not w["classes_defined"] else "."))


def h_show_items(ctx, a):
    aid = _account(ctx, a)
    cards = []
    for iid in a["item_ids"][:12]:
        try:
            v = _item_view(ctx, aid, int(iid), body_chars=400)
        except HubError:
            continue
        body = next((f["value"] for k, f in v["fields"].items() if k in ("message", "body", "description", "content")
                     and isinstance(f["value"], str)), None)
        due = v["fields"].get("due_at", {}).get("value")
        cards.append({"item_id": v["item_id"], "type": v["type"], "title": v["title"], "course": v["course"],
                      "excerpt": body, "due": due.get("local") if isinstance(due, dict) else None,
                      "source_url": v["source_url"], "last_read": v["last_read"]})
    return _display("items", {"items": cards}, f"Showed {len(cards)} item card(s): " + "; ".join(c["title"] or "" for c in cards))




def h_show_grades(ctx, a):
    aid = _account(ctx, a)
    hub = ctx.api.hub
    sql = "SELECT id, source_key FROM items WHERE account_id=? AND item_type='submission' AND current_revision>0"
    params: list = [aid]
    if a.get("course_id"):
        sql += " AND course_canvas_id=?"
        params.append(str(a["course_id"]))
    labels, values, notes = [], [], []
    for r in hub.db.query(sql, params):
        f = hub.query._fields(r["id"])
        asg = hub.db.one("SELECT id, title FROM items WHERE account_id=? AND item_type='assignment' AND source_key=?",
                         (aid, r["source_key"]))
        if not asg:
            continue
        pts = hub.query._fields(asg["id"]).get("points_possible")
        if f.get("score") is not None and pts:
            labels.append((asg["title"] or "")[:40])
            values.append(round(100 * float(f["score"]) / float(pts), 1))
        elif f.get("workflow_state") == "graded" and f.get("grade_visible") is False:
            notes.append(f"{asg['title']}: graded, score hidden")
    return _display("chart", {"title": "Scores (% of points)", "labels": labels[:24], "values": values[:24], "unit": "%",
                              "max": 100, "notes": notes[:10]},
                    f"Showed {len(values)} visible score(s): "
                    + "; ".join(f"{l_} {v}%" for l_, v in zip(labels, values, strict=True))
                    + (f". Hidden: {', '.join(notes[:5])}" if notes else "") + ".")


def h_show_choices(ctx, a):
    opts = [str(o)[:120] for o in a["options"]][:6]
    return _display("choices", {"question": a["question"][:300], "options": opts},
                    "Showed buttons; the user's click arrives as their next message.")


# ---- stateful components, artifacts and viewers ------------------------------------
def _component_display(ctx, view: dict, verb: str) -> dict:
    desc = ctx.api.hub.components.describe(view_account(ctx, view), view["id"])
    t = desc["totals"]
    if view["kind"] == "score_model":
        head = (f"{verb} score model #{view['id']} '{view['title']}': booked {t['booked']:g}, projected {t['projected']:g}, "
                f"ceiling {t['ceiling']:g}, lost {t['lost']:g}")
        if t.get("threshold") is not None:
            head += f", threshold {t['threshold']:g} ({'pass' if t['passes'] else 'below'}, margin {t['margin']:g})"
    else:
        head = (f"{verb} checklist #{view['id']} '{view['title']}': {t['done_count']}/{t['count']} done"
                + (f", {t['done_points']:g}/{t['points']:g} points" if t["points"] else ""))
    return {"display": {"component": view["kind"], "component_id": view["id"], "view": view},
            "summary": head + ". The user can change it in place; read it back with get_component.",
            "component_id": view["id"]}


def view_account(ctx, view: dict) -> int:
    return ctx.account_id if ctx.account_id is not None else ctx.api.hub.db.one(
        "SELECT account_id FROM components WHERE id=?", (view["id"],))["account_id"]


def h_show_score_model(ctx, a):
    aid = _account(ctx, a)
    spec = {k: a[k] for k in ("title", "threshold", "unit_label", "buckets", "scenarios") if a.get(k) is not None}
    v = ctx.api.hub.components.create(aid, "score_model", spec, conversation_id=ctx.conversation_id)
    if a.get("pin"):
        v = ctx.api.hub.components.pin(aid, v["id"], True)
    return _component_display(ctx, v, "Created")


def _need_notes_for_todos(ctx):
    """Linked checklists create and complete to-dos, so they need the notes permission too."""
    if ctx.permissions is not None and "notes" not in ctx.permissions:
        raise InvalidInput("a checklist linked to to-dos changes your to-dos; allow notes and to-dos in the assistant "
                           "settings, or use sync_todos: false", code="notes_permission_required")


def h_show_checklist(ctx, a):
    aid = _account(ctx, a)
    if a.get("sync_todos"):
        _need_notes_for_todos(ctx)
    spec = {k: a[k] for k in ("title", "sections", "sync_todos") if a.get(k) is not None}
    v = ctx.api.hub.components.create(aid, "checklist", spec, conversation_id=ctx.conversation_id)
    if a.get("pin"):
        v = ctx.api.hub.components.pin(aid, v["id"], True)
    return _component_display(ctx, v, "Created")


def h_get_component(ctx, a):
    return ctx.api.hub.components.describe(_account(ctx, a), a["component_id"])


def h_list_components(ctx, a):
    aid = _account(ctx, a)
    rows = ctx.api.hub.components.list(aid, pinned=a.get("pinned"), limit=50)
    return {"components": [{"component_id": v["id"], "kind": v["kind"], "title": v["title"], "pinned": v["pinned"],
                            "updated_at": v["updated_at"]} for v in rows]}


def h_update_component(ctx, a):
    aid = _account(ctx, a)
    svc = ctx.api.hub.components
    if a.get("spec") is None and a.get("state") is None and a.get("pin") is None:
        raise InvalidInput("give spec, state or pin")
    v = svc.get(aid, a["component_id"])
    if v["kind"] == "checklist" and (a.get("spec") is not None or a.get("state") is not None):
        if (a["spec"] if isinstance(a.get("spec"), dict) else v["spec"]).get("sync_todos"):   # the spec in force after the call
            _need_notes_for_todos(ctx)
    if a.get("spec") is not None:
        v = svc.update_spec(aid, v["id"], a["spec"], by="assistant")
    if a.get("state") is not None:
        v = svc.set_state(aid, v["id"], a["state"], by="assistant")
    if a.get("pin") is not None:
        v = svc.pin(aid, v["id"], a["pin"])
    return _component_display(ctx, v, "Updated")


def h_course_score_lines(ctx, a):
    """Draft score-model buckets from a course's assignments and submission states."""
    aid = _account(ctx, a)
    hub = ctx.api.hub
    now = datetime.now(timezone.utc)
    lines, groups = [], {}
    for r in hub.db.query("""SELECT id, canvas_id, title FROM items WHERE account_id=? AND item_type='assignment'
                             AND course_canvas_id=? AND current_revision>0 ORDER BY id""", (aid, str(a["course_id"]))):
        f = hub.query._fields(r["id"])
        pts = f.get("points_possible")
        if not pts or f.get("omit_from_final_grade"):
            continue
        sub = hub.query._submission(aid, r["canvas_id"]) or {}
        due = dates.instant(f.get("due_at")) if f.get("due_at") else None
        line = {"id": f"a{r['canvas_id']}", "label": (r["title"] or "")[:120], "max": float(pts), "item_id": r["id"],
                "group": f.get("assignment_group")}
        if due:
            line["due"] = due.isoformat()
        if sub.get("excused"):
            line["status"] = "excused"
        elif sub.get("score") is not None and sub.get("workflow_state") == "graded":
            line.update(status="earned", earned=float(sub["score"]))
        elif sub.get("missing") or (due and due < now and not sub.get("submitted_at")):
            line.update(status="lost", earned=0.0)
        elif f.get("locked_for_user") and (not due or due < now):
            line["status"] = "locked"
        else:
            line["status"] = "pending"
        if f.get("assignment_group_weight"):
            groups[f.get("assignment_group")] = f.get("assignment_group_weight")
        lines.append(line)
    return {"course_id": str(a["course_id"]), "buckets": lines, "group_weights": groups,
            "note": "Drafted from Canvas points and submission states. Gating rules (all-or-nothing, minimum hours) and "
                    "pass thresholds are not in Canvas data: ask the user or read the syllabus, then pass the buckets to "
                    "show_score_model. Graded = earned; missing or past due without a submission = lost."}


def _num_or_text(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)[:300]


def h_show_table_v2(ctx, a):
    cols = [str(c)[:60] for c in a["columns"]][:12]
    rows = [[_num_or_text(x) for x in r][:len(cols)] for r in a["rows"][:200] if isinstance(r, list)]
    types = [t if t in ("text", "number", "date") else "text" for t in (a.get("column_types") or [])][:len(cols)]
    totals = [c for c in (a.get("totals") or []) if c in cols]
    group_by = a.get("group_by") if a.get("group_by") in cols else None
    details = [str(d)[:4000] for d in (a.get("details") or [])][:len(rows)]
    item_ids = [int(x) if isinstance(x, int) else None for x in (a.get("item_ids") or [])][:len(rows)]
    return _display("table", {"title": (a.get("title") or "")[:120], "columns": cols, "rows": rows, "column_types": types,
                              "totals": totals, "group_by": group_by, "details": details, "item_ids": item_ids},
                    f"Showed a table with {len(rows)} row(s) (sortable, filterable"
                    + (f", totals of {', '.join(totals)}" if totals else "") + (f", grouped by {group_by}" if group_by else "") + ").")


def h_show_chart_v2(ctx, a):
    kind = a.get("kind") or "bar"
    labels = [str(x)[:40] for x in a["labels"]][:24]
    series = []
    if a.get("series"):
        for s_ in a["series"][:6]:
            if not isinstance(s_, dict) or not isinstance(s_.get("values"), list):
                raise InvalidInput("each series needs a name and values")
            vals = [float(v) for v in s_["values"][:len(labels)] if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if len(vals) != len(labels):
                raise InvalidInput(f"series {s_.get('name')!r}: one number per label")
            series.append({"name": str(s_.get("name") or f"Series {len(series) + 1}")[:40], "values": vals})
    else:
        vals = [float(v) for v in (a.get("values") or [])[:len(labels)] if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(vals) != len(labels):
            raise InvalidInput("values must be numbers, one per label")
        series = [{"name": a.get("series_name") or "Value", "values": vals}]
    if kind == "bar" and len(series) > 1:
        kind = "grouped"
    return _display("chart", {"title": (a.get("title") or "")[:120], "kind": kind, "labels": labels, "series": series,
                              "values": series[0]["values"], "unit": (a.get("unit") or "")[:12], "max": a.get("max"),
                              "threshold": a.get("threshold")},
                    f"Showed a {kind} chart of {len(labels)} categor{'y' if len(labels) == 1 else 'ies'}"
                    + (f" with a threshold at {a['threshold']:g}" if a.get("threshold") is not None else "") + ".")


def h_show_gauge(ctx, a):
    segs = []
    for s_ in a["segments"][:8]:
        if not isinstance(s_, dict) or not isinstance(s_.get("value"), (int, float)) or isinstance(s_.get("value"), bool):
            raise InvalidInput("each segment needs a label and a numeric value")
        segs.append({"label": str(s_.get("label") or "")[:40], "value": float(s_["value"])})
    total = sum(x["value"] for x in segs)
    mx = float(a.get("max") or total or 1)
    thr = a.get("threshold")
    return _display("gauge", {"title": (a.get("title") or "")[:120], "segments": segs, "max": mx, "threshold": thr,
                              "unit": (a.get("unit") or "")[:12], "style": a.get("style") or "bar"},
                    f"Showed a progress gauge: {total:g} of {mx:g}" + (f", threshold {thr:g} ({'reached' if total >= thr else f'{thr - total:g} to go'})"
                                                                     if thr is not None else "") + ".")


def h_show_artifact(ctx, a):
    content = a["content"]
    if len(content) > 200_000:
        raise InvalidInput("content is limited to 200,000 characters")
    name = re.sub(r"[^\w.\- ]", "_", a.get("filename") or "")[:80] or None
    lang = (a.get("language") or "text")[:20]
    return _display("artifact", {"title": (a.get("title") or "Artifact")[:120], "content": content, "language": lang,
                                 "filename": name},
                    f"Showed an artifact block ({len(content):,} characters, {len(content.split()):,} words) with Copy and "
                    "Download buttons. The user can copy or save it; do not repeat its text.")


def h_show_json(ctx, a):
    data = a["data"]
    if len(json.dumps(data, default=str)) > 200_000:
        raise InvalidInput("data is limited to 200,000 characters as JSON")
    return _display("json", {"title": (a.get("title") or "")[:120], "data": data}, "Showed a collapsible JSON tree.")


def h_show_diff(ctx, a):
    if a.get("item_id") is not None:
        aid = _account(ctx, a)
        h = ctx.api.item_history(aid, a["item_id"])
        changes = [{"field": e["field"], "old": e["old"], "new": e["new"], "when": e["observed_at"]}
                   for e in h["events"] if e["kind"] == "field_changed"][:40]
        title = a.get("title") or f"Changes to {h['title']}"
    elif a.get("before") is not None and a.get("after") is not None:
        changes = [{"field": a.get("label") or "text", "old": a["before"], "new": a["after"], "when": None}]
        title = a.get("title") or "Comparison"
    else:
        raise InvalidInput("give item_id, or before and after")
    return _display("diff", {"title": str(title)[:160], "changes": changes},
                    f"Showed {len(changes)} change(s) side by side" + ("" if changes else " (no recorded edits)") + ".")


_CATS = [c for c in CATEGORIES if c != "courses"]
_DATE_DESC = "ISO-8601 timestamp with offset, e.g. 2026-11-01T00:00:00+08:00"

TOOLS: list[AgentTool] = [
    AgentTool("list_accounts", "List the Canvas accounts stored in Canvas Hub.", "read", handler=h_list_accounts,
              account_scoped=False),
    AgentTool("list_courses", "List the user's Canvas courses with term, access state and how many items of each type are stored.",
              "read", handler=h_list_courses),
    AgentTool("search", "Full-text search over everything collected: titles, announcement/page/assignment bodies, comments, "
              "staff replies and text extracted from course documents (PDF, slides, Word...). All words must match; "
              "use \"quotes\" for phrases and word* for prefixes.", "read",
              {"query": {"type": "string"}, "course_id": {"type": "string"},
               "types": {"type": "array", "items": {"type": "string", "enum": list(ITEM_TYPES)}},
               "include_documents": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50},
               "cursor": {"type": "string"}}, ["query"], h_search),
    AgentTool("get_item", "Read one stored item in full (assignment, announcement, page, file, submission, module...): fields with "
              "the time each was last read from Canvas, text of its body, documents, links, date conflicts and the user's notes.",
              "read", {"item_id": {"type": "integer"}}, ["item_id"], h_get_item),
    AgentTool("get_item_history", "Change history of an item: edits with old and new values.", "read",
              {"item_id": {"type": "integer"}}, ["item_id"], h_item_history),
    AgentTool("list_items", "List stored items, optionally by course and type.", "read",
              {"course_id": {"type": "string"}, "type": {"type": "string", "enum": list(ITEM_TYPES)},
               "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "cursor": {"type": "string"}}, [], h_list_items),
    AgentTool("get_announcements", "Most recent stored announcements (newest first) with their text.", "read",
              {"course_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50},
               "body_chars": {"type": "integer", "minimum": 100, "maximum": 8000}}, [], h_announcements),
    AgentTool("get_deadlines", "Assignment/quiz due dates and calendar events in a date range, with the user's Canvas submission "
              "state. Default: from yesterday to days_ahead (14) days ahead.", "read",
              {"days_ahead": {"type": "integer", "minimum": 1, "maximum": 365}, "start": {"type": "string", "description": _DATE_DESC},
               "end": {"type": "string", "description": _DATE_DESC}, "time_zone": {"type": "string", "description": "IANA name"},
               "course_id": {"type": "string"}, "include_calendar": {"type": "boolean"}}, [], h_deadlines),
    AgentTool("get_changes", "What changed in Canvas since a cursor or time (edits, new items, items no longer listed). "
              "Pass next_cursor from a previous call to get only newer events.", "read",
              {"cursor": {"type": "string"}, "since": {"type": "string", "description": _DATE_DESC},
               "include_baseline": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
              [], h_changes),
    AgentTool("get_coverage", "What has and has not been read per course and category, and why (locked, hidden, not in navigation...).",
              "read", {"course_id": {"type": "string"}}, [], h_coverage),
    AgentTool("get_freshness", "When data was last collected and the most recent collection jobs.", "read", handler=h_freshness),
    AgentTool("read_document", "Read text extracted from a stored course document by resource_id (from get_item or search). "
              "Paged by offset.", "read",
              {"resource_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
               "limit": {"type": "integer", "minimum": 500, "maximum": 20000}}, ["resource_id"], h_read_document),
    AgentTool("get_signin_status", "Whether the app's browser is signed in to Canvas, and jobs waiting for sign-in.", "read",
              handler=h_signin),
    AgentTool("list_jobs", "Recent collection jobs.", "read", {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
              [], h_list_jobs),
    AgentTool("get_job", "State and progress of a collection job.", "read", {"job_id": {"type": "integer"}}, ["job_id"], h_get_job),
    AgentTool("list_notes", "The user's own local notes and to-dos.", "read",
              {"item_id": {"type": "integer"}, "kind": {"type": "string", "enum": ["note", "task"]},
               "task_state": {"type": "string", "enum": ["open", "done"]}}, [], h_list_notes),
    # ---- notes (local writes)
    AgentTool("create_note", "Create a local note or to-do, optionally attached to an item. Local only; never sent to Canvas "
              "and never a submission.", "notes",
              {"kind": {"type": "string", "enum": ["note", "task"]}, "title": {"type": "string"}, "body": {"type": "string"},
               "item_id": {"type": "integer"}, "local_due": {"type": "string", "description": _DATE_DESC},
               "task_state": {"type": "string", "enum": ["open", "done"]}}, ["kind"], h_create_note),
    AgentTool("update_note", "Edit a local note or mark a local to-do open/done.", "notes",
              {"note_id": {"type": "integer"}, "title": {"type": "string"}, "body": {"type": "string"},
               "task_state": {"type": "string", "enum": ["open", "done"]}, "local_due": {"type": "string", "description": _DATE_DESC}},
              ["note_id"], h_update_note),
    AgentTool("delete_note", "Delete a local note or to-do.", "notes", {"note_id": {"type": "integer"}}, ["note_id"], h_delete_note),
    # ---- collection (Canvas traffic through the app's browser)
    AgentTool("start_collection", "Check Canvas for updates using the app's signed-in browser and store what it finds. "
              "mode 'quick' re-reads lists, dates, submissions and grades; 'thorough' re-reads everything including documents. "
              "Optionally limit to course_ids and categories for speed. Read-only toward Canvas.", "collect",
              {"mode": {"type": "string", "enum": ["quick", "thorough", "baseline"]},
               "course_ids": {"type": "array", "items": {"type": "string"}},
               "categories": {"type": "array", "items": {"type": "string", "enum": _CATS}}}, ["mode"], h_start_collection),
    AgentTool("wait_for_job", "Wait (up to timeout_seconds, max 300) for a collection job to finish and return its state.", "collect",
              {"job_id": {"type": "integer"}, "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 300}},
              ["job_id"], h_wait_job),
    AgentTool("cancel_job", "Cancel a collection job (collected data is kept).", "collect", {"job_id": {"type": "integer"}},
              ["job_id"], h_cancel_job),
    # ---- settings
    AgentTool("update_scope", "Change the remembered collection scope: courses ('current', 'all' or comma-separated course ids), "
              "categories, file downloads and calendar window.", "settings",
              {"courses": {"type": "string"}, "categories": {"type": "array", "items": {"type": "string", "enum": _CATS}},
               "download_files": {"type": "boolean"}, "calendar_days_ahead": {"type": "integer", "minimum": 0, "maximum": 3650},
               "calendar_days_back": {"type": "integer", "minimum": 0, "maximum": 3650}}, [], h_update_scope),
]
_TIME = {"type": "string", "description": "HH:MM, 24-hour, local time"}
TOOLS += [
    # ---- planner
    AgentTool("get_timetable", "The user's week: class times, Canvas calendar events, deadlines with submission state and "
              "dated to-dos. Optionally suggest study blocks before unfinished deadlines (suggestions are not saved).", "read",
              {"week_of": {"type": "string", "description": "any date in the week, YYYY-MM-DD (default: this week)"},
               "suggest_study": {"type": "boolean"}, "study_hours": {"type": "number", "description": "per deadline, default 3"}},
              [], h_timetable),
    AgentTool("list_class_times", "The user's saved weekly class times.", "read", handler=h_list_classes),
    AgentTool("add_class_time", "Save a weekly class time in the user's local timetable (e.g. a lecture every Tuesday "
              "14:00-15:20). Local only.", "notes",
              {"title": {"type": "string"}, "weekday": {"type": "string", "description": "day name, e.g. Tuesday"},
               "start": _TIME, "end": _TIME, "course_id": {"type": "string"}, "location": {"type": "string"},
               "valid_from": {"type": "string", "description": "YYYY-MM-DD"}, "valid_until": {"type": "string", "description": "YYYY-MM-DD"}},
              ["title", "weekday", "start", "end"], h_add_class),
    AgentTool("remove_class_time", "Remove a saved class time.", "notes", {"class_id": {"type": "integer"}}, ["class_id"],
              h_remove_class),
    # ---- external sites
    AgentTool("list_view_requests", "Requests for connected agents to read sites Canvas Hub cannot reach (e.g. a separate "
              "course website), with their status and results.", "read",
              {"status": {"type": "string", "enum": ["open", "claimed", "done", "declined", "cancelled", "active"]}},
              [], h_list_requests),
    AgentTool("request_external_view", "Leave a request for a connected agent (with its own browser access) to read an "
              "external site, such as a course website outside Canvas. Canvas Hub does not open the site itself.", "notes",
              {"url": {"type": "string"}, "title": {"type": "string"}, "course_id": {"type": "string"},
               "note": {"type": "string", "description": "instructions for the agent, e.g. which browser profile to use"}},
              ["url"], h_request_view),
    AgentTool("claim_view_request", "Claim an open request to read an external site, so other agents leave it to you.", "fulfil",
              {"request_id": {"type": "integer"}, "agent_name": {"type": "string", "description": "who you are, e.g. Codex"}},
              ["request_id", "agent_name"], h_claim_request),
    AgentTool("submit_view_result", "Submit the text you read from a requested external site (plain text or markdown, up "
              "to 200,000 characters). It is stored as agent-provided content with your name and the time.", "fulfil",
              {"request_id": {"type": "integer"}, "agent_name": {"type": "string"}, "content": {"type": "string"},
               "title": {"type": "string"}, "source_url": {"type": "string", "description": "the exact page read, if different"},
               "read_at": {"type": "string", "description": _DATE_DESC}},
              ["request_id", "agent_name", "content"], h_submit_request),
    AgentTool("decline_view_request", "Decline a request you cannot fulfil (no access, login failed, ...), with a reason.",
              "fulfil", {"request_id": {"type": "integer"}, "agent_name": {"type": "string"}, "reason": {"type": "string"}},
              ["request_id", "agent_name", "reason"], h_decline_request),
    # ---- saved components (readable from any agent)
    AgentTool("get_component", "Read back a saved score model or checklist: which lines are ticked or skipped, values the "
              "user entered, and the computed totals.", "read", {"component_id": {"type": "integer"}}, ["component_id"],
              h_get_component),
    AgentTool("list_components", "List saved interactive components (score models, checklists).", "read",
              {"pinned": {"type": "boolean"}}, [], h_list_components),
    AgentTool("course_score_lines", "Draft score-model buckets for a course from its assignments: points, due dates, "
              "assignment groups and status from the submission state.", "read", {"course_id": {"type": "string"}},
              ["course_id"], h_course_score_lines),
    # ---- interactive components (in-app chat)
    AgentTool("show_deadlines", "Show deadlines to the user as interactive cards (open, add to-do, open in Canvas).", "ui",
              {"days_ahead": {"type": "integer", "minimum": 1, "maximum": 120}, "course_id": {"type": "string"}}, [],
              h_show_deadlines),
    AgentTool("show_timetable", "Show a week timetable grid (classes, events, deadlines, to-dos, optional suggested study "
              "blocks the user can add with one click).", "ui",
              {"week_of": {"type": "string"}, "suggest_study": {"type": "boolean"}, "study_hours": {"type": "number"}},
              [], h_show_timetable),
    AgentTool("show_items", "Show stored items (announcements, assignments, pages, files...) as cards.", "ui",
              {"item_ids": {"type": "array", "items": {"type": "integer"}}}, ["item_ids"], h_show_items),
    AgentTool("show_table", "Show a table (max 12 columns, 200 rows) the user can sort, filter, total, group and export. "
              "Optional per-row detail text expands in place; item_ids open the full item in a side panel.", "ui",
              {"title": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
               "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                        "description": "cell values; numbers may be given as text"},
               "column_types": {"type": "array", "items": {"type": "string", "enum": ["text", "number", "date"]}},
               "totals": {"type": "array", "items": {"type": "string"}, "description": "column names to sum"},
               "group_by": {"type": "string", "description": "a column name"},
               "details": {"type": "array", "items": {"type": "string"}, "description": "one expandable text per row"},
               "item_ids": {"type": "array", "items": {"type": "integer"}, "description": "one Canvas Hub item id per row"}},
              ["columns", "rows"], h_show_table_v2),
    AgentTool("show_chart", "Show a chart of numbers you computed: bar (one series), grouped or stacked (several series, "
              "e.g. earned vs remaining vs lost per category), or waterfall (running total). Optional threshold line.", "ui",
              {"title": {"type": "string"}, "kind": {"type": "string", "enum": ["bar", "grouped", "stacked", "waterfall"]},
               "labels": {"type": "array", "items": {"type": "string"}},
               "values": {"type": "array", "items": {"type": "number"}, "description": "single series"},
               "series": {"type": "array", "items": {"type": "object", "properties": {
                   "name": {"type": "string"}, "values": {"type": "array", "items": {"type": "number"}}},
                   "required": ["name", "values"]}},
               "unit": {"type": "string"}, "max": {"type": "number"}, "threshold": {"type": "number"}},
              ["labels"], h_show_chart_v2),
    AgentTool("show_gauge", "Show a progress gauge (bar or donut) split into segments, with an optional pass threshold and "
              "the distance to it.", "ui",
              {"title": {"type": "string"}, "segments": {"type": "array", "items": {"type": "object", "properties": {
                  "label": {"type": "string"}, "value": {"type": "number"}}, "required": ["label", "value"]}},
               "max": {"type": "number"}, "threshold": {"type": "number"}, "unit": {"type": "string"},
               "style": {"type": "string", "enum": ["bar", "donut"]}}, ["segments"], h_show_gauge),
    AgentTool("show_artifact", "Hand a finished text artefact to the user (a spec, prompt, report, essay draft, JSON, CSV, "
              "code) in a block with line numbers, Copy and Download buttons. Use this instead of pasting long text.", "ui",
              {"title": {"type": "string"}, "content": {"type": "string"},
               "language": {"type": "string", "description": "markdown, json, csv, python, text, ..."},
               "filename": {"type": "string", "description": "suggested download name, e.g. study-plan.md"}},
              ["content"], h_show_artifact),
    AgentTool("show_json", "Show nested data (e.g. a model's buckets and lines) as a collapsible tree.", "ui",
              {"title": {"type": "string"}, "data": {"type": "object"}}, ["data"], h_show_json),
    AgentTool("show_diff", "Show edits side by side in red/green: the recorded changes of a Canvas item (item_id), or two "
              "texts you compare (before/after).", "ui",
              {"item_id": {"type": "integer"}, "before": {"type": "string"}, "after": {"type": "string"},
               "label": {"type": "string"}, "title": {"type": "string"}}, [], h_show_diff),
    AgentTool("show_score_model", "Create an interactive score model (saved): one bucket per assessed line with max, earned, "
              "status (earned/pending/lost/locked/excused), gating (none/all_or_nothing/min_threshold with min_value), "
              "unit (points or hours, with full_at hours for full marks). The user ticks lines to include or skip and edits "
              "expected values; totals (booked, projected, ceiling, lost, margin to threshold, skip budget) update live. "
              "Draft buckets from Canvas with course_score_lines. Scenarios are named presets (e.g. conservative/target/max).",
              "ui",
              {"title": {"type": "string"}, "threshold": {"type": "number", "description": "pass mark, same unit as the totals"},
               "unit_label": {"type": "string", "description": "e.g. pts"},
               "buckets": {"type": "array", "items": {"type": "object", "properties": {
                   "id": {"type": "string"}, "label": {"type": "string"}, "max": {"type": "number"},
                   "earned": {"type": "number"}, "status": {"type": "string", "enum": ["earned", "pending", "lost", "locked", "excused"]},
                   "gating": {"type": "string", "enum": ["none", "all_or_nothing", "min_threshold"]},
                   "min_value": {"type": "number"}, "unit": {"type": "string", "enum": ["points", "hours"]},
                   "full_at": {"type": "number"}, "group": {"type": "string"}, "due": {"type": "string"},
                   "item_id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["label", "max"]}},
               "scenarios": {"type": "array", "items": {"type": "object", "properties": {
                   "id": {"type": "string"}, "label": {"type": "string"},
                   "include": {"type": "object", "description": "bucket id -> true/false"},
                   "values": {"type": "object", "description": "bucket id -> expected points or hours"}},
                   "required": ["id"]}},
               "pin": {"type": "boolean", "description": "also pin it to the Trackers page"}},
              ["buckets"], h_show_score_model),
    AgentTool("show_checklist", "Create an interactive checklist (saved): sections of rows with optional points, due date "
              "and linked item; ticks persist, sections show subtotals and progress. sync_todos links each row to a local "
              "to-do (ticking one ticks the other; needs the notes permission).", "ui",
              {"title": {"type": "string"}, "sync_todos": {"type": "boolean"}, "pin": {"type": "boolean"},
               "sections": {"type": "array", "items": {"type": "object", "properties": {
                   "id": {"type": "string"}, "title": {"type": "string"},
                   "rows": {"type": "array", "items": {"type": "object", "properties": {
                       "id": {"type": "string"}, "label": {"type": "string"}, "points": {"type": "number"},
                       "due": {"type": "string"}, "item_id": {"type": "integer"}, "note": {"type": "string"},
                       "done": {"type": "boolean"}}, "required": ["label"]}}}, "required": ["rows"]}}},
              ["sections"], h_show_checklist),
    AgentTool("update_component", "Change a saved score model or checklist: replace its definition (spec, same shape as "
              "when created), set its state, or pin it to Trackers.", "ui",
              {"component_id": {"type": "integer"}, "spec": {"type": "object"}, "state": {"type": "object"},
               "pin": {"type": "boolean"}}, ["component_id"], h_update_component),
    AgentTool("show_grades", "Show visible assignment scores as a chart (% of points); hidden grades are listed, not zero.",
              "ui", {"course_id": {"type": "string"}}, [], h_show_grades),
    AgentTool("show_choices", "Offer the user up to 6 quick-reply buttons; their click is sent as their next message.", "ui",
              {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
              ["question", "options"], h_show_choices),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tools_for(permissions: set[str]) -> list[AgentTool]:
    return [t for t in TOOLS if t.permission in permissions]


def run_tool(ctx: ToolContext, name: str, args: Any, permissions: set[str], *, with_account: bool = False) -> tuple[str, bool]:
    """Execute a tool. Returns (result_text, is_error); never raises for
    tool-level problems so the model can recover."""
    text, is_error, _ = run_tool_display(ctx, name, args, permissions, with_account=with_account)
    return text, is_error


def run_tool_display(ctx: ToolContext, name: str, args: Any, permissions: set[str], *,
                     with_account: bool = False) -> tuple[str, bool, dict | None]:
    """Like run_tool, but a show_* component payload is returned separately
    (untruncated) and only the rest of the result goes into the text."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None or tool.permission not in permissions:
        return json.dumps({"error": "unknown_or_disabled_tool", "message": f"Tool '{name}' is not available."}), True, None
    try:
        args = validate_args(tool, args if args is not None else {}, with_account=with_account)
        if ctx is not None:
            ctx = replace(ctx, permissions=frozenset(permissions))
        result = tool.handler(ctx, args)
        display = result.pop("display", None) if isinstance(result, dict) else None
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + '… [result truncated; narrow the query or page with cursor/offset]'
        return text, False, display
    except HubError as e:
        return json.dumps(e.to_dict(), ensure_ascii=False), True, None
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "tool_failed", "message": f"{type(e).__name__}: {e}"}), True, None
