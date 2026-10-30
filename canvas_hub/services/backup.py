"""Backup/restore and portable export/import.

* Backup: a zip with a consistent SQLite snapshot (sqlite3 backup API), all
  resources (originals + extracted text) and a manifest with checksums.
  Restore validates the archive and the database integrity before swapping
  it in; the previous data directory is kept aside, not deleted.
* Portable export: per-account zip with an observation package
  (canvas-hub.observations v1) of the current merged state, the referenced
  originals, and local annotations. It can be imported into another Canvas
  Hub installation.

Neither format ever contains browser profiles, cookies or the local API token:
those live outside the data directory and are not read here.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .. import OBSERVATION_FORMAT, OBSERVATION_FORMAT_VERSION, __version__
from ..db import schema_version
from ..util import InvalidInput, now_iso, sha256_bytes

BACKUP_FORMAT = "canvas-hub.backup"
EXPORT_FORMAT = "canvas-hub.export"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_backup(hub, dest: Path | None = None) -> Path:
    dest = Path(dest) if dest else hub.paths.backups / f"canvas-hub-backup-{_stamp()}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "canvas-hub.sqlite3"
        dst = sqlite3.connect(snap)
        hub.db.conn.backup(dst)
        dst.close()
        db_bytes = snap.read_bytes()
        manifest = {
            "format": BACKUP_FORMAT, "format_version": 1, "app_version": __version__,
            "schema_version": schema_version(hub.db.conn), "created_at": now_iso(),
            "database_sha256": sha256_bytes(db_bytes),
            "excluded": ["browser profiles and sessions", "local API token"],
            "resources": [],
        }
        tmp = dest.with_suffix(".partial")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("database/canvas-hub.sqlite3", db_bytes)
            for p in sorted(hub.paths.resources.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(hub.paths.resources).as_posix()
                    z.write(p, "resources/" + rel)
                    manifest["resources"].append(rel)
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        tmp.replace(dest)
    return dest


def _safe_members(z: zipfile.ZipFile, allowed_prefixes: tuple[str, ...]) -> list[zipfile.ZipInfo]:
    out = []
    for info in z.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or not name.startswith(allowed_prefixes) and name != "manifest.json":
            raise InvalidInput(f"archive contains a disallowed path: {name}", code="invalid_archive")
    for info in z.infolist():
        if not info.filename.endswith("/"):
            out.append(info)
    return out


def verify_backup(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        _safe_members(z, ("database/", "resources/"))
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError as e:
            raise InvalidInput("not a Canvas Hub backup (manifest missing)", code="invalid_archive") from e
        if manifest.get("format") != BACKUP_FORMAT:
            raise InvalidInput("not a Canvas Hub backup", code="invalid_archive")
        db_bytes = z.read("database/canvas-hub.sqlite3")
        if sha256_bytes(db_bytes) != manifest.get("database_sha256"):
            raise InvalidInput("backup database checksum mismatch", code="invalid_archive")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "check.sqlite3"
            p.write_bytes(db_bytes)
            c = sqlite3.connect(p)
            try:
                ok = c.execute("PRAGMA integrity_check").fetchone()[0]
                counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("instances", "accounts", "items", "annotations", "change_events")}
            finally:
                c.close()
        if ok != "ok":
            raise InvalidInput(f"backup database failed integrity check: {ok}", code="invalid_archive")
        names = set(z.namelist())
        missing = [r for r in manifest.get("resources", []) if "resources/" + r not in names]
        if missing:
            raise InvalidInput(f"backup is missing {len(missing)} resources", code="invalid_archive")
        return {"manifest": {k: v for k, v in manifest.items() if k != "resources"},
                "resource_files": len(manifest.get("resources", [])), "counts": counts}


def restore_backup(hub, path: Path) -> dict:
    """Replace the data directory with the backup's contents. The previous
    data directory is moved aside (data.before-restore-<stamp>)."""
    info = verify_backup(path)
    if hub.db.one("SELECT 1 FROM jobs WHERE state IN ('running','cancel_requested')"):
        raise InvalidInput("stop running collection jobs before restoring", code="job_running")
    staging = hub.paths.home / f"data.restoring-{_stamp()}"
    staging.mkdir(parents=True)
    with zipfile.ZipFile(path) as z:
        for m in _safe_members(z, ("database/", "resources/")):
            if m.filename == "manifest.json":
                continue
            if m.filename.startswith("database/"):
                target = staging / "canvas-hub.sqlite3"
            else:
                target = staging / m.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    hub.close()
    aside = hub.paths.home / f"data.before-restore-{_stamp()}"
    if hub.paths.data.exists():
        hub.paths.data.rename(aside)
    staging.rename(hub.paths.data)
    hub.paths.ensure()
    hub.reopen()
    return {**info, "previous_data_moved_to": str(aside)}


# ---- portable export ------------------------------------------------------

def export_account(hub, account_id: int, dest: Path | None = None) -> Path:
    acct = hub.identity.get_account(account_id)
    dest = Path(dest) if dest else hub.paths.backups / f"canvas-hub-export-account{account_id}-{_stamp()}.zip"
    items_out, shas = [], {}
    rows = hub.db.query("SELECT * FROM items WHERE account_id=? AND current_revision>0 ORDER BY id", (account_id,))
    for it in rows:
        fields = {r["field"]: json.loads(r["value_json"]) for r in
                  hub.db.query("SELECT field, value_json FROM item_fields WHERE item_id=?", (it["id"],))}
        links = []
        for ln in hub.db.query(
            """SELECT l.relation, l.label, l.position, l.attrs_json, t.item_type, t.course_canvas_id, t.canvas_id,
               t.source_key, t.source_url, t.title FROM item_links l JOIN items t ON t.id=l.to_item_id WHERE l.from_item_id=?""",
            (it["id"],),
        ):
            links.append({"relation": ln["relation"], "label": ln["label"], "position": ln["position"],
                          "attrs": json.loads(ln["attrs_json"]),
                          "to": {"type": ln["item_type"], "course_id": ln["course_canvas_id"], "canvas_id": ln["canvas_id"],
                                 "source_key": ln["source_key"], "url": ln["source_url"], "title": ln["title"]}})
        res = []
        for r in hub.db.query("SELECT resource_id, filename FROM item_resources WHERE item_id=? ORDER BY observed_at DESC LIMIT 1",
                              (it["id"],)):
            res.append({"sha256": r["resource_id"], "filename": r["filename"]})
            shas[r["resource_id"]] = True
        items_out.append({
            "type": it["item_type"], "course_id": it["course_canvas_id"], "canvas_id": it["canvas_id"],
            "source_key": it["source_key"], "url": it["source_url"], "title": it["title"],
            "observed_at": it["last_observed_at"], "observation_id": f"export:{it['item_type']}:{it['source_key']}:{it['last_observed_at']}",
            "method": "canvas_hub_export", "depth": "detail", "fields": fields, "links": links, "resources": res,
        })
    package = {
        "format": OBSERVATION_FORMAT, "format_version": OBSERVATION_FORMAT_VERSION,
        "package_id": f"export-{acct['canvas_user_id']}-{_stamp()}", "generated_at": now_iso(),
        "collector": {"name": "canvas-hub-export", "version": __version__, "method": "export_of_current_state"},
        "identity": {k: acct.get(k) for k in ("origin", "canvas_user_id", "display_name", "login_id", "primary_email",
                                               "time_zone", "time_zone_source", "locale")},
        "run": {"mode": "import", "scope": acct["scope"]},
        "items": items_out, "coverage": [], "presence_checks": [],
    }
    annotations = [dict(r) for r in hub.db.query("SELECT * FROM annotations WHERE account_id=?", (account_id,))]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("observations.json", json.dumps(package, ensure_ascii=False))
        z.writestr("annotations.json", json.dumps(annotations, ensure_ascii=False))
        for sha in shas:
            row = hub.db.one("SELECT stored_path FROM resources WHERE id=?", (sha,))
            if row:
                z.write(hub.paths.resources / row["stored_path"], f"resources/{sha}")
        z.writestr("manifest.json", json.dumps({
            "format": EXPORT_FORMAT, "format_version": 1, "app_version": __version__, "created_at": now_iso(),
            "account": {"origin": acct["origin"], "canvas_user_id": acct["canvas_user_id"]},
            "items": len(items_out), "resources": len(shas), "annotations": len(annotations),
            "note": "History (revisions/events) is not included; use a backup for full history. No browser data included.",
        }, indent=2))
    return dest


def import_export(hub, path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        _safe_members(z, ("resources/", "observations.json", "annotations.json"))
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != EXPORT_FORMAT:
            raise InvalidInput("not a Canvas Hub export", code="invalid_archive")
        package = json.loads(z.read("observations.json"))
        annotations = json.loads(z.read("annotations.json"))
        with tempfile.TemporaryDirectory() as td:
            rdir = Path(td) / "resources"
            rdir.mkdir()
            for m in z.infolist():
                if m.filename.startswith("resources/") and not m.filename.endswith("/"):
                    (rdir / Path(m.filename).name).write_bytes(z.read(m))
            result = hub.importer.import_package(package, allow_new_account=True, resource_dir=rdir)
    account_id = result["account_id"]
    added = 0
    for a in annotations:
        item_id = None
        if a.get("item_type") and a.get("item_source_key"):
            row = hub.db.one("SELECT id FROM items WHERE account_id=? AND item_type=? AND source_key=?",
                             (account_id, a["item_type"], a["item_source_key"]))
            item_id = row["id"] if row else None
        dup = hub.db.one("SELECT 1 FROM annotations WHERE account_id=? AND created_at=? AND body=? AND kind=?",
                         (account_id, a["created_at"], a["body"], a["kind"]))
        if dup:
            continue
        with hub.db.tx() as c:
            c.execute(
                """INSERT INTO annotations(account_id, item_id, item_type, item_source_key, course_canvas_id, kind, title,
                   body, task_state, local_due, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, item_id, a.get("item_type"), a.get("item_source_key"), a.get("course_canvas_id"), a["kind"],
                 a.get("title"), a.get("body") or "", a.get("task_state"), a.get("local_due"), a["created_at"], a["updated_at"]),
            )
        added += 1
    return {**result, "annotations_added": added}
