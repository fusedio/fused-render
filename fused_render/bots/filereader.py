"""Read a bot's files for the model: the `readfile` tool.

A bot's FILES list (attached by the user, downloaded by Chrome, saved by an
earlier task) was name-and-size only; the model could `upload` a file but
never look inside it. This module turns one such file into something the
model can read, with zero new dependencies (pyproject: "every megabyte has
to earn its place"):

  text-like  (txt md csv tsv json xml html log yaml toml py js …)
             -> UTF-8 text, paged: `page` picks a PAGE_CHARS window, the
                result says how many pages there are.
  images     (png jpg jpeg gif webp heic bmp tiff)
             -> a JPEG the model sees as an image, downscaled with macOS
                `sips` (the same tool browser.py uses for screenshots).
  pdf        -> per-page text through PDFKit (PyObjC Quartz, present in the
                packaged app and the dev venv); a page without a text layer
                (a scan) is rendered to a JPEG instead so the model reads it
                visually. Without Quartz, page 1 is rasterised with `sips`.
  rtf doc docx odt
             -> plain text through macOS `textutil`.

Everything is looked up by name through `bot.resolve_file`, which only
searches the bot's own folders (and the user's explicit paths), so the model
never gets a filesystem walk.  `read_file(path, page)` returns
`(text, jpeg_bytes_or_None)`; `kind(path)` is the classifier.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

PAGE_CHARS = 20_000       # one page of a text result
MAX_IMAGE_WIDTH = 1400    # px; a screenshot-sized JPEG the model can read
PDF_PAGE_CHARS = 20_000   # one PDF page's text, in case of a giant page

TEXT_EXT = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xml", ".html", ".htm",
    ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".css", ".sql", ".sh", ".rst", ".tex", ".srt", ".vtt", ".ics", ".eml", ".svg",
})
IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"})
RICH_EXT = frozenset({".rtf", ".rtfd", ".doc", ".docx", ".odt", ".wordml", ".webarchive"})
PDF_EXT = frozenset({".pdf"})


def kind(path: str) -> str:
    """'text' | 'image' | 'pdf' | 'rich' | 'binary', by extension then by
    sniffing (a downloaded file often has no useful extension)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        return "text"
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in RICH_EXT:
        return "rich"
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return "binary"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"BM")):
        return "image"
    if head.startswith(b"{\\rtf"):
        return "rich"
    if not head:
        return "text"
    if b"\x00" in head:
        return "binary"
    try:
        head.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "binary"


def read_file(path: str, page: int = 1) -> tuple[str, bytes | None]:
    """The tool result for one file: `(text, jpeg or None)`. Never raises for
    an unreadable file; the text starts with "error:" instead."""
    if not os.path.isfile(path):
        return f"error: not a file: {os.path.basename(path)}", None
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    name = os.path.basename(path)
    size = os.path.getsize(path)
    k = kind(path)
    try:
        if k == "text":
            return _read_text(path, name, size, page), None
        if k == "image":
            img = _image_jpeg(path)
            if not img:
                return f"error: could not convert {name} to an image the model can see", None
            return f"IMAGE {name} ({size} bytes) is attached.", img
        if k == "pdf":
            return _read_pdf(path, name, page)
        if k == "rich":
            txt = _textutil(path)
            if txt is None:
                return f"error: could not extract text from {name} (textutil failed)", None
            return _paged(txt, f"FILE {name} ({size} bytes, converted to text)", page), None
    except Exception as e:  # noqa: BLE001
        return f"error: reading {name}: {type(e).__name__}: {e}", None
    return (f"error: {name} ({size} bytes) is a binary file this tool cannot read "
            f"(text, images, PDF, RTF/DOC/DOCX are supported). You can still `upload` it."), None


# -------------------------------------------------------------------- text ---
def _read_text(path: str, name: str, size: int, page: int) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    txt = raw.decode("utf-8", errors="replace")
    return _paged(txt, f"FILE {name} ({size} bytes)", page)


def _paged(txt: str, head: str, page: int) -> str:
    txt = txt.replace("\r\n", "\n")
    total = max(1, (len(txt) + PAGE_CHARS - 1) // PAGE_CHARS)
    page = min(page, total)
    chunk = txt[(page - 1) * PAGE_CHARS: page * PAGE_CHARS]
    if total > 1:
        head += f", part {page} of {total} (pass page=N for the others)"
    return f"{head}:\n{chunk}" if chunk.strip() else f"{head}: (empty)"


# ------------------------------------------------------------------ images ---
def _image_jpeg(path: str, max_width: int = MAX_IMAGE_WIDTH) -> bytes | None:
    """A JPEG no wider than max_width, through sips. The original is never
    touched."""
    if not shutil.which("sips"):
        return None
    with tempfile.TemporaryDirectory(prefix="fb-read-") as d:
        out = os.path.join(d, "img.jpg")
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
               "--resampleWidth", str(max_width), path, "--out", out]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20, close_fds=False)
            if r.returncode != 0 or not os.path.isfile(out):
                # A small image fails --resampleWidth upscaling on some sips builds; retry without.
                r = subprocess.run(["sips", "-s", "format", "jpeg", path, "--out", out], capture_output=True, timeout=20, close_fds=False)
                if r.returncode != 0 or not os.path.isfile(out):
                    return None
            with open(out, "rb") as f:
                return f.read()
        except Exception:  # noqa: BLE001
            return None


# --------------------------------------------------------------------- pdf ---
def _quartz():
    try:
        import Quartz  # PyObjC; part of the app's dependency tree
        return Quartz
    except Exception:  # noqa: BLE001
        return None


def _read_pdf(path: str, name: str, page: int) -> tuple[str, bytes | None]:
    Q = _quartz()
    if Q is None:
        # No PDFKit: rasterise page 1 with sips and say so.
        img = _image_jpeg(path)
        if not img:
            return f"error: cannot read {name}: no PDF reader available on this Mac", None
        return (f"PDF {name}: page 1 is attached as an image (text extraction is unavailable here; "
                "other pages cannot be shown)."), img
    url = Q.NSURL.fileURLWithPath_(path)
    doc = Q.PDFDocument.alloc().initWithURL_(url)
    if doc is None:
        return f"error: {name} is not a readable PDF (damaged or encrypted)", None
    n = doc.pageCount()
    if n == 0:
        return f"PDF {name}: no pages", None
    page = min(max(1, page), n)
    pg = doc.pageAtIndex_(page - 1)
    txt = (pg.string() or "").strip()
    head = f"PDF {name}, page {page} of {n}" + (" (pass page=N for the others)" if n > 1 else "")
    if len(txt) >= 40:
        if len(txt) > PDF_PAGE_CHARS:
            txt = txt[:PDF_PAGE_CHARS] + " …[truncated]"
        return f"{head}:\n{txt}", None
    # A scan or a drawing: render the page so the model reads it as an image.
    img = _render_pdf_page(Q, pg)
    if img:
        return f"{head}: no text layer; the page is attached as an image.", img
    return f"{head}: (no extractable text)" + (f"\n{txt}" if txt else ""), None


def _render_pdf_page(Q, pg, width: int = MAX_IMAGE_WIDTH) -> bytes | None:
    """One PDFPage -> JPEG bytes via PDFKit's thumbnail renderer + sips."""
    try:
        box = pg.boundsForBox_(Q.kPDFDisplayBoxMediaBox)
        w, h = box.size.width, box.size.height
        if w <= 0 or h <= 0:
            return None
        scale = width / w
        size = Q.NSMakeSize(w * scale, h * scale)
        nsimg = pg.thumbnailOfSize_forBox_(size, Q.kPDFDisplayBoxMediaBox)
        if nsimg is None:
            return None
        tiff = nsimg.TIFFRepresentation()
        if tiff is None:
            return None
        with tempfile.TemporaryDirectory(prefix="fb-pdf-") as d:
            src = os.path.join(d, "page.tiff")
            with open(src, "wb") as f:
                f.write(bytes(tiff))
            return _image_jpeg(src, width)
    except Exception:  # noqa: BLE001
        return None


# -------------------------------------------------------------------- rich ---
def _textutil(path: str) -> str | None:
    if not shutil.which("textutil"):
        return None
    try:
        r = subprocess.run(["textutil", "-convert", "txt", "-stdout", path], capture_output=True, timeout=30, close_fds=False)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")
