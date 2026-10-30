"""App-managed browser and Canvas sign-in lifecycle.

One dedicated thread owns Playwright and every browser context (Playwright's
sync API is thread-bound). Each Canvas instance gets its own persistent
Chromium profile under <home>/browser-profiles/instance-<id>; nothing is
imported from other browser profiles.

Sign-in is *interactive*: the app opens the institution's normal login page in
a visible browser window and the user signs in there (SSO redirects, MFA).
The app never sees the password. "Signed in" is only reported after a
verified, same-origin Canvas request for the current user succeeds.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable

from ..urls import same_origin
from ..util import now_iso
from .client import AccessDenied, AuthRequired, CanvasClient, CollectorError, NotFoundError

log = logging.getLogger("canvas_hub.browser")


def _headless_default() -> bool:
    return os.environ.get("CANVAS_HUB_HEADLESS", "").lower() in ("1", "true", "yes")


class BrowserWorker:
    def __init__(self, hub, *, headless: bool | None = None, on_signed_in: Callable[[int, int], None] | None = None,
                 min_interval: float | None = None):
        self.hub = hub
        self.headless = _headless_default() if headless is None else headless
        # Minimum spacing between Canvas requests (politeness / rate limits).
        self.min_interval = float(os.environ.get("CANVAS_HUB_MIN_INTERVAL", "0.35")) if min_interval is None else min_interval
        self.on_signed_in = on_signed_in
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="canvas-hub-browser", daemon=True)
        self._pw = None
        self._contexts: dict[int, Any] = {}
        self._lock = threading.Lock()
        self.sessions: dict[int, dict] = {}
        self.busy_with: str | None = None
        self._started = False

    # ---- thread plumbing -------------------------------------------------
    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def stop(self, timeout: float = 15) -> None:
        if not self._started:
            return
        fut: Future = Future()
        self._q.put((None, (), fut))
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass

    def submit(self, fn: Callable, *args) -> Future:
        self.start()
        fut: Future = Future()
        self._q.put((fn, args, fut))
        return fut

    def call(self, fn: Callable, *args, timeout: float | None = 120) -> Any:
        return self.submit(fn, *args).result(timeout=timeout)

    def _run(self) -> None:
        while True:
            fn, args, fut = self._q.get()
            if fn is None:
                self._shutdown()
                fut.set_result(True)
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)

    def _shutdown(self) -> None:
        for iid in list(self._contexts):
            self._close_context(iid)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    # ---- contexts (worker thread only) ------------------------------------
    def _playwright(self):
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        return self._pw

    def _context(self, instance_id: int):
        ctx = self._contexts.get(instance_id)
        if ctx is not None:
            try:
                _ = ctx.pages  # raises if the context was closed
                ctx.cookies()
                return ctx
            except Exception:  # noqa: BLE001
                self._contexts.pop(instance_id, None)
        profile = self.hub.paths.profile_dir(instance_id)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(profile, 0o700)
        except OSError:
            pass
        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "viewport": None if not self.headless else {"width": 1280, "height": 900},
            "accept_downloads": False,
        }
        exe = os.environ.get("CANVAS_HUB_CHROMIUM")
        if exe:
            kwargs["executable_path"] = exe
        ctx = self._playwright().chromium.launch_persistent_context(str(profile), **kwargs)
        ctx.on("close", lambda *_: self._contexts.pop(instance_id, None))
        self._contexts[instance_id] = ctx
        return ctx

    def _close_context(self, instance_id: int) -> None:
        ctx = self._contexts.pop(instance_id, None)
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    def _set_session(self, instance_id: int, **state) -> dict:
        with self._lock:
            cur = dict(self.sessions.get(instance_id, {"instance_id": instance_id}))
            cur.update(state)
            cur["checked_at"] = now_iso()
            cur["browser_open"] = instance_id in self._contexts
            self.sessions[instance_id] = cur
            return dict(cur)

    def session(self, instance_id: int) -> dict:
        with self._lock:
            s = dict(self.sessions.get(instance_id) or {"instance_id": instance_id, "status": "unknown",
                                                        "message": "Not checked yet"})
        s["browser_open"] = instance_id in self._contexts
        s["worker_busy_with"] = self.busy_with
        return s

    # ---- operations (call through submit/call) ----------------------------
    def open_login(self, instance_id: int) -> dict:
        inst = self.hub.identity.get_instance(instance_id)
        ctx = self._context(instance_id)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        target = inst["entry_url"] or inst["origin"] + "/"
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # noqa: BLE001
            return self._set_session(instance_id, status="error", message=f"Could not open {target}: {e}")
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        state = self.check_session(instance_id, quiet=True)
        if state["status"] != "signed_in":
            state = self._set_session(
                instance_id, status="login_open",
                message="A browser window is open. Sign in there with your institution's normal login (including MFA). "
                        "This app never sees your password.")
        return state

    def check_session(self, instance_id: int, quiet: bool = False) -> dict:
        inst = self.hub.identity.get_instance(instance_id)
        if instance_id not in self._contexts and not self.hub.paths.profile_dir(instance_id).exists():
            return self._set_session(instance_id, status="signed_out", message="No saved browser session for this Canvas")
        prev = self.sessions.get(instance_id, {})
        try:
            ctx = self._context(instance_id)
            client = CanvasClient(ctx, inst["origin"], min_interval=0.0, max_retries=1)
            user = client.api_get("/api/v1/users/self")
        except AuthRequired as e:
            status = "expired" if prev.get("status") in ("signed_in", "expired") else (
                "login_open" if prev.get("status") == "login_open" else "signed_out")
            msg = ("Your Canvas session has expired. Sign in again; paused jobs resume afterwards."
                   if status == "expired" else
                   "Waiting for you to finish signing in in the browser window." if status == "login_open"
                   else "Not signed in.")
            return self._set_session(instance_id, status=status, message=msg, detail=str(e), account_id=prev.get("account_id"))
        except Exception as e:  # noqa: BLE001
            return self._set_session(instance_id, status="error", message=f"Could not check sign-in: {e}")
        if not isinstance(user, dict) or not user.get("id"):
            return self._set_session(instance_id, status="error", message="Canvas returned no user identity")
        ident = {"canvas_user_id": str(user["id"]), "display_name": user.get("name") or user.get("short_name")}
        try:
            prof = client.api_get("/api/v1/users/self/profile")
            ident.update(login_id=prof.get("login_id"), primary_email=prof.get("primary_email"), locale=prof.get("locale"))
            if prof.get("time_zone"):
                ident.update(time_zone=prof["time_zone"], time_zone_source="canvas_api:users/self/profile")
        except (AccessDenied, NotFoundError, CollectorError):
            pass
        entry_ok = None
        from ..urls import parse_canvas_address
        addr = parse_canvas_address(inst["entry_url"] or inst["origin"])
        if addr.course_id:
            try:
                client.api_get(f"/api/v1/courses/{addr.course_id}")
                entry_ok = True
            except (AccessDenied, NotFoundError):
                entry_ok = False
            except CollectorError:
                entry_ok = None
        # Confirm the login tab actually reached this Canvas origin.
        on_origin = any(same_origin(p.url, inst["origin"]) for p in ctx.pages if p.url and p.url != "about:blank")
        acct = self.hub.identity.upsert_verified_account(instance_id, ident)
        newly = prev.get("status") != "signed_in" or prev.get("account_id") != acct["id"]
        state = self._set_session(
            instance_id, status="signed_in", account_id=acct["id"], canvas_user_id=acct["canvas_user_id"],
            display_name=acct["display_name"], time_zone=acct["time_zone"], entry_course_accessible=entry_ok,
            tab_on_canvas_origin=on_origin, verified_at=now_iso(),
            message=f"Signed in to {inst['origin']} as {acct['display_name'] or acct['canvas_user_id']} (verified with Canvas)",
            detail=None,
        )
        if newly and self.on_signed_in:
            try:
                self.on_signed_in(instance_id, acct["id"])
            except Exception:  # noqa: BLE001
                log.exception("on_signed_in hook failed")
        return state

    def sign_out(self, instance_id: int, *, clear_profile: bool = False) -> dict:
        """Clear the app browser's Canvas cookies (local sign-out). Stored
        course data is not touched."""
        ctx = self._contexts.get(instance_id)
        if ctx is not None:
            try:
                ctx.clear_cookies()
            except Exception:  # noqa: BLE001
                pass
        self._close_context(instance_id)
        cleared = False
        if clear_profile:
            cleared = self.hub.clear_browser_session(instance_id)
        return self._set_session(instance_id, status="signed_out", account_id=None,
                                 message="Signed out locally" + ("; saved browser profile deleted" if cleared else ""))

    def close_browser(self, instance_id: int) -> dict:
        self._close_context(instance_id)
        return self._set_session(instance_id, message="Browser window closed")

    def client_for(self, instance_id: int, should_stop=None) -> CanvasClient:
        inst = self.hub.identity.get_instance(instance_id)
        return CanvasClient(self._context(instance_id), inst["origin"], should_stop=should_stop,
                            min_interval=self.min_interval)
