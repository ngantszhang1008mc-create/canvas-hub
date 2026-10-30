"""Application services shared by the UI, the CLI and (later) the Codex
adapter. ``Hub`` is the single entry point; see CODEX_HANDOFF.md."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..db import Database
from ..paths import AppPaths, get_paths
from ..util import InvalidInput, NotFound
from .annotations import AnnotationService
from .components import ComponentService
from .datefinder import DateFinder
from .files import SavedFileService
from .grading import GradingRules
from .identity import IdentityService
from .importer import Importer
from .jobs import JobService
from .notifications import NotificationService
from .planner import PlannerService
from .query import QueryService
from .reminders import ReminderService
from .requests import RequestService
from .resources import ResourceService
from .search import SearchService
from .settings import AccountSettings
from .writes import WriteService


class Hub:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or get_paths()
        self._open()

    def _open(self) -> None:
        self.db = Database(self.paths.db)
        self.identity = IdentityService(self.db)
        self.jobs = JobService(self.db)
        self.resources = ResourceService(self.db, self.paths)
        self.search = SearchService(self.db)
        self.query = QueryService(self.db)
        self.annotations = AnnotationService(self.db)
        self.importer = Importer(self.db, self.identity, self.jobs, self.search, self.resources)
        self.planner = PlannerService(self.db, self.query)
        self.requests = RequestService(self.db, self.identity, self.importer)
        self.components = ComponentService(self.db, self.annotations)
        self.settings = AccountSettings(self.db)
        self.files = SavedFileService(self.db)
        self.writes = WriteService(self.db, self.identity, self.query, self.files, self.settings)
        self.notifications = NotificationService(self.db, self.settings)
        self.datefinder = DateFinder(self.db, self.query, self.annotations, self.settings)
        self.reminders = ReminderService(self.db)
        self.grading = GradingRules(self.db)

    def close(self) -> None:
        self.db.close()

    def reopen(self) -> None:
        self.close()
        self._open()

    # ---- maintenance ------------------------------------------------------
    def rebuild_search(self, account_id: int | None = None) -> int:
        return self.search.rebuild(account_id, extracted_text_for=self.resources.extracted_text_for)

    def run_ocr(self, limit: int = 10) -> dict:
        """OCR stored photos and scanned PDFs (when switched on) and make the new text searchable."""
        if not self.resources.ocr_settings()["enabled"]:
            raise InvalidInput("OCR is off; switch it on first", code="ocr_off")
        done = []
        for sha in self.resources.ocr_pending_ids(max(1, min(int(limit), 50))):
            rows = self.db.query("""SELECT ir.item_id, ir.filename, i.account_id, i.title FROM item_resources ir
                                    JOIN items i ON i.id=ir.item_id WHERE ir.resource_id=?""", (sha,))
            r = self.resources.extract(sha, rows[0]["filename"] if rows else None, force=True)
            text = self.resources.extracted_text_for(sha)
            if text:
                with self.db.tx() as c:
                    for x in rows:
                        self.search.index_resource(c, x["account_id"], x["item_id"], sha, x["filename"] or x["title"] or "", text)
            done.append({"resource_id": sha, "filename": rows[0]["filename"] if rows else None,
                         "status": r["extraction_status"], "confidence": r["ocr_confidence"], "detail": r["extraction_detail"]})
        return {"processed": done, "pending": self.resources.ocr_pending()}

    def delete_account_data(self, account_id: int, *, confirm: str) -> dict:
        """Delete all stored course data and notes for one account. Requires
        confirm == 'DELETE'. Browser sessions are untouched (separate action)."""
        if confirm != "DELETE":
            raise InvalidInput("confirmation text must be DELETE", code="confirmation_required")
        acct = self.identity.get_account(account_id)
        if self.db.one("SELECT 1 FROM jobs WHERE account_id=? AND state IN ('running','cancel_requested')", (account_id,)):
            raise InvalidInput("a collection job is running for this account", code="job_running")
        with self.db.tx() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM search_docs WHERE account_id=?", (account_id,))]
            for rid in ids:
                c.execute("DELETE FROM search_fts WHERE rowid=?", (rid,))
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        removed = self.gc_resources()
        return {"deleted_account": {"origin": acct["origin"], "canvas_user_id": acct["canvas_user_id"]},
                "resources_removed": removed}

    def gc_resources(self) -> int:
        n = 0
        rows = self.db.query("SELECT id, stored_path, extracted_path FROM resources WHERE id NOT IN (SELECT resource_id FROM item_resources)")
        with self.db.tx() as c:
            for r in rows:
                for rel in (r["stored_path"], r["extracted_path"]):
                    if rel:
                        p = self.paths.resources / rel
                        if p.exists():
                            p.unlink()
                c.execute("DELETE FROM resources WHERE id=?", (r["id"],))
                n += 1
        return n

    def clear_browser_session(self, instance_id: int) -> bool:
        """Delete the app-managed browser profile for an instance (saved
        login). Course data is untouched."""
        self.identity.get_instance(instance_id)
        p = self.paths.profile_dir(instance_id)
        if p.exists():
            shutil.rmtree(p)
            return True
        return False

    def account_for(self, account_id) -> dict:
        try:
            return self.identity.require_account(account_id)
        except NotFound:
            raise

    @staticmethod
    def open(home: str | Path | None = None) -> "Hub":
        return Hub(get_paths(Path(home) if home else None))
