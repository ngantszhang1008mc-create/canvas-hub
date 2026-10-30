"""Local web application (FastAPI): JSON API over HubAPI + static UI."""

from __future__ import annotations

import json
import logging
import mimetypes
from importlib import resources as importlib_resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ..boundary import HubAPI
from ..util import HubError, InvalidInput
from .security import COOKIE, CSP, allowed_hosts, token_ok

log = logging.getLogger("canvas_hub.web")
mimetypes.add_type("font/woff2", ".woff2")
STATIC = Path(str(importlib_resources.files("canvas_hub") / "web" / "static"))


def mcp_command() -> list[str]:
    """The command an MCP client should run to reach this installation."""
    import shutil
    import sys
    exe = Path(sys.executable).parent / "canvas-hub"
    cmd = str(exe) if exe.exists() else (shutil.which("canvas-hub") or "canvas-hub")
    return [cmd, "mcp"]


def create_app(api: HubAPI, *, token: str, port: int) -> FastAPI:
    app = FastAPI(title="Canvas Hub", docs_url=None, redoc_url=None, openapi_url=None)
    hosts = allowed_hosts(port)
    own_origins = {f"http://{h}" for h in hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in hosts:
            return JSONResponse({"error": "forbidden_host", "message": "Canvas Hub only answers on 127.0.0.1"}, 403)
        path = request.url.path
        if path.startswith("/api/"):
            presented = request.cookies.get(COOKIE)
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                presented = auth[7:].strip()
            if not token_ok(token, presented):
                return JSONResponse({"error": "unauthorized",
                                     "message": "Open Canvas Hub through the link printed by `canvas-hub serve`."}, 401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                if origin and origin not in own_origins:
                    return JSONResponse({"error": "forbidden_origin", "message": "cross-origin request refused"}, 403)
                if request.headers.get("x-canvas-hub") != "1":
                    return JSONResponse({"error": "missing_header", "message": "X-Canvas-Hub header required"}, 403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(HubError)
    async def hub_error(_request: Request, exc: HubError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(Exception)
    async def any_error(_request: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse({"error": "internal_error", "message": f"{type(exc).__name__}: {exc}"}, status_code=500)

    async def body(request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise InvalidInput("request body must be JSON") from e
        if not isinstance(data, dict):
            raise InvalidInput("request body must be a JSON object")
        return data

    # ---- launch / static ------------------------------------------------------
    @app.get("/")
    async def index(request: Request):
        t = request.query_params.get("token")
        if t:
            if not token_ok(token, t):
                return JSONResponse({"error": "unauthorized"}, 401)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
            return resp
        if not token_ok(token, request.cookies.get(COOKIE)):
            return Response("<!doctype html><title>Canvas Hub</title><p>Open Canvas Hub with the link printed by "
                            "<code>canvas-hub serve</code> (it contains a one-time local access token).</p>",
                            media_type="text/html", status_code=401)
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    async def static(name: str):
        if "/" in name or name.startswith(".") or not (STATIC / name).is_file():
            return JSONResponse({"error": "not_found"}, 404)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return FileResponse(STATIC / name, media_type=ctype)

    # ---- app / identities ----------------------------------------------------------
    @app.get("/api/info")
    def info():
        d = api.app_info()
        d["mcp_command"] = mcp_command()
        d["runner"] = api.runner.status() if api.runner else None
        return d

    @app.get("/api/identities")
    def identities():
        return api.list_identities()

    @app.post("/api/instances")
    async def add_instance(request: Request):
        b = await body(request)
        return api.add_instance(b.get("address"), b.get("label"))

    def _worker():
        if api.worker is None:
            raise HubError("this process does not manage a browser", code="browser_unavailable", status=503)
        return api.worker

    @app.post("/api/instances/{iid}/login")
    def open_login(iid: int):
        w = _worker()
        api.hub.identity.get_instance(iid)
        if w.busy_with:
            raise HubError(f"the browser is busy ({w.busy_with}); try again when it finishes or pause the job",
                           code="browser_busy", status=409)
        return w.call(w.open_login, iid, timeout=90)

    @app.get("/api/instances/{iid}/session")
    def session(iid: int, check: bool = True, launch: bool = False):
        """check=false: cached state. launch=true: may start a headless browser
        from the saved profile to verify (never opens a visible window)."""
        w = _worker()
        api.hub.identity.get_instance(iid)
        if not check or w.busy_with:
            return w.session(iid)
        return w.call(lambda: w.check_session(iid, launch=launch), timeout=60)

    @app.post("/api/instances/{iid}/logout")
    async def logout(iid: int, request: Request):
        b = await body(request)
        w = _worker()
        if w.busy_with:
            raise HubError("a collection job is using the browser; pause or cancel it first", code="browser_busy", status=409)
        return w.call(w.sign_out, iid, timeout=60) if not b.get("clear_profile") else \
            w.call(lambda: w.sign_out(iid, clear_profile=True), timeout=60)

    @app.post("/api/instances/{iid}/close-browser")
    def close_browser(iid: int):
        w = _worker()
        if w.busy_with:
            raise HubError("a collection job is using the browser", code="browser_busy", status=409)
        return w.call(w.close_browser, iid, timeout=30)

    # ---- account-scoped reads -------------------------------------------------------
    @app.get("/api/accounts/{aid}")
    def account(aid: int):
        return api.get_account(aid)

    @app.put("/api/accounts/{aid}/scope")
    async def scope(aid: int, request: Request):
        return api.set_scope(aid, await body(request))

    @app.get("/api/accounts/{aid}/courses")
    def courses(aid: int):
        return api.list_courses(aid)

    @app.get("/api/accounts/{aid}/items")
    def items(aid: int, course_id: str | None = None, type: str | None = None, presence: str | None = None,
              limit: int | None = None, cursor: str | None = None):
        return api.list_items(aid, course_id=course_id, item_type=type, presence=presence, limit=limit, cursor=cursor)

    @app.get("/api/accounts/{aid}/items/{iid}")
    def item(aid: int, iid: int):
        return api.read_item(aid, iid)

    @app.get("/api/accounts/{aid}/items/{iid}/history")
    def history(aid: int, iid: int):
        return api.item_history(aid, iid)

    @app.get("/api/accounts/{aid}/search")
    def search(aid: int, q: str, course_id: str | None = None, types: str | None = None,
               include_documents: bool = True, limit: int | None = None, cursor: str | None = None):
        return api.search(aid, q, course_id=course_id, types=types, include_documents=include_documents,
                          limit=limit, cursor=cursor)

    @app.get("/api/accounts/{aid}/deadlines")
    def deadlines(aid: int, start: str, end: str, tz: str | None = None, course_id: str | None = None,
                  include_calendar: bool = True, limit: int | None = None, cursor: str | None = None):
        return api.deadlines(aid, start=start, end=end, time_zone=tz, course_id=course_id,
                             include_calendar=include_calendar, limit=limit, cursor=cursor)

    @app.get("/api/accounts/{aid}/changes")
    def changes(aid: int, cursor: str | None = None, since: str | None = None, include_baseline: bool = False,
                include_first_observed: bool = True, kinds: str | None = None, course_id: str | None = None,
                limit: int | None = None):
        return api.changes(aid, cursor=cursor, since=since, include_baseline=include_baseline,
                           include_first_observed=include_first_observed, kinds=kinds, course_id=course_id, limit=limit)

    @app.get("/api/accounts/{aid}/coverage")
    def coverage(aid: int, course_id: str | None = None):
        return api.coverage(aid, course_id=course_id)

    @app.get("/api/accounts/{aid}/freshness")
    def freshness(aid: int):
        return api.freshness(aid)

    @app.get("/api/accounts/{aid}/discrepancies")
    def discrepancies(aid: int):
        return api.discrepancies(aid)

    @app.get("/api/accounts/{aid}/resources/{rid}")
    def resource(aid: int, rid: str):
        return api.describe_resource(aid, rid)

    @app.get("/api/accounts/{aid}/resources/{rid}/text")
    def resource_text(aid: int, rid: str, offset: int = 0, limit: int = 20000):
        return api.read_resource_text(aid, rid, offset=offset, limit=limit)

    @app.get("/api/accounts/{aid}/resources/{rid}/original")
    def resource_original(aid: int, rid: str):
        path, info = api.resource_original(aid, rid)
        name = (info["items"][0].get("filename") if info["items"] else None) or rid
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "._- ")[:150] or rid
        # Always a download, never rendered in the app's origin.
        return FileResponse(path, media_type="application/octet-stream", filename=safe)

    # ---- annotations ---------------------------------------------------------------------
    @app.get("/api/accounts/{aid}/annotations")
    def annotations(aid: int, item_id: int | None = None, course_id: str | None = None, kind: str | None = None,
                    task_state: str | None = None):
        return api.list_annotations(aid, item_id=item_id, course_id=course_id, kind=kind, task_state=task_state)

    @app.post("/api/accounts/{aid}/annotations")
    async def create_annotation(aid: int, request: Request):
        return api.create_annotation(aid, await body(request))

    @app.patch("/api/accounts/{aid}/annotations/{nid}")
    async def update_annotation(aid: int, nid: int, request: Request):
        return api.update_annotation(aid, nid, await body(request))

    @app.delete("/api/accounts/{aid}/annotations/{nid}")
    def delete_annotation(aid: int, nid: int):
        return api.delete_annotation(aid, nid)

    # ---- jobs ---------------------------------------------------------------------------------
    @app.get("/api/accounts/{aid}/jobs")
    def jobs(aid: int, limit: int | None = None, cursor: str | None = None):
        return api.list_jobs(aid, limit=limit, cursor=cursor)

    @app.post("/api/accounts/{aid}/jobs")
    async def start_job(aid: int, request: Request):
        b = await body(request)
        return api.request_collection(aid, mode=b.get("mode", "quick"), scope=b.get("scope"))

    @app.get("/api/accounts/{aid}/jobs/{jid}")
    def job(aid: int, jid: int, tasks: bool = False):
        return api.job_status(aid, jid, include_tasks=tasks)

    @app.post("/api/accounts/{aid}/jobs/{jid}/{action}")
    def job_action(aid: int, jid: int, action: str):
        if action == "cancel":
            return api.cancel_job(aid, jid)
        if action == "pause":
            return api.pause_job(aid, jid)
        if action == "resume":
            return api.resume_job(aid, jid)
        raise InvalidInput("unknown job action")

    # ---- observations & data ---------------------------------------------------------------------
    @app.post("/api/accounts/{aid}/observations")
    async def import_obs(aid: int, request: Request):
        raw = await request.body()
        try:
            pkg = json.loads(raw)
        except ValueError as e:
            raise InvalidInput("observation package must be JSON") from e
        return api.import_observations(pkg, account_id=aid)

    @app.post("/api/observations/validate")
    async def validate_obs(request: Request):
        return api.validate_observations(json.loads(await request.body() or b"{}"))

    @app.get("/api/backups")
    def backups():
        return api.list_backups()

    @app.post("/api/backups")
    def create_backup():
        return api.create_backup()

    @app.post("/api/backups/{name}/verify")
    def verify_backup(name: str):
        return api.verify_backup(name)

    @app.post("/api/backups/{name}/restore")
    def restore_backup(name: str):
        if api.runner and api.runner.current_job:
            raise InvalidInput("stop the running collection job before restoring", code="job_running")
        return api.restore_backup(name)

    @app.get("/api/backups/{name}/download")
    def download_backup(name: str):
        return FileResponse(api._backup_path(name), media_type="application/zip", filename=name)

    @app.post("/api/accounts/{aid}/export")
    def export(aid: int):
        return api.export_account(aid)

    @app.post("/api/import-archive")
    async def import_archive(request: Request):
        data = await request.body()
        if len(data) > 2 * 1024 ** 3:
            raise InvalidInput("archive too large")
        return api.import_archive_bytes(data)

    @app.post("/api/accounts/{aid}/delete-data")
    async def delete_data(aid: int, request: Request):
        b = await body(request)
        return api.delete_account_data(aid, b.get("confirm", ""))

    # ---- planner & external-site requests --------------------------------------------
    @app.get("/api/accounts/{aid}/planner/week")
    def planner_week(aid: int, week_of: str | None = None, suggest: bool = False, hours: float = 3.0):
        return api.planner_week(aid, week_of=week_of, suggest_study=suggest, study_hours=hours)

    @app.get("/api/accounts/{aid}/planner/classes")
    def planner_classes(aid: int):
        return api.list_class_times(aid)

    @app.post("/api/accounts/{aid}/planner/classes")
    async def planner_add_class(aid: int, request: Request):
        return api.add_class_time(aid, await body(request))

    @app.delete("/api/accounts/{aid}/planner/classes/{cid}")
    def planner_delete_class(aid: int, cid: int):
        return api.delete_class_time(aid, cid)

    @app.get("/api/accounts/{aid}/planner.ics")
    def planner_ics(aid: int, weeks: int = 8):
        return Response(api.planner_ics(aid, weeks), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="canvas-hub.ics"'})

    @app.get("/api/accounts/{aid}/requests")
    def view_requests(aid: int, status: str | None = None):
        return api.list_view_requests(aid, status)

    @app.post("/api/accounts/{aid}/requests")
    async def create_view_request(aid: int, request: Request):
        return api.create_view_request(aid, await body(request))

    @app.post("/api/accounts/{aid}/requests/{rid}/cancel")
    def cancel_view_request(aid: int, rid: int):
        return api.cancel_view_request(aid, rid)

    # ---- assistant (bring-your-own API key) ---------------------------------------
    def _assistant():
        a = getattr(api, "assistant", None)
        if a is None:
            raise HubError("the assistant is not available in this process", code="assistant_unavailable", status=503)
        return a

    @app.get("/api/assistant/config")
    def assistant_config():
        return _assistant().public_config()

    @app.put("/api/assistant/config")
    async def assistant_set_config(request: Request):
        return _assistant().update_config(await body(request))

    @app.post("/api/assistant/test")
    async def assistant_test(request: Request):
        # The provider call can take minutes; keep it off the event loop.
        pid = (await body(request)).get("profile_id")
        return await run_in_threadpool(_assistant().test, pid)

    @app.post("/api/assistant/profiles/{pid}/activate")
    def assistant_activate(pid: str):
        return _assistant().activate_profile(pid)

    @app.delete("/api/assistant/profiles/{pid}")
    def assistant_delete_profile(pid: str):
        return _assistant().delete_profile(pid)

    @app.get("/api/accounts/{aid}/assistant/conversations")
    def assistant_conversations(aid: int):
        api.get_account(aid)
        return {"conversations": _assistant().list_conversations(aid)}

    @app.post("/api/accounts/{aid}/assistant/messages")
    async def assistant_send(aid: int, request: Request):
        b = await body(request)
        api.get_account(aid)
        cid = b.get("conversation_id")
        pid, reasoning = b.get("profile_id"), b.get("reasoning")
        return _assistant().send(aid, b.get("text", ""), int(cid) if cid else None, str(pid) if pid else None,
                                 str(reasoning) if reasoning else None)

    @app.get("/api/accounts/{aid}/assistant/conversations/{cid}")
    def assistant_conversation(aid: int, cid: int, after: int = 0):
        return _assistant().get_conversation(aid, cid, after)

    @app.post("/api/accounts/{aid}/assistant/conversations/{cid}/stop")
    def assistant_stop(aid: int, cid: int):
        return _assistant().stop(aid, cid)

    @app.delete("/api/accounts/{aid}/assistant/conversations/{cid}")
    def assistant_delete(aid: int, cid: int):
        return _assistant().delete_conversation(aid, cid)

    @app.post("/api/instances/{iid}/clear-session")
    def clear_session(iid: int):
        w = api.worker
        if w is not None:
            if w.busy_with:
                raise HubError("a collection job is using the browser", code="browser_busy", status=409)
            return w.call(lambda: w.sign_out(iid, clear_profile=True), timeout=60)
        return {"cleared": api.hub.clear_browser_session(iid)}

    return app
