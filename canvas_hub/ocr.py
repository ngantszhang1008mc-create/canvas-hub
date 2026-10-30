"""Optional OCR for photos and scanned PDFs, with a confidence flag per document.

Off by default (Data & settings → Text from scans and photos). It needs the
``tesseract`` program (macOS: ``brew install tesseract``); scanned PDFs also
need ``pdftoppm`` from poppler (``brew install poppler``). Nothing is sent
anywhere: both run locally on the stored original. ``CANVAS_HUB_TESSERACT`` and
``CANVAS_HUB_PDFTOPPM`` override where the programs are found.

Each document gets the mean confidence tesseract reports for the words it
read (0-100). Below ``LOW_CONFIDENCE`` the text is kept but flagged, because
OCR of handwriting, equations or low-resolution photos is often wrong.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

LOW_CONFIDENCE = 70.0
MAX_PAGES = 40
PAGE_TIMEOUT = 90           # seconds per page
_LANG = re.compile(r"^[a-z_]{3,8}(\+[a-z_]{3,8}){0,3}$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}


@dataclass
class OcrResult:
    text: str
    confidence: float | None     # mean word confidence, None when no words were found
    pages: int
    tool: str


def tesseract_path() -> str | None:
    return os.environ.get("CANVAS_HUB_TESSERACT") or shutil.which("tesseract")


def pdftoppm_path() -> str | None:
    return os.environ.get("CANVAS_HUB_PDFTOPPM") or shutil.which("pdftoppm")


def status() -> dict:
    t = tesseract_path()
    version = None
    if t:
        try:
            out = subprocess.run([t, "--version"], capture_output=True, text=True, timeout=10, check=False)
            version = ((out.stdout or out.stderr).splitlines() or [""])[0].strip() or None
        except (OSError, subprocess.SubprocessError):
            t = None
    return {"tesseract": bool(t), "version": version, "pdf": bool(pdftoppm_path()),
            "install_hint": None if t else "Install tesseract (macOS: brew install tesseract; scanned PDFs also need "
                                           "brew install poppler), then restart Canvas Hub."}


def valid_lang(lang: str) -> str:
    if not isinstance(lang, str) or not _LANG.match(lang):
        raise ValueError("language must look like eng or eng+chi_tra")
    return lang


def _parse_tsv(tsv: str) -> tuple[str, list[float]]:
    """Rebuild text from tesseract's TSV (one row per word) and collect word confidences."""
    lines: dict[tuple, list[str]] = {}
    paras: dict[tuple, list[tuple]] = {}
    confs: list[float] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        word = cols[11].strip()
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if not word or conf < 0:
            continue
        confs.append(conf)
        para = (int(cols[1]), int(cols[2]), int(cols[3]))
        line = (*para, int(cols[4]))
        if line not in lines:
            lines[line] = []
            paras.setdefault(para, []).append(line)
        lines[line].append(word)
    text = "\n\n".join("\n".join(" ".join(lines[ln]) for ln in paras[p]) for p in paras)
    return text, confs


def _ocr_image(tess: str, image: Path, lang: str) -> tuple[str, list[float]]:
    out = subprocess.run([tess, str(image), "stdout", "-l", lang, "--psm", "3", "tsv"], capture_output=True, text=True, check=False,
                         timeout=PAGE_TIMEOUT)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "tesseract failed").strip()[:300])
    return _parse_tsv(out.stdout)


def ocr_file(path: Path, ext: str, lang: str = "eng") -> OcrResult:
    """OCR an image or a PDF (page by page, up to MAX_PAGES). Raises RuntimeError when it cannot run."""
    tess = tesseract_path()
    if not tess:
        raise RuntimeError("tesseract is not installed")
    lang = valid_lang(lang)
    st = status()
    tool = st["version"] or "tesseract"
    if ext in IMAGE_EXTS:
        text, confs = _ocr_image(tess, path, lang)
        return OcrResult(text, sum(confs) / len(confs) if confs else None, 1, tool)
    if ext != ".pdf":
        raise RuntimeError(f"OCR does not handle {ext or 'this file type'}")
    ppm = pdftoppm_path()
    if not ppm:
        raise RuntimeError("scanned PDFs need pdftoppm (poppler)")
    with tempfile.TemporaryDirectory() as td:
        out = subprocess.run([ppm, "-r", "200", "-gray", "-png", "-l", str(MAX_PAGES), str(path), str(Path(td) / "p")],
                             capture_output=True, text=True, timeout=PAGE_TIMEOUT * 2, check=False)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or "pdftoppm failed").strip()[:300])
        pages = sorted(Path(td).glob("p*.png"))
        texts, confs = [], []
        for pg in pages:
            t, c = _ocr_image(tess, pg, lang)
            texts.append(t)
            confs.extend(c)
    return OcrResult("\n\n".join(f"<!-- page {i + 1} -->\n{t}" for i, t in enumerate(texts) if t.strip()),
                     sum(confs) / len(confs) if confs else None, len(pages), f"{tool} + pdftoppm")
