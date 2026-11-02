"""Bounded stages; the durable job_tasks queue remains the source of truth.

Session storage is copied only in memory on the browser owner thread. Each
background request context is created, used and closed on its own thread.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from .client import CanvasClient, Cancelled
from ..util import IdentityMismatch


class BackgroundPipeline:
    def __init__(self, run):
        self.run = run
        self.stop_event = threading.Event()
        self.downloads = ThreadPoolExecutor(max_workers=2, thread_name_prefix="canvas-download")
        self.processing = ThreadPoolExecutor(max_workers=2, thread_name_prefix="canvas-document")
        self.verification = ThreadPoolExecutor(max_workers=1, thread_name_prefix="canvas-verification")
        self.futures = {}
        client = run.client
        # Never pass a live browser context into an executor.
        if isinstance(client, CanvasClient):
            user = client.api_get("/api/v1/users/self")
            if str(user.get("id")) != str(run.account["canvas_user_id"]):
                raise IdentityMismatch("background session belongs to a different Canvas account")
            self.storage = client.context.storage_state()
        else:
            self.storage = None
        self.factory = getattr(client, "background_client", None)
        if isinstance(client, CanvasClient):
            client.rate_gate.max_concurrent = 2 if run.scope.get("experimental_network_concurrency") else 1
        self.gate = getattr(client, "rate_gate", None)

    def stopped(self):
        return self.stop_event.is_set() or self.run.should_stop()

    def _download(self, task):
        artifact = task["payload"].get("artifact")
        if artifact:
            from ..services.resources import validate_resource_id
            sha = validate_resource_id(artifact["sha"])
            body = self.run.hub.resources._original_path(sha).read_bytes()
            self.run.hub.jobs.update_stats(self.run.job_id, download_checkpoint_cache_hits=1)
            return body, artifact["ctype"], artifact["meta"], {}
        # Tests supply an explicitly independent fake client factory.
        if self.factory:
            return self._download_measured(task, self.factory(self.stopped))
        if self.storage is None:
            raise RuntimeError("provider must supply an independent background_client factory")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = pw.request.new_context(storage_state=self.storage)
            try:
                client = CanvasClient(SimpleNamespace(request=context), self.run.origin,
                                      should_stop=self.stopped, rate_gate=self.gate)
                return self._download_measured(task, client)
            finally:
                context.dispose()

    def _download_measured(self, task, client):
        try:
            return self.run.download_file(task, client)
        finally:
            self.run.hub.jobs.update_stats(self.run.job_id, **{
                f"download_{k}": v for k, v in getattr(client, "metrics", lambda: {})().items()})

    def _extract(self, task):
        payload = task["payload"]
        cached = self.run.hub.db.one("SELECT extraction_status FROM resources WHERE id=?", (payload["sha"],))
        if cached and cached["extraction_status"] not in ("not_attempted", "failed", "tool_unavailable"):
            self.run.hub.jobs.update_stats(self.run.job_id, extraction_cache_hits=1)
        return self.run.hub.resources.extract_bounded(payload["sha"], payload["name"], should_stop=self.stopped)

    def pump(self):
        """Commit finished work on the owner thread, then admit at most two per stage."""
        for future, (task, started) in list(self.futures.items()):
            if not future.done():
                continue
            del self.futures[future]
            self.run.execute_task(task, lambda: self._commit(task, future.result()), started=started)
            if self.run.hub.jobs.get(self.run.job_id)["state"] != "running":
                self.stop_event.set()
                return
        active = {task["id"] for task, _ in self.futures.values()}
        for kind, pool, worker in (("download", self.downloads, self._download),
                                   ("process_document", self.processing, self._extract),
                                   ("supplemental", self.verification, lambda task: self.run._supplemental(self.stopped))):
            count = sum(t["kind"] == kind for t, _ in self.futures.values())
            for task in self.run.hub.jobs.pending_tasks(self.run.job_id, kinds=[kind], limit=4):
                if count >= (1 if kind == "supplemental" else 2) or self.stopped():
                    break
                if task["id"] in active:
                    continue
                if kind == "download" and self.run.hub.db.one(
                    "SELECT 1 FROM job_tasks WHERE job_id=? AND kind IN ('identity','courses','course','modules') AND state IN ('pending','running')",
                    (self.run.job_id,)):
                    continue
                # On restart the memory-only signed URL cache is empty. Revalidate
                # after core metadata unless the user explicitly opted into API overlap.
                if (kind == "download" and self.storage is not None
                    and not self.run._download_metadata.get(task["payload"]["id"], {}).get("url")
                    and not self.run.scope.get("experimental_network_concurrency")
                    and not self.run.hub.jobs.get(self.run.job_id)["stats"].get("core_status")):
                    continue
                if kind == "download" and self.run.hub.db.one(
                    "SELECT 1 FROM job_tasks WHERE job_id=? AND kind='process_document' AND json_extract(payload_json, '$.id')=?",
                    (self.run.job_id, task["payload"]["id"])):
                    self.run.hub.jobs.finish_task(task["id"], "done")
                    continue
                self.run.hub.jobs.mark_running(task["id"])
                started = time.monotonic()
                self.futures[pool.submit(worker, task)] = (task, started)
                count += 1

    def _commit(self, task, result):
        if self.stopped():
            raise Cancelled("stopped before background result was committed")
        if task["kind"] == "download":
            self.run.commit_download(task, result)
        elif task["kind"] == "process_document":
            self.run.commit_document(task, result)

    def close(self):
        self.stop_event.set()
        # Joining prevents shutdown from closing the database beneath a worker.
        self.downloads.shutdown(wait=True, cancel_futures=True)
        self.processing.shutdown(wait=True, cancel_futures=True)
        self.verification.shutdown(wait=True, cancel_futures=True)
        for task, _ in self.futures.values():
            self.run.hub.jobs.defer_task(task["id"], "background work stopped; resume to retry")
        self.futures.clear()
        self.storage = None
