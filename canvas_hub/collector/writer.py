"""Sends confirmed writes to Canvas through the signed-in browser session.

This is the only code that issues non-GET requests to Canvas, and it only
runs for a write the user confirmed in the app (see services/writes.py). It
knows exactly four operations, each a documented Canvas REST call:

- text submission:   POST /api/v1/courses/:c/assignments/:a/submissions
- file submission:   POST .../submissions/self/files (upload slot), POST the
                     file to the upload URL Canvas returns, then submit it
- discussion reply:  POST /api/v1/courses/:c/discussion_topics/:t/entries
- inbox message:     POST /api/v1/conversations

Requests carry Canvas's CSRF token from the session cookie, exactly like the
Canvas web UI. Nothing is retried automatically: a failed write is reported,
never re-sent behind the user's back.
"""

from __future__ import annotations

from urllib.parse import unquote, urlencode, urljoin

from ..urls import same_origin
from .client import ACCEPT, AuthRequired, CollectorError, _looks_like_login, _strip_json


class CanvasWriter:
    def __init__(self, context, origin: str, timeout_ms: int = 120000):
        self.context = context
        self.origin = origin.rstrip("/")
        self.timeout_ms = timeout_ms

    def _csrf(self) -> str:
        for c in self.context.cookies(self.origin):
            if c["name"] == "_csrf_token":
                return unquote(c["value"])
        raise AuthRequired("no Canvas CSRF token in the session; open Canvas once while signed in and try again")

    def _url(self, path: str) -> str:
        url = urljoin(self.origin + "/", path.lstrip("/"))
        if not same_origin(url, self.origin):
            raise CollectorError("refusing a cross-origin write")
        return url

    def _check(self, resp, what: str):
        st = resp.status
        if st in (301, 302, 303, 307, 308) and _looks_like_login(urljoin(self.origin, resp.headers.get("location", "")), self.origin):
            raise AuthRequired("Canvas asked to sign in again")
        if st == 401:
            raise AuthRequired("Canvas returned 401 (session expired)")
        if st >= 400:
            try:
                body = _strip_json(resp.text())
                msg = (body.get("errors") or [{}])[0].get("message") if isinstance(body.get("errors"), list) else body.get("message") or body.get("errors")
            except Exception:  # noqa: BLE001
                msg = None
            raise CollectorError(f"Canvas refused {what} (HTTP {st}{': ' + str(msg) if msg else ''})")
        text = resp.text()
        return _strip_json(text) if text.strip() else {}

    def _post(self, path: str, form: list[tuple[str, str]] | dict, what: str):
        pairs = list(form.items()) if isinstance(form, dict) else form       # lists allow repeated keys (recipients[])
        resp = self.context.request.post(self._url(path), data=urlencode(pairs), max_redirects=0, fail_on_status_code=False,
                                         timeout=self.timeout_ms,
                                         headers={"Accept": ACCEPT, "Content-Type": "application/x-www-form-urlencoded",
                                                  "X-CSRF-Token": self._csrf(), "X-Requested-With": "XMLHttpRequest"})
        return self._check(resp, what)

    # ---- operations ------------------------------------------------------------------
    def send(self, job: dict) -> dict:
        k = job["kind"]
        if k == "submission_text":
            return self.submit_text(job["course_id"], job["target_id"], job["html"])
        if k == "submission_file":
            return self.submit_file(job["course_id"], job["target_id"], job["filename"], job["mime"], job["data"], job.get("comment"))
        if k == "discussion_reply":
            return self.reply(job["course_id"], job["target_id"], job["html"])
        if k == "message":
            return self.message(job["course_id"], job["recipients"], job["subject"], job["html"])
        raise CollectorError(f"unknown write kind {k}")

    def submit_text(self, course_id: str, assignment_id: str, body_html: str) -> dict:
        r = self._post(f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
                       {"submission[submission_type]": "online_text_entry", "submission[body]": body_html}, "the submission")
        return {"submission_id": r.get("id"), "attempt": r.get("attempt"), "submitted_at": r.get("submitted_at"),
                "workflow_state": r.get("workflow_state")}

    def submit_file(self, course_id: str, assignment_id: str, filename: str, mime: str, data: bytes,
                    comment: str | None = None) -> dict:
        base = f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions"
        slot = self._post(f"{base}/self/files", {"name": filename, "size": str(len(data)), "content_type": mime,
                                                 "on_duplicate": "rename"}, "the upload slot")
        up_url = slot.get("upload_url")
        if not up_url:
            raise CollectorError("Canvas did not return an upload URL")
        # The upload URL is Canvas's own file service (possibly another host); send exactly what it asked for.
        multipart = {k: str(v) for k, v in (slot.get("upload_params") or {}).items()}
        multipart["file"] = {"name": filename, "mimeType": mime, "buffer": data}
        resp = self.context.request.post(up_url, multipart=multipart, max_redirects=0, fail_on_status_code=False,
                                         timeout=self.timeout_ms)
        if resp.status in (301, 302, 303, 307, 308):
            loc = urljoin(up_url, resp.headers.get("location", ""))
            if not same_origin(loc, self.origin):
                raise CollectorError("the upload tried to redirect outside Canvas")
            resp = self.context.request.get(loc, headers={"Accept": ACCEPT, "X-CSRF-Token": self._csrf()},
                                            max_redirects=0, fail_on_status_code=False, timeout=self.timeout_ms)
        f = self._check(resp, "the file upload")
        fid = f.get("id")
        if not fid:
            raise CollectorError("the upload did not return a file id")
        form = [("submission[submission_type]", "online_upload"), ("submission[file_ids][]", str(fid))]
        if comment:
            form.append(("comment[text_comment]", comment))
        r = self._post(base, form, "the submission")
        return {"submission_id": r.get("id"), "attempt": r.get("attempt"), "submitted_at": r.get("submitted_at"),
                "workflow_state": r.get("workflow_state"), "file_id": fid, "filename": f.get("display_name") or filename}

    def reply(self, course_id: str, topic_id: str, body_html: str) -> dict:
        r = self._post(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries", {"message": body_html}, "the reply")
        return {"entry_id": r.get("id"), "created_at": r.get("created_at")}

    def message(self, course_id: str, recipients: list[str], subject: str, body_html: str) -> dict:
        form = [("recipients[]", r) for r in recipients] + [
            ("subject", subject), ("body", body_html), ("context_code", f"course_{course_id}"),
            ("group_conversation", "false"), ("force_new", "true")]
        r = self._post("/api/v1/conversations", form, "the message")
        conv = r[0] if isinstance(r, list) and r else r
        return {"conversation_id": conv.get("id") if isinstance(conv, dict) else None}
