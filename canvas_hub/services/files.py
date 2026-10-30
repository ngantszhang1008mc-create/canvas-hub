"""Files the assistant or the user saved in Canvas Hub.

Finished work (a study plan, a spec, a CSV) is kept here instead of in a
note, so it can be downloaded, read back in full, or attached to a prepared
Canvas submission. Files live in the database, so backups include them.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re

from ..db import Database
from ..util import InvalidInput, NotFound, now_iso

MAX_BYTES = 25 * 1024 * 1024
_BAD = re.compile(r"[^\w.\- ()]+")


def safe_filename(name: str) -> str:
    base = _BAD.sub("_", (name or "").strip()).strip(" ._")[:120]
    if not base:
        raise InvalidInput("a file name is required")
    return base


class SavedFileService:
    def __init__(self, db: Database):
        self.db = db

    def save(self, account_id: int, *, filename: str, content: bytes | str, title: str | None = None,
             mime: str | None = None, course_id: str | None = None, created_by: str = "assistant",
             conversation_id: int | None = None) -> dict:
        name = safe_filename(filename)
        is_text = isinstance(content, str)
        data = content.encode("utf-8") if is_text else bytes(content)
        if not data:
            raise InvalidInput("the file is empty")
        if len(data) > MAX_BYTES:
            raise InvalidInput("files are limited to 25 MB")
        mime = mime or mimetypes.guess_type(name)[0] or ("text/plain" if is_text else "application/octet-stream")
        with self.db.tx() as c:
            fid = c.execute(
                """INSERT INTO saved_files(account_id, title, filename, mime, size, sha256, content, is_text, course_canvas_id,
                   created_by, conversation_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, (title or name)[:200], name, mime, len(data), hashlib.sha256(data).hexdigest(), data,
                 1 if is_text else 0, course_id, created_by, conversation_id, now_iso())).lastrowid
        return self.get(account_id, fid)

    def get(self, account_id: int, file_id: int, *, with_content: bool = False) -> dict:
        cols = "*" if with_content else ("id, account_id, title, filename, mime, size, sha256, is_text, course_canvas_id, "
                                         "created_by, conversation_id, created_at")
        row = self.db.one(f"SELECT {cols} FROM saved_files WHERE id=? AND account_id=?", (int(file_id), account_id))
        if not row:
            raise NotFound("file not found", code="file_not_found")
        d = dict(row)
        if with_content:
            d["content"] = bytes(d["content"])
        return d

    def text(self, account_id: int, file_id: int, max_chars: int | None = None) -> str:
        f = self.get(account_id, file_id, with_content=True)
        if not f["is_text"]:
            raise InvalidInput("this file is not text", code="not_text")
        t = f["content"].decode("utf-8", errors="replace")
        return t if max_chars is None else t[:max_chars]

    def list(self, account_id: int, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.query(
            """SELECT id, title, filename, mime, size, sha256, is_text, course_canvas_id, created_by, created_at
               FROM saved_files WHERE account_id=? ORDER BY id DESC LIMIT ?""", (account_id, max(1, min(int(limit), 1000))))]

    def delete(self, account_id: int, file_id: int) -> dict:
        self.get(account_id, file_id)
        with self.db.tx() as c:
            c.execute("DELETE FROM saved_files WHERE id=? AND account_id=?", (int(file_id), account_id))
        return {"deleted": int(file_id)}
