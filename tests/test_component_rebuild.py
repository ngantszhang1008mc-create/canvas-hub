"""Guarded tracker rebuilds use private fixtures, never providers or real data."""
import copy
import json

import pytest

from canvas_hub.agent_tools import ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.util import Conflict, InvalidInput
from .test_components import HMAW


def rebuild(svc, aid, view, spec, **kwargs):
    return svc.propose_rebuild(aid, view["id"], spec, expected_version=view["version"],
                               state_token=svc.state_token(view), **kwargs)


def score_tracker(hub, account):
    svc, aid = hub.components, account["id"]
    view = svc.create(aid, "score_model", HMAW)
    view = svc.pin(aid, view["id"], True)
    view = svc.set_state(aid, view["id"], {"include": {"nest": False, "fyes": False}, "values": {"cm2": 0}})
    assert view["computed"]["totals"]["projected"] == 70
    return svc, aid, view


def rename_score(spec):
    result = copy.deepcopy(spec)
    mapping = {row["id"]: "rebuilt_" + row["id"] for row in result["buckets"]}
    for row in result["buckets"]:
        row["id"] = mapping[row["id"]]
    for scenario in result["scenarios"]:
        for field in ("include", "values"):
            scenario[field] = {mapping[key]: value for key, value in scenario[field].items()}
    return result, mapping


def test_rebuild_keeps_false_zero_total_pin_identity_and_original_until_approved(hub, account):
    svc, aid, original = score_tracker(hub, account)
    spec, mapping = rename_score(original["spec"])
    spec["title"] = "Rebuilt tracker"
    proposed = rebuild(svc, aid, original, spec, identity_map={"rows": mapping})
    assert proposed["id"] == original["id"] and proposed["pinned"]
    assert proposed["state"] == original["state"] and proposed["spec"] == original["spec"]
    migration = proposed["pending_spec"]["migration"]
    assert migration["can_accept"] and migration["preserved_fields"] == 3
    assert migration["state"]["include"] == {"rebuilt_nest": False, "rebuilt_fyes": False}
    assert migration["state"]["values"] == {"rebuilt_cm2": 0}
    accepted = svc.accept_spec(aid, proposed["id"], proposed["version"])
    assert accepted["id"] == original["id"] and accepted["pinned"]
    assert accepted["pending_spec"] is None and accepted["state"] == migration["state"]
    assert accepted["computed"]["totals"]["projected"] == 70
    assert len(svc.list(aid)) == 1
    undone = svc.undo(aid, accepted["id"])
    assert undone["spec"] == original["spec"] and undone["state"] == original["state"]


def test_stable_ids_need_no_explicit_mapping(hub, account):
    svc, aid, original = score_tracker(hub, account)
    spec = {**original["spec"], "title": "New title", "buckets": list(reversed(original["spec"]["buckets"]))}
    proposed = rebuild(svc, aid, original, spec)
    assert all(match["method"] == "stable_id" for match in proposed["pending_spec"]["migration"]["matches"])
    accepted = svc.accept_spec(aid, proposed["id"], proposed["version"])
    assert accepted["state"] == original["state"] and accepted["computed"]["totals"]["projected"] == 70


def test_unique_source_identity_migrates_bindings_values_and_overrides(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid, svc = account["id"], hub.components
    iid = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1", (aid,))["id"]
    view = svc.create(aid, "score_model", {"buckets": [{"id": "old", "label": "A", "max": 10, "item_id": iid}]},
                      sources={"bindings": [{"target": "buckets/old/max", "item_id": iid, "field": "points_possible"}]})
    view = svc.set_source_override(aid, view["id"], "buckets/old/max", 0)
    view = svc.set_state(aid, view["id"], {"include": {"old": False}, "values": {"old": 0}})
    spec = copy.deepcopy(view["spec"])
    spec["buckets"][0]["id"] = "new"
    proposed = rebuild(svc, aid, view, spec)
    assert proposed["pending_spec"]["migration"]["matches"][0]["method"] == "source_identity"
    assert proposed["source_config"] == view["source_config"]
    accepted = svc.accept_spec(aid, view["id"], proposed["version"])
    assert accepted["source_config"]["bindings"][0] == {"target": "buckets/new/max", "item_id": iid, "field": "points_possible"}
    assert accepted["source_overrides"] == {"buckets/new/max": view["source_overrides"]["buckets/old/max"]}
    assert accepted["source_values"] == {"buckets/new/max": view["source_values"]["buckets/old/max"]}
    assert accepted["state"]["include"] == {"new": False} and accepted["state"]["values"] == {"new": 0}
    with hub.db.tx() as conn:
        conn.execute("UPDATE item_fields SET value_json='22' WHERE item_id=? AND field='points_possible'", (iid,))
    svc.sync_sources(aid)
    assert svc.get(aid, view["id"])["spec"]["buckets"][0]["max"] == 0
    followed = svc.set_source_override(aid, view["id"], "buckets/new/max", follow_source=True)
    assert followed["spec"]["buckets"][0]["max"] == 22


@pytest.mark.parametrize("problem", ["unmapped", "conflict", "incompatible", "rule_change", "unknown_identity"])
def test_unresolved_migration_is_reported_and_cannot_be_accepted(hub, account, problem):
    svc, aid, original = score_tracker(hub, account)
    spec, mapping = rename_score(original["spec"])
    identity_map = {"rows": mapping}
    if problem == "unmapped":
        identity_map = None  # Identical labels and positions are not evidence of identity.
    elif problem == "conflict":
        mapping["fyes"] = mapping["nest"]
    elif problem == "incompatible":
        spec["buckets"][3].update(unit="hours", full_at=10)
    elif problem == "rule_change":
        spec["threshold"] = 10
    else:
        mapping["does_not_exist"] = "rebuilt_intro"
    proposed = rebuild(svc, aid, original, spec, identity_map=identity_map)
    diagnostics = proposed["pending_spec"]["migration"]
    assert not diagnostics["can_accept"]
    assert problem in {issue["type"] for issue in diagnostics["unresolved"]}
    with pytest.raises(Conflict) as error:
        svc.accept_spec(aid, proposed["id"], proposed["version"])
    assert error.value.code == "migration_unresolved"
    current = svc.get(aid, proposed["id"])
    assert current["spec"] == original["spec"] and current["state"] == original["state"] and current["pinned"]


def test_ambiguous_source_identity_requires_explicit_one_to_one_mapping(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid, svc = account["id"], hub.components
    iid = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1", (aid,))["id"]
    view = svc.create(aid, "score_model", {"buckets": [{"id": "a", "max": 5, "item_id": iid}, {"id": "b", "max": 5, "item_id": iid}]},
                      state={"include": {"a": False}, "values": {"b": 0}})
    spec = copy.deepcopy(view["spec"])
    spec["buckets"][0]["id"], spec["buckets"][1]["id"] = "x", "y"
    proposed = rebuild(svc, aid, view, spec)
    assert "ambiguous" in {issue["type"] for issue in proposed["pending_spec"]["migration"]["unresolved"]}
    with pytest.raises(Conflict):
        svc.accept_spec(aid, view["id"], proposed["version"])
    corrected = rebuild(svc, aid, proposed, spec, identity_map={"rows": {"a": "x", "b": "y"}})
    accepted = svc.accept_spec(aid, view["id"], corrected["version"])
    assert accepted["state"]["include"] == {"x": False} and accepted["state"]["values"] == {"y": 0}


def test_stale_proposal_and_acceptance_guards(hub, account):
    svc, aid, view = score_tracker(hub, account)
    newer = svc.set_state(aid, view["id"], {**view["state"], "values": {"cm2": 5}})
    with pytest.raises(Conflict):
        rebuild(svc, aid, view, view["spec"])
    assert svc.get(aid, view["id"])["pending_spec"] is None
    with pytest.raises(Conflict):
        svc.propose_rebuild(aid, view["id"], newer["spec"], expected_version=newer["version"], state_token="stale")
    proposed = rebuild(svc, aid, newer, newer["spec"])
    newest = svc.set_state(aid, view["id"], {**newer["state"], "values": {"cm2": 0}})
    for version in (proposed["version"], newest["version"]):
        with pytest.raises(Conflict):
            svc.accept_spec(aid, view["id"], version)
    assert svc.get(aid, view["id"])["state"] == newest["state"]


def test_state_token_is_bound_to_component_identity(hub, account):
    svc, aid = hub.components, account["id"]
    first = svc.create(aid, "score_model", HMAW)
    second = svc.create(aid, "score_model", HMAW)
    assert first["state"] == second["state"] and first["version"] == second["version"]
    with pytest.raises(Conflict):
        svc.propose_rebuild(aid, second["id"], second["spec"], expected_version=second["version"],
                             state_token=svc.state_token(first))


def test_grade_projection_and_hours_state_are_preserved(hub, account):
    svc, aid = hub.components, account["id"]
    grade = svc.create(aid, "grade_projector", {"categories": [{"id": "c", "weight": 100,
        "items": [{"id": "a", "max": 10}]}]}, state={"hypothetical": {"a": 0}})
    spec = copy.deepcopy(grade["spec"])
    spec["categories"][0]["id"], spec["categories"][0]["items"][0]["id"] = "renamed_c", "renamed_a"
    proposed = rebuild(svc, aid, grade, spec, identity_map={"rows": {"a": "renamed_a"}})
    accepted = svc.accept_spec(aid, grade["id"], proposed["version"])
    assert accepted["state"] == {"hypothetical": {"renamed_a": 0}}
    assert accepted["computed"]["percent"] == grade["computed"]["percent"] == 0
    hours = svc.create(aid, "hours_tracker", {"target": 20, "minimum": 10}, state={
        "entries": [{"id": "e", "date": "2026-01-01", "hours": 0, "note": "Checked"}],
        "refreshed_at": "2026-01-01T12:00:00Z"})
    proposed = rebuild(svc, aid, hours, {**hours["spec"], "title": "Rebuilt hours"})
    accepted = svc.accept_spec(aid, hours["id"], proposed["version"])
    assert accepted["state"] == hours["state"]


def test_renamed_active_scenario_preserves_identity(hub, account):
    svc, aid, view = score_tracker(hub, account)
    view = svc.set_state(aid, view["id"], {**view["state"], "scenario": "min"})
    spec = copy.deepcopy(view["spec"])
    spec["scenarios"][0]["id"] = "minimum"
    proposed = rebuild(svc, aid, view, spec, identity_map={"scenarios": {"min": "minimum"}})
    accepted = svc.accept_spec(aid, view["id"], proposed["version"])
    assert accepted["state"]["scenario"] == "minimum"
    assert accepted["computed"]["totals"]["projected"] == 70


def linked_tracker(hub, account):
    svc, aid = hub.components, account["id"]
    view = svc.create(aid, "checklist", {"sync_todos": True, "sections": [{"id": "s", "rows": [{"id": "a", "label": "Original"}]}]})
    spec = copy.deepcopy(view["spec"])
    spec["sections"][0]["rows"][0].update(id="renamed", label="Rebuilt")
    return svc, aid, view, spec


def test_linked_todo_changes_invalidate_same_version_state_token(hub, account):
    svc, aid, view, spec = linked_tracker(hub, account)
    todo = view["state"]["links"]["a"]
    proposed = rebuild(svc, aid, view, spec, identity_map={"rows": {"a": "renamed"}})
    hub.annotations.update(aid, todo, task_state="done")
    assert svc.get(aid, view["id"])["version"] == proposed["version"]
    with pytest.raises(Conflict):
        svc.accept_spec(aid, view["id"], proposed["version"])
    with pytest.raises(Conflict):
        rebuild(svc, aid, proposed, spec, identity_map={"rows": {"a": "renamed"}})
    assert hub.annotations.get(aid, todo)["task_state"] == "done"


@pytest.mark.parametrize("after_save", [False, True])
def test_acceptance_rolls_back_definition_state_bindings_and_todos_on_failure(hub, account, monkeypatch, after_save):
    svc, aid, view, spec = linked_tracker(hub, account)
    proposed = rebuild(svc, aid, view, spec, identity_map={"rows": {"a": "renamed"}})
    before = dict(svc._row(aid, view["id"]))
    todo = hub.annotations.get(aid, view["state"]["links"]["a"])
    original_save = svc._save
    def fail(*args, **kwargs):
        if args[4] == "accepted_spec":
            if after_save:
                original_save(*args, **kwargs)
            raise Conflict("synthetic failure", code="version_conflict")
        return original_save(*args, **kwargs)
    monkeypatch.setattr(svc, "_save", fail)
    with pytest.raises(Conflict):
        svc.accept_spec(aid, view["id"], proposed["version"])
    current = svc._row(aid, view["id"])
    for field in ("spec_json", "state_json", "version", "source_config_json", "source_values_json", "source_overrides_json", "pending_spec_json"):
        assert current[field] == before[field]
    assert hub.annotations.get(aid, todo["id"])["title"] == todo["title"]
    assert len(hub.annotations.list(aid)) == 1


def test_checklist_renames_preserve_links_false_and_collapsed_state(hub, account):
    svc, aid, view, spec = linked_tracker(hub, account)
    spec["sections"][0]["id"] = "new_section"
    proposed = rebuild(svc, aid, view, spec, identity_map={"rows": {"a": "renamed"}})
    accepted = svc.accept_spec(aid, view["id"], proposed["version"])
    assert accepted["state"]["links"] == {"renamed": view["state"]["links"]["a"]}
    assert len(hub.annotations.list(aid)) == 1
    assert hub.annotations.get(aid, view["state"]["links"]["a"])["title"] == "Rebuilt"
    plain = svc.create(aid, "checklist", {"sections": [{"id": "s", "rows": [{"id": "r", "label": "Row"}]}]},
                       state={"done": {"r": False}, "collapsed": {"s": False}})
    new_spec = {**plain["spec"], "sections": [{"id": "new_s", "rows": [{"id": "new_r", "label": "Row"}]}]}
    proposed = rebuild(svc, aid, plain, new_spec, identity_map={"rows": {"r": "new_r"}})
    accepted = svc.accept_spec(aid, plain["id"], proposed["version"])
    assert accepted["state"]["done"] == {"new_r": False} and accepted["state"]["collapsed"] == {"new_s": False}


def test_legacy_definition_paths_cannot_silently_drop_saved_state(hub, account):
    svc, aid, view = score_tracker(hub, account)
    spec, _ = rename_score(view["spec"])
    with pytest.raises(Conflict):
        svc.update_spec(aid, view["id"], spec)
    proposed = svc.propose_spec(aid, view["id"], spec)
    assert not proposed["pending_spec"]["migration"]["can_accept"]
    with pytest.raises(Conflict):
        svc.accept_spec(aid, view["id"], proposed["version"])
    assert svc.get(aid, view["id"])["state"] == view["state"]


def test_tool_requires_guards_and_leaves_pin_until_user_approval(hub, account):
    svc, aid, view = score_tracker(hub, account)
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    args = {"component_id": view["id"], "spec": {**view["spec"], "title": "Rebuilt"}}
    _, error, _ = run_tool_display(ctx, "propose_component_rebuild", args, {"ui"})
    assert error
    args.update(expected_version=view["version"], state_token=svc.state_token(view))
    text, error, _ = run_tool_display(ctx, "propose_component_rebuild", args, {"ui"})
    assert not error and json.loads(text)["migration"]["can_accept"]
    assert svc.get(aid, view["id"])["spec"]["title"] == view["spec"]["title"]
    _, error, _ = run_tool_display(ctx, "update_component", {"component_id": view["id"], "spec": view["spec"], "pin": False}, {"ui"})
    assert error and svc.get(aid, view["id"])["pinned"]
    unrelated = svc.create(aid, "score_model", {"title": "Unrelated", "buckets": [{"id": "a", "max": 5}]})
    assert unrelated["id"] != view["id"] and len(svc.list(aid)) == 2


def test_source_bound_scalar_still_requires_explicit_override(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    svc, aid = hub.components, account["id"]
    iid = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1", (aid,))["id"]
    view = svc.create(aid, "score_model", {"buckets": [{"id": "a", "max": 10, "item_id": iid}]},
                      sources={"bindings": [{"target": "buckets/a/max", "item_id": iid, "field": "points_possible"}]})
    spec = copy.deepcopy(view["spec"])
    spec["buckets"][0].update(id="new", max=999)
    with pytest.raises(InvalidInput, match="source override"):
        rebuild(svc, aid, view, spec)
    assert svc.get(aid, view["id"])["pending_spec"] is None


def test_new_source_fact_invalidates_rebuild_guard_and_can_be_read_again(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    svc, aid = hub.components, account["id"]
    iid = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='assignment' LIMIT 1", (aid,))["id"]
    view = svc.create(aid, "score_model", {"buckets": [{"id": "a", "max": 10}]},
        sources={"bindings": [{"target": "buckets/a/max", "item_id": iid, "field": "points_possible"}]})
    with hub.db.tx() as conn:
        conn.execute("UPDATE item_fields SET value_json='22' WHERE item_id=? AND field='points_possible'", (iid,))
    with pytest.raises(Conflict):
        rebuild(svc, aid, view, view["spec"])
    fresh = svc.get(aid, view["id"])
    assert fresh["spec"]["buckets"][0]["max"] == 22 and fresh["pending_spec"] is None
    proposed = rebuild(svc, aid, fresh, fresh["spec"])
    assert proposed["pending_spec"]["migration"]["can_accept"]
