"""Original documents and extracted text.

Originals are stored content-addressed under resources/originals/<aa>/<sha256>.
Extracted text goes to resources/extracted/<sha256>.md. Resource ids are the
sha256 hex digest; every read is checked against the requesting account via
item_resources, and paths are never taken from callers.
"""

from __future__ import annotations

import mimetypes
import os
import re
import tempfile
import threading
from pathlib import Path

from ..db import Database
from ..paths import AppPaths
from ..util import InvalidInput, NotFound, now_iso, sha256_bytes

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_TEXT_TYPES = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".epub", ".html", ".htm", ".csv", ".txt", ".md",
               ".json", ".xml", ".rtf", ".ipynb", ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".tex"}
_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".svg"}
_MEDIA_TYPES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}
_ARCHIVE_TYPES = {".zip", ".rar", ".7z", ".tar", ".gz"}

_extract_lock = threading.Lock()


def validate_resource_id(resource_id: str) -> str:
    if not isinstance(resource_id, str) or not _SHA_RE.match(resource_id):
        raise InvalidInput("resource_id must be a 64-character lowercase sha256 hex id", code="invalid_resource_id")
    return resource_id


class ResourceService:
    def __init__(self, db: Database, paths: AppPaths):
        self.db = db
        self.paths = paths

    def _original_path(self, sha: str) -> Path:
        return self.paths.originals / sha[:2] / sha

    def store_bytes(self, data: bytes, content_type: str | None = None) -> str:
        sha = sha256_bytes(data)
        target = self._original_path(sha)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO resources(id, size, content_type, stored_path, created_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                (sha, len(data), content_type, str(target.relative_to(self.paths.resources)), now_iso()),
            )
        return sha

    def store_file(self, path: Path, content_type: str | None = None) -> str:
        return self.store_bytes(Path(path).read_bytes(), content_type)

    def exists(self, sha: str) -> bool:
        return self.db.one("SELECT 1 FROM resources WHERE id=?", (sha,)) is not None

    # ---- extraction -----------------------------------------------------
    def extract(self, sha: str, filename: str | None, *, force: bool = False) -> dict:
        row = self.db.one("SELECT * FROM resources WHERE id=?", (validate_resource_id(sha),))
        if not row:
            raise NotFound("resource not found")
        if row["extraction_status"] != "not_attempted" and not force:
            return dict(row)
        ext = Path(filename or "").suffix.lower()
        if not ext and row["content_type"]:
            ext = mimetypes.guess_extension(row["content_type"].split(";")[0].strip()) or ""
        status, detail, text, tool = self._run_extraction(self.paths.resources / row["stored_path"], ext, row["size"])
        extracted_path = None
        if text:
            out = self.paths.extracted / f"{sha}.md"
            out.write_text(text, encoding="utf-8")
            extracted_path = str(out.relative_to(self.paths.resources))
        with self.db.tx() as c:
            c.execute(
                """UPDATE resources SET extraction_status=?, extraction_tool=?, extraction_detail=?,
                   extracted_path=?, extracted_chars=?, extracted_at=? WHERE id=?""",
                (status, tool, detail, extracted_path, len(text) if text else 0, now_iso(), sha),
            )
        return dict(self.db.one("SELECT * FROM resources WHERE id=?", (sha,)))

    def _run_extraction(self, path: Path, ext: str, size: int) -> tuple[str, str | None, str, str | None]:
        if ext in _MEDIA_TYPES:
            return "unsupported_media", "audio/video is stored but not transcribed", "", None
        if ext in _IMAGE_TYPES:
            return "image_only", "image content; text not extracted (no OCR performed)", "", None
        if ext in _ARCHIVE_TYPES:
            return "unsupported_type", "archive stored; contents not unpacked", "", None
        if ext and ext not in _TEXT_TYPES:
            return "unsupported_type", f"no extractor configured for {ext}", "", None
        try:
            from markitdown import MarkItDown  # type: ignore
            import markitdown as _mi
        except Exception as e:  # noqa: BLE001
            return "tool_unavailable", f"markitdown not importable: {e}", "", None
        tool = f"markitdown {getattr(_mi, '__version__', '?')}"
        try:
            with _extract_lock:
                # Local conversion only: no LLM client or remote service is configured.
                md = MarkItDown(enable_plugins=False)
                # Give MarkItDown the real extension; content-addressed files have none.
                with tempfile.TemporaryDirectory() as td:
                    link = Path(td) / ("document" + (ext or ".bin"))
                    try:
                        os.symlink(path, link)
                    except OSError:
                        link.write_bytes(path.read_bytes())
                    result = md.convert(str(link))
            text = (result.text_content or "").strip()
        except Exception as e:  # noqa: BLE001
            return "failed", f"{type(e).__name__}: {str(e)[:300]}", "", tool
        if ext == ".pdf":
            # No text layer, or almost no text in a large file, usually means a
            # scanned PDF. Any text found is kept, but the status says OCR is needed.
            if not text or (size > 200_000 and len(text) < size / 5000):
                return "needs_ocr", f"only {len(text)} characters of text in a {size}-byte PDF; likely scanned images", text, tool
        if not text:
            return "empty", "extractor returned no text", "", tool
        return "ok", None, text, tool

    # ---- reads (account-checked) ---------------------------------------
    def describe(self, account_id: int, resource_id: str) -> dict:
        validate_resource_id(resource_id)
        rows = self.db.query(
            """SELECT ir.item_id, ir.filename, ir.observed_at, i.title, i.item_type, i.course_canvas_id, i.source_url
               FROM item_resources ir JOIN items i ON i.id=ir.item_id
               WHERE ir.resource_id=? AND i.account_id=? ORDER BY ir.observed_at DESC""",
            (resource_id, account_id),
        )
        if not rows:
            raise NotFound("resource not found for this account", code="resource_not_found")
        r = dict(self.db.one("SELECT * FROM resources WHERE id=?", (resource_id,)))
        return {
            "resource_id": resource_id,
            "size": r["size"],
            "content_type": r["content_type"],
            "stored_at": r["created_at"],
            "extraction": {
                "status": r["extraction_status"], "tool": r["extraction_tool"], "detail": r["extraction_detail"],
                "chars": r["extracted_chars"], "extracted_at": r["extracted_at"],
            },
            "items": [dict(x) for x in rows],
        }

    def read_text(self, account_id: int, resource_id: str, offset: int = 0, limit: int = 20000) -> dict:
        info = self.describe(account_id, resource_id)
        r = self.db.one("SELECT extracted_path FROM resources WHERE id=?", (resource_id,))
        if not r["extracted_path"]:
            return {**info, "text": None, "offset": 0, "next_offset": None, "total_chars": 0}
        text = (self.paths.resources / r["extracted_path"]).read_text(encoding="utf-8")
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200000))
        chunk = text[offset: offset + limit]
        nxt = offset + limit if offset + limit < len(text) else None
        return {**info, "text": chunk, "offset": offset, "next_offset": nxt, "total_chars": len(text)}

    def original_path(self, account_id: int, resource_id: str) -> tuple[Path, dict]:
        info = self.describe(account_id, resource_id)
        r = self.db.one("SELECT stored_path FROM resources WHERE id=?", (resource_id,))
        p = (self.paths.resources / r["stored_path"]).resolve()
        if self.paths.resources.resolve() not in p.parents:
            raise NotFound("resource path invalid")
        return p, info

    def extracted_text_for(self, sha: str) -> str:
        r = self.db.one("SELECT extracted_path FROM resources WHERE id=?", (sha,))
        if not r or not r["extracted_path"]:
            return ""
        p = self.paths.resources / r["extracted_path"]
        return p.read_text(encoding="utf-8") if p.exists() else ""
