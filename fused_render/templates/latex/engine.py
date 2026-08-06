# /// script
# dependencies = ["pypandoc-binary"]
# ///
# `pypandoc-binary`, NOT `pypandoc`: the two are the same import name and the
# same version, but the plain wheel is 0.0 MB of pure Python with NO pandoc
# executable, while pypandoc-binary is ~41 MB and ships one. This file calls
# `pypandoc.convert_file`, which needs the binary — so with the plain
# distribution the venv builds cleanly and then fails at RUNTIME on any
# machine without a system pandoc. docs/docs.py already declared the right one.
# This is why the header invariants cannot rest on "is it importable".
"""Backend for the latex template — a local, live-preview LaTeX viewer/editor.

One bare `main(action=...)` dispatcher (the fused-render contract; see the note
at the definition for why it is NOT @fused.udf). `_file` (the target `.tex`)
is the single source of truth on disk — never a parsed model — so an editor,
an agent, or git can all edit it directly and this module is just a lens that
compiles and indexes it.

Actions
  tectonic_status()                  -> {available, path, progress}
  tectonic_install()                 -> spawns the detached installer, {available, path, progress}
  tree(path)                         -> {root, main, tree:[{path,rel,size,ext,editable}]}
  browse(path=dir, template=ext)     -> {dir, parent, entries}   file-browser listing
  find(path=root,name=q,template=ext)-> {root, results, truncated} recursive search
  compile(path)                      -> {ok, pdf, log_tail, errors:[{file,line,severity,message}], seconds}
  outline(path)                      -> {sections, labels, cites_used, envs, inputs, bib_resources}
  bib(path)                          -> {entries:[{key,type,title,author,year,file}]}
  synctex(path,line,target)          -> {page, vfrac, hits}      forward search
  export(path,template=fmt)          -> {path,name,size}         pdf|docx|html|md via pandoc

Compilation shells out to a `tectonic` binary resolved at runtime (PATH, else
~/.fused-render/bin/) — never vendored in the package. Its package/font cache
and per-document build output both live under ~/.fused-render/cache/latex/, so
nothing is ever written into this template's own folder or uninvited next to
the user's file.
"""
import glob
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

# NOTE: bare `def main` (no @fused.udf) is deliberate — under the built-in
# executor the worker calls main() by its own signature; @fused.udf hides that
# signature and triggers a hosted-auth flow that times out.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "engine.py")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))
from procutil import pid_alive as _pid_alive

CACHE_ROOT = os.path.expanduser("~/.fused-render/cache/latex")
TECTONIC_CACHE = os.path.join(CACHE_ROOT, "tectonic-cache")  # shared package/font cache
BUILDS = os.path.join(CACHE_ROOT, "builds")                  # per-doc aux output, hashed
EXPORTS = os.path.join(CACHE_ROOT, "exports")                # per-doc pandoc exports, hashed
INSTALL_DIR = os.path.join(CACHE_ROOT, "_install")           # tectonic download staging
WARM_DIR = os.path.join(CACHE_ROOT, "_warm")                 # cache-warm worker progress staging
WARM_MARKER = os.path.join(TECTONIC_CACHE, ".warmed")        # written once the common packages are cached
BIN_DIR = os.path.expanduser("~/.fused-render/bin")          # user-owned install location
LIBRARY = os.path.expanduser("~/.fused-render/latex/projects")  # user-owned; one folder per project created from Home

TECTONIC_VERSION = "0.16.9"

TEX_EXT = (".tex", ".ltx", ".latex")


# --------------------------------------------------------------- tectonic ---
def _tectonic_bin_name():
    return "tectonic.exe" if platform.system() == "Windows" else "tectonic"


def _tectonic_bin():
    found = shutil.which("tectonic")
    if found:
        return found
    candidate = os.path.join(BIN_DIR, _tectonic_bin_name())
    return candidate if os.path.exists(candidate) else None


def _install_progress():
    path = os.path.join(INSTALL_DIR, "progress.json")
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


def _tectonic_status():
    return {"available": _tectonic_bin() is not None, "path": _tectonic_bin(),
            "progress": _install_progress()}


def _tectonic_install():
    prog = _install_progress()
    if _tectonic_bin() or (prog and not prog.get("done")):
        return _tectonic_status()
    os.makedirs(INSTALL_DIR, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)
    worker = os.path.join(HERE, "install_worker.py")
    logf = open(os.path.join(INSTALL_DIR, "worker.log"), "ab")
    # detach: outlive this 30 s subprocess. start_new_session (setsid) is
    # POSIX-only and silently a no-op on Windows, where DETACHED_PROCESS +
    # CREATE_NEW_PROCESS_GROUP is the equivalent (mirrors usd_studio).
    detach_kwargs = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    child = subprocess.Popen(
        [sys.executable, worker, TECTONIC_VERSION, BIN_DIR, INSTALL_DIR],
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, cwd=HERE, **detach_kwargs)
    logf.close()
    stamp = os.path.join(INSTALL_DIR, "progress.json")
    with open(stamp + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"stage": "spawn", "pct": 0, "detail": "starting installer",
                   "done": False, "error": None, "pid": child.pid}, f)
    os.replace(stamp + ".tmp", stamp)
    time.sleep(0.3)
    return _tectonic_status()


# ------------------------------------------------------------- cache warm ---
# A cold compile fetches the packages/fonts a document needs (~30 MB, ~2 min) —
# far beyond the 60s runPython budget, so it can never finish inside a compile.
# When that happens we compile the document in a detached worker (no timeout)
# into its build dir; the page polls warm_status and, when it finishes, a plain
# recompile serves the produced PDF. `.warmed` records that the cache has been
# populated once, so a fresh install's first compile skips the doomed inline
# attempt and defers straight to the background compile.
def _cache_warm() -> bool:
    return os.path.exists(WARM_MARKER)


def _warm_progress():
    path = os.path.join(WARM_DIR, "progress.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("done") and not _pid_alive(data.get("pid", -1)):
        data["done"] = True
        data["error"] = data.get("error") or "cache warm exited unexpectedly"
    return data


def _warm_running() -> bool:
    prog = _warm_progress()
    return bool(prog and not prog.get("done"))


def _ensure_warming(main_path: str):
    """Spawn the detached worker to compile `main_path` (fetching whatever
    packages it needs, no timeout) into its build dir, unless one is already
    running. The worker drops the `.warmed` marker on success."""
    bin_path = _tectonic_bin()
    if not bin_path or not main_path or _warm_running():
        return
    os.makedirs(WARM_DIR, exist_ok=True)
    worker = os.path.join(HERE, "warm_worker.py")
    args = [sys.executable, worker, bin_path, TECTONIC_CACHE, WARM_DIR,
            os.path.abspath(main_path), _build_dir_for(main_path)]
    logf = open(os.path.join(WARM_DIR, "worker.log"), "ab")
    detach_kwargs = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    child = subprocess.Popen(
        args, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, cwd=HERE, **detach_kwargs)
    logf.close()
    stamp = os.path.join(WARM_DIR, "progress.json")
    with open(stamp + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"stage": "spawn", "detail": "starting package fetch",
                   "done": False, "error": None, "pid": child.pid}, f)
    os.replace(stamp + ".tmp", stamp)


# ---------------------------------------------------------------- helpers ---
def _read_text(p: str) -> str:
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def _build_dir_for(main_path: str) -> str:
    """Aux output (.aux/.log/.pdf/.synctex.gz) is quarantined here, never in
    the user's own directory — one folder per document, keyed by its abs path."""
    h = hashlib.sha1(os.path.abspath(main_path).encode()).hexdigest()[:16]
    d = os.path.join(BUILDS, h)
    os.makedirs(d, exist_ok=True)
    return d


def _export_dir_for(main_path: str) -> str:
    h = hashlib.sha1(os.path.abspath(main_path).encode()).hexdigest()[:16]
    d = os.path.join(EXPORTS, h)
    os.makedirs(d, exist_ok=True)
    return d


# -------------------------------------------------------------- compile + log
_ERR_RE = re.compile(r"^(error|warning):\s*(?:([^:\n]+?):(\d+):\s*)?(.*)$")


def _parse_tectonic_stderr(stderr: str):
    """Tectonic prints machine-friendly lines: `error: file:line: message` and
    `warning: ...`. Turn them into structured diagnostics; skip the noisy
    `note:` download/rerun chatter."""
    out = []
    for line in stderr.splitlines():
        m = _ERR_RE.match(line.strip())
        if not m:
            continue
        sev, f, ln, msg = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        if not msg:
            continue
        out.append({
            "file": os.path.basename(f) if f else "",
            "line": int(ln) if ln else 0,
            "severity": sev,
            "message": msg,
        })
    return out


def _parse_tex_log(log_path: str):
    """Fallback/enrichment: pull `! ...` errors (+ their `l.NN` line) and
    LaTeX/Overfull warnings out of the traditional TeX .log."""
    out = []
    if not os.path.exists(log_path):
        return out
    txt = _read_text(log_path)
    lines = txt.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("! "):
            msg = ln[2:].strip()
            lineno = 0
            for j in range(i + 1, min(i + 12, len(lines))):
                lm = re.match(r"l\.(\d+)", lines[j])
                if lm:
                    lineno = int(lm.group(1))
                    break
            out.append({"file": "", "line": lineno, "severity": "error", "message": msg})
        elif ln.startswith("LaTeX Warning:"):
            msg = ln[len("LaTeX Warning:"):].strip()
            lm = re.search(r"on input line (\d+)", ln)
            out.append({"file": "", "line": int(lm.group(1)) if lm else 0,
                        "severity": "warning", "message": msg})
        i += 1
    return out


def _dedup(diags):
    seen, out = set(), []
    for d in diags:
        k = (d["file"], d["line"], d["message"][:80])
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


# --- mount-safe directory listing ------------------------------------------
# A kernel listing (os.listdir/os.scandir/os.walk) on a path under a remote
# rclone NFS mount forces rclone to enumerate the ENTIRE parent S3 prefix and
# can DROP the mount, wedging the server. This template stays mount-AGNOSTIC:
# it never imports shell.mounts and never matches mount paths. Instead the UI
# passes `src` (server origin + /api/fs/raw?path=) and we ask the server whether
# a path is remote (/api/fs/stat); if so we list it via the mount-routed,
# paginated /api/fs/list — never through the kernel. _server_url + _stat are
# copied verbatim from pyramid/overview_pyramid.py.
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq


def _server_url(src, endpoint, path):
    u = _urlparse.urlsplit(src)
    return (f"{u.scheme}://{u.netloc}{endpoint}?path="
            + _urlparse.quote(path))


def _stat(src, path):
    url = _server_url(src, "/api/fs/stat", path)
    try:
        with _urlreq.urlopen(url, timeout=10) as r:
            return ("ok", json.load(r))
    except _urlerr.HTTPError as e:
        if e.code == 404:
            return ("missing", None)
        return ("unreachable", None)
    except Exception:  # noqa: BLE001 — any network error -> fall back to local
        return ("unreachable", None)


def _remote_dir(src, path):
    """True iff the server says `path` is a remote (mount-backed) directory.
    No src / unreachable / missing -> False (presume local, kernel listing OK)."""
    if not src or not path:
        return False
    status, meta = _stat(src, path)
    return status == "ok" and bool(meta.get("remote"))


def _list_remote(src, path, cap=5000):
    """List `path` via the server's mount-routed, paginated /api/fs/list — never
    the kernel. Follows the cursor up to `cap` entries so a huge S3 prefix
    returns a bounded page set instead of tripping the NFS deadman."""
    entries, cursor, truncated = [], "", False
    while True:
        url = _server_url(src, "/api/fs/list", path)
        if cursor:
            url += "&cursor=" + _urlparse.quote(cursor)
        with _urlreq.urlopen(url, timeout=30) as r:
            payload = json.load(r)
        entries.extend(payload.get("entries") or [])
        truncated = bool(payload.get("truncated"))
        cursor = payload.get("cursor") or ""
        if len(entries) >= cap or not truncated or not cursor:
            break
    return entries, truncated


def _newest_source_mtime(root):
    """Newest mtime under root (same exclusions as _tree). None means the dir
    is too big to scan cheaply — treat as unknown and just compile."""
    newest, seen = 0.0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".build", ".git", "__pycache__")]
        for fn in filenames:
            seen += 1
            if seen > 2000:
                return None
            newest = max(newest, os.path.getmtime(os.path.join(dirpath, fn)))
    return newest


def _compile(main_path: str, synctex: bool = True, force: bool = False, remote: bool = False):
    main_path = os.path.abspath(main_path)
    if not os.path.exists(main_path):
        return {"ok": False, "error": f"no such file: {main_path}", "errors": []}
    bin_path = _tectonic_bin()
    if not bin_path:
        return {"ok": False, "missing_tectonic": True, "errors": [],
                "error": "Tectonic isn't installed — install it to compile."}
    os.makedirs(TECTONIC_CACHE, exist_ok=True)
    # Fresh install (nothing cached yet): the fetch can't fit the compile budget,
    # so skip the doomed inline attempt and compile in the background instead.
    if not _cache_warm():
        _ensure_warming(main_path)
        return {"ok": False, "warming": True, "progress": _warm_progress(), "errors": [],
                "error": "Preparing the LaTeX packages (one-time, ~1–2 min). "
                         "Your document compiles automatically when they're ready."}
    build = _build_dir_for(main_path)
    stem = os.path.splitext(os.path.basename(main_path))[0]
    pdf = os.path.join(build, stem + ".pdf")
    # A compile costs ~10s (tectonic runs several passes), so skip it when the
    # last PDF is newer than every file under the doc's directory — page
    # reloads become instant. `force` (the Recompile button) always runs it.
    if not force:
        if os.path.exists(pdf):
            # Never os.walk a mount-backed project dir (unbounded S3 enumeration
            # can drop the mount). Remote -> treat mtimes as unknown and compile.
            newest = None if remote else _newest_source_mtime(os.path.dirname(main_path))
            if newest is not None and os.path.getmtime(pdf) > newest:
                diags = _dedup(_parse_tex_log(os.path.join(build, stem + ".log")))
                return {"ok": True, "pdf": pdf,
                        "synctex": os.path.join(build, stem + ".synctex.gz"),
                        "log_tail": "", "errors": diags, "seconds": 0.0,
                        "cached": True}
    env = dict(os.environ, TECTONIC_CACHE_DIR=TECTONIC_CACHE)
    # We do NOT pass --only-cached: the Tectonic subprocess is server-side (not
    # the sandboxed browser iframe), so it may reach the package repo. A warm
    # cache makes the common case instant/offline; anything missing self-heals
    # with a small fetch that stays well under the 28s budget below; fully
    # offline + uncached still fails fast with a clear "not found" diagnostic.
    cmd = [bin_path, "-X", "compile", "--keep-logs", "--outdir", build]
    if synctex:
        cmd.append("--synctex")
    cmd.append(main_path)
    # Drop any PDF from a previous compile first: Tectonic keeps the old PDF when
    # a run fails, so its mere presence can't be read as this run having produced
    # one — a crash must not leave a stale PDF looking current.
    try:
        os.remove(pdf)
    except OSError:
        pass
    t0 = time.time()
    try:
        # cwd = the .tex file's own directory, so relative \input/\includegraphics
        # resolve the way the author expects. CREATE_NO_WINDOW: the server is
        # windowless (pythonw), so a console subprocess would otherwise flash
        # a terminal window per compile.
        p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           cwd=os.path.dirname(main_path), timeout=28,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        # Didn't fit the budget — almost always a cold fetch of this document's
        # packages. Compile it in the background (no timeout) so the cache warms
        # and the PDF lands in the build dir; the page polls and serves it.
        _ensure_warming(main_path)
        return {"ok": False, "warming": True, "progress": _warm_progress(), "errors": [],
                "error": "Fetching the LaTeX packages this document needs (one-time). "
                         "It compiles automatically when they're ready."}
    seconds = round(time.time() - t0, 2)
    logf = os.path.join(build, stem + ".log")
    diags = _dedup(_parse_tectonic_stderr(p.stderr) + _parse_tex_log(logf))
    # A missing cached package is the one error worth phrasing helpfully.
    for d in diags:
        if "not found" in d["message"] and (".sty" in d["message"] or ".cls" in d["message"]):
            d["message"] += "  (package unavailable — offline, or not in the TeX repo)"
    # A viewable result is one where a PDF was produced and nothing error-level
    # was reported. Tectonic's exit code isn't part of this: a warnings-only run
    # can still exit non-zero yet write a perfectly good PDF, and the stale PDF is
    # already gone (removed above), so a present PDF is this run's.
    pdf_exists = os.path.exists(pdf)
    has_error = any(d.get("severity") == "error" for d in diags)
    # Tectonic prints its "note:" progress to stdout and errors to stderr —
    # include both so the tail is actually useful (a crash often leaves only a
    # stdout "note: Running TeX ..." with an empty stderr).
    combined = "\n".join(x for x in (p.stdout, p.stderr) if x).strip()
    log_tail = "\n".join(combined.splitlines()[-40:])
    result = {
        "ok": pdf_exists and not has_error,
        "pdf": pdf if pdf_exists else "",
        "synctex": os.path.join(build, stem + ".synctex.gz"),
        "log_tail": log_tail,
        "errors": diags,
        "seconds": seconds,
    }
    # A failure Tectonic didn't explain — no PDF and no parseable diagnostics
    # (e.g. it crashed before writing the .log) — would otherwise surface as a
    # blank "? errors" with an empty Problems list. Give the user the exit code
    # and output tail so it's actionable. Only when there's genuinely no PDF:
    # a run that produced one is served above, warnings and all.
    if not pdf_exists and not has_error:
        detail = f":\n{log_tail}" if log_tail else "."
        result["error"] = (
            f"Tectonic exited with code {p.returncode} and produced no PDF{detail}\n\n"
            "The document may use a package or font Tectonic can't build, or the "
            "compiler crashed on this input.")
    return result


# ---------------------------------------------------------------- source index
_SECT_RE = re.compile(r"\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\s*\{")
_LABEL_RE = re.compile(r"\\label\{([^}]*)\}")
_CITE_RE = re.compile(r"\\(?:cite|citep|citet|citeauthor|citeyear|parencite|textcite|autocite)\*?(?:\[[^\]]*\])*\{([^}]*)\}")
_INPUT_RE = re.compile(r"\\(?:input|include|subfile)\{([^}]*)\}")
_BIBRES_RE = re.compile(r"\\(?:addbibresource|bibliography)\{([^}]*)\}")
_ENV_RE = re.compile(r"\\begin\{(figure|table|equation|align|algorithm|lstlisting|tikzpicture|theorem|lemma|proof|definition)\*?\}")
_TITLE_RE = re.compile(r"\\title\{(.+?)\}", re.S)
_LEVELS = {"part": 0, "chapter": 1, "section": 1, "subsection": 2,
           "subsubsection": 3, "paragraph": 4}


def _match_braces(s: str, start: int) -> str:
    """Given index of the opening '{', return the balanced inner text."""
    depth, i, out = 0, start, []
    while i < len(s):
        c = s[i]
        if c == "{":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif c == "}":
            depth -= 1
            if depth == 0:
                return "".join(out)
        if depth >= 1:
            out.append(c)
        i += 1
    return "".join(out)


def _resolve_input(base_dir: str, ref: str) -> str:
    ref = ref.strip()
    for cand in (ref, ref + ".tex"):
        p = os.path.join(base_dir, cand)
        if os.path.exists(p):
            return os.path.abspath(p)
    return ""


def _project_files(main_path: str):
    """Follow \\input/\\include from the main file to gather the .tex set (so the
    outline spans the whole document, in include order). Falls back to just the
    main file if nothing is referenced."""
    main_path = os.path.abspath(main_path)
    seen, order = set(), []

    def walk(p):
        if p in seen or not os.path.exists(p):
            return
        seen.add(p)
        order.append(p)
        try:
            txt = _read_text(p)
        except OSError:
            return
        for m in _INPUT_RE.finditer(txt):
            child = _resolve_input(os.path.dirname(p), m.group(1))
            if child:
                walk(child)

    walk(main_path)
    return order


def _outline(main_path: str):
    files = _project_files(main_path)
    sections, labels, cites, envs, inputs, bibres = [], [], [], [], [], []
    title = ""
    for f in files:
        rel = os.path.basename(f)
        txt = _read_text(f)
        offsets = [0]
        for ch in txt.split("\n"):
            offsets.append(offsets[-1] + len(ch) + 1)

        def line_of(pos):
            lo, hi = 0, len(offsets) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if offsets[mid] <= pos:
                    lo = mid + 1
                else:
                    hi = mid
            return lo

        if not title:
            tm = _TITLE_RE.search(txt)
            if tm:
                title = re.sub(r"\s+", " ", tm.group(1)).strip()
        for m in _SECT_RE.finditer(txt):
            kind = m.group(1)
            inner = _match_braces(txt, m.end() - 1)
            sections.append({"level": _LEVELS.get(kind, 2), "kind": kind,
                             "title": re.sub(r"\s+", " ", inner).strip()[:120],
                             "file": rel, "line": line_of(m.start())})
        for m in _LABEL_RE.finditer(txt):
            labels.append({"name": m.group(1), "file": rel, "line": line_of(m.start())})
        for m in _CITE_RE.finditer(txt):
            for key in m.group(1).split(","):
                key = key.strip()
                if key:
                    cites.append(key)
        for m in _ENV_RE.finditer(txt):
            envs.append({"env": m.group(1), "file": rel, "line": line_of(m.start())})
        for m in _INPUT_RE.finditer(txt):
            inputs.append(m.group(1).strip())
        for m in _BIBRES_RE.finditer(txt):
            for b in m.group(1).split(","):
                if b.strip():
                    bibres.append(b.strip())
    return {"title": title, "files": [os.path.basename(f) for f in files],
            "sections": sections, "labels": labels,
            "cites_used": sorted(set(cites)), "envs": envs,
            "inputs": inputs, "bib_resources": bibres}


# ------------------------------------------------------------------- bib parse
_BIB_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.I)


def _bib_field(block: str, name: str) -> str:
    m = re.search(r"\b" + name + r"\s*=\s*", block, re.I)
    if not m:
        return ""
    i = m.end()
    if i >= len(block):
        return ""
    if block[i] == "{":
        return re.sub(r"\s+", " ", _match_braces(block, i)).strip()
    if block[i] == '"':
        j = block.find('"', i + 1)
        return block[i + 1:j].strip() if j > 0 else ""
    m2 = re.match(r"([^,}\n]+)", block[i:])
    return m2.group(1).strip() if m2 else ""


def _parse_bib(paths):
    entries = []
    for p in paths:
        try:
            txt = _read_text(p)
        except OSError:
            continue
        for m in _BIB_ENTRY_RE.finditer(txt):
            etype, key = m.group(1).lower(), m.group(2)
            block = txt[m.start():m.start() + 2000]
            entries.append({
                "key": key, "type": etype,
                "title": _bib_field(block, "title")[:200],
                "author": _bib_field(block, "author")[:160],
                "year": _bib_field(block, "year"),
                "file": os.path.basename(p),
            })
    return entries


def _bib_paths_for(main_path: str):
    base = os.path.dirname(os.path.abspath(main_path))
    out = _outline(main_path)
    paths = []
    for b in out["bib_resources"]:
        for cand in (b, b + ".bib"):
            p = os.path.join(base, cand)
            if os.path.exists(p):
                paths.append(os.path.abspath(p))
                break
    # also any .bib sitting next to the document
    for p in glob.glob(os.path.join(base, "**", "*.bib"), recursive=True):
        if os.path.abspath(p) not in paths:
            paths.append(os.path.abspath(p))
    return paths


# ---------------------------------------------------------------- synctex fwd
def _synctex_forward(synctex_gz: str, target_file: str, line: int):
    """Best-effort forward search: parse the (gzipped) SyncTeX file, find output
    boxes tagged with target_file at (or near) `line`, and return the page plus a
    vertical fraction so the UI can flash a highlight. Page number is reliable;
    vfrac is an estimate normalized by the max v seen on that page."""
    if not os.path.exists(synctex_gz):
        return {"page": 0, "vfrac": 0.0, "hits": 0}
    try:
        with gzip.open(synctex_gz, "rt", errors="replace") as f:
            data = f.read()
    except OSError:
        return {"page": 0, "vfrac": 0.0, "hits": 0}
    tags, target_tag = {}, None
    tbase = os.path.basename(target_file)
    for m in re.finditer(r"Input:(\d+):(.+)", data):
        tag, path = int(m.group(1)), m.group(2).strip()
        tags[tag] = path
        if os.path.basename(path) == tbase:
            target_tag = tag
    if target_tag is None:
        return {"page": 0, "vfrac": 0.0, "hits": 0}
    # Walk content, tracking current page; record (page, v) for our tag+line.
    page_max_v, hits = {}, []
    rec = re.compile(r"^[xkgvh\$\[\(]" + str(target_tag) + r",(\d+):(-?\d+),(-?\d+)")
    cur_page = 0
    for raw in data.splitlines():
        if not raw:
            continue
        c = raw[0]
        if c == "{":
            try:
                cur_page = int(raw[1:])
            except ValueError:
                pass
            continue
        # track any v on this page for normalization
        mv = re.match(r"^[xkgvh\$\[\(]\d+,\d+:-?\d+,(-?\d+)", raw)
        if mv:
            v = int(mv.group(1))
            page_max_v[cur_page] = max(page_max_v.get(cur_page, 0), v)
        m = rec.match(raw)
        if m:
            rline, h, v = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hits.append((abs(rline - line), cur_page, v))
    if not hits:
        return {"page": 0, "vfrac": 0.0, "hits": 0}
    hits.sort()
    _, best_page, best_v = hits[0]
    maxv = page_max_v.get(best_page, 0) or 1
    return {"page": best_page, "vfrac": max(0.0, min(1.0, best_v / maxv)),
            "hits": len(hits)}


# ----------------------------------------------------------------- projects ---
# Home-screen projects: a scaffold is pure file-writing, so a new blank document
# succeeds even when tectonic isn't installed (compilation then fails gracefully
# through the usual missing-tectonic path). Projects live under LIBRARY
# (~/.fused-render/latex/projects), one folder each with a main.tex + meta.json.
PROJECT_TEMPLATES = {
    "article": r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb,amsthm}
\usepackage[margin=1in]{geometry}
\usepackage{graphicx}
\usepackage[colorlinks=true,linkcolor=blue,citecolor=teal,urlcolor=magenta]{hyperref}
\usepackage{booktabs}

\title{__TITLE__}
\author{Your Name}
\date{\today}

\begin{document}
\maketitle

\begin{abstract}
A short abstract. Edit on the left, the PDF recompiles on the right.
\end{abstract}

\section{Introduction}\label{sec:intro}
Hello, \LaTeX! Inline math like $E = mc^2$ and display math:
\begin{equation}\label{eq:euler}
  e^{i\pi} + 1 = 0.
\end{equation}
See Section~\ref{sec:intro} and Equation~\eqref{eq:euler}.

\section{Method}
\begin{itemize}
  \item First point.
  \item Second point.
\end{itemize}

\end{document}
""",
    "report": r"""\documentclass[11pt]{report}
\usepackage{amsmath,amssymb}
\usepackage[margin=1in]{geometry}
\usepackage{graphicx}
\usepackage[colorlinks=true]{hyperref}
\title{__TITLE__}
\author{Your Name}
\date{\today}
\begin{document}
\maketitle
\tableofcontents
\chapter{Introduction}\label{ch:intro}
Text of the first chapter.
\chapter{Background}
More text.
\end{document}
""",
    "beamer": r"""\documentclass{beamer}
\usetheme{Madrid}
\usepackage{amsmath}
\title{__TITLE__}
\author{Your Name}
\date{\today}
\begin{document}
\frame{\titlepage}
\begin{frame}{Overview}
  \begin{itemize}
    \item A point.
    \item Another point with math: $\sum_{i=1}^n i = \frac{n(n+1)}{2}$.
  \end{itemize}
\end{frame}
\end{document}
""",
    "letter": r"""\documentclass{letter}
\usepackage[margin=1in]{geometry}
\signature{Your Name}
\address{Your Street \\ Your City}
\begin{document}
\begin{letter}{Recipient \\ Their Address}
\opening{Dear Recipient,}
Body of the letter.
\closing{Sincerely,}
\end{letter}
\end{document}
""",
}


def _fwd(p):
    return p.replace(os.sep, "/")


def _safe_slug(s):
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", (s or "").strip())[:64].strip("-")
    return slug or "untitled"


def _project_dir(slug):
    return os.path.join(LIBRARY, _safe_slug(slug))


def _project_meta(d):
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _list_projects():
    os.makedirs(LIBRARY, exist_ok=True)
    out = []
    for name in sorted(os.listdir(LIBRARY)):
        d = os.path.join(LIBRARY, name)
        if not os.path.isdir(d):
            continue
        meta = _project_meta(d)
        main_rel = meta.get("main", "main.tex")
        mainp = os.path.join(d, main_rel)
        out.append({"slug": name, "title": meta.get("title", name),
                    "main": main_rel,
                    "main_path": _fwd(mainp) if os.path.exists(mainp) else "",
                    "mtime": os.path.getmtime(mainp) if os.path.exists(mainp)
                    else os.path.getmtime(d)})
    out.sort(key=lambda e: -(e["mtime"] or 0))
    return out


def _new_project(title, template):
    os.makedirs(LIBRARY, exist_ok=True)
    # Build a unique dir directly under LIBRARY. The suffix is applied to a base
    # trimmed to leave room for it, so a title that already fills the 64-char
    # slug budget can't collapse back onto the existing dir and spin forever.
    base = _safe_slug(title or "untitled")
    slug = base
    n = 2
    while os.path.exists(os.path.join(LIBRARY, slug)):
        suffix = f"-{n}"
        slug = base[: 64 - len(suffix)].rstrip("-") + suffix
        n += 1
    d = os.path.join(LIBRARY, slug)
    os.makedirs(d, exist_ok=True)
    body = PROJECT_TEMPLATES.get(template, PROJECT_TEMPLATES["article"]).replace(
        "__TITLE__", (title or slug).replace("{", "").replace("}", ""))
    with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as f:
        f.write(body)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": title or slug, "main": "main.tex",
                   "created": time.time(), "template": template}, f)
    return {"slug": slug, "title": title or slug, "main": "main.tex",
            "main_path": _fwd(os.path.join(d, "main.tex"))}


# -------------------------------------------------------------------- dispatcher
def main(action: str = "tectonic_status", path: str = "", target: str = "",
         line: int = 0, synctex: bool = True, name: str = "", force: int = 0,
         src: str = "", title: str = "", slug: str = "", template: str = ""):
    if action == "tectonic_status":
        return _tectonic_status()

    if action == "warm_status":
        return {"warm": _cache_warm(), "progress": _warm_progress()}

    if action == "tectonic_install":
        return _tectonic_install()

    if action == "list_projects":
        return {"projects": _list_projects(), "dir": _fwd(LIBRARY)}

    if action == "new_project":
        return _new_project(title, template or "article")

    if action == "open_project":
        d = _project_dir(slug)
        if not os.path.isdir(d):
            return {"error": "no such project"}
        meta = _project_meta(d)
        main_rel = meta.get("main", "main.tex")
        mainp = os.path.join(d, main_rel)
        if not os.path.exists(mainp):
            return {"error": "project has no main file"}
        return {"slug": slug, "title": meta.get("title", slug), "main": main_rel,
                "main_path": _fwd(mainp)}

    if action == "browse":
        # List one directory for the file-browser modal. `path` = dir to show
        # (defaults to the target file's own directory); `target` (reused as
        # an ext filter) = "tex" or "" for all. Server-side, so it sees the
        # whole machine incl. WSL /mnt/c, /home, etc.
        d = os.path.abspath(path) if path else os.path.expanduser("~")
        tex_only = (target or "").lower() == "tex"
        entries = []
        # Ask the server once: is this a remote (mount-backed) path, and is it a dir?
        status, meta = _stat(src, d) if src else ("", None)
        if status == "ok" and meta.get("remote"):
            # Mount-backed: list via /api/fs/list, never a kernel scan. If `d` is a
            # file (not a dir), descend to its parent with pure string ops — never a
            # kernel os.path call on a remote path (that call wedges the NFS mount).
            if not meta.get("is_dir"):
                d = os.path.dirname(d) or "/"
            try:
                ents, _ = _list_remote(src, d)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc), "dir": d}
            for ent in ents:
                nm = ent["name"]
                if nm.startswith("."):
                    continue
                isdir = bool(ent.get("is_dir"))
                ext = os.path.splitext(nm)[1].lower()
                if not isdir and tex_only and ext not in TEX_EXT:
                    continue
                entries.append({"name": nm, "path": os.path.join(d, nm),
                                "is_dir": isdir, "ext": ext,
                                "size": 0 if isdir else (ent.get("size") or 0)})
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            parent = os.path.dirname(d.rstrip("/")) or "/"
            return {"dir": d, "parent": parent, "entries": entries}
        if os.path.isfile(d):
            d = os.path.dirname(d)
        if not os.path.isdir(d):
            d = os.path.expanduser("~")
        try:
            names = os.listdir(d)
        except OSError as e:
            return {"error": str(e), "dir": d}
        for nm in names:
            if nm.startswith("."):
                continue
            full = os.path.join(d, nm)
            isdir = os.path.isdir(full)
            ext = os.path.splitext(nm)[1].lower()
            if not isdir and tex_only and ext not in TEX_EXT:
                continue
            try:
                size = 0 if isdir else os.path.getsize(full)
            except OSError:
                continue
            entries.append({"name": nm, "path": full, "is_dir": isdir,
                            "ext": ext, "size": size})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        parent = os.path.dirname(d.rstrip("/")) or "/"
        return {"dir": d, "parent": parent, "entries": entries}

    if action == "find":
        # Recursive search under `path` for the file browser. `name` = query
        # substring; `target` (reused as an ext filter) = "tex" or "" for all.
        # Capped so it can't blow the 30s budget on a huge tree — reports
        # `truncated` when it stops early.
        root = os.path.abspath(path) if path else os.path.expanduser("~")
        q = (name or "").lower()
        tex_only = (target or "").lower() == "tex"
        out, cap = [], 400
        if _remote_dir(src, root):
            # A recursive os.walk of a mount-backed dir can enumerate a huge S3
            # prefix and drop the mount. Degrade to a single-level /api/fs/list
            # (non-recursive) so search still works without the kernel walk.
            try:
                ents, trunc = _list_remote(src, root)
            except Exception:  # noqa: BLE001
                ents, trunc = [], False
            for ent in sorted(ents, key=lambda e: e["name"].lower()):
                if ent.get("is_dir"):
                    continue
                fn = ent["name"]
                ext = os.path.splitext(fn)[1].lower()
                if tex_only and ext not in TEX_EXT:
                    continue
                if q and q not in fn.lower():
                    continue
                out.append({"name": fn, "path": os.path.join(root, fn),
                            "rel": fn, "ext": ext})
                if len(out) >= cap:
                    return {"root": root, "results": out, "truncated": True}
            return {"root": root, "results": out, "truncated": trunc}
        for dp, dns, fns in os.walk(root):
            dns[:] = [x for x in dns if not x.startswith(".")
                      and x not in ("node_modules", "__pycache__", ".git")]
            # relpath on a file named "nul"/"con" device-expands and raises;
            # derive rel from the walk's dir instead.
            reldir = os.path.relpath(dp, root)
            for fn in sorted(fns, key=str.lower):
                ext = os.path.splitext(fn)[1].lower()
                if tex_only and ext not in TEX_EXT:
                    continue
                if q and q not in fn.lower():
                    continue
                full = os.path.join(dp, fn)
                rel = fn if reldir == os.curdir else os.path.join(reldir, fn)
                out.append({"name": fn, "path": full, "rel": rel, "ext": ext})
                if len(out) >= cap:
                    return {"root": root, "results": out, "truncated": True}
        return {"root": root, "results": out, "truncated": False}

    if action == "compile":
        if not path:
            return {"ok": False, "error": "compile needs path", "errors": []}
        # remote -> skip the project-dir mtime walk (mount-drop risk) in _compile
        remote = _remote_dir(src, os.path.dirname(os.path.abspath(path)))
        return _compile(path, synctex=synctex, force=bool(force), remote=remote)

    if action == "outline":
        if not path or not os.path.exists(path):
            return {"error": "outline needs an existing path"}
        return _outline(path)

    if action == "bib":
        if not path or not os.path.exists(path):
            return {"entries": []}
        return {"entries": _parse_bib(_bib_paths_for(path))}

    if action == "synctex":
        # `path` is the compiled entrypoint (its .synctex.gz lives in the build
        # dir); `target` is the file the line belongs to (an \input child, or
        # the same file). They differ for multi-file documents and outline jumps.
        if not path:
            return {"error": "synctex needs path"}
        build = _build_dir_for(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        gz = os.path.join(build, stem + ".synctex.gz")
        return _synctex_forward(gz, target or path, line)

    if action == "export":
        # Convenience interop: the compiled PDF is the real artifact, but pandoc
        # turns the .tex into docx/html/md for round-trips.
        if not path or not os.path.exists(path):
            return {"error": "export needs an existing path"}
        fmt = (target or "pdf").lower()   # reuse `target` param as the format
        if fmt == "pdf":
            c = _compile(path)
            return {"path": c.get("pdf", ""), "ok": c.get("ok", False),
                    "missing_tectonic": c.get("missing_tectonic", False)}
        import pypandoc
        exports = _export_dir_for(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        to = {"docx": "docx", "html": "html", "md": "gfm",
              "markdown": "gfm", "odt": "odt", "rtf": "rtf"}.get(fmt)
        if not to:
            return {"error": f"unsupported format: {fmt}"}
        out_ext = {"gfm": "md"}.get(to, to)
        out = os.path.join(exports, f"{stem}.{out_ext}")
        # Per-format flags. HTML is the tricky one: without these, math renders
        # as bare text, \tableofcontents is dropped, and section numbers/links
        # are missing. --mathml keeps the file self-contained (no MathJax CDN),
        # --toc rebuilds the contents, --number-sections makes \ref targets and
        # the TOC line up, and resolving refs needs section numbers present.
        extra = ["--standalone"]
        if to == "html":
            extra += ["--toc", "--toc-depth=3", "--number-sections", "--mathml",
                      "--section-divs", "--embed-resources"]
        elif to in ("docx", "odt"):
            extra += ["--toc"]
        try:
            pypandoc.convert_file(path, to, format="latex+raw_tex",
                                  outputfile=out, extra_args=extra)
        except Exception as e:
            return {"error": f"export to {fmt} failed: {e}"}
        return {"path": out, "name": os.path.basename(out), "size": os.path.getsize(out)}

    return {"error": f"unknown action: {action}"}
