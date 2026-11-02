"""Indexed dependency refresh, including changes that contain no field edits."""
import json
import time

import pytest

from canvas_hub import db as database
from .conftest import variant
from .test_component_sync import score_sources, score_spec, synced  # noqa: F401


def one_item(fixture_pkg, key, timestamp, item_type="assignment", canvas_id="7001"):
    pkg = variant(fixture_pkg, key, timestamp)
    pkg["items"] = [i for i in pkg["items"] if i["type"] == item_type and i.get("canvas_id") == canvas_id]
    pkg["coverage"] = []
    pkg["presence_checks"] = []
    return pkg


def test_import_targets_dependencies_preserving_overrides_and_freshness(hub, synced, fixture_pkg, monkeypatch):
    svc, aid, ids = synced
    first = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    unrelated = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7002"]))
    first = svc.set_source_override(aid, first["id"], "buckets/proposal/max", 99)
    first = svc.set_state(aid, first["id"], {"values": {"proposal": 3}})
    calls = []
    original = svc._sync_component
    monkeypatch.setattr(svc, "_sync_component", lambda a, row: (calls.append(row["id"]), original(a, row))[1])
    package = one_item(fixture_pkg, "targeted-new", "2026-11-01T00:00:00Z")
    package["items"][0]["fields"]["points_possible"] = 22
    result = hub.importer.import_package(package)
    assert calls == [first["id"]]
    assert result["stats"]["components_refreshed"] == 1
    assert result["stats"]["component_sync_seconds"] >= 0
    current = svc.get(aid, first["id"])
    assert current["spec"]["buckets"][0]["max"] == 99
    assert current["source_values"]["buckets/proposal/max"]["value"] == 22
    assert current["state"] == first["state"]
    assert current["source_status"]["last_success_at"] == "2026-11-01T00:00:00Z"
    assert svc.get(aid, unrelated["id"]) == unrelated
    # Equal source values still refresh their actual observation time.
    package = variant(package, "targeted-unchanged", "2026-11-02T00:00:00Z")
    result = hub.importer.import_package(package)
    assert result["stats"]["items_unchanged"] == 1
    assert svc.get(aid, first["id"])["source_status"]["last_success_at"] == "2026-11-02T00:00:00Z"
    # Replayed observations with the same job cannot generate refreshes/events.
    calls.clear()
    result = hub.importer.import_package(package, job_id=result["job_id"])
    assert calls == [] and result["stats"]["components_refreshed"] == 0


def test_partial_observation_marks_only_dependent_source_stale(hub, synced, fixture_pkg):
    svc, aid, ids = synced
    component = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    success = component["source_status"]["last_success_at"]
    package = one_item(fixture_pkg, "targeted-partial", "2026-11-01T00:00:00Z")
    package["items"][0]["fields"] = {"title": "Only title was read"}
    hub.importer.import_package(package)
    current = svc.get(aid, component["id"])
    assert current["source_status"]["availability"] == "partial"
    assert current["source_status"]["last_success_at"] == success
    assert current["spec"]["buckets"][0]["max"] == 10


def test_refresh_outbox_survives_restart_and_duplicate_package(hub, synced, fixture_pkg, monkeypatch):
    svc, aid, ids = synced
    component = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    package = one_item(fixture_pkg, "targeted-interrupted", "2026-11-01T00:00:00Z")
    package["items"][0]["fields"]["points_possible"] = 22
    def failed_sync(*args, **kwargs):
        raise RuntimeError("synthetic stopped process after import commit")
    monkeypatch.setattr(hub.importer, "after_changed_import", failed_sync)
    with pytest.raises(RuntimeError):
        hub.importer.import_package(package)
    assert hub.db.query("SELECT * FROM component_source_changes WHERE account_id=?", (aid,))
    assert svc.get(aid, component["id"])["spec"]["buckets"][0]["max"] == 10
    events = hub.db.one("SELECT COUNT(*) n FROM change_events")["n"]
    hub.reopen()
    result = hub.importer.import_package(package)
    assert result["status"] == "already_imported"
    assert hub.components.get(aid, component["id"])["spec"]["buckets"][0]["max"] == 22
    assert not hub.db.query("SELECT * FROM component_source_changes WHERE account_id=?", (aid,))
    assert hub.db.one("SELECT COUNT(*) n FROM change_events")["n"] == events


def test_presence_and_empty_coverage_refresh_dependencies(hub, synced, fixture_pkg):
    svc, aid, ids = synced
    bound = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    collection = svc.create(aid, "checklist", {"rows": []}, course_id="4242", sources={
        "collection": {"course_id": "4242", "item_type": "assignment", "section_id": "assignments"}})
    package = one_item(fixture_pkg, "targeted-presence", "2026-11-01T00:00:00Z")
    package["items"] = []
    package["presence_checks"] = [{"item": {"type": "assignment", "canvas_id": "7001", "course_id": "4242"},
                                   "result": "forbidden", "http_status": 403, "checked_at": "2026-11-01T00:00:00Z"}]
    result = hub.importer.import_package(package)
    assert result["stats"]["components_refreshed"] == 2
    assert svc.get(aid, bound["id"])["source_status"]["availability"] == "partial"
    package["package_id"] = "targeted-empty-index"
    package["presence_checks"] = []
    package["coverage"] = [{"course_id": "4242", "category": "assignments", "status": "partial",
                            "index_depth": "partial", "observed_at": "2026-11-02T00:00:00Z"}]
    result = hub.importer.import_package(package)
    assert result["stats"]["components_refreshed"] == 1
    current = svc.get(aid, collection["id"])
    assert any(g["reason"] == "collection_partial" for g in current["source_status"]["unavailable"])


def test_absence_inference_refreshes_explicit_item_bindings(hub, synced, fixture_pkg):
    svc, aid, ids = synced
    bound = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    package = one_item(fixture_pkg, "targeted-absent", "2026-11-02T00:00:00Z")
    package["items"] = []
    package["coverage"] = [{"course_id": "4242", "category": "assignments", "status": "complete",
                            "index_depth": "complete", "observed_at": "2026-11-02T00:00:00Z",
                            "absence_inference": {"item_types": ["assignment"]}}]
    result = hub.importer.import_package(package)
    assert result["stats"]["components_refreshed"] == 1
    assert svc.get(aid, bound["id"])["source_status"]["availability"] == "partial"


def test_linked_file_refresh_selects_review_parent_once(hub, synced, monkeypatch):
    svc, aid, ids = synced
    parent = ids["assignment:7001"]
    with hub.db.tx() as c:
        file = c.execute("""INSERT INTO items(account_id,item_type,source_key,first_observed_at,last_observed_at)
                            VALUES (?,'file','synthetic-file','2026-11-01','2026-11-01')""", (aid,)).lastrowid
    component = svc.create(aid, "checklist", {"rows": []}, sources={
        "review_sources": [{"item_id": parent, "fields": ["body"], "attachments": True}]})
    with hub.db.tx() as c:
        c.execute("""INSERT OR IGNORE INTO item_links(account_id,from_item_id,to_item_id,relation,label,first_observed_at,last_observed_at)
                     VALUES (?,?,?,'attachment','','2026-11-01','2026-11-01')""", (aid, parent, file))
    calls = []
    original = svc._sync_component
    monkeypatch.setattr(svc, "_sync_component", lambda a, row: (calls.append(row["id"]), original(a, row))[1])
    svc.sync_changed_sources(aid, item_ids=[file, parent, file])
    assert calls == [component["id"]]
    assert svc.get(aid, component["id"])["source_status"]["availability"] == "partial"


def test_dependency_triggers_cover_reconfigure_undo_and_account_isolation(hub, synced):
    svc, aid, ids = synced
    first, second = ids["assignment:7001"], ids["assignment:7002"]
    component = svc.create(aid, "score_model", score_spec(), sources=score_sources(first))
    svc.configure_sources(aid, component["id"], score_sources(second))
    assert svc.sync_changed_sources(aid, item_ids=[first]) == []
    assert len(svc.sync_changed_sources(aid, item_ids=[second])) == 1
    svc.undo(aid, component["id"])
    assert len(svc.sync_changed_sources(aid, item_ids=[first])) == 1
    assert svc.sync_changed_sources(aid, item_ids=[second]) == []
    instance = hub.identity.add_instance("https://other.example.edu")
    other = hub.identity.upsert_verified_account(instance["id"], {"canvas_user_id": "other"})["id"]
    assert svc.sync_changed_sources(other, item_ids=[first], course_ids=["4242"]) == []
    svc.configure_sources(aid, component["id"], {})
    assert svc.sync_changed_sources(aid, item_ids=[first]) == []
    assert not hub.db.query("SELECT * FROM component_source_dependencies WHERE component_id=?", (component["id"],))


def test_migration_backfills_and_accepts_gap_then_late_0015(monkeypatch):
    files = database._migration_files()
    conn = database.connect(":memory:")
    try:
        with monkeypatch.context() as m:
            m.setattr(database, "_migration_files", lambda: [f for f in files if f[0] < 16])
            database.migrate(conn)
        conn.execute("INSERT INTO instances(id,origin,created_at) VALUES (1,'https://example.edu','now')")
        conn.execute("INSERT INTO accounts(id,instance_id,canvas_user_id,first_verified_at,last_verified_at) VALUES (1,1,'a','now','now')")
        sources = {"bindings": [{"item_id": 123, "target": "title", "field": "title"}],
                   "collection": {"course_id": "4242"}, "review_sources": [{"item_id": 234}]}
        conn.execute("""INSERT INTO components(id,account_id,kind,title,spec_json,state_json,source_config_json,created_at,updated_at)
                        VALUES (1,1,'checklist','Saved','{}','{}',?,'now','now')""", (json.dumps(sources),))
        assert database.migrate(conn) == [16]
        assert {(r["dependency_kind"],r["dependency_key"]) for r in conn.execute("SELECT * FROM component_source_dependencies")} == {
            ("item", "123"), ("item", "234"), ("collection", "4242")}
        # A later independently integrated migration must still run despite MAX=16.
        missing = (15, "0015_synthetic_parallel_feature.sql", "CREATE TABLE synthetic_parallel_feature (id INTEGER);")
        with monkeypatch.context() as m:
            m.setattr(database, "_migration_files", lambda: sorted([*files, missing]))
            assert database.migrate(conn) == [15]
            assert database.migrate(conn) == []
        conn.execute("DELETE FROM components WHERE id=1")
        assert not conn.execute("SELECT 1 FROM component_source_dependencies").fetchone()
    finally:
        conn.close()


def test_synthetic_targeted_refresh_benchmark(hub, synced, monkeypatch):
    svc, aid, ids = synced
    for _ in range(49):
        svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7002"]))
    selected = svc.create(aid, "score_model", score_spec(), sources=score_sources(ids["assignment:7001"]))
    calls = []
    original = svc._sync_component
    monkeypatch.setattr(svc, "_sync_component", lambda a, row: (calls.append(row["id"]), original(a, row))[1])
    start = time.monotonic()
    svc.sync_sources(aid)
    full_seconds, full_count = time.monotonic() - start, len(calls)
    calls.clear()
    start = time.monotonic()
    svc.sync_changed_sources(aid, item_ids=[ids["assignment:7001"]] * 1000)
    targeted_seconds = time.monotonic() - start
    assert full_count == 50 and calls == [selected["id"]]
    print(f"synthetic component refresh: full={full_count} components/{full_seconds:.6f}s; "
          f"targeted={len(calls)} component/{targeted_seconds:.6f}s; unrelated avoided=49")
