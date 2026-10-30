"""The application service boundary.

``HubAPI`` is the single, documented set of operations that every client
uses: the local web UI (through canvas_hub.web), the CLI, and - once Codex
implements it - an MCP adapter. Methods take plain JSON-compatible arguments,
validate them, and return JSON-compatible dicts. Errors are raised as
``canvas_hub.util.HubError`` subclasses with stable ``code`` values.

Deliberately NOT exposed here: SQL, filesystem paths chosen by the caller,
shell execution, browser control, cookies or credentials. Collection is
requested as a job; the app process that owns the browser runs it.

See CODEX_HANDOFF.md for schemas and examples.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import __version__
from .model import CATEGORIES, ITEM_TYPES
from .services import Hub
from .services import backup as backup_mod
from .util import Conflict, HubError, InvalidInput, NotFound

_BACKUP_NAME = re.compile(r"^canvas-hub-(backup|export)-[A-Za-z0-9\-]+\.zip$")


class HubAPI:
    def __init__(self, hub: Hub, runner=None, worker=None):
        self.hub = hub
        self.runner = runner      # JobRunner when this process can run collection
        self.worker = worker      # BrowserWorker when this process owns the browser

    # ---- helpers ----------------------------------------------------------
    def _acct(self, account_id) -> dict:
        return self.hub.identity.require_account(account_id)

    @staticmethod
    def _bool(v, default=False) -> bool:
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "on")

    # ---- identities ---------------------------------------------------------
    def app_info(self) -> dict:
        return {
            "app": "canvas-hub", "version": __version__,
            "collection_available": bool(self.runner and self.runner.has_lock),
            "item_types": ITEM_TYPES, "categories": CATEGORIES,
            "data_home": str(self.hub.paths.home),
        }

    def list_identities(self) -> dict:
        """Configured Canvas instances, their verified accounts and (when this
        process owns the browser) the current sign-in state."""
        out = []
        for inst in self.hub.identity.list_instances():
            entry = {k: inst[k] for k in ("id", "origin", "label", "entry_url", "created_at")}
            entry["accounts"] = [self._public_account(a) for a in inst["accounts"]]
            entry["session"] = self.worker.session(inst["id"]) if self.worker else {"status": "unknown",
                                                                                     "message": "browser not managed by this process"}
            out.append(entry)
        return {"instances": out}

    @staticmethod
    def _public_account(a: dict) -> dict:
        return {k: a.get(k) for k in ("id", "instance_id", "origin", "canvas_user_id", "display_name", "login_id",
                                       "time_zone", "time_zone_source", "locale", "first_verified_at",
                                       "last_verified_at", "scope")}

    def get_account(self, account_id) -> dict:
        return self._public_account(self._acct(account_id))

    def add_instance(self, address: str, label: str | None = None) -> dict:
        inst = self.hub.identity.add_instance(address, label)
        return {k: inst[k] for k in ("id", "origin", "label", "entry_url", "created_at")}

    def set_scope(self, account_id, scope: dict) -> dict:
        self._acct(account_id)
        if not isinstance(scope, dict):
            raise InvalidInput("scope must be an object")
        return self.hub.identity.set_scope(int(account_id), scope)

    # ---- reading ---------------------------------------------------------------
    def list_courses(self, account_id) -> dict:
        a = self._acct(account_id)
        return {"account_id": a["id"], "courses": self.hub.query.list_courses(a["id"])}

    def link_courses(self, account_id, course_id, data: dict) -> dict:
        a = self._acct(account_id)
        if set(data) - {"other_course_id", "relation"}:
            raise InvalidInput("give other_course_id and relation (same, different or none)")
        self.hub.query.link_courses(a["id"], course_id, str(data.get("other_course_id") or ""), data.get("relation") or "")
        return self.list_courses(account_id)

    def list_items(self, account_id, *, course_id=None, item_type=None, presence=None, limit=None, cursor=None) -> dict:
        a = self._acct(account_id)
        return self.hub.query.list_items(a["id"], course_id=course_id, item_type=item_type, presence=presence,
                                         limit=limit, cursor=cursor)

    def search(self, account_id, q: str, *, course_id=None, types=None, include_documents=True,
               observed_after=None, observed_before=None, limit=None, cursor=None) -> dict:
        a = self._acct(account_id)
        if isinstance(types, str):
            types = [t for t in types.split(",") if t]
        return self.hub.search.search(a["id"], q, course_id=course_id, types=types,
                                      include_documents=self._bool(include_documents, True),
                                      observed_after=observed_after, observed_before=observed_before,
                                      limit=limit, cursor=cursor)

    def read_item(self, account_id, item_id) -> dict:
        a = self._acct(account_id)
        try:
            iid = int(item_id)
        except (TypeError, ValueError) as e:
            raise InvalidInput("item_id must be an integer") from e
        return self.hub.query.get_item(a["id"], iid)

    def item_history(self, account_id, item_id) -> dict:
        a = self._acct(account_id)
        return self.hub.query.history(a["id"], int(item_id))

    def deadlines(self, account_id, *, start: str, end: str, time_zone=None, course_id=None,
                  include_calendar=True, limit=None, cursor=None) -> dict:
        a = self._acct(account_id)
        return self.hub.query.deadlines(a["id"], start=start, end=end, time_zone=time_zone, course_id=course_id,
                                        include_calendar=self._bool(include_calendar, True), limit=limit, cursor=cursor)

    def changes(self, account_id, *, cursor=None, since=None, include_baseline=False, include_first_observed=True,
                kinds=None, course_id=None, limit=None) -> dict:
        a = self._acct(account_id)
        if isinstance(kinds, str):
            kinds = [k for k in kinds.split(",") if k]
        return self.hub.query.changes(a["id"], cursor=cursor, since=since,
                                      include_baseline=self._bool(include_baseline),
                                      include_first_observed=self._bool(include_first_observed, True),
                                      kinds=kinds, course_id=course_id, limit=limit)

    def coverage(self, account_id, *, course_id=None) -> dict:
        a = self._acct(account_id)
        return self.hub.query.coverage(a["id"], course_id=course_id)

    def freshness(self, account_id) -> dict:
        a = self._acct(account_id)
        return self.hub.query.freshness(a["id"])

    def discrepancies(self, account_id) -> dict:
        a = self._acct(account_id)
        return {"discrepancies": self.hub.query.discrepancies(a["id"])}

    def describe_resource(self, account_id, resource_id: str) -> dict:
        a = self._acct(account_id)
        return self.hub.resources.describe(a["id"], resource_id)

    def read_resource_text(self, account_id, resource_id: str, *, offset=0, limit=20000) -> dict:
        a = self._acct(account_id)
        return self.hub.resources.read_text(a["id"], resource_id, int(offset or 0), int(limit or 20000))

    def resource_original(self, account_id, resource_id: str) -> tuple[Path, dict]:
        """For the local UI's download only (returns a server-side path)."""
        a = self._acct(account_id)
        return self.hub.resources.original_path(a["id"], resource_id)

    # ---- local annotations -----------------------------------------------------
    def list_annotations(self, account_id, **filters) -> dict:
        a = self._acct(account_id)
        return {"annotations": self.hub.annotations.list(a["id"], **filters)}

    def create_annotation(self, account_id, data: dict) -> dict:
        a = self._acct(account_id)
        allowed = {"kind", "body", "title", "item_id", "course_id", "local_due", "task_state"}
        bad = set(data) - allowed
        if bad:
            raise InvalidInput(f"unknown fields {sorted(bad)}")
        return self.hub.annotations.create(a["id"], **data)

    def update_annotation(self, account_id, annotation_id, data: dict) -> dict:
        a = self._acct(account_id)
        return self.hub.annotations.update(a["id"], int(annotation_id), **data)

    def delete_annotation(self, account_id, annotation_id) -> dict:
        a = self._acct(account_id)
        self.hub.annotations.delete(a["id"], int(annotation_id))
        return {"deleted": int(annotation_id)}

    # ---- planner (local) ---------------------------------------------------------
    def planner_week(self, account_id, *, week_of=None, suggest_study=False, study_hours=3.0) -> dict:
        a = self._acct(account_id)
        return self.hub.planner.week(a["id"], week_of=week_of, suggestions=self._bool(suggest_study),
                                     study_hours=float(study_hours or 3))

    def list_class_times(self, account_id) -> dict:
        return {"class_times": self.hub.planner.list_classes(self._acct(account_id)["id"])}

    def add_class_time(self, account_id, data: dict) -> dict:
        allowed = {"title", "weekday", "start", "end", "course_id", "location", "valid_from", "valid_until"}
        if set(data) - allowed:
            raise InvalidInput(f"unknown fields {sorted(set(data) - allowed)}")
        return self.hub.planner.add_class(self._acct(account_id)["id"], **data)

    def delete_class_time(self, account_id, class_id) -> dict:
        return self.hub.planner.delete_class(self._acct(account_id)["id"], int(class_id))

    def planner_ics(self, account_id, weeks=8) -> str:
        return self.hub.planner.ics(self._acct(account_id)["id"], int(weeks or 8))

    # ---- external-site requests ---------------------------------------------------
    # ---- confirmed writes to Canvas (off unless the user enables them) --------------------
    def write_settings(self, account_id) -> dict:
        return self.hub.writes.get_settings(self._acct(account_id)["id"])

    def set_write_settings(self, account_id, data: dict) -> dict:
        if set(data) - {"enabled", "kinds"}:
            raise InvalidInput(f"unknown fields {sorted(set(data) - {'enabled', 'kinds'})}")
        return self.hub.writes.set_settings(self._acct(account_id)["id"], enabled=data.get("enabled"), kinds=data.get("kinds"))

    def list_writes(self, account_id, status=None) -> dict:
        return {"writes": self.hub.writes.list(self._acct(account_id)["id"], status or None),
                "settings": self.hub.writes.get_settings(self._acct(account_id)["id"])}

    def get_write(self, account_id, write_id) -> dict:
        return self.hub.writes.get(self._acct(account_id)["id"], write_id)

    def prepare_write(self, account_id, data: dict, *, prepared_by: str = "user", conversation_id=None) -> dict:
        allowed = {"kind", "item_id", "text", "file_id", "course_id", "recipients", "subject"}
        if set(data) - allowed:
            raise InvalidInput(f"unknown fields {sorted(set(data) - allowed)}")
        kind = data.get("kind")
        return self.hub.writes.prepare(self._acct(account_id)["id"], kind, prepared_by=prepared_by,
                                       conversation_id=conversation_id, **{k: v for k, v in data.items() if k != "kind"})

    def cancel_write(self, account_id, write_id) -> dict:
        return self.hub.writes.cancel(self._acct(account_id)["id"], write_id)

    def confirm_write(self, account_id, write_id, preview_hash: str) -> dict:
        """Send a prepared write. Only the app's own UI calls this, after the user
        has read the preview; there is no assistant or MCP tool for it."""
        a = self._acct(account_id)
        if self.worker is None:
            raise HubError("the signed-in browser is not available in this process", code="collector_unavailable", status=503)
        inst = self.hub.identity.get_instance(a["instance_id"])

        def same_user(state: dict) -> None:
            # The browser session belongs to the Canvas instance, not to an account: whoever signed in last is the
            # one a write would be sent as. It must be the account the write was prepared for.
            if state.get("status") != "signed_in":
                raise HubError("Sign in to Canvas first; nothing was sent.", code="needs_login", status=409)
            if state.get("account_id") != a["id"]:
                raise Conflict(f"The browser is signed in as a different Canvas user. Sign in as "
                               f"{a.get('display_name') or a['canvas_user_id']} to send this; nothing was sent.",
                               code="wrong_account")

        # Checked before the write is marked as sending (so it stays prepared), and again right before the POST.
        same_user(self.worker.call(lambda: self.worker.check_session(a["instance_id"], launch=True), timeout=120))

        def send(job):
            from .collector.writer import CanvasWriter

            def run():
                same_user(self.worker.check_session(a["instance_id"], launch=True))
                return CanvasWriter(self.worker._context(a["instance_id"]), inst["origin"]).send(job)
            return self.worker.submit(run)          # a Future: confirm waits, or leaves it `sending` until it ends
        r = self.hub.writes.confirm(a["id"], write_id, preview_hash, send)
        if r["status"] == "sent" and self.runner is not None:
            try:      # read the course again so the submission or reply shows up in Canvas Hub
                self.runner.start_collection(a["id"], "quick", {"courses": [r["course_id"]]})
            except HubError:
                pass
        return r

    # ---- freshness: automatic refresh and the change bell -------------------------------------
    def refresh_settings(self, account_id) -> dict:
        a = self._acct(account_id)
        s = self.hub.settings.get(a["id"], "auto_refresh")
        last = self.hub.db.one("""SELECT MAX(finished_at) AS f FROM jobs WHERE account_id=? AND kind='collect'
                                  AND state IN ('completed','completed_with_gaps')""", (a["id"],))
        return {**s, "last_completed_at": last["f"] if last else None,
                "runs_here": bool(self.runner and self.runner.has_lock)}

    def set_refresh_settings(self, account_id, data: dict) -> dict:
        m = data.get("minutes")
        if set(data) - {"minutes"} or m not in (0, 30, 60, 120, 240):
            raise InvalidInput("minutes must be 0 (off), 30, 60, 120 or 240")
        self.hub.settings.set(self._acct(account_id)["id"], "auto_refresh", {"minutes": m})
        return self.refresh_settings(account_id)

    def notifications(self, account_id, limit=30) -> dict:
        return self.hub.notifications.feed(self._acct(account_id)["id"], int(limit or 30))

    def mark_notifications_seen(self, account_id, up_to=None) -> dict:
        return self.hub.notifications.mark_seen(self._acct(account_id)["id"], int(up_to) if up_to else None)

    # ---- dates found in course text, timetable import ------------------------------------------
    def date_candidates(self, account_id, *, scan: bool = False, status=None) -> dict:
        a = self._acct(account_id)
        if scan:
            return self.hub.datefinder.scan(a["id"])
        return self.hub.datefinder.list(a["id"], status or None)

    def confirm_date_candidate(self, account_id, candidate_id, data: dict) -> dict:
        if set(data) - {"label", "starts_at"}:
            raise InvalidInput("only label and starts_at can be adjusted")
        return self.hub.datefinder.confirm(self._acct(account_id)["id"], candidate_id, **data)

    def dismiss_date_candidate(self, account_id, candidate_id) -> dict:
        return self.hub.datefinder.dismiss(self._acct(account_id)["id"], candidate_id)

    def import_timetable(self, account_id, ics_text: str) -> dict:
        from .services.datefinder import parse_ics
        a = self._acct(account_id)
        skipped: list[dict] = []
        rows = parse_ics(ics_text, a.get("time_zone"), skipped=skipped)
        if not rows:
            if skipped:
                raise InvalidInput("no class times imported: only events that repeat every week are supported. "
                                   + " ".join(f"{s['title']}: {s['reason']}" for s in skipped[:3]),
                                   code="no_weekly_events", detail={"skipped": skipped})
            raise InvalidInput("no weekly repeating events found in that calendar file", code="no_weekly_events")
        have = {(c["title"], c["weekday"], c["start_time"]) for c in self.hub.planner.list_classes(a["id"])}
        added = 0
        for r in rows[:200]:
            if (r["title"], r["weekday"], r["start"]) in have:
                continue
            self.hub.planner.add_class(a["id"], title=r["title"], weekday=r["weekday"], start=r["start"], end=r["end"],
                                       location=r["location"], valid_from=r["valid_from"], valid_until=r["valid_until"])
            have.add((r["title"], r["weekday"], r["start"]))
            added += 1
        return {"found": len(rows), "added": added, "skipped": skipped, "class_times": self.hub.planner.list_classes(a["id"])}

    # ---- saved files ------------------------------------------------------------------------
    def list_files(self, account_id) -> dict:
        return {"files": self.hub.files.list(self._acct(account_id)["id"])}

    def save_file(self, account_id, *, filename: str, content, title=None, mime=None, course_id=None,
                  created_by: str = "user") -> dict:
        return self.hub.files.save(self._acct(account_id)["id"], filename=filename, content=content, title=title, mime=mime,
                                   course_id=course_id, created_by=created_by)

    def get_file(self, account_id, file_id, *, with_content: bool = False) -> dict:
        return self.hub.files.get(self._acct(account_id)["id"], file_id, with_content=with_content)

    def delete_file(self, account_id, file_id) -> dict:
        return self.hub.files.delete(self._acct(account_id)["id"], file_id)

    # ---- interactive components ----------------------------------------------------
    def list_components(self, account_id, pinned=None) -> dict:
        pin = None if pinned in (None, "") else self._bool(pinned)
        return {"components": self.hub.components.list(self._acct(account_id)["id"], pinned=pin)}

    def get_component(self, account_id, component_id) -> dict:
        return self.hub.components.get(self._acct(account_id)["id"], component_id)

    def update_component(self, account_id, component_id, data: dict) -> dict:
        aid = self._acct(account_id)["id"]
        allowed = {"state", "base", "expected_version", "pinned", "undo"}
        if set(data) - allowed:
            raise InvalidInput(f"unknown fields {sorted(set(data) - allowed)}")
        if data.get("undo"):
            return self.hub.components.undo(aid, component_id)
        out = None
        if "pinned" in data:
            out = self.hub.components.pin(aid, component_id, self._bool(data["pinned"]))
        if "state" in data:
            out = self.hub.components.set_state(aid, component_id, data["state"], base=data.get("base"),
                                                expected_version=data.get("expected_version"), by="user")
        return out or self.hub.components.get(aid, component_id)

    def delete_component(self, account_id, component_id) -> dict:
        return self.hub.components.delete(self._acct(account_id)["id"], component_id)

    def connected_agents(self) -> dict:
        row = self.hub.db.one("SELECT value FROM app_settings WHERE key='mcp_agents'")
        agents = json.loads(row["value"]) if row else {}
        return {"agents": sorted(({"name": k, **v, "can_fulfil": "fulfil" in v.get("permissions", [])}
                                  for k, v in agents.items()), key=lambda x: x["last_seen"], reverse=True)}

    def list_view_requests(self, account_id, status=None) -> dict:
        return {"requests": self.hub.requests.list(self._acct(account_id)["id"], status)}

    def create_view_request(self, account_id, data: dict) -> dict:
        allowed = {"url", "title", "note", "course_id"}
        if set(data) - allowed:
            raise InvalidInput(f"unknown fields {sorted(set(data) - allowed)}")
        return self.hub.requests.create(self._acct(account_id)["id"], requested_by="user", **data)

    def cancel_view_request(self, account_id, request_id) -> dict:
        return self.hub.requests.cancel(self._acct(account_id)["id"], int(request_id))

    # ---- observations -----------------------------------------------------------
    def validate_observations(self, package: Any) -> dict:
        pkg = self.hub.importer.validate(package)
        return {"valid": True, "package_id": pkg.package_id, "items": len(pkg.items), "coverage": len(pkg.coverage),
                "identity": {"origin": pkg.identity.origin, "canvas_user_id": pkg.identity.canvas_user_id}}

    def import_observations(self, package: Any, *, account_id=None, allow_new_account=False) -> dict:
        """Import a versioned observation package from a trusted collector.
        With account_id the package identity must match that account."""
        return self.hub.importer.import_package(package, expected_account_id=int(account_id) if account_id else None,
                                                allow_new_account=self._bool(allow_new_account))

    # ---- collection jobs ---------------------------------------------------------
    def request_collection(self, account_id, *, mode: str = "quick", scope: dict | None = None) -> dict:
        a = self._acct(account_id)
        if self.runner is not None:
            job = self.runner.start_collection(a["id"], mode, scope)
        else:
            from .services.identity import normalize_scope
            if mode not in ("baseline", "thorough", "quick"):
                raise InvalidInput("mode must be baseline, thorough or quick")
            job = self.hub.jobs.create(account_id=a["id"], instance_id=a["instance_id"], kind="collect", mode=mode,
                                       scope=normalize_scope(scope, a["scope"]) if scope else a["scope"])
            self.hub.jobs.update_stats(job["id"], note="Queued. It runs when the Canvas Hub app (which owns the browser "
                                                       "session) is running and signed in.")
            job = self.hub.jobs.get(job["id"])
        return job

    def list_jobs(self, account_id, *, limit=None, cursor=None) -> dict:
        a = self._acct(account_id)
        return self.hub.jobs.list(a["id"], limit=limit, cursor=cursor)

    def job_status(self, account_id, job_id, *, include_tasks=False) -> dict:
        a = self._acct(account_id)
        job = self.hub.jobs.get(int(job_id), a["id"])
        if self._bool(include_tasks):
            job["task_list"] = self.hub.jobs.tasks(int(job_id))
        job["coverage"] = [dict(r) for r in self.hub.db.query(
            "SELECT course_canvas_id, category, status, index_depth, body_depth, attachment_depth, reason FROM coverage WHERE job_id=? ORDER BY id",
            (int(job_id),))]
        return job

    def cancel_job(self, account_id, job_id) -> dict:
        a = self._acct(account_id)
        return self.hub.jobs.request_cancel(int(job_id), a["id"])

    def pause_job(self, account_id, job_id) -> dict:
        a = self._acct(account_id)
        job = self.hub.jobs.get(int(job_id), a["id"])
        if job["state"] not in ("running", "queued"):
            raise InvalidInput(f"job is {job['state']} and cannot be paused", code="job_not_pausable")
        self.hub.jobs.set_state(int(job_id), "paused", message="Paused. Resume to continue from the checkpoint.")
        return self.hub.jobs.get(int(job_id))

    def resume_job(self, account_id, job_id) -> dict:
        a = self._acct(account_id)
        return self.hub.jobs.resume(int(job_id), a["id"])

    # ---- data management ------------------------------------------------------------
    def create_backup(self) -> dict:
        p = backup_mod.create_backup(self.hub)
        return {"name": p.name, "size": p.stat().st_size}

    def list_backups(self) -> dict:
        out = []
        for p in sorted(self.hub.paths.backups.glob("canvas-hub-*.zip"), reverse=True):
            out.append({"name": p.name, "size": p.stat().st_size,
                        "kind": "backup" if "-backup-" in p.name else "export"})
        return {"directory": str(self.hub.paths.backups), "files": out}

    def _backup_path(self, name: str) -> Path:
        if not isinstance(name, str) or not _BACKUP_NAME.match(name):
            raise InvalidInput("unknown backup name", code="invalid_backup_name")
        p = self.hub.paths.backups / name
        if not p.is_file():
            raise NotFound("backup not found", code="backup_not_found")
        return p

    def verify_backup(self, name: str) -> dict:
        return backup_mod.verify_backup(self._backup_path(name))

    def restore_backup(self, name: str) -> dict:
        return backup_mod.restore_backup(self.hub, self._backup_path(name))

    def export_account(self, account_id) -> dict:
        a = self._acct(account_id)
        p = backup_mod.export_account(self.hub, a["id"])
        return {"name": p.name, "size": p.stat().st_size}

    def import_archive_bytes(self, data: bytes) -> dict:
        import tempfile
        import zipfile
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=self.hub.paths.runtime) as tf:
            tf.write(data)
            tmp = Path(tf.name)
        try:
            if not zipfile.is_zipfile(tmp):
                raise InvalidInput("upload is not a zip archive", code="invalid_archive")
            return backup_mod.import_export(self.hub, tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def delete_account_data(self, account_id, confirm: str) -> dict:
        a = self._acct(account_id)
        return self.hub.delete_account_data(a["id"], confirm=confirm)
