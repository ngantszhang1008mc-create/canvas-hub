"""Document extraction, backup/restore/export, and the local HTTP boundary."""

import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from canvas_hub.boundary import HubAPI
from canvas_hub.services import backup
from canvas_hub.testing.fake_canvas import make_pdf, make_png
from canvas_hub.util import InvalidInput
from canvas_hub.web.app import create_app

from .conftest import variant


def test_extraction_statuses(hub):
    cases = {
        "ok.pdf": make_pdf(["Spectral theorem", "Eigenvalues are real for symmetric matrices"]),
        "scan.pdf": make_pdf([]),
        "img.png": make_png(),
        "talk.mp4": b"\x00\x00\x00\x18ftypmp42" + b"\0" * 100,
        "notes.txt": b"plain notes",
        "bundle.zip": b"PK\x05\x06" + b"\0" * 18,
        "weird.xyz": b"???",
    }
    got = {}
    for name, data in cases.items():
        sha = hub.resources.store_bytes(data)
        got[name] = hub.resources.extract(sha, name)["extraction_status"]
    assert got == {"ok.pdf": "ok", "scan.pdf": "needs_ocr", "img.png": "image_only", "talk.mp4": "unsupported_media",
                   "notes.txt": "ok", "bundle.zip": "unsupported_type", "weird.xyz": "unsupported_type"}
    sha = hub.resources.store_bytes(cases["ok.pdf"])
    assert "Spectral theorem" in hub.resources.extracted_text_for(sha)
    stored = hub.db.one("SELECT stored_path, extracted_path FROM resources WHERE id=?", (sha,))
    assert "\\" not in stored["stored_path"]
    assert "/" in stored["extracted_path"] and "\\" not in stored["extracted_path"]


def _seed(hub, fixture_pkg):
    inst = hub.identity.add_instance(fixture_pkg["identity"]["origin"])
    acct = hub.identity.upsert_verified_account(inst["id"], {"canvas_user_id": "100001", "time_zone": "Asia/Hong_Kong"})
    sha = hub.resources.store_bytes(make_pdf(["Rubric for the project"]))
    hub.resources.extract(sha, "rubric.pdf")

    def attach(p):
        p["items"].append({"type": "file", "course_id": "4242", "canvas_id": "8001", "title": "rubric.pdf",
                           "observed_at": p["items"][0]["observed_at"], "fields": {"title": "rubric.pdf"},
                           "resources": [{"sha256": sha, "filename": "rubric.pdf"}]})
    hub.importer.import_package(variant(fixture_pkg, "seed", "2026-10-30T02:00:00Z", attach))
    item = hub.query.list_items(acct["id"], item_type="assignment")["items"][0]
    hub.annotations.create(acct["id"], kind="note", item_id=item["id"], body="ask about scope")
    # a browser profile with a "cookie" that must never be exported
    prof = hub.paths.profile_dir(inst["id"])
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "Cookies").write_text("SECRET-SESSION-COOKIE")
    return acct


def test_backup_verify_restore_roundtrip(hub, fixture_pkg):
    acct = _seed(hub, fixture_pkg)
    p = backup.create_backup(hub)
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        assert not any("Cookies" in n or "browser-profiles" in n or "api-token" in n for n in names)
        assert all(b"SECRET-SESSION-COOKIE" not in z.read(n) for n in names)
    info = backup.verify_backup(p)
    assert info["counts"]["items"] > 0 and info["counts"]["annotations"] == 1
    # wreck local data, then restore
    hub.delete_account_data(acct["id"], confirm="DELETE")
    assert hub.identity.list_accounts() == []
    res = backup.restore_backup(hub, p)
    assert res["previous_data_moved_to"]
    acct2 = hub.identity.list_accounts()[0]
    assert hub.annotations.list(acct2["id"])[0]["body"] == "ask about scope"
    assert hub.search.search(acct2["id"], "rubric")["results"]
    # browser session untouched by data operations
    assert (hub.paths.profile_dir(acct2["instance_id"]) / "Cookies").exists()


def test_backup_rejects_tampering(hub, fixture_pkg, tmp_path):
    _seed(hub, fixture_pkg)
    p = backup.create_backup(hub)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(bad, "w") as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n.startswith("database/"):
                data = data[:-10] + b"0123456789"
            zout.writestr(n, data)
    with pytest.raises(InvalidInput):
        backup.verify_backup(bad)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("../escape.txt", "x")
    with pytest.raises(InvalidInput):
        backup.verify_backup(evil)


def test_portable_export_import_into_fresh_install(hub, fixture_pkg, tmp_path):
    from canvas_hub.paths import get_paths
    from canvas_hub.services import Hub
    acct = _seed(hub, fixture_pkg)
    p = backup.export_account(hub, acct["id"])
    with zipfile.ZipFile(p) as z:
        assert all(b"SECRET-SESSION-COOKIE" not in z.read(n) for n in z.namelist())
    other = Hub(get_paths(tmp_path / "other"))
    try:
        r = backup.import_export(other, p)
        assert r["annotations_added"] == 1
        a2 = other.identity.list_accounts()[0]
        assert a2["canvas_user_id"] == "100001"
        assert other.search.search(a2["id"], "rubric")["results"]
        assert other.annotations.list(a2["id"])[0]["item_id"] is not None
        again = backup.import_export(other, p)
        assert again["status"] == "already_imported" and again["annotations_added"] == 0
    finally:
        other.close()


@pytest.fixture
def client(hub, fixture_pkg):
    acct = _seed(hub, fixture_pkg)
    app = create_app(HubAPI(hub), token="t" * 40, port=8730)
    c = TestClient(app, base_url="http://127.0.0.1:8730")
    c.acct = acct
    return c


def test_web_requires_token_and_loopback_host(client):
    assert client.get("/api/identities").status_code == 401
    assert client.get("/api/identities", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/api/identities", headers={"Authorization": "Bearer " + "t" * 40})
    assert ok.status_code == 200 and "default-src 'self'" in ok.headers["content-security-policy"]
    assert client.get("/api/identities", headers={"Authorization": "Bearer " + "t" * 40, "Host": "evil.example"}).status_code == 403
    # launch link sets the cookie, then the UI works without the token in the URL
    r = client.get("/?token=" + "t" * 40, follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    client.cookies.set("canvas_hub_token", "t" * 40)
    assert client.get("/api/identities").status_code == 200


def test_web_mutations_need_header_and_same_origin(client):
    client.cookies.set("canvas_hub_token", "t" * 40)
    aid = client.acct["id"]
    body = {"kind": "note", "body": "hi"}
    assert client.post(f"/api/accounts/{aid}/annotations", json=body).status_code == 403
    assert client.post(f"/api/accounts/{aid}/annotations", json=body,
                       headers={"X-Canvas-Hub": "1", "Origin": "https://evil.example"}).status_code == 403
    r = client.post(f"/api/accounts/{aid}/annotations", json=body,
                    headers={"X-Canvas-Hub": "1", "Origin": "http://127.0.0.1:8730"})
    assert r.status_code == 200 and r.json()["body"] == "hi"


def test_web_reads_and_errors(client):
    client.cookies.set("canvas_hub_token", "t" * 40)
    aid = client.acct["id"]
    assert client.get(f"/api/accounts/{aid}/courses").json()["courses"][0]["course_id"] == "4242"
    s = client.get(f"/api/accounts/{aid}/search", params={"q": "rubric"}).json()
    rid = next(r["resource_id"] for r in s["results"] if r["resource_id"])
    orig = client.get(f"/api/accounts/{aid}/resources/{rid}/original")
    assert orig.status_code == 200 and orig.headers["content-type"] == "application/octet-stream"
    assert "attachment" in orig.headers["content-disposition"]
    assert client.get(f"/api/accounts/{aid}/resources/..%2F..%2Fx/text").status_code in (400, 404)
    nf = client.get(f"/api/accounts/{aid}/items/999999")
    assert nf.status_code == 404 and nf.json()["error"] == "item_not_found"
    bad = client.get(f"/api/accounts/{aid}/deadlines", params={"start": "x", "end": "y"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_input"
    assert client.get("/api/backups/..%2Fsecret/download").status_code in (400, 404)
    # import through HTTP with identity enforcement
    pkg = json.loads(json.dumps(client.get(f"/api/accounts/{aid}").json()))
    assert pkg["canvas_user_id"] == "100001"
    # collection request without a runner queues a job that the app process will run
    j = client.post(f"/api/accounts/{aid}/jobs", json={"mode": "quick"}, headers={"X-Canvas-Hub": "1"}).json()
    assert j["state"] == "queued" and "owns the browser" in j["stats"]["note"]
    dup = client.post(f"/api/accounts/{aid}/jobs", json={"mode": "quick"}, headers={"X-Canvas-Hub": "1"})
    assert dup.status_code == 409 and dup.json()["error"] == "job_already_active"
    c = client.post(f"/api/accounts/{aid}/jobs/{j['id']}/cancel", headers={"X-Canvas-Hub": "1"}).json()
    assert c["state"] == "cancelled"
