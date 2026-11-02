"""Imported source data stays distinct from rules and the user's choices."""

import copy
import json

import pytest

from canvas_hub import db as database
from canvas_hub.util import Conflict, InvalidInput, NotFound, now_iso
from .conftest import variant


@pytest.fixture
def synced(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    items = {r["item_type"] + ":" + str(r["canvas_id"]): r["id"] for r in hub.db.query(
        "SELECT id,item_type,canvas_id FROM items WHERE account_id=?", (aid,))}
    items["syllabus"] = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='syllabus'", (aid,))["id"]
    return hub.components, aid, items


def field(hub, iid, name, value):
    with hub.db.tx() as conn:
        conn.execute("UPDATE item_fields SET value_json=? WHERE item_id=? AND field=?", (json.dumps(value), iid, name))


def score_spec():
    return {"title": "Course plan", "threshold": 5,
            "buckets": [{"id": "proposal", "label": "Proposal", "max": 7, "formula": "min(value, max)"}]}


def score_sources(iid):
    return {"bindings": [{"target": "buckets/proposal/max", "item_id": iid, "field": "points_possible"}]}


def test_binding_refresh_keeps_what_if_values_and_safe_formula(hub, synced):
    svc, aid, ids = synced
    iid = ids["assignment:7001"]
    v = svc.create(aid, "score_model", score_spec(), sources=score_sources(iid), course_id="4242")
    assert v["spec"]["buckets"][0]["max"] == 10
    assert v["source_status"]["last_success_at"] and not v["source_status"]["needs_review"]
    assert v["source_status"]["last_success_at"] == "2026-10-30T01:59:00Z"
    v = svc.set_state(aid, v["id"], {"include": {"proposal": False}, "values": {"proposal": 3}})
    previous = copy.deepcopy(v["state"])
    field(hub, iid, "points_possible", 22)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["spec"]["buckets"][0]["max"] == 22
    assert v["spec"]["buckets"][0]["formula"] == "min(value, max)"
    assert v["state"] == previous
    assert v["source_values"]["buckets/proposal/max"]["value"] == 22
    version = v["version"]
    svc.sync_sources(aid)
    assert svc.get(aid, v["id"])["version"] == version


@pytest.mark.parametrize("missing", ["null", "deleted_field", "missing_item", "partial", "invalid"])
def test_missing_or_partial_source_keeps_last_known_value(hub, synced, missing):
    svc, aid, ids = synced
    iid = ids["assignment:7001"]
    v = svc.create(aid, "score_model", score_spec(), sources=score_sources(iid))
    success = v["source_status"]["last_success_at"]
    with hub.db.tx() as conn:
        if missing == "null":
            field(hub, iid, "points_possible", None)
        elif missing == "invalid":
            field(hub, iid, "points_possible", "not a number")
        elif missing == "partial":
            conn.execute("UPDATE items SET last_observed_at='2030-01-01T00:00:00Z' WHERE id=?", (iid,))
        elif missing == "missing_item":
            conn.execute("DELETE FROM items WHERE id=?", (iid,))
        else:
            conn.execute("DELETE FROM item_fields WHERE item_id=? AND field='points_possible'", (iid,))
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["spec"]["buckets"][0]["max"] == 10
    assert v["source_status"]["availability"] == "partial" and v["source_status"]["unavailable"]
    assert v["source_status"]["last_success_at"] == success
    assert v["source_values"]["buckets/proposal/max"]["value"] == 10


def test_zero_is_observed_and_not_treated_as_missing(hub, synced):
    svc, aid, ids = synced
    iid = ids["assignment:7001"]
    v = svc.create(aid, "score_model", score_spec(), sources=score_sources(iid))
    field(hub, iid, "points_possible", 0)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["spec"]["buckets"][0]["max"] == 0
    assert v["source_status"]["availability"] == "available"


def test_override_survives_refresh_follows_latest_and_can_be_undone(hub, synced):
    svc, aid, ids = synced
    iid, target = ids["assignment:7001"], "buckets/proposal/max"
    v = svc.create(aid, "score_model", score_spec(), sources=score_sources(iid))
    v = svc.set_source_override(aid, v["id"], target, 15)
    assert v["source_overrides"][target]["value"] == 15
    field(hub, iid, "points_possible", 20)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["spec"]["buckets"][0]["max"] == 15
    assert v["source_values"][target]["value"] == 20
    v = svc.set_source_override(aid, v["id"], target, follow_source=True)
    assert v["spec"]["buckets"][0]["max"] == 20 and not v["source_overrides"]
    v = svc.undo(aid, v["id"])
    assert v["spec"]["buckets"][0]["max"] == 15 and v["source_overrides"][target]["value"] == 15
    svc.sync_sources(aid)
    assert svc.get(aid, v["id"])["spec"]["buckets"][0]["max"] == 15


def test_detaching_source_preserves_local_value_without_stale_binding_metadata(hub, synced):
    svc, aid, ids = synced
    iid, target = ids["assignment:7001"], "buckets/proposal/max"
    v = svc.create(aid, "score_model", score_spec(), sources=score_sources(iid))
    v = svc.set_source_override(aid, v["id"], target, 15)
    v = svc.configure_sources(aid, v["id"], {})
    assert not v["source_values"] and not v["source_overrides"]
    assert v["source_config"] == {}
    assert v["spec"]["buckets"][0]["max"] == 15
    field(hub, iid, "points_possible", 90)
    svc.sync_sources(aid)
    assert svc.get(aid, v["id"])["spec"]["buckets"][0]["max"] == 15
    with pytest.raises(InvalidInput):
        svc.set_source_override(aid, v["id"], target, follow_source=True)
    assert svc.sync_sources(aid) == []
    changed = svc.update_spec(aid, v["id"], {**v["spec"], "threshold": 30})
    assert changed["spec"]["threshold"] == 30 and not changed["pending_spec"]


def test_scoped_purpose_dedup_returns_existing_without_overwrite(hub, synced):
    svc, aid, ids = synced
    first = svc.create(aid, "score_model", score_spec(), course_id="4242", purpose_key="course-plan")
    first = svc.set_state(aid, first["id"], {"values": {"proposal": 2}})
    second = svc.create(aid, "score_model", {"title": "Do not overwrite"}, course_id="4242", purpose_key="course-plan")
    assert second["id"] == first["id"] and second["spec"] == first["spec"] and second["state"] == first["state"]
    item = svc.create(aid, "score_model", score_spec(), item_id=ids["assignment:7001"], purpose_key="course-plan")
    kind = svc.create(aid, "checklist", {"rows": []}, course_id="4242", purpose_key="course-plan")
    purpose = svc.create(aid, "score_model", score_spec(), course_id="4242", purpose_key="other-plan")
    assert len({first["id"], item["id"], kind["id"], purpose["id"]}) == 4


def test_primary_adopts_earliest_unkeyed_component_without_replacing_work(synced):
    svc, aid, ids = synced
    original = svc.create(aid, "checklist", {"title": "Existing breakdown", "rows": [{"id": "draft", "label": "Draft"}]},
                          item_id=ids["assignment:7001"])
    original = svc.set_state(aid, original["id"], {"done": {"draft": True}})
    second = svc.create(aid, "checklist", {"rows": []}, item_id=ids["assignment:7001"])
    reused = svc.create(aid, "checklist", {"rows": [{"label": "New generation"}]},
                        item_id=ids["assignment:7001"], purpose_key="primary")
    assert reused["id"] == original["id"] and reused["purpose_key"] == "primary"
    assert reused["spec"] == original["spec"] and reused["state"] == original["state"] and reused["version"] == original["version"]
    assert svc.get(aid, second["id"])["purpose_key"] is None
    assert svc.create(aid, "checklist", {}, item_id=ids["assignment:7001"], purpose_key="primary")["id"] == original["id"]
    other = svc.create(aid, "checklist", {"rows": []}, item_id=ids["assignment:7001"], purpose_key="other")
    assert other["id"] not in (original["id"], second["id"])


def test_collection_reconciles_new_assignments_and_preserves_ticks_and_todos(hub, synced, fixture_pkg):
    svc, aid, ids = synced
    iid, rid = ids["assignment:7001"], f"canvas:{ids['assignment:7001']}"
    sources = {"collection": {"course_id": "4242", "item_type": "assignment", "section_id": "assignments"}}
    v = svc.create(aid, "checklist", {"title": "All assignments", "sync_todos": True,
        "sections": [{"id": "assignments", "rows": []}]}, course_id="4242", sources=sources)
    assert len(v["spec"]["sections"][0]["rows"]) == 2
    v = svc.set_state(aid, v["id"], {"done": {rid: True}, "collapsed": {"assignments": True}})
    link = v["state"]["links"][rid]
    newer = variant(fixture_pkg, "sync-new-assignment", "2026-11-04T00:00:00Z")
    original = next(x for x in newer["items"] if x["type"] == "assignment" and x["canvas_id"] == "7001")
    original["fields"].update(title="Revised proposal", points_possible=18,
                               due_at={"iso": "2026-12-01T00:00:00Z"})
    new = copy.deepcopy(original)
    new.update(canvas_id="7003", title="Third assignment", url="https://canvas.example.edu/courses/4242/assignments/7003")
    new["fields"]["title"] = "Third assignment"
    newer["items"].append(new)
    hub.importer.import_package(newer)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert len(v["spec"]["sections"][0]["rows"]) == 3
    revised = next(r for r in v["spec"]["sections"][0]["rows"] if r["id"] == rid)
    assert revised["points"] == 18 and revised["label"] == "Revised proposal" and revised["due"] == "2026-12-01T00:00:00Z"
    assert v["state"]["done"][rid] and v["state"]["collapsed"]["assignments"] and v["state"]["links"][rid] == link
    todo = hub.annotations.get(aid, link)
    assert todo["task_state"] == "done" and todo["task_role"] == "substep" and todo["title"] == "Revised proposal"
    with hub.db.tx() as conn:
        conn.execute("UPDATE items SET presence='source_unavailable' WHERE id=?", (iid,))
    svc.sync_sources(aid)
    assert len(svc.get(aid, v["id"])["spec"]["sections"][0]["rows"]) == 3


def test_score_collection_does_not_invent_zero_for_unknown_points(hub, synced):
    svc, aid, ids = synced
    v = svc.create(aid, "score_model", score_spec(), course_id="4242", sources={
        "collection": {"course_id": "4242", "item_type": "assignment", "section_id": "assignments"}})
    buckets = {b["id"]: b for b in v["spec"]["buckets"]}
    assert f"canvas:{ids['assignment:7001']}" in buckets and f"canvas:{ids['assignment:7002']}" not in buckets
    assert any(u["reason"] == "collection_fields_unavailable" for u in v["source_status"]["unavailable"])


def test_score_collection_tracks_actual_grades_without_resetting_user_scenarios(hub, synced):
    svc, aid, ids = synced
    rid, sub = f"canvas:{ids['assignment:7001']}", ids["submission:7001"]
    v = svc.create(aid, "score_model", score_spec(), course_id="4242", sources={
        "collection": {"course_id": "4242", "item_type": "assignment", "section_id": "assignments"}})
    v = svc.set_state(aid, v["id"], {"include": {rid: False}, "values": {rid: 3}})
    row = next(b for b in v["spec"]["buckets"] if b["id"] == rid)
    assert row["status"] == "pending" and row["earned"] is None
    field(hub, sub, "workflow_state", "graded")
    field(hub, sub, "score", 8)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    row = next(b for b in v["spec"]["buckets"] if b["id"] == rid)
    assert row["status"] == "earned" and row["earned"] == 8
    assert v["state"]["include"][rid] is False and v["state"]["values"][rid] == 3
    field(hub, sub, "score", None)
    svc.sync_sources(aid)
    row = next(b for b in svc.get(aid, v["id"])["spec"]["buckets"] if b["id"] == rid)
    assert row["status"] == "earned" and row["earned"] == 8
    field(hub, sub, "score", 0)
    svc.sync_sources(aid)
    row = next(b for b in svc.get(aid, v["id"])["spec"]["buckets"] if b["id"] == rid)
    assert row["status"] == "earned" and row["earned"] == 0
    field(hub, sub, "excused", True)
    svc.sync_sources(aid)
    row = next(b for b in svc.get(aid, v["id"])["spec"]["buckets"] if b["id"] == rid)
    assert row["status"] == "excused"


def test_review_changes_are_flagged_and_require_versioned_acceptance(hub, synced):
    svc, aid, ids = synced
    sources = {"review_sources": [{"item_id": ids["syllabus"], "fields": ["body", "description", "syllabus_body"], "attachments": True}]}
    v = svc.create(aid, "score_model", score_spec(), sources=sources)
    v = svc.set_state(aid, v["id"], {"include": {"proposal": False}, "values": {"proposal": 4}})
    original, original_state = copy.deepcopy(v["spec"]), copy.deepcopy(v["state"])
    field(hub, ids["syllabus"], "body", "<p>Pass now requires 12 points.</p>")
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["source_status"]["needs_review"] and v["spec"] == original and v["state"] == original_state
    proposed = {**original, "threshold": 12}
    v = svc.update_spec(aid, v["id"], proposed)
    assert v["spec"] == original and v["pending_spec"]["spec"]["threshold"] == 12
    assert v["pending_spec"]["base_version"] == v["version"]
    with pytest.raises(Conflict):
        svc.accept_spec(aid, v["id"], v["version"] - 1)
    v = svc.accept_spec(aid, v["id"], v["version"])
    assert v["spec"]["threshold"] == 12 and v["pending_spec"] is None and not v["source_status"]["needs_review"]
    assert v["state"] == original_state
    svc.sync_sources(aid)
    assert not svc.get(aid, v["id"])["source_status"]["needs_review"]


def test_reconfiguring_same_review_item_cannot_approve_changed_rules(hub, synced):
    svc, aid, ids = synced
    iid = ids["syllabus"]
    v = svc.create(aid, "score_model", score_spec(), sources={
        "review_sources": [{"item_id": iid, "fields": ["body"], "attachments": False}]})
    accepted = v["source_status"]["accepted_fingerprints"]
    field(hub, iid, "body", "The pass rules have changed")
    svc.sync_sources(aid)
    v = svc.configure_sources(aid, v["id"], {"review_sources": [{"item_id": iid, "fields": ["body"], "attachments": True}]})
    assert v["source_status"]["needs_review"] and v["source_status"]["accepted_fingerprints"] == accepted


def test_source_changed_after_proposal_rejects_stale_acceptance(hub, synced):
    svc, aid, ids = synced
    v = svc.create(aid, "score_model", score_spec(), sources={
        "review_sources": [{"item_id": ids["syllabus"], "fields": ["body"]}]})
    v = svc.propose_spec(aid, v["id"], {**v["spec"], "threshold": 10})
    field(hub, ids["syllabus"], "body", "The requirement changed again")
    with pytest.raises(Conflict):
        svc.accept_spec(aid, v["id"], v["version"])
    assert svc.get(aid, v["id"])["spec"]["threshold"] == 5
    assert svc.get(aid, v["id"])["source_status"]["needs_review"]


def test_replacing_proposal_invalidates_the_previous_review_screen(synced):
    svc, aid, ids = synced
    v = svc.create(aid, "hours_tracker", {"target": 20, "minimum": 10})
    first = svc.propose_spec(aid, v["id"], {**v["spec"], "minimum": 12})
    second = svc.propose_spec(aid, v["id"], {**v["spec"], "minimum": 18})
    assert second["version"] > first["version"]
    assert second["pending_spec"]["base_version"] == second["version"]
    with pytest.raises(Conflict):
        svc.accept_spec(aid, v["id"], first["version"])
    assert svc.get(aid, v["id"])["spec"]["minimum"] == 10
    accepted = svc.accept_spec(aid, v["id"], second["version"])
    assert accepted["spec"]["minimum"] == 18


def test_proposals_cannot_silently_replace_source_overrides(synced):
    svc, aid, ids = synced
    v = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    v = svc.set_source_override(aid, v["id"], "buckets/proposal/max", 15)
    proposed = copy.deepcopy(v["spec"])
    proposed["buckets"][0]["max"] = 30
    with pytest.raises(InvalidInput, match="source override"):
        svc.propose_spec(aid, v["id"], proposed)
    assert svc.get(aid, v["id"])["spec"]["buckets"][0]["max"] == 15


def test_review_fingerprints_text_and_attachment_ids(hub, synced):
    svc, aid, ids = synced
    v = svc.create(aid, "hours_tracker", {"target": 20, "minimum": 10, "points": 20}, sources={
        "review_sources": [{"item_id": ids["syllabus"], "fields": ["body"], "attachments": True}]})
    before = v["source_status"]["review_fingerprints"]
    old = hub.db.one("SELECT value_json FROM item_fields WHERE item_id=? AND field='body'", (ids["syllabus"],))[0]
    field(hub, ids["syllabus"], "body", "<div>" + json.loads(old) + "</div>")
    svc.sync_sources(aid)
    assert svc.get(aid, v["id"])["source_status"]["review_fingerprints"] == before
    rid = hub.resources.store_bytes(b"new syllabus attachment")
    with hub.db.tx() as conn:
        conn.execute("INSERT INTO item_resources(item_id,resource_id,observed_at) VALUES (?,?,?)", (ids["syllabus"], rid, now_iso()))
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["source_status"]["needs_review"] and v["spec"]["minimum"] == 10
    assert v["state"]["entries"] == []


def test_reverting_to_previous_attachment_still_requires_review(hub, synced):
    svc, aid, ids = synced
    iid = ids["syllabus"]
    rid_a, rid_b = hub.resources.store_bytes(b"original rules"), hub.resources.store_bytes(b"new rules")
    with hub.db.tx() as conn:
        conn.execute("INSERT INTO item_resources(item_id,resource_id,observed_at) VALUES (?,?,?)", (iid, rid_a, "2026-10-30T00:00:00Z"))
    v = svc.create(aid, "score_model", score_spec(), sources={
        "review_sources": [{"item_id": iid, "fields": ["body"], "attachments": True}]})
    with hub.db.tx() as conn:
        conn.execute("INSERT INTO item_resources(item_id,resource_id,observed_at) VALUES (?,?,?)", (iid, rid_b, "2026-11-01T00:00:00Z"))
    svc.sync_sources(aid)
    v = svc.propose_spec(aid, v["id"], v["spec"])
    v = svc.accept_spec(aid, v["id"], v["version"])
    assert not v["source_status"]["needs_review"]
    with hub.db.tx() as conn:
        conn.execute("UPDATE item_resources SET observed_at='2026-11-02T00:00:00Z' WHERE item_id=? AND resource_id=?", (iid, rid_a))
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["source_status"]["needs_review"]
    assert v["source_status"]["review_snapshots"][str(iid)]["resource_ids"] == [rid_a]


def test_stale_resource_reimport_cannot_revive_an_older_original(hub, synced, fixture_pkg):
    svc, aid, ids = synced
    iid = ids["syllabus"]
    rid_a, rid_b = hub.resources.store_bytes(b"original A"), hub.resources.store_bytes(b"updated original B")

    def import_original(package_id, observed_at, rid):
        pkg = variant(fixture_pkg, package_id, observed_at)
        syllabus = next(item for item in pkg["items"] if item["type"] == "syllabus")
        syllabus["resources"] = [{"sha256": rid, "filename": "rules.txt"}]
        pkg["items"], pkg["coverage"] = [syllabus], []
        hub.importer.import_package(pkg)

    import_original("original-a", "2026-10-31T00:00:00Z", rid_a)
    import_original("original-b", "2026-11-02T00:00:00Z", rid_b)
    v = svc.create(aid, "score_model", score_spec(), sources={
        "review_sources": [{"item_id": iid, "fields": ["body"], "attachments": True}]})
    before = v["source_status"]["review_fingerprints"]
    import_original("stale-original-b", "2026-10-30T00:00:00Z", rid_b)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["source_status"]["review_fingerprints"] == before and not v["source_status"]["needs_review"]
    observed = hub.db.one("SELECT observed_at FROM item_resources WHERE item_id=? AND resource_id=?", (iid, rid_b))[0]
    assert observed == "2026-11-02T00:00:00Z"
    import_original("original-a-restored", "2026-11-03T00:00:00Z", rid_a)
    svc.sync_sources(aid)
    assert svc.get(aid, v["id"])["source_status"]["needs_review"]


@pytest.mark.parametrize("unavailable", ["presence", "old_original", "no_original"])
def test_linked_attachment_missing_or_partial_retains_fingerprint(hub, synced, unavailable):
    svc, aid, ids = synced
    syllabus, linked = ids["syllabus"], ids["page:5001"]
    rid = hub.resources.store_bytes(b"rules in linked original")
    with hub.db.tx() as conn:
        conn.execute("INSERT INTO item_resources(item_id,resource_id,observed_at) VALUES (?,?,?)", (linked, rid, "2026-10-30T01:58:30Z"))
        conn.execute("""INSERT INTO item_links(account_id,from_item_id,to_item_id,relation,first_observed_at,last_observed_at)
                        VALUES (?,?,?,'attachment',?,?)""", (aid, syllabus, linked, "2026-10-30T01:58:30Z", "2026-10-30T01:58:30Z"))
    v = svc.create(aid, "score_model", score_spec(), sources={
        "review_sources": [{"item_id": syllabus, "fields": ["body"], "attachments": True}]})
    before = v["source_status"]["review_fingerprints"]
    assert v["source_status"]["availability"] == "available"
    with hub.db.tx() as conn:
        if unavailable == "presence":
            conn.execute("UPDATE items SET presence='source_unavailable' WHERE id=?", (linked,))
        elif unavailable == "old_original":
            conn.execute("UPDATE items SET last_observed_at='2026-11-01T00:00:00Z' WHERE id=?", (linked,))
        else:
            conn.execute("DELETE FROM item_resources WHERE item_id=?", (linked,))
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["source_status"]["availability"] == "partial" and v["source_status"]["unavailable"]
    assert v["source_status"]["review_fingerprints"] == before


def test_removing_last_confirmed_attachment_link_flags_review_without_body(hub, synced):
    svc, aid, ids = synced
    syllabus, linked = ids["syllabus"], ids["page:5001"]
    rid = hub.resources.store_bytes(b"linked requirements")
    with hub.db.tx() as conn:
        conn.execute("INSERT INTO item_resources(item_id,resource_id,observed_at) VALUES (?,?,?)", (linked, rid, "2026-10-30T01:58:30Z"))
        conn.execute("""INSERT INTO item_links(account_id,from_item_id,to_item_id,relation,first_observed_at,last_observed_at)
                        VALUES (?,?,?,'attachment',?,?)""", (aid, syllabus, linked, "2026-10-30T01:58:30Z", "2026-10-30T01:58:30Z"))
    v = svc.create(aid, "score_model", score_spec(), sources={
        "review_sources": [{"item_id": syllabus, "fields": [], "attachments": True}]})
    assert not v["source_status"]["needs_review"]
    with hub.db.tx() as conn:
        conn.execute("DELETE FROM item_links WHERE from_item_id=? AND relation='attachment'", (syllabus,))
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["source_status"]["needs_review"]
    assert v["source_status"]["review_snapshots"][str(syllabus)]["resource_ids"] == []


def test_related_source_fields_apply_as_one_snapshot_regardless_of_binding_order(hub, synced):
    svc, aid, ids = synced
    first, second = ids["assignment:7001"], ids["assignment:7002"]
    with hub.db.tx() as conn:
        conn.execute("INSERT INTO item_fields(item_id,field,value_json,compare_hash,first_observed_at,last_observed_at,last_changed_at) VALUES (?,?,?,'x',?,?,?)",
                     (second, "points_possible", "3", "2026-10-30T01:59:05Z", "2026-10-30T01:59:05Z", "2026-10-30T01:59:05Z"))
    field(hub, first, "points_possible", 5)
    v = svc.create(aid, "hours_tracker", {"target": 20, "minimum": 10}, sources={"bindings": [
        {"target": "target", "item_id": first, "field": "points_possible"},
        {"target": "minimum", "item_id": second, "field": "points_possible"}]})
    assert v["spec"]["target"] == 5 and v["spec"]["minimum"] == 3
    assert v["source_status"]["availability"] == "available"
    assert v["source_status"]["last_success_at"] == "2026-10-30T01:59:00Z"


@pytest.mark.parametrize("target,source_field", [("__class__/value", "points_possible"),
    ("buckets/proposal/formula", "points_possible"), ("buckets/0/max", "points_possible"),
    ("buckets/proposal/max", "submission.score"), ("buckets/proposal/max", "__import__('os')")])
def test_sources_reject_arbitrary_fields_paths_and_code(synced, target, source_field):
    svc, aid, ids = synced
    with pytest.raises(InvalidInput):
        svc.create(aid, "score_model", score_spec(), sources={"bindings": [
            {"target": target, "item_id": ids["assignment:7001"], "field": source_field}]})
    assert not svc.list(aid)


def test_source_configuration_and_operations_are_account_scoped(hub, account, synced):
    svc, aid, ids = synced
    other = hub.identity.upsert_verified_account(account["instance_id"], {"canvas_user_id": "other-sync"})["id"]
    v = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    for call in [lambda: svc.create(other, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"])),
                 lambda: svc.configure_sources(other, v["id"], {}),
                 lambda: svc.set_source_override(other, v["id"], "buckets/proposal/max", 30),
                 lambda: svc.propose_spec(other, v["id"], score_spec()),
                 lambda: svc.accept_spec(other, v["id"], v["version"])]:
        with pytest.raises(NotFound):
            call()
    assert svc.sync_sources(other) == []
    field(hub, ids["assignment:7001"], "points_possible", 19)
    svc.sync_sources(other)
    assert svc.get(aid, v["id"])["spec"]["buckets"][0]["max"] == 10


def test_configuration_validates_atomically_before_creating_todos(synced):
    svc, aid, ids = synced
    with pytest.raises(InvalidInput):
        svc.create(aid, "checklist", {"sync_todos": True, "rows": [{"label": "Do this"}]},
                   sources={"collection": {"course_id": "4242", "item_type": "assignment", "section_id": "../bad"}})
    assert not svc.annotations.list(aid, kind="task") and not svc.list(aid)


def test_deleted_link_fallback_still_creates_substep(hub, synced):
    svc, aid, ids = synced
    iid = ids["assignment:7001"]
    v = svc.create(aid, "checklist", {"sync_todos": True, "rows": [{"id": "draft", "label": "Draft", "item_id": iid}]})
    with hub.db.tx() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (iid,))
        conn.execute("DELETE FROM annotations WHERE id=?", (v["state"]["links"]["draft"],))
    v = svc.set_state(aid, v["id"], {"done": {"draft": True}})
    task = hub.annotations.get(aid, v["state"]["links"]["draft"])
    assert task["task_role"] == "substep" and task["task_state"] == "done" and task["item_id"] is None


def test_projector_score_binding_preserves_hypotheticals_and_unknown_score(hub, synced):
    svc, aid, ids = synced
    sub = ids["submission:7001"]
    v = svc.create(aid, "grade_projector", {"categories": [{"id": "course", "items": [
        {"id": "actual", "max": 10}, {"id": "future", "max": 20}]}]}, sources={"bindings": [
            {"target": "categories/course/items/actual/score", "item_id": sub, "field": "score"}]})
    assert v["spec"]["categories"][0]["items"][0]["score"] is None
    v = svc.set_state(aid, v["id"], {"hypothetical": {"future": 15}})
    field(hub, sub, "score", 8)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["spec"]["categories"][0]["items"][0]["score"] == 8
    assert v["state"]["hypothetical"] == {"future": 15}
    field(hub, sub, "score", None)
    svc.sync_sources(aid)
    v = svc.get(aid, v["id"])
    assert v["spec"]["categories"][0]["items"][0]["score"] == 8 and v["source_status"]["unavailable"]


def test_migration_preserves_existing_components_and_history(monkeypatch):
    files = database._migration_files()
    conn = database.connect(":memory:")
    try:
        with monkeypatch.context() as m:
            m.setattr(database, "_migration_files", lambda: [f for f in files if f[0] < 11])
            database.migrate(conn)
        conn.execute("INSERT INTO instances(id,origin,created_at) VALUES (1,'https://example.edu','now')")
        conn.execute("INSERT INTO accounts(id,instance_id,canvas_user_id,first_verified_at,last_verified_at) VALUES (1,1,'a','now','now')")
        conn.execute("""INSERT INTO components(id,account_id,kind,title,spec_json,state_json,created_at,updated_at)
                        VALUES (1,1,'score_model','Saved','{"buckets":[]}','{"values":{"x":3}}','now','now')""")
        conn.execute("""INSERT INTO component_versions(component_id,version,spec_json,state_json,changed_by,created_at)
                        VALUES (1,1,'{}','{}','user','now')""")
        with monkeypatch.context() as m:
            m.setattr(database, "_migration_files", lambda: [f for f in files if f[0] <= 11])
            assert database.migrate(conn) == [11] and database.migrate(conn) == []
        row = dict(conn.execute("SELECT * FROM components").fetchone())
        assert json.loads(row["state_json"]) == {"values": {"x": 3}} and row["source_config_json"] == "{}"
        assert conn.execute("SELECT source_meta_json FROM component_versions").fetchone()[0] is None
    finally:
        conn.close()
