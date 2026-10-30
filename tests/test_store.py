"""Storage, import, change detection, isolation and coverage rules."""

import pytest

from canvas_hub.util import IdentityMismatch, InvalidInput, NotFound

from .conftest import variant


def events(hub, account_id, **kw):
    return hub.query.changes(account_id, include_baseline=True, limit=500, **kw)["events"]


def test_fixture_validates_and_imports(hub, account, fixture_pkg):
    r = hub.importer.import_package(fixture_pkg)
    assert r["status"] == "imported" and r["stats"]["items_new"] == 6
    ev = events(hub, account["id"])
    assert {e["kind"] for e in ev} == {"first_observed"}
    assert all(e["baseline"] for e in ev)
    # The page was first referenced by the syllabus link, then read: one item, linked.
    pages = hub.query.list_items(account["id"], item_type="page")["items"]
    assert len(pages) == 1
    detail = hub.query.get_item(account["id"], pages[0]["id"])
    assert detail["links_in"][0]["relation"] == "body_link"


def test_reimport_is_idempotent(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    again = hub.importer.import_package(fixture_pkg)
    assert again["status"] == "already_imported"
    n = hub.db.one("SELECT COUNT(*) n FROM items")["n"]
    # Same observations under a different package id with identical observation ids
    for it in fixture_pkg["items"]:
        it["observation_id"] = "fixed-" + it["type"] + (it.get("canvas_id") or it.get("source_key"))
    p2 = variant(fixture_pkg, "fixture-demo-0002", "2026-10-30T03:00:00Z")
    hub.importer.import_package(p2)
    r3 = hub.importer.import_package(variant(p2, "fixture-demo-0003", "2026-10-30T03:00:00Z"))
    assert r3["stats"]["observations_duplicate"] == 6
    assert hub.db.one("SELECT COUNT(*) n FROM items")["n"] == n


def test_unchanged_second_pass_has_no_false_updates(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    before = len(events(hub, account["id"]))

    def reformat(p):  # harmless formatting differences only
        for it in p["items"]:
            if it["type"] == "page":
                it["fields"]["body"] = "<p>Build a small   data visualisation.\n Teams of two.</p>"
    r = hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z", reformat))
    assert r["stats"]["events"] == 0 and r["stats"]["items_unchanged"] == 6
    assert len(events(hub, account["id"])) == before
    assert hub.db.one("SELECT COUNT(*) n FROM item_revisions")["n"] == 6


def test_changed_deadline_and_body_detected(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)

    def edit(p):
        for it in p["items"]:
            if it["type"] == "assignment" and it["canvas_id"] == "7001":
                it["fields"]["due_at"] = {**it["fields"]["due_at"], "raw": "2026-11-08T15:59:00Z", "iso": "2026-11-08T15:59:00Z"}
            if it["type"] == "page":
                it["fields"]["body"] = "<p>Build a small data visualisation. Teams of THREE.</p>"
    hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z", edit))
    ch = hub.query.changes(account["id"])["events"]   # baseline excluded by default
    kinds = {(e["kind"], e["field"]) for e in ch}
    assert ("field_changed", "due_at") in kinds and ("field_changed", "body") in kinds
    due = next(e for e in ch if e["field"] == "due_at")
    assert due["old"]["iso"] == "2026-11-06T15:59:00Z" and due["new"]["iso"] == "2026-11-08T15:59:00Z"
    hist = hub.query.history(account["id"], due["item_id"])
    assert len(hist["revisions"]) == 2 and hist["revisions"][0]["changed_fields"] == ["due_at"]


def test_newly_read_field_is_not_an_edit(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)

    def add(p):
        for it in p["items"]:
            if it["type"] == "assignment" and it["canvas_id"] == "7001":
                it["fields"]["rubric"] = [{"description": "Clarity", "points": 5}]
    hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z", add))
    ch = hub.query.changes(account["id"])["events"]
    assert [(e["kind"], e["field"]) for e in ch] == [("field_added", "rubric")]


def test_missing_field_is_not_erased_and_freshness_is_mixed(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)

    def drop(p):
        for it in p["items"]:
            if it["type"] == "assignment" and it["canvas_id"] == "7001":
                del it["fields"]["description"]   # not collected this time
    hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z", drop))
    asg = next(i for i in hub.query.list_items(account["id"], item_type="assignment")["items"] if i["canvas_id"] == "7001")
    item = hub.query.get_item(account["id"], asg["id"])
    desc = next(f for f in item["fields"] if f["field"] == "description")
    assert "One page" in desc["value"] and desc["last_observed_at"] == "2026-10-30T01:59:00Z"
    assert item["freshness"]["state"] == "mixed"


def test_volatile_fields_never_emit_changes(hub, account, fixture_pkg):
    def add_vol(p, n):
        p["items"].append({"type": "announcement", "course_id": "4242", "canvas_id": "9001", "title": "Hello",
                           "observed_at": p["items"][0]["observed_at"], "fields": {"title": "Hello", "message": "<p>x</p>",
                                                                                     "read_state": "unread", "unread_count": n}})
    hub.importer.import_package(variant(fixture_pkg, "p1", "2026-10-30T02:00:00Z", lambda p: add_vol(p, 3)))
    hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z", lambda p: add_vol(p, 0)))
    assert hub.query.changes(account["id"])["events"] == []


def test_stale_observation_does_not_overwrite(hub, account, fixture_pkg):
    hub.importer.import_package(variant(fixture_pkg, "new", "2026-11-01T00:00:00Z"))

    def old_title(p):
        for it in p["items"]:
            if it["type"] == "page":
                it["fields"]["title"] = "Old title"
    r = hub.importer.import_package(variant(fixture_pkg, "old", "2026-10-01T00:00:00Z", old_title))
    assert r["stats"]["stale_fields_ignored"] > 0
    page = hub.query.list_items(account["id"], item_type="page")["items"][0]
    assert page["title"] == "Project brief"


def test_partial_pass_never_marks_items_missing(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)

    def partial(p):
        p["items"] = [it for it in p["items"] if it.get("canvas_id") != "7002"]
        for c in p["coverage"]:
            if c["category"] == "assignments":
                c["status"] = "partial"
                c["index_depth"] = "partial"
    hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z", partial))
    assert hub.db.one("SELECT COUNT(*) n FROM items WHERE presence!='observed'")["n"] == 0


def test_complete_index_marks_not_in_index_then_presence_check(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)

    def gone(p):
        p["items"] = [it for it in p["items"] if it.get("canvas_id") != "7002"]
    hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z", gone))
    row = hub.db.one("SELECT * FROM items WHERE canvas_id='7002' AND item_type='assignment'")
    assert row["presence"] == "not_in_latest_index"
    # evidence kept
    assert hub.db.one("SELECT COUNT(*) n FROM item_fields WHERE item_id=?", (row["id"],))["n"] > 0

    def check(p):
        p["items"] = []
        p["coverage"] = []
        p["presence_checks"] = [{"item": {"type": "assignment", "course_id": "4242", "canvas_id": "7002"},
                                 "result": "not_found", "http_status": 404, "checked_at": "2026-10-31T02:05:00Z"}]
    hub.importer.import_package(variant(fixture_pkg, "p3", "2026-10-31T02:05:00Z", check))
    assert hub.db.one("SELECT presence FROM items WHERE id=?", (row["id"],))["presence"] == "source_unavailable"
    kinds = [e["kind"] for e in hub.query.changes(account["id"])["events"]]
    assert kinds == ["not_in_latest_index", "source_unavailable"]
    # It comes back -> reobserved
    hub.importer.import_package(variant(fixture_pkg, "p4", "2026-11-01T02:00:00Z"))
    assert hub.db.one("SELECT presence FROM items WHERE id=?", (row["id"],))["presence"] == "observed"
    assert hub.query.changes(account["id"])["events"][-1]["kind"] == "reobserved"


def test_account_isolation(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    other = hub.identity.upsert_verified_account(account["instance_id"], {"canvas_user_id": "100002"})
    with pytest.raises(IdentityMismatch):
        hub.importer.import_package(variant(fixture_pkg, "x", "2026-10-31T00:00:00Z"), expected_account_id=other["id"])
    assert hub.query.list_items(other["id"])["items"] == []
    pkg_b = variant(fixture_pkg, "b1", "2026-10-31T00:00:00Z")
    pkg_b["identity"]["canvas_user_id"] = "100002"
    hub.importer.import_package(pkg_b)
    a_items = {i["id"] for i in hub.query.list_items(account["id"])["items"]}
    b_items = {i["id"] for i in hub.query.list_items(other["id"])["items"]}
    assert a_items and b_items and not (a_items & b_items)
    with pytest.raises(NotFound):
        hub.query.get_item(other["id"], next(iter(a_items)))
    assert hub.search.search(other["id"], "visualisation")["results"][0]["item_id"] in b_items
    # unknown account is refused without explicit permission
    pkg_c = variant(fixture_pkg, "c1", "2026-10-31T00:00:00Z")
    pkg_c["identity"]["canvas_user_id"] = "999"
    with pytest.raises(NotFound):
        hub.importer.import_package(pkg_c)


def test_invalid_packages_rejected(hub, account, fixture_pkg):
    bad = variant(fixture_pkg, "bad", "2026-10-31T00:00:00Z")
    bad["items"][0]["type"] = "nonsense"
    with pytest.raises(InvalidInput) as e:
        hub.importer.import_package(bad)
    assert e.value.code == "invalid_package"
    bad2 = variant(fixture_pkg, "bad2", "2026-10-31T00:00:00Z")
    bad2["items"][0]["observed_at"] = "2026-10-31 10:00"   # no offset
    with pytest.raises(InvalidInput):
        hub.importer.import_package(bad2)
    bad3 = variant(fixture_pkg, "bad3", "2026-10-31T00:00:00Z")
    bad3["items"][0]["resources"] = [{"sha256": "0" * 64}]
    with pytest.raises(InvalidInput):
        hub.importer.import_package(bad3)


def test_annotations_survive_refresh(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    asg = next(i for i in hub.query.list_items(account["id"], item_type="assignment")["items"] if i["canvas_id"] == "7001")
    note = hub.annotations.create(account["id"], kind="task", item_id=asg["id"], title="Start early", task_state="done")
    hub.importer.import_package(variant(fixture_pkg, "p2", "2026-10-31T02:00:00Z"))
    assert hub.annotations.get(account["id"], note["id"])["task_state"] == "done"
    # Local "done" is not a Canvas submission claim
    sub = hub.query.deadlines(account["id"], start="2026-11-01T00:00:00Z", end="2026-12-01T00:00:00Z")["deadlines"][0]
    assert sub["submission"]["workflow_state"] == "unsubmitted"
    assert sub["local_tasks"][0]["task_state"] == "done"


def test_deadlines_keep_unresolved_dates_and_timezone(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    d = hub.query.deadlines(account["id"], start="2026-11-01T00:00:00+08:00", end="2026-12-01T00:00:00+08:00")
    assert d["time_zone"] == "Asia/Hong_Kong"
    assert [x["title"] for x in d["deadlines"]] == ["Project proposal"]
    assert d["deadlines"][0]["date_local"] == "2026-11-06T23:59:00+08:00"
    assert [x["date"]["raw"] for x in d["unresolved_dates"]] == ["Nov 13 at 11:59pm"]
    utc = hub.query.deadlines(account["id"], start="2026-11-01T00:00:00Z", end="2026-12-01T00:00:00Z", time_zone="UTC")
    assert utc["deadlines"][0]["date_local"] == "2026-11-06T15:59:00+00:00"
    with pytest.raises(InvalidInput):
        hub.query.deadlines(account["id"], start="2026-11-01", end="2026-12-01")


def test_changes_cursor_is_stable(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    first = hub.query.changes(account["id"], include_baseline=True, limit=4)
    rest = hub.query.changes(account["id"], include_baseline=True, cursor=first["next_cursor"], limit=50)
    ids = [e["id"] for e in first["events"] + rest["events"]]
    assert ids == sorted(ids) and len(ids) == len(set(ids)) == 6
    empty = hub.query.changes(account["id"], include_baseline=True, cursor=rest["next_cursor"])
    assert empty["events"] == [] and empty["next_cursor"] == rest["next_cursor"]


def test_coverage_keeps_thorough_baseline_separate(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)

    def quick(p):
        p["run"]["mode"] = "quick"
        for c in p["coverage"]:
            if c["category"] == "assignments":
                c["status"] = "partial"
                c["body_depth"] = "not_checked"
                c["reason"] = "quick check"
    hub.importer.import_package(variant(fixture_pkg, "q1", "2026-10-31T02:00:00Z", quick))
    rec = next(r for r in hub.query.coverage(account["id"])["records"] if r["category"] == "assignments")
    assert rec["latest"]["status"] == "partial" and rec["latest"]["mode"] == "quick"
    assert rec["last_thorough"]["status"] == "complete"
    assert rec["last_complete_at"] == "2026-10-30T01:59:10Z"


def test_search_filters_and_pagination(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    r = hub.search.search(account["id"], "visualisation")
    assert r["results"][0]["title"] == "Project brief" and "[[visualisation]]" in r["results"][0]["snippet"]
    assert hub.search.search(account["id"], "visualisation", types=["assignment"])["results"] == []
    assert hub.search.search(account["id"], '"teams of two"')["results"]
    assert hub.search.search(account["id"], "visual*")["results"]
    p1 = hub.search.search(account["id"], "project", limit=1)
    assert p1["next_cursor"]
    p2 = hub.search.search(account["id"], "project", limit=1, cursor=p1["next_cursor"])
    assert p2["results"][0]["item_id"] != p1["results"][0]["item_id"]
    with pytest.raises(InvalidInput):
        hub.search.search(account["id"], "   ")
    # FTS syntax in user input is neutralised, not executed
    assert hub.search.search(account["id"], 'project OR NEAR( "')["results"] is not None


def test_resource_reads_are_account_checked(hub, account, fixture_pkg, tmp_path):
    sha = hub.resources.store_bytes(b"Lecture text about eigenvalues", "text/plain")
    hub.resources.extract(sha, "notes.txt")

    def attach(p):
        p["items"].append({"type": "file", "course_id": "4242", "canvas_id": "8001", "title": "notes.txt",
                           "observed_at": p["items"][0]["observed_at"], "fields": {"title": "notes.txt", "size": 30},
                           "resources": [{"sha256": sha, "filename": "notes.txt"}]})
    hub.importer.import_package(variant(fixture_pkg, "r1", "2026-10-30T02:00:00Z", attach))
    t = hub.resources.read_text(account["id"], sha)
    assert "eigenvalues" in t["text"]
    hits = hub.search.search(account["id"], "eigenvalues")["results"]
    assert hits[0]["resource_id"] == sha
    other = hub.identity.upsert_verified_account(account["instance_id"], {"canvas_user_id": "55"})
    with pytest.raises(NotFound):
        hub.resources.read_text(other["id"], sha)
    with pytest.raises(InvalidInput):
        hub.resources.read_text(account["id"], "../../etc/passwd")
