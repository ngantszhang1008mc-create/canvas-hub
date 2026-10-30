"""Confirmed writes to Canvas: off by default, prepared with an exact preview,
sent only with the preview's hash, logged. The browser test sends real HTTP to
the synthetic Canvas (which accepts writes only when a test switches them on)."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from canvas_hub.agent_tools import TOOLS, ToolContext, run_tool_display
from canvas_hub.boundary import HubAPI
from canvas_hub.util import Conflict, InvalidInput, NotFound


def _ids(hub, account, canvas_id):
    return hub.db.one("SELECT id FROM items WHERE account_id=? AND canvas_id=? AND item_type='assignment'",
                      (account["id"], canvas_id))["id"]


def test_writes_are_off_until_enabled_and_need_the_preview_hash(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    w = hub.writes
    f = hub.files.save(aid, filename="proposal.md", content="# Proposal\n\nMy project.", created_by="user")
    asg = _ids(hub, account, "7001")
    with pytest.raises(InvalidInput) as e:
        w.prepare(aid, "submission_file", prepared_by="assistant", item_id=asg, file_id=f["id"])
    assert e.value.code == "writes_disabled"
    w.set_settings(aid, enabled=True, kinds=["submission_file"])
    with pytest.raises(InvalidInput) as e:          # kind not allowed
        w.prepare(aid, "submission_text", prepared_by="assistant", item_id=asg, text="hi")
    assert e.value.code == "write_kind_disabled"
    w.set_settings(aid, kinds=["submission_text", "submission_file", "discussion_reply", "message"])
    with pytest.raises(InvalidInput) as e:          # 7001 accepts uploads only
        w.prepare(aid, "submission_text", prepared_by="assistant", item_id=asg, text="hi")
    assert e.value.code == "submission_type_not_allowed"
    with pytest.raises(InvalidInput):               # 7002 accepts no online submission at all
        w.prepare(aid, "submission_file", prepared_by="assistant", item_id=_ids(hub, account, "7002"), file_id=f["id"])

    p = w.prepare(aid, "submission_file", prepared_by="assistant", item_id=asg, file_id=f["id"], text="Draft v1")
    pv = p["preview"]
    assert p["status"] == "prepared" and pv["file"]["filename"] == "proposal.md" and pv["file"]["sha256"] == f["sha256"]
    assert pv["target"] == "Project proposal" and pv["comment"] == "Draft v1" and pv["action"].startswith("Upload a file")
    sent = []
    with pytest.raises(Conflict):                   # wrong hash: nothing is sent
        w.confirm(aid, p["id"], "0" * 64, lambda job: sent.append(job))
    assert sent == []
    r = w.confirm(aid, p["id"], pv["hash"], lambda job: sent.append(job) or {"submission_id": "s1", "attempt": 1})
    assert r["status"] == "sent" and r["result"]["submission_id"] == "s1"
    job = sent[0]
    assert job["kind"] == "submission_file" and job["data"] == b"# Proposal\n\nMy project." and job["target_id"] == "7001"
    with pytest.raises(InvalidInput):               # once only
        w.confirm(aid, p["id"], pv["hash"], lambda job: sent.append(job))
    assert len(sent) == 1

    # a failure is recorded, not retried
    p2 = w.prepare(aid, "submission_file", prepared_by="user", item_id=asg, file_id=f["id"])
    r2 = w.confirm(aid, p2["id"], p2["preview"]["hash"], lambda job: (_ for _ in ()).throw(RuntimeError("HTTP 422")))
    assert r2["status"] == "failed" and "HTTP 422" in r2["error"]
    # cancel and expiry
    p3 = w.prepare(aid, "submission_file", prepared_by="user", item_id=asg, file_id=f["id"])
    assert w.cancel(aid, p3["id"])["status"] == "cancelled"
    p4 = w.prepare(aid, "submission_file", prepared_by="user", item_id=asg, file_id=f["id"])
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with hub.db.tx() as c:
        c.execute("UPDATE canvas_writes SET created_at=? WHERE id=?", (old, p4["id"]))
    assert w.get(aid, p4["id"])["status"] == "expired"
    # switching writes off again blocks confirmation of anything still open
    p5 = w.prepare(aid, "submission_file", prepared_by="user", item_id=asg, file_id=f["id"])
    w.set_settings(aid, enabled=False)
    with pytest.raises(InvalidInput):
        w.confirm(aid, p5["id"], p5["preview"]["hash"], lambda job: sent.append(job))
    assert len(sent) == 1
    assert [x["status"] for x in w.list(aid)] == ["prepared", "expired", "cancelled", "failed", "sent"]


def test_assistant_can_only_prepare(hub, account, fixture_pkg):
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    ctx = ToolContext(api=HubAPI(hub), account_id=aid)
    assert not any("confirm" in t.name or "send" in t.name for t in TOOLS)        # no tool can send
    assert {t.name for t in TOOLS if t.permission == "prepare"} == {"prepare_submission", "prepare_discussion_reply",
                                                                       "prepare_message"}
    hub.writes.set_settings(aid, enabled=True)
    saved = json.loads(run_tool_display(ctx, "save_file", {"filename": "plan.md", "content": "Plan"}, {"notes"})[0])["saved"]
    text, err, display = run_tool_display(ctx, "prepare_submission", {"item_id": _ids(hub, account, "7001"),
                                                                       "file_id": saved["id"]}, {"prepare"})
    out = json.loads(text)
    assert not err and display["component"] == "write_preview" and "NOTHING HAS BEEN SENT" in out["summary"]
    assert display["write"]["status"] == "prepared"
    listed = json.loads(run_tool_display(ctx, "list_canvas_writes", {}, {"read"})[0])
    assert listed["enabled"] and listed["writes"][0]["status"] == "prepared"
    with pytest.raises(NotFound):
        hub.writes.prepare(aid, "message", prepared_by="assistant", course_id="999", recipients=["1"], subject="s", text="b")


def test_notes_read_in_full_append_and_search(hub, account):
    ctx = ToolContext(api=HubAPI(hub), account_id=account["id"])
    long = "x" * 2500 + " TAIL-MARKER"
    n = hub.annotations.create(account["id"], kind="note", title="Spec #13", body=long)
    listed = json.loads(run_tool_display(ctx, "list_notes", {"query": "spec"}, {"read"})[0])
    assert listed["notes"][0]["truncated"] and listed["notes"][0]["body_chars"] == len(long)
    full = json.loads(run_tool_display(ctx, "get_note", {"note_id": n["id"]}, {"read"})[0])
    assert full["body"].endswith("TAIL-MARKER") and full["next_offset"] is None
    run_tool_display(ctx, "update_note", {"note_id": n["id"], "body": "Appendix A", "mode": "append"}, {"notes"})
    body = hub.annotations.get(account["id"], n["id"])["body"]
    assert body.startswith("xxx") and body.endswith("TAIL-MARKER\n\nAppendix A")
    assert json.loads(run_tool_display(ctx, "list_notes", {"query": "nothing-like-this"}, {"read"})[0])["notes"] == []


def test_file_download_is_always_an_attachment(hub, account):
    from fastapi.testclient import TestClient

    from canvas_hub.web.app import create_app
    c = TestClient(create_app(HubAPI(hub), token="t" * 40, port=8730), base_url="http://127.0.0.1:8730")
    hdr = {"Authorization": "Bearer " + "t" * 40, "X-Canvas-Hub": "1"}
    base = f"/api/accounts/{account['id']}/files"
    r = c.post(base, content=b"<script>alert(1)</script>", headers={**hdr, "Content-Type": "text/html", "X-Filename": "x.html"})
    fid = r.json()["id"]
    d = c.get(f"{base}/{fid}/download", headers=hdr)
    assert d.headers["content-type"] == "application/octet-stream" and d.headers["content-disposition"].startswith("attachment")
    assert d.headers["x-content-type-options"] == "nosniff" and d.content == b"<script>alert(1)</script>"
    # a .json file uploaded as it is, even one that looks like a save request
    for raw in (b'{"answer": 42}', b'{"filename": "evil.md", "content": "x"}'):
        r = c.post(base, content=raw, headers={**hdr, "Content-Type": "application/json", "X-Filename": "data.json"})
        assert r.status_code == 200, r.text
        assert r.json()["filename"] == "data.json" and c.get(f"{base}/{r.json()['id']}/download", headers=hdr).content == raw
    r = c.post(base, json={"filename": "plan.md", "content": "hello"}, headers=hdr)
    assert r.json()["size"] == 5 and sorted(f["filename"] for f in c.get(base, headers=hdr).json()["files"]) == [
        "data.json", "data.json", "plan.md", "x.html"]
    # writes need the user's settings switch, done in the app only
    assert c.put(f"/api/accounts/{account['id']}/write-settings", json={"enabled": True}, headers=hdr).json()["enabled"]


@pytest.mark.browser
def test_confirmed_writes_reach_canvas_through_the_browser(tmp_path):
    from .test_collector_e2e import Env
    env = Env(tmp_path)
    try:
        env.login()
        acct = env.hub.identity.list_accounts()[0]
        env.run(acct["id"])
        fc, hub = env.fc, env.hub
        api = HubAPI(hub, worker=env.worker)
        api.set_write_settings(acct["id"], {"enabled": True})
        item = lambda typ, cid: hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type=? AND canvas_id=?",
                                           (acct["id"], typ, cid))["id"]
        # text submission (assignment 3001 accepts online_text_entry)
        p = api.prepare_write(acct["id"], {"kind": "submission_text", "item_id": item("assignment", "3001"),
                                           "text": "My reflection.\n\nSecond <paragraph>."})
        assert fc.accepted_writes == []                         # preparing sends nothing
        fc.allow_writes = True
        r = api.confirm_write(acct["id"], p["id"], p["preview"]["hash"])
        assert r["status"] == "sent", r["error"]
        sub = fc.accepted_writes[-1]
        assert sub["kind"] == "online_text_entry" and sub["body"] == "<p>My reflection.</p><p>Second &lt;paragraph&gt;.</p>"
        # file submission: upload slot, upload, submit with the file id
        f = api.save_file(acct["id"], filename="ps2.txt", content="answers", created_by="user")
        p = api.prepare_write(acct["id"], {"kind": "submission_file", "item_id": item("assignment", "1002"), "file_id": f["id"]})
        r = api.confirm_write(acct["id"], p["id"], p["preview"]["hash"])
        assert r["status"] == "sent", r["error"]
        up, sub = fc.accepted_writes[-2], fc.accepted_writes[-1]
        assert up["kind"] == "upload" and up["bytes"] == b"answers" and sub["file_ids"] == [up["file_id"]]
        # discussion reply and inbox message
        disc = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type='discussion' LIMIT 1", (acct["id"],))["id"]
        p = api.prepare_write(acct["id"], {"kind": "discussion_reply", "item_id": disc, "text": "Agreed."})
        assert api.confirm_write(acct["id"], p["id"], p["preview"]["hash"])["status"] == "sent"
        assert fc.accepted_writes[-1]["kind"] == "reply" and fc.accepted_writes[-1]["message"] == "<p>Agreed.</p>"
        p = api.prepare_write(acct["id"], {"kind": "message", "course_id": "101", "recipients": ["9001"],
                                           "subject": "Regrade", "text": "Could you check Q2?"})
        assert api.confirm_write(acct["id"], p["id"], p["preview"]["hash"])["status"] == "sent"
        m = fc.accepted_writes[-1]
        assert m["kind"] == "message" and m["recipients"] == ["9001"] and m["context"] == "course_101"
        # a write Canvas refuses is recorded as failed (wrong CSRF -> 422)
        fc.csrf_tokens = {k: "other" for k in fc.csrf_tokens}
        p = api.prepare_write(acct["id"], {"kind": "discussion_reply", "item_id": disc, "text": "Again"})
        r = api.confirm_write(acct["id"], p["id"], p["preview"]["hash"])
        assert r["status"] == "failed" and "422" in r["error"]
        time.sleep(0.1)
    finally:
        env.close() if hasattr(env, "close") else None


class _Worker:
    """Stands in for the browser: reports who is signed in, and fails the test if anything is sent."""
    def __init__(self, *states):
        self.states = list(states)
        self.sent = False

    def call(self, fn, timeout=None):
        return fn()

    def submit(self, fn):
        from concurrent.futures import Future
        f = Future()
        try:
            f.set_result(fn())
        except Exception as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def check_session(self, instance_id, launch=True):
        return self.states.pop(0)

    def _context(self, instance_id):
        self.sent = True
        raise AssertionError("nothing may be sent as the wrong user")


def test_writes_are_only_sent_as_the_account_they_were_prepared_for(hub, account, fixture_pkg):
    from canvas_hub.util import Conflict
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    other = {"status": "signed_in", "account_id": aid + 99}
    api = HubAPI(hub, worker=_Worker(other))
    api.set_write_settings(aid, {"enabled": True})
    p = api.prepare_write(aid, {"kind": "message", "course_id": "4242", "recipients": ["9001"], "subject": "Q", "text": "Hi"})
    with pytest.raises(Conflict, match="different Canvas user"):
        api.confirm_write(aid, p["id"], p["preview"]["hash"])
    assert hub.writes.get(aid, p["id"])["status"] == "prepared" and not api.worker.sent   # still there to send later
    # someone signs in as another user between the check and the send
    api.worker = _Worker({"status": "signed_in", "account_id": aid}, other)
    r = api.confirm_write(aid, p["id"], p["preview"]["hash"])
    assert r["status"] == "failed" and "different Canvas user" in r["error"] and not api.worker.sent


def test_a_slow_send_stays_sending_until_the_browser_answers(hub, account, fixture_pkg, monkeypatch):
    # Reporting "failed" while the browser may still submit would invite a second, duplicate attempt.
    import threading
    from concurrent.futures import Future
    hub.importer.import_package(fixture_pkg)
    aid = account["id"]
    release, fut = threading.Event(), Future()

    class SlowWorker(_Worker):
        def submit(self, fn):
            def later():
                release.wait(5)
                fut.set_result({"id": 77})
            threading.Thread(target=later, daemon=True).start()
            return fut
    me = {"status": "signed_in", "account_id": aid}
    api = HubAPI(hub, worker=SlowWorker(me, me))
    api.set_write_settings(aid, {"enabled": True})
    monkeypatch.setattr(type(hub.writes), "SEND_WAIT", 0.2)
    p = api.prepare_write(aid, {"kind": "message", "course_id": "4242", "recipients": ["9001"], "subject": "Q", "text": "Hi"})
    r = api.confirm_write(aid, p["id"], p["preview"]["hash"])
    assert r["status"] == "sending"                                         # not "failed"
    with pytest.raises(Exception, match="cannot be sent"):
        api.confirm_write(aid, p["id"], p["preview"]["hash"])              # no second attempt meanwhile
    release.set()
    for _ in range(50):
        if hub.writes.get(aid, p["id"])["status"] != "sending":
            break
        time.sleep(0.05)
    assert hub.writes.get(aid, p["id"])["status"] == "sent"
