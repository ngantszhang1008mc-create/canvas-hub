"""Original documents and extracted text.

Originals are stored content-addressed under resources/originals/<aa>/<sha256>.
Extracted text goes to resources/extracted/<sha256>.md. Resource ids are the
sha256 hex digest; every read is checked against the requesting account via
item_resources, and paths are never taken from callers.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import signal
import sys
import time
import tempfile
import threading
from pathlib import Path

from .. import ocr
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
OCR_FAILED_TOOL = "tesseract: failed"      # tried and failed; retried only on request, so it cannot block the queue
OCR_PENDING = ("extraction_status IN ('image_only','needs_ocr') AND (extraction_tool IS NULL OR "
               "extraction_tool NOT LIKE 'tesseract%')")


def validate_resource_id(resource_id: str) -> str:
    if not isinstance(resource_id, str) or not _SHA_RE.match(resource_id):
        raise InvalidInput("resource_id must be a 64-character lowercase sha256 hex id", code="invalid_resource_id")
    return resource_id


class ResourceService:
    def __init__(self, db: Database, paths: AppPaths):
        self.db = db
        self.paths = paths

    # ---- OCR (optional, off by default; app-wide setting) ---------------------------------
    def ocr_settings(self) -> dict:
        row = self.db.one("SELECT value FROM app_settings WHERE key='ocr'")
        return {"enabled": False, "lang": "eng", **(json.loads(row["value"]) if row else {})}

    def set_ocr_settings(self, enabled: bool, lang: str = "eng") -> dict:
        try:
            ocr.valid_lang(lang)
        except ValueError as e:
            raise InvalidInput(str(e)) from e
        with self.db.tx() as c:
            c.execute("INSERT INTO app_settings(key, value) VALUES ('ocr', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps({"enabled": bool(enabled), "lang": lang}),))
        return self.ocr_settings()

    def ocr_pending(self) -> int:
        """Stored documents that OCR could read but has not yet."""
        return self.db.one(f"SELECT COUNT(*) n FROM resources WHERE {OCR_PENDING}")["n"]

    def ocr_failed(self) -> int:
        return self.db.one("SELECT COUNT(*) n FROM resources WHERE extraction_tool=?", (OCR_FAILED_TOOL,))["n"]

    def ocr_pending_ids(self, limit: int, *, retry_failed: bool = False) -> list[str]:
        where, params = (("extraction_tool=?", (OCR_FAILED_TOOL,)) if retry_failed else (OCR_PENDING, ()))
        return [r["id"] for r in self.db.query(f"SELECT id FROM resources WHERE {where} ORDER BY created_at LIMIT ?",
                                               (*params, limit))]

    def _try_ocr(self, path: Path, ext: str, fallback: tuple) -> tuple:
        """(status, detail, text, tool, confidence): OCR when it is switched on and installed; otherwise, or when it
        fails or reads less than the text layer, the non-OCR result (a tool name starting 'tesseract' marks it tried)."""
        s = self.ocr_settings()
        if not s["enabled"] or not ocr.tesseract_path():
            why = "OCR is off" if not s["enabled"] else "OCR needs tesseract, which is not installed"
            return (fallback[0], f"{fallback[1]}; {why}", *fallback[2:], None)
        try:
            r = ocr.ocr_file(path, ext, s["lang"])
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
            return (fallback[0], f"{fallback[1]}; OCR failed: {str(e)[:200]}", fallback[2], OCR_FAILED_TOOL, None)
        if not r.text.strip() or len(r.text.strip()) <= len(fallback[2] or ""):
            return (fallback[0], f"{fallback[1]}; OCR found no more text", fallback[2], r.tool, None)
        low = r.confidence is None or r.confidence < ocr.LOW_CONFIDENCE
        detail = (f"OCR ({r.pages} page{'s' if r.pages != 1 else ''}), mean word confidence "
                  f"{r.confidence:.0f}%" if r.confidence is not None else "OCR, no confidence reported")
        return ("ocr_low_confidence" if low else "ocr", detail + ("; check it against the original" if low else ""),
                r.text.strip(), r.tool, r.confidence)

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
                (sha, len(data), content_type, target.relative_to(self.paths.resources).as_posix(), now_iso()),
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
        result = self._convert(row, filename)
        return self._save_extraction(sha, result)

    def _convert(self, row, filename):
        ext = Path(filename or "").suffix.lower()
        if not ext and row["content_type"]:
            ext = mimetypes.guess_extension(row["content_type"].split(";")[0].strip()) or ""
        status, detail, text, tool = self._run_extraction(self.paths.resources / row["stored_path"], ext, row["size"])
        conf = None
        if status in ("image_only", "needs_ocr"):
            status, detail, text, tool, conf = self._try_ocr(self.paths.resources / row["stored_path"], ext,
                                                             (status, detail, text, tool))
        return status, detail, text, tool, conf

    def _save_extraction(self, sha, result):
        status, detail, text, tool, conf = result
        extracted_path = None
        if text:
            out = self.paths.extracted / f"{sha}.md"
            fd, temp = tempfile.mkstemp(dir=self.paths.extracted)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(text)
                os.replace(temp, out)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            extracted_path = out.relative_to(self.paths.resources).as_posix()
        with self.db.tx() as c:
            c.execute(
                """UPDATE resources SET extraction_status=?, extraction_tool=?, extraction_detail=?,
                   extracted_path=?, extracted_chars=?, extracted_at=?, ocr_confidence=? WHERE id=?""",
                (status, tool, detail, extracted_path, len(text) if text else 0, now_iso(),
                 None if conf is None else round(conf, 1), sha),
            )
        return dict(self.db.one("SELECT * FROM resources WHERE id=?", (sha,)))

    def extract_bounded(self, sha: str, filename: str | None, *, should_stop,
                        timeout_seconds: float = 120) -> dict:
        """Convert in a disposable local process; only the parent commits results.

        The durable queue owns retry policy. Timeout/cancellation leave the resource
        unattempted, so a restart can retry it without accepting partial output.
        Neither document text nor converter diagnostics enter process logs.
        """
        from ..collector.client import Cancelled

        row = self.db.one("SELECT * FROM resources WHERE id=?", (validate_resource_id(sha),))
        if not row:
            raise NotFound("resource not found")
        if should_stop():
            raise Cancelled("document processing cancelled")
        if row["extraction_status"] not in ("not_attempted", "failed", "tool_unavailable"):
            return dict(row)
        if timeout_seconds <= 0:
            raise ValueError("extraction timeout must be positive")
        with tempfile.TemporaryDirectory(prefix="canvas-extract-") as directory:
            root = Path(directory)
            request = root / "request.json"
            result = root / "result.json"
            request.write_text(json.dumps({"db": self.db.path, "home": str(self.paths.home),
                                           "sha": sha, "filename": filename}), encoding="utf-8")
            env = os.environ.copy()
            env.update(TMPDIR=directory, TEMP=directory, TMP=directory)
            options = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            process = subprocess.Popen(self._extraction_command(request, result),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=env, **options)
            deadline = time.monotonic() + timeout_seconds
            try:
                while process.poll() is None:
                    if should_stop():
                        raise Cancelled("document processing cancelled")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("document processing exceeded its time limit")
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                if should_stop():
                    raise Cancelled("document processing cancelled")
                if process.returncode != 0 or not result.exists():
                    raise RuntimeError("document processing worker failed; retry is available")
                converted = json.loads(result.read_text(encoding="utf-8"))
                return self._save_extraction(sha, converted)
            finally:
                self._stop_extraction_process(process)

    @staticmethod
    def _extraction_command(request, result):
        return [sys.executable, "-m", "canvas_hub.extraction_worker", str(request), str(result)]

    @staticmethod
    def _stop_extraction_process(process):
        # POSIX children (including OCR helpers) inherit this dedicated group.
        # Windows taskkill /T terminates the worker and its descendant tree.
        if os.name == "nt":
            if process.poll() is None:
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait()

    def _run_extraction(self, path: Path, ext: str, size: int) -> tuple[str, str | None, str, str | None]:
        if ext in _MEDIA_TYPES:
            return "unsupported_media", "audio/video is stored but not transcribed", "", None
        if ext in _IMAGE_TYPES:
            return "image_only", "image content, no text layer", "", None
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
                "ocr_confidence": r["ocr_confidence"],
                "low_confidence": r["extraction_status"] == "ocr_low_confidence",
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
