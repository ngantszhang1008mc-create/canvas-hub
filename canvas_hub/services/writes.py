"""Confirmed writes to Canvas: submissions, discussion replies, inbox messages.

Canvas Hub is read-only unless the user switches writes on for an account
(Data & settings). Even then nothing is sent on its own:

1. A write is *prepared* (by the assistant or the user). Preparing checks the
   target and builds an exact preview: action, course, item, Canvas link,
   the body text, the file name, size and SHA-256, the recipients.
2. The user reads the preview in the app and confirms it. Confirmation must
   carry the preview's hash, so what is sent is exactly what was shown.
3. Only then does the signed-in browser send it. The result (or error) is
   kept in ``canvas_writes`` as a log.

There is no tool or MCP call that confirms a write; only the app's own UI
can. Prepared writes expire after 24 hours.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone

from .. import dates
from ..db import Database
from ..util import Conflict, InvalidInput, NotFound, now_iso, parse_iso
from .files import SavedFileService
from .settings import AccountSettings

KINDS = ("submission_text", "submission_file", "discussion_reply", "message")
KIND_LABEL = {"submission_text": "Submit text to an assignment", "submission_file": "Upload a file to an assignment",
              "discussion_reply": "Post a reply in a discussion", "message": "Send an inbox message"}
EXPIRES = timedelta(hours=24)
MAX_BODY = 100_000


def text_to_html(text: str) -> str:
    """Plain text or light markdown -> simple, escaped HTML paragraphs."""
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in paras)


class WriteService:
    def __init__(self, db: Database, identity, query, files: SavedFileService, settings: AccountSettings):
        self.db = db
        self.identity = identity
        self.query = query
        self.files = files
        self.settings = settings

    # ---- settings -------------------------------------------------------------------
    def get_settings(self, account_id: int) -> dict:
        return self.settings.get(account_id, "canvas_writes")

    def set_settings(self, account_id: int, *, enabled: bool | None = None, kinds: list | None = None) -> dict:
        val = {}
        if enabled is not None:
            val["enabled"] = bool(enabled)
        if kinds is not None:
            if not isinstance(kinds, list) or any(k not in KINDS for k in kinds):
                raise InvalidInput(f"kinds must be a list from {list(KINDS)}")
            val["kinds"] = kinds
        return self.settings.set(account_id, "canvas_writes", val)

    def _require_enabled(self, account_id: int, kind: str) -> None:
        s = self.get_settings(account_id)
        if not s["enabled"]:
            raise InvalidInput("Writing to Canvas is switched off. The user can switch it on in Data & settings.",
                               code="writes_disabled")
        if kind not in s["kinds"]:
            raise InvalidInput(f"'{KIND_LABEL[kind]}' is not allowed in the write settings", code="write_kind_disabled")

    # ---- targets ----------------------------------------------------------------------
    def _item(self, account_id: int, item_id: int, types: tuple) -> dict:
        row = self.db.one("""SELECT id, item_type, canvas_id, course_canvas_id, title, source_url FROM items
                             WHERE id=? AND account_id=?""", (int(item_id), account_id))
        if not row or row["item_type"] not in types:
            raise NotFound(f"no {' or '.join(types)} with item id {item_id}", code="write_target_not_found")
        return dict(row)

    def _course_title(self, account_id: int, course_id: str) -> str | None:
        r = self.db.one("SELECT title FROM items WHERE account_id=? AND item_type='course' AND canvas_id=?",
                        (account_id, str(course_id)))
        return r["title"] if r else None

    def _check_assignment(self, account_id: int, item: dict, submission_type: str) -> list[str]:
        f = self.query._fields(item["id"])
        allowed = f.get("submission_types") or []
        if submission_type not in allowed:
            raise InvalidInput(f"this assignment accepts {', '.join(allowed) or 'no online submissions'}, not {submission_type}",
                               code="submission_type_not_allowed")
        if f.get("locked_for_user"):
            raise InvalidInput("Canvas reports this assignment as locked for you", code="assignment_locked")
        now = datetime.now(timezone.utc)
        lock = dates.instant(f.get("lock_at"))
        if lock and lock < now:
            raise InvalidInput("the assignment's availability window has closed", code="assignment_closed")
        warnings = []
        due = dates.instant(f.get("due_at"))
        if due and due < now:
            warnings.append("The due date has passed; Canvas will mark this submission late.")
        sub = self.query._submission(account_id, item["canvas_id"]) or {}
        if sub.get("submitted_at"):
            warnings.append("You have already submitted; this becomes a new attempt.")
        att = f.get("allowed_attempts")
        if att and att > 0 and (sub.get("attempt") or 0) >= att:
            raise InvalidInput(f"all {att} allowed attempts are used", code="no_attempts_left")
        return warnings

    # ---- prepare -------------------------------------------------------------------------
    def prepare(self, account_id: int, kind: str, *, prepared_by: str, item_id: int | None = None, text: str | None = None,
                file_id: int | None = None, course_id: str | None = None, recipients: list | None = None,
                subject: str | None = None, conversation_id: int | None = None) -> dict:
        if kind not in KINDS:
            raise InvalidInput(f"kind must be one of {list(KINDS)}")
        self._require_enabled(account_id, kind)
        payload: dict = {}
        warnings: list[str] = []
        target_canvas_id = None
        target_title = None
        item = None
        if kind in ("submission_text", "submission_file"):
            if item_id is None:
                raise InvalidInput("item_id of the assignment is required")
            item = self._item(account_id, item_id, ("assignment",))
            course_id, target_canvas_id, target_title = item["course_canvas_id"], item["canvas_id"], item["title"]
            if kind == "submission_text":
                if not (text or "").strip():
                    raise InvalidInput("the submission text is empty")
                warnings = self._check_assignment(account_id, item, "online_text_entry")
                payload["body"] = text[:MAX_BODY]
            else:
                if file_id is None:
                    raise InvalidInput("file_id of a saved file is required (save it first)")
                warnings = self._check_assignment(account_id, item, "online_upload")
                f = self.files.get(account_id, file_id)
                payload["file"] = {"file_id": f["id"], "filename": f["filename"], "size": f["size"], "sha256": f["sha256"],
                                   "mime": f["mime"]}
                if text:
                    payload["comment"] = text[:2000]
        elif kind == "discussion_reply":
            if item_id is None:
                raise InvalidInput("item_id of the discussion is required")
            item = self._item(account_id, item_id, ("discussion", "announcement"))
            f = self.query._fields(item["id"])
            if f.get("locked") or f.get("locked_for_user"):
                raise InvalidInput("this discussion is locked for replies", code="discussion_locked")
            if item["item_type"] == "announcement":
                warnings.append("Replies to announcements only work when the instructor allows comments.")
            if not (text or "").strip():
                raise InvalidInput("the reply is empty")
            course_id, target_canvas_id, target_title = item["course_canvas_id"], item["canvas_id"], item["title"]
            payload["body"] = text[:MAX_BODY]
        else:  # message
            if not course_id:
                raise InvalidInput("course_id is required for a message (the course context)")
            if not recipients:
                raise InvalidInput("at least one recipient Canvas user id is required")
            rec = []
            for r in recipients[:20]:
                rid = str(r.get("id") if isinstance(r, dict) else r).strip()
                if not re.fullmatch(r"\d{1,20}", rid):
                    raise InvalidInput(f"recipient {rid!r} is not a Canvas user id")
                rec.append({"id": rid, "name": (r.get("name") if isinstance(r, dict) else None) or self._user_name(account_id, rid)})
            if not (text or "").strip() or not (subject or "").strip():
                raise InvalidInput("a message needs a subject and a body")
            payload.update(recipients=rec, subject=subject.strip()[:255], body=text[:MAX_BODY])
            target_title = payload["subject"]
        course_id = str(course_id)
        if not self._course_title(account_id, course_id) and kind == "message":
            raise NotFound(f"course {course_id} is not among the collected courses", code="course_not_found")
        payload["warnings"] = warnings
        preview = self._preview(account_id, kind, course_id, item, target_title, payload)
        now = now_iso()
        with self.db.tx() as c:
            wid = c.execute(
                """INSERT INTO canvas_writes(account_id, kind, course_canvas_id, target_item_id, target_canvas_id, target_title,
                   payload_json, preview_hash, prepared_by, conversation_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, kind, course_id, item["id"] if item else None, target_canvas_id, target_title,
                 json.dumps(payload), preview["hash"], prepared_by, conversation_id, now)).lastrowid
        return self.get(account_id, wid)

    def _user_name(self, account_id: int, user_id: str) -> str | None:
        # Authors seen in collected announcements/discussions carry id and display name.
        for r in self.db.query("""SELECT value_json FROM item_fields f JOIN items i ON i.id=f.item_id
                                  WHERE i.account_id=? AND f.field='author' LIMIT 2000""", (account_id,)):
            try:
                v = json.loads(r["value_json"])
            except ValueError:
                continue
            if isinstance(v, dict) and str(v.get("id")) == user_id:
                return v.get("display_name") or v.get("name")
        return None

    def _preview(self, account_id: int, kind: str, course_id: str, item: dict | None, target_title: str | None,
                 payload: dict) -> dict:
        p = {"action": KIND_LABEL[kind], "course_id": course_id, "course": self._course_title(account_id, course_id),
             "target": target_title, "canvas_url": item["source_url"] if item else None}
        for k in ("body", "comment", "subject", "recipients", "file"):
            if k in payload:
                p[k] = payload[k]
        p["hash"] = hashlib.sha256(json.dumps({k: v for k, v in p.items() if k != "hash"}, sort_keys=True).encode()).hexdigest()
        return p

    # ---- read ------------------------------------------------------------------------------
    def _row(self, account_id: int, wid: int) -> dict:
        row = self.db.one("SELECT * FROM canvas_writes WHERE id=? AND account_id=?", (int(wid), account_id))
        if not row:
            raise NotFound("prepared write not found", code="write_not_found")
        return dict(row)

    def get(self, account_id: int, wid: int) -> dict:
        r = self._row(account_id, wid)
        if r["status"] == "prepared" and parse_iso(r["created_at"]) + EXPIRES < datetime.now(timezone.utc):
            with self.db.tx() as c:
                c.execute("UPDATE canvas_writes SET status='expired', finished_at=? WHERE id=?", (now_iso(), r["id"]))
            r["status"] = "expired"
        payload = json.loads(r["payload_json"])
        item = self.db.one("SELECT source_url FROM items WHERE id=?", (r["target_item_id"],)) if r["target_item_id"] else None
        preview = self._preview(account_id, r["kind"], r["course_canvas_id"], dict(item) if item else None, r["target_title"],
                                payload)
        return {"id": r["id"], "kind": r["kind"], "status": r["status"], "course_id": r["course_canvas_id"],
                "target_item_id": r["target_item_id"], "target_title": r["target_title"], "prepared_by": r["prepared_by"],
                "preview": preview, "warnings": payload.get("warnings", []),
                "result": json.loads(r["result_json"]) if r["result_json"] else None, "error": r["error"],
                "created_at": r["created_at"], "confirmed_at": r["confirmed_at"], "finished_at": r["finished_at"],
                "expires_at": (parse_iso(r["created_at"]) + EXPIRES).isoformat()}

    def list(self, account_id: int, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT id FROM canvas_writes WHERE account_id=?", [account_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, max(1, min(int(limit), 500))))
        return [self.get(account_id, r["id"]) for r in rows]

    def cancel(self, account_id: int, wid: int) -> dict:
        r = self.get(account_id, wid)
        if r["status"] != "prepared":
            raise InvalidInput(f"this write is {r['status']}", code="write_not_open")
        with self.db.tx() as c:
            c.execute("UPDATE canvas_writes SET status='cancelled', finished_at=? WHERE id=?", (now_iso(), r["id"]))
        return self.get(account_id, wid)

    # ---- confirm and send --------------------------------------------------------------------
    SEND_WAIT = 300     # seconds to wait for the browser before answering "still sending"

    def confirm(self, account_id: int, wid: int, preview_hash: str, send) -> dict:
        """Send a prepared write. ``send(job)`` performs the Canvas requests in the
        signed-in browser and returns a result dict (or raises), or a Future of one.
        A Future that has not finished within SEND_WAIT leaves the write `sending`
        (never `failed`: the browser may still submit it) and records the outcome
        when it arrives, so a second attempt cannot be made in the meantime."""
        r = self.get(account_id, wid)
        if r["status"] != "prepared":
            raise InvalidInput(f"this write is {r['status']} and cannot be sent", code="write_not_open")
        if not preview_hash or preview_hash != r["preview"]["hash"]:
            raise Conflict("the preview changed; review it again before sending", code="preview_mismatch")
        self._require_enabled(account_id, r["kind"])
        row = self._row(account_id, wid)
        payload = json.loads(row["payload_json"])
        if r["kind"] in ("submission_text", "submission_file"):           # re-check: windows close, attempts get used
            self._check_assignment(account_id, self._item(account_id, row["target_item_id"], ("assignment",)),
                                   "online_text_entry" if r["kind"] == "submission_text" else "online_upload")
        job = {"kind": r["kind"], "course_id": row["course_canvas_id"], "target_id": row["target_canvas_id"]}
        if r["kind"] == "submission_file":
            f = self.files.get(account_id, payload["file"]["file_id"], with_content=True)
            if f["sha256"] != payload["file"]["sha256"]:
                raise Conflict("the file changed after the preview", code="preview_mismatch")
            job.update(filename=f["filename"], mime=f["mime"], data=f["content"], comment=payload.get("comment"))
        else:
            job["html"] = text_to_html(payload["body"])
        if r["kind"] == "message":
            job.update(recipients=[x["id"] for x in payload["recipients"]], subject=payload["subject"])
        with self.db.tx() as c:
            n = c.execute("UPDATE canvas_writes SET status='sending', confirmed_at=? WHERE id=? AND status='prepared'",
                          (now_iso(), r["id"])).rowcount
        if n != 1:
            raise Conflict("this write is already being sent", code="write_not_open")
        try:
            out = send(job)
            if isinstance(out, Future):
                try:
                    out = out.result(timeout=self.SEND_WAIT)
                except FutureTimeout:
                    if not out.done():                      # still running in the browser: decide when it ends
                        out.add_done_callback(lambda f: self._record(r["id"], f))
                        return self.get(account_id, wid)
                    raise
        except Exception as e:  # noqa: BLE001 - the error is recorded for the user
            self._record_failed(r["id"], e)
            return self.get(account_id, wid)
        self._record_sent(r["id"], out)
        return self.get(account_id, wid)

    def _record(self, wid: int, fut: Future) -> None:
        e = fut.exception()
        self._record_failed(wid, e) if e else self._record_sent(wid, fut.result())

    def _record_failed(self, wid: int, e: BaseException) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE canvas_writes SET status='failed', error=?, finished_at=? WHERE id=? AND status='sending'",
                      (f"{type(e).__name__}: {e}"[:2000], now_iso(), wid))

    def _record_sent(self, wid: int, result) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE canvas_writes SET status='sent', result_json=?, finished_at=? WHERE id=? AND status='sending'",
                      (json.dumps(result, default=str), now_iso(), wid))
