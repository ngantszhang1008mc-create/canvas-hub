"""Automatic refresh (in-app only) and the change bell."""

from datetime import datetime, timedelta, timezone

from canvas_hub.collector.runner import JobRunner

from .conftest import variant


def test_bell_shows_important_changes_after_the_baseline(hub, account, fixture_pkg):
    aid = account["id"]
    hub.importer.import_package(fixture_pkg)
    assert hub.notifications.feed(aid)["unread"] == 0             # the first baseline is never notified

    def mut(p):
        for it in p["items"]:
            if it.get("canvas_id") == "7001" and it["type"] == "assignment":
                it["fields"]["due_at"] = {"raw": "2026-11-08T15:59:00Z", "iso": "2026-11-08T15:59:00Z",
                                          "source": "canvas_api_iso8601", "precision": "instant"}
    hub.importer.import_package(variant(fixture_pkg, "later", "2026-10-31T02:00:00Z", mut))
    f = hub.notifications.feed(aid)
    due = [x for x in f["items"] if x["headline"] == "Due date changed"]
    assert f["unread"] >= 1 and due and due[0]["unread"] and due[0]["title"] == "Project proposal"
    assert due[0]["detail"] == "Fri 06 Nov 2026 23:59 HKT → Sun 08 Nov 2026 23:59 HKT"
    f = hub.notifications.mark_seen(aid, f["latest_change_id"])
    assert f["unread"] == 0 and not any(x["unread"] for x in f["items"])


class _Worker:
    def __init__(self, account_id, status="signed_in"):
        self.on_signed_in = None
        self.account_id = account_id
        self.status = status

    def session(self, iid):
        return {"status": self.status, "account_id": self.account_id}

    def call(self, fn, *a, timeout=None):
        return fn(*a)

    def check_session(self, iid, launch=False):
        return self.session(iid)


def _finished(hub, account, minutes_ago):
    j = hub.jobs.create(account_id=account["id"], instance_id=account["instance_id"], kind="collect", mode="baseline",
                        scope=account["scope"])
    hub.jobs.set_state(j["id"], "completed")
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with hub.db.tx() as c:
        c.execute("UPDATE jobs SET finished_at=? WHERE id=?", (when, j["id"]))


def _queued(hub, account):
    return [j for j in hub.jobs.list(account["id"])["jobs"] if j["state"] == "queued"]


def test_automatic_refresh_queues_quick_checks_only_when_due(hub, account):
    account = hub.identity.get_account(account["id"])
    runner = JobRunner(hub, _Worker(account["id"]))       # not started; we tick it by hand
    runner.has_lock = True
    runner._auto_refresh()
    assert _queued(hub, account) == []                     # off by default
    hub.settings.set(account["id"], "auto_refresh", {"minutes": 30})
    runner._last_auto = 0
    runner._auto_refresh()
    assert _queued(hub, account) == []                     # never before a first collection
    _finished(hub, account, minutes_ago=10)
    runner._last_auto = 0
    runner._auto_refresh()
    assert _queued(hub, account) == []                     # not due yet
    _finished(hub, account, minutes_ago=45)
    with hub.db.tx() as c:
        c.execute("UPDATE jobs SET finished_at=? WHERE account_id=?",
                  ((datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(), account["id"]))
    runner.worker.status = "expired"
    runner._last_auto = 0
    runner._auto_refresh()
    assert _queued(hub, account) == []                     # an expired session waits for the user
    runner.worker.status = "signed_in"
    runner._last_auto = 0
    runner._auto_refresh()
    q = _queued(hub, account)
    assert len(q) == 1 and q[0]["mode"] == "quick" and "Automatic" in (q[0]["stats"].get("note") or "")
    runner._last_auto = 0
    runner._auto_refresh()
    assert len(_queued(hub, account)) == 1                 # never stacks jobs


def test_query_layer_is_read_only_and_account_scoped(hub, account, fixture_pkg):
    import pytest

    from canvas_hub.services.querylayer import evaluate, run_query
    from canvas_hub.util import InvalidInput
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    other = hub.identity.upsert_verified_account(account["instance_id"], {"canvas_user_id": "4242"})
    r = run_query(hub.paths.db, aid, "SELECT item_type, COUNT(*) n FROM items GROUP BY item_type ORDER BY item_type")
    assert r["columns"] == ["item_type", "n"] and dict(r["rows"])["assignment"] == 2
    r = run_query(hub.paths.db, aid, """SELECT i.title, json_extract(f.value_json, '$.iso') FROM items i JOIN fields f
                                        ON f.item_id=i.id WHERE f.field='due_at' AND i.item_type='assignment' ORDER BY 1""")
    assert r["rows"][0] == ["Project proposal", "2026-11-06T15:59:00Z"]
    assert run_query(hub.paths.db, other["id"], "SELECT COUNT(*) FROM items")["rows"] == [[0]]    # other account sees nothing
    for bad in ("SELECT * FROM main.accounts", "SELECT * FROM assistant_messages", "DELETE FROM items",
                "INSERT INTO notes(title) VALUES ('x')", "SELECT * FROM sqlite_master", "ATTACH DATABASE ':memory:' AS x",
                "PRAGMA table_info(items)", "SELECT load_extension('x')"):
        with pytest.raises(InvalidInput):
            run_query(hub.paths.db, aid, bad)
    with pytest.raises(InvalidInput):          # stopped by the time limit
        run_query(hub.paths.db, aid, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c")
    assert run_query(hub.paths.db, aid, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 7) "
                                         "SELECT SUM(x) FROM c")["rows"] == [[28]]
    assert run_query(hub.paths.db, aid, "SELECT COUNT(*) FROM main.items")["rows"] == run_query(
        hub.paths.db, aid, "SELECT COUNT(*) FROM items")["rows"]               # `main` is only this account's copy
    # a CTE or alias named like an allowed table still cannot reach the main database
    for bad in ("WITH items AS (SELECT * FROM main.accounts) SELECT * FROM items",
                "WITH notes AS (SELECT * FROM assistant_messages) SELECT * FROM notes",
                f"WITH x AS (SELECT * FROM main.items WHERE account_id={other['id']}) SELECT * FROM x",
                "SELECT * FROM (SELECT * FROM main.accounts) AS items"):
        with pytest.raises(InvalidInput):
            run_query(hub.paths.db, aid, bad)
    assert run_query(hub.paths.db, aid, "WITH a AS (SELECT * FROM items WHERE item_type='assignment') "
                                         "SELECT COUNT(*) FROM a")["rows"] == [[2]]             # CTEs over the tables work
    r = run_query(hub.paths.db, aid, "SELECT i.title, c.title FROM items i JOIN courses c ON c.course_id=i.course_id "
                                     "WHERE i.item_type='assignment' ORDER BY 1 LIMIT 1")
    assert r["columns"] == ["title", "title"] and r["rows"][0][0] != r["rows"][0][1]          # repeated names kept apart
    assert hub.db.one("SELECT COUNT(*) n FROM items WHERE account_id=?", (aid,))["n"] > 0          # untouched
    assert evaluate("min(20, hours / 20 * 20) if hours >= 10 else 0", {"hours": 15}) == 15
    assert evaluate("sum(xs) - 80", {"xs": [15, 10, 10, 10, 15, 20]}) == 0
    for bad in ("__import__('os')", "x.__class__", "open('f')", "2 ** 1000", "[i for i in xs]"):
        with pytest.raises(InvalidInput):
            evaluate(bad, {"xs": [1], "x": 1})
