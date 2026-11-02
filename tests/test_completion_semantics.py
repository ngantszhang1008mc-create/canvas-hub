"""A finished step is not a finished assignment or a Canvas submission."""
import copy
import json
from datetime import timedelta

import pytest

from canvas_hub import db
from canvas_hub.util import InvalidInput, NotFound
from .conftest import variant
from .test_deadline_archives import deadlines


def rows(api, aid, due, **extra):
    return api.deadlines(aid, start=(due-timedelta(days=7)).isoformat(),
                         end=(due+timedelta(days=7)).isoformat(), **extra)["deadlines"]


def test_finished_ordinary_task_and_all_checklist_steps_do_not_finish_assignment(deadlines):
    api, aid, iid, due, _ = deadlines
    api.hub.annotations.create(aid, kind="task", item_id=iid, task_state="done", title="Read question")
    component = api.hub.components.create(aid, "checklist", {
        "title": "Steps", "sync_todos": True,
        "rows": [{"id": "read", "label": "Read", "item_id": iid},
                 {"id": "draft", "label": "Draft", "item_id": iid, "due": due.date().isoformat()}],
    })
    result = api.hub.components.set_state(aid, component["id"], {"done": {"read": True, "draft": True}})
    assert result["computed"]["done_count"] == 2
    step_ids = result["state"]["links"].values()
    assert all(api.hub.annotations.get(aid, tid)["task_role"] == "substep" for tid in step_ids)
    entry = next(x for x in rows(api, aid, due) if x["item_id"] == iid)
    assert not entry["done_locally"] and not entry["completed"] and not entry["canvas_completed"]
    assert not entry["submission"].get("submitted_at")


def test_local_completion_only_updates_markers_and_shared_mirrors(deadlines):
    api, aid, iid, due, pkg = deadlines
    newer = variant(pkg, "semantic-mirrors", "2026-11-01T00:00:00Z")
    original = next(i for i in newer["items"] if i["type"] == "assignment" and i["canvas_id"] == "7001")
    for kind, cid in [("quiz", "8001"), ("calendar_event", "9001")]:
        mirror = copy.deepcopy(original)
        mirror.update(type=kind, canvas_id=cid, title="Mirror", observation_id="semantic-"+cid)
        mirror["fields"]["assignment_id"] = "7001"
        if kind == "calendar_event":
            mirror["fields"]["start_at"] = mirror["fields"].pop("due_at")
        newer["items"].append(mirror)
    api.hub.importer.import_package(newer)
    quiz = api.hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='quiz' AND canvas_id='8001'", (aid,))["id"]
    step = api.hub.annotations.create(aid, kind="task", item_id=quiz, task_role="substep", task_state="done")
    task = api.hub.annotations.create(aid, kind="task", item_id=iid, task_state="in_progress")
    first = api.set_deadline_local_completion(aid, quiz, True)
    second = api.set_deadline_local_completion(aid, iid, True)
    assert first["task_ids"] == second["task_ids"]
    for kind in ("assignment", "quiz", "calendar_event"):
        entries = rows(api, aid, due, item_type=kind, status="completed")
        assert len(entries) == 1 and entries[0]["done_locally"] and not entries[0]["canvas_completed"]
    api.set_deadline_local_completion(aid, quiz, False)
    assert api.hub.annotations.get(aid, step["id"])["task_state"] == "done"
    assert api.hub.annotations.get(aid, task["id"])["task_state"] == "in_progress"
    for kind in ("assignment", "quiz", "calendar_event"):
        assert len(rows(api, aid, due, item_type=kind, status="unfinished")) == 1
    other = api.hub.identity.upsert_verified_account(api.get_account(aid)["instance_id"],
        {"canvas_user_id": "other", "display_name": "Other", "time_zone": "UTC"})["id"]
    with pytest.raises(NotFound):
        api.set_deadline_local_completion(other, iid, True)


def test_canvas_submission_stays_distinct_when_local_marker_is_reopened(deadlines):
    api, aid, iid, due, pkg = deadlines
    newer = variant(pkg, "submitted-semantic", "2026-11-01T00:00:00Z")
    submission = next(i for i in newer["items"] if i["type"] == "submission" and i["canvas_id"] == "7001")
    submission["fields"].update(workflow_state="submitted", submitted_at="2026-11-01T00:00:00Z")
    api.hub.importer.import_package(newer)
    api.set_deadline_local_completion(aid, iid, True)
    api.set_deadline_local_completion(aid, iid, False)
    entry = next(x for x in rows(api, aid, due) if x["item_id"] == iid)
    assert entry["completed"] and entry["canvas_completed"] and not entry["done_locally"]


def test_completion_markers_cannot_be_regular_dated_tasks(deadlines):
    api, aid, iid, due, _ = deadlines
    with pytest.raises(InvalidInput):
        api.hub.annotations.create(aid, kind="task", item_id=iid, task_role="completion", local_due=due.isoformat())
    with pytest.raises(InvalidInput):
        api.hub.annotations.create(aid, kind="note", item_id=iid, task_role="substep")
    marker = api.set_deadline_local_completion(aid, iid, True)["task_ids"][0]
    with pytest.raises(InvalidInput):
        api.hub.annotations.update(aid, marker, local_due=due.isoformat())


def test_migration_retains_legacy_markers_and_recognizes_current_and_history_steps(monkeypatch):
    files = db._migration_files()
    conn = db.connect(":memory:")
    try:
        with monkeypatch.context() as m:
            m.setattr(db, "_migration_files", lambda: [f for f in files if f[0] < 10])
            db.migrate(conn)
        conn.execute("INSERT INTO instances(id,origin,created_at) VALUES (1,'https://canvas.example.edu','now')")
        conn.execute("INSERT INTO accounts(id,instance_id,canvas_user_id,first_verified_at,last_verified_at) VALUES (1,1,'1','now','now')")
        conn.execute("INSERT INTO items(id,account_id,item_type,source_key,first_observed_at,last_observed_at) VALUES (1,1,'assignment','a','now','now')")
        for aid, title in [(1, "Manual marker"), (2, "Current checklist step"), (3, "Historical step")]:
            conn.execute("""INSERT INTO annotations(id,account_id,item_id,item_type,kind,title,body,task_state,created_at,updated_at)
                            VALUES (?,1,1,'assignment','task',?,'','done','now','now')""", (aid, title))
        conn.execute("""INSERT INTO components(id,account_id,kind,title,spec_json,state_json,created_at,updated_at)
                        VALUES (1,1,'checklist','Steps','{}',?,'now','now')""", (json.dumps({"links": {"r": 2}}),))
        conn.execute("""INSERT INTO component_versions(component_id,version,spec_json,state_json,changed_by,created_at)
                        VALUES (1,1,'{}',?,'user','now')""", (json.dumps({"links": {"old": 3}}),))
        with monkeypatch.context() as m:
            m.setattr(db, "_migration_files", lambda: [f for f in files if f[0] <= 10])
            assert db.migrate(conn) == [10]
            assert db.migrate(conn) == []
        annotations = [dict(r) for r in conn.execute("SELECT id,task_role,task_state,title FROM annotations ORDER BY id")]
        assert [r["task_role"] for r in annotations] == ["completion", "substep", "substep"]
        assert all(r["task_state"] == "done" for r in annotations)
        assert annotations[0]["title"] == "Manual marker"
    finally:
        conn.close()
