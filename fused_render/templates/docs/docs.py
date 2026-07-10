# /// script
# dependencies = ["pypandoc-binary"]
# ///
"""Backend for the docs preview template (fused-render).

The document is one user file — a Microsoft Word .docx file — converted
to/from the editor's HTML by pandoc. Everything the editor offers is limited
to what pandoc's HTML<->docx round-trip genuinely preserves, so the .docx is
the single source of truth: text, headings, lists, tables, images, math, and
comments (written as native Word comments via pandoc's comment-start/
comment-end spans). Version history lives in the file's JSON sidecar
(<file>.json under the "docs" key). This script only holds what genuinely
needs Python: pandoc conversion, PDF via the typst compiler, and browsing the
filesystem for "Save a copy…". Params arrive as strings; annotate.

Error handling: raise. The executor turns any exception into the error payload
the page toasts; only structured, expected outcomes (conflict, missing typst)
are returned as data.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "docs.py")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))
from procutil import pid_alive as _pid_alive

CACHE_ROOT = os.path.join(os.path.expanduser("~"), ".fused-render", "cache", "docs")
BIN_DIR = os.path.expanduser(os.path.join("~", ".fused-render", "bin"))
TYPST_INSTALL_DIR = os.path.join(CACHE_ROOT, "_typst_install")
TYPST_VERSION = "v0.13.1"

# The editor emits \(…\)/\[…\] math delimiters (tex_math_single_backslash) as
# well as $…$; the import side asks for --mathjax so equations come back the
# same way. --track-changes=all surfaces Word comments as comment-start/
# comment-end spans; --embed-resources returns images as data URIs instead of
# dangling media/ paths.
HTML_FROM = "html+tex_math_dollars+tex_math_single_backslash"

# pandoc target format per requested extension (typst/pdf handled specially).
PANDOC_TO = {
    "docx": "docx",
    "md": "gfm",
    "markdown": "gfm",
    "html": "html",
    "latex": "latex",
    "tex": "latex",
    "epub": "epub3",
    "odt": "odt",
    "rtf": "rtf",
}


def _pandoc(args, input_text=None):
    """Run the bundled pandoc. Returns stdout bytes; raises on failure."""
    import pypandoc
    exe = pypandoc.get_pandoc_path()
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if input_text is not None:
        kw["input"] = input_text.encode("utf-8")
    p = subprocess.run([exe, *args], **kw)
    if p.returncode != 0:
        raise RuntimeError("pandoc failed: " + p.stderr.decode("utf-8", "replace")[:800])
    return p.stdout


def _typst_bin():
    found = shutil.which("typst")
    if found:
        return found
    candidate = os.path.join(BIN_DIR, "typst.exe" if os.name == "nt" else "typst")
    return candidate if os.path.exists(candidate) else None


def _install_progress():
    path = os.path.join(TYPST_INSTALL_DIR, "progress.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("done") and not _pid_alive(data.get("pid", -1)):
        data["done"] = True
        data["error"] = data.get("error") or "installer exited unexpectedly"
    return data


def _typst_status():
    return {"available": _typst_bin() is not None, "path": _typst_bin(),
            "progress": _install_progress()}


def _typst_install():
    prog = _install_progress()
    if _typst_bin() or (prog and not prog.get("done")):
        return _typst_status()
    os.makedirs(TYPST_INSTALL_DIR, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)
    worker = os.path.join(HERE, "install_worker.py")
    logf = open(os.path.join(TYPST_INSTALL_DIR, "worker.log"), "ab")
    detach_kwargs = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    child = subprocess.Popen(
        [sys.executable, worker, TYPST_VERSION, BIN_DIR, TYPST_INSTALL_DIR],
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, cwd=HERE, **detach_kwargs)
    logf.close()
    stamp = os.path.join(TYPST_INSTALL_DIR, "progress.json")
    with open(stamp + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"stage": "spawn", "pct": 0, "detail": "starting installer",
                   "done": False, "error": None, "pid": child.pid}, f)
    os.replace(stamp + ".tmp", stamp)
    time.sleep(0.3)
    return _typst_status()


def _cache_dir(file: str) -> str:
    # One subfolder per document (keyed by its own path) so exports from
    # different documents never collide; lives outside the template folder.
    digest = hashlib.sha256(os.path.abspath(file).encode("utf-8")).hexdigest()[:16]
    d = os.path.join(CACHE_ROOT, digest)
    os.makedirs(d, exist_ok=True)
    return d


# -------------------------------------------------------------------- dispatcher
def main(action: str = "export", file: str = "", html: str = "", title: str = "",
         fmt: str = "pdf", path: str = "", directory: str = "", expected_mtime: str = "",
         sha: str = ""):
    if action == "warmup":
        import pypandoc
        return {"pandoc": pypandoc.get_pandoc_version()}

    if action == "typst_status":
        return _typst_status()

    if action == "typst_install":
        return _typst_install()

    # ---- directory listing for the "Save a copy…" browser
    if action == "listdir":
        base = os.path.abspath(os.path.expanduser(path)) if path else os.path.expanduser("~")
        if not os.path.isdir(base):
            base = os.path.dirname(base) or os.path.expanduser("~")
        dirs, files = [], []
        for nm in sorted(os.listdir(base), key=str.lower):
            if nm.startswith("."):
                continue
            if os.path.isdir(os.path.join(base, nm)):
                dirs.append(nm)
            elif nm.lower().endswith(".docx"):
                files.append(nm)
        parent = os.path.dirname(base) or base   # dirname(root) == root, so "up" stops there
        # forward slashes on every platform: the browser's crumb/join logic is "/"-based
        return {"path": base.replace(os.sep, "/"), "parent": parent.replace(os.sep, "/"),
                "dirs": dirs, "files": files,
                "home": os.path.expanduser("~").replace(os.sep, "/")}

    # ---- open an existing .docx: convert to HTML for the editor
    if action == "import":
        if not file or not os.path.isfile(file):
            raise FileNotFoundError(f"file not found: {file}")
        out = _pandoc(["-f", "docx", "-t", "html+tex_math_dollars", "--mathjax",
                       "--track-changes=all", "--embed-resources",
                       "--wrap=none", file])
        return {"html": out.decode("utf-8", "replace"), "mtime": os.path.getmtime(file)}

    # ---- export/convert: browser sends serialized HTML, we fan out to formats
    if action == "export":
        os.makedirs(CACHE_ROOT, exist_ok=True)
        if not html:
            raise ValueError("no html to export")
        out_dir = _cache_dir(file)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", (title or "document")).strip("_") or "document"
        ext = fmt.lower()
        if ext == "pdf":
            typ_bin = _typst_bin()
            if not typ_bin:
                return {"error": "typst is not installed", "missing_typst": True}
            typ = _pandoc(["-f", HTML_FROM, "-t", "typst", "--wrap=none"],
                          input_text=html)
            typ_path = os.path.join(out_dir, stem + ".typ")
            with open(typ_path, "wb") as f:
                f.write(typ)
            out_path = os.path.join(out_dir, stem + ".pdf")
            subprocess.run([typ_bin, "compile", typ_path, out_path],
                           check=True, capture_output=True)
        elif ext in PANDOC_TO:
            out_ext = {"latex": "tex", "markdown": "md"}.get(ext, ext)
            out_path = os.path.join(out_dir, f"{stem}.{out_ext}")
            data = _pandoc(["-f", HTML_FROM, "-t", PANDOC_TO[ext],
                            "--wrap=none", "--standalone", "-o", out_path],
                           input_text=html)
            if not os.path.exists(out_path):  # some writers go to stdout
                with open(out_path, "wb") as f:
                    f.write(data)
        else:
            raise ValueError(f"unsupported format: {fmt}")
        return {"path": out_path, "name": os.path.basename(out_path), "size": os.path.getsize(out_path)}

    # ---- save the bound .docx in place, with a conflict lock
    if action == "save":
        if not html:
            raise ValueError("nothing to save")
        file = os.path.abspath(file)
        if expected_mtime and os.path.exists(file):
            on_disk = os.path.getmtime(file)
            if abs(on_disk - float(expected_mtime)) > 1e-6:
                return {"conflict": True, "mtime": on_disk}
        tmp = file + ".tmp"
        try:
            _pandoc(["-f", HTML_FROM, "-t", "docx", "--wrap=none",
                     "--standalone", "-o", tmp], input_text=html)
            os.replace(tmp, file)
        finally:
            # cleanup only — errors still propagate; on success the replace
            # already consumed the tmp
            with contextlib.suppress(OSError):
                os.remove(tmp)
        # Version snapshot for the history panel: content-addressed blob in the
        # cache, deduped by sha and capped at 200 — the sidecar holds metadata
        # only, so image-heavy documents can't balloon it. Best-effort: a
        # snapshot hiccup must not fail a document save that already landed.
        version_sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
        vdir = os.path.join(_cache_dir(file), "versions")
        try:
            os.makedirs(vdir, exist_ok=True)
            vpath = os.path.join(vdir, version_sha + ".html")
            if not os.path.exists(vpath):
                with open(vpath, "w", encoding="utf-8") as f:
                    f.write(html)
        except OSError:
            version_sha = ""
        else:
            # The cap prune is per-file best-effort, separate from the write:
            # one locked old blob must neither un-record the version that just
            # landed nor block pruning the rest.
            with contextlib.suppress(OSError):
                blobs = sorted(os.scandir(vdir), key=lambda e: e.stat().st_mtime)
                for e in blobs[:-200]:
                    with contextlib.suppress(OSError):
                        os.unlink(e.path)
        return {"path": file.replace(os.sep, "/"), "mtime": os.path.getmtime(file),
                "version_sha": version_sha}

    # ---- fetch a version snapshot recorded by a previous save
    if action == "version_html":
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError(f"bad version id: {sha}")
        vpath = os.path.join(_cache_dir(file), "versions", sha + ".html")
        with open(vpath, encoding="utf-8") as f:
            return {"html": f.read()}

    # ---- "Save a copy…": write a .docx to a location the user browsed to
    if action == "save_as":
        if not html:
            raise ValueError("nothing to save")
        # os.path.join resolves it: a full path in `path` wins, a bare name joins
        # onto `directory` — handles absolute/relative and either separator.
        raw = os.path.join(directory, path) if directory else path
        dest = os.path.abspath(os.path.expanduser(raw))
        if not dest.lower().endswith(".docx"):
            dest += ".docx"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _pandoc(["-f", HTML_FROM, "-t", "docx", "--wrap=none",
                 "--standalone", "-o", dest], input_text=html)
        return {"path": dest.replace(os.sep, "/"), "name": os.path.basename(dest)}

    raise ValueError(f"unknown action: {action}")
