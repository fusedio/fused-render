# /// script
# dependencies = ["pypandoc"]
# ///
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

CACHE_ROOT = os.path.expanduser("~/.fused-render/cache/latex")
TECTONIC_CACHE = os.path.join(CACHE_ROOT, "tectonic-cache")  # shared package/font cache
BUILDS = os.path.join(CACHE_ROOT, "builds")                  # per-doc aux output, hashed
EXPORTS = os.path.join(CACHE_ROOT, "exports")                # per-doc pandoc exports, hashed
INSTALL_DIR = os.path.join(CACHE_ROOT, "_install")           # tectonic download staging
BIN_DIR = os.path.expanduser("~/.fused-render/bin")          # user-owned install location

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


def _pid_alive(pid):
    # os.kill(pid, 0) is the POSIX no-op liveness check, but on Windows signal 0
    # aliases CTRL_C_EVENT and doesn't reliably error on a dead pid — check the
    # process's exit code via the Win32 API instead.
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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


def _compile(main_path: str, synctex: bool = True):
    main_path = os.path.abspath(main_path)
    if not os.path.exists(main_path):
        return {"ok": False, "error": f"no such file: {main_path}", "errors": []}
    bin_path = _tectonic_bin()
    if not bin_path:
        return {"ok": False, "missing_tectonic": True, "errors": [],
                "error": "Tectonic isn't installed — install it to compile."}
    os.makedirs(TECTONIC_CACHE, exist_ok=True)
    build = _build_dir_for(main_path)
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
    t0 = time.time()
    try:
        # cwd = the .tex file's own directory, so relative \input/\includegraphics
        # resolve the way the author expects.
        p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           cwd=os.path.dirname(main_path), timeout=28)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "compile exceeded 28s (too complex, or a "
                "cold package fetch) — simplify, or recompile once the fetch "
                "has warmed the cache", "errors": [], "seconds": round(time.time() - t0, 2)}
    seconds = round(time.time() - t0, 2)
    stem = os.path.splitext(os.path.basename(main_path))[0]
    pdf = os.path.join(build, stem + ".pdf")
    logf = os.path.join(build, stem + ".log")
    diags = _dedup(_parse_tectonic_stderr(p.stderr) + _parse_tex_log(logf))
    # A missing cached package is the one error worth phrasing helpfully.
    for d in diags:
        if "not found" in d["message"] and (".sty" in d["message"] or ".cls" in d["message"]):
            d["message"] += "  (package unavailable — offline, or not in the TeX repo)"
    ok = os.path.exists(pdf) and p.returncode == 0
    log_tail = "\n".join((p.stderr or "").splitlines()[-40:])
    return {
        "ok": ok,
        "pdf": pdf if os.path.exists(pdf) else "",
        "synctex": os.path.join(build, stem + ".synctex.gz"),
        "log_tail": log_tail,
        "errors": diags,
        "seconds": seconds,
    }


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


# -------------------------------------------------------------------- dispatcher
def main(action: str = "tectonic_status", path: str = "", target: str = "",
         line: int = 0, synctex: bool = True, name: str = ""):
    if action == "tectonic_status":
        return _tectonic_status()

    if action == "tectonic_install":
        return _tectonic_install()

    if action == "browse":
        # List one directory for the file-browser modal. `path` = dir to show
        # (defaults to the target file's own directory); `target` (reused as
        # an ext filter) = "tex" or "" for all. Server-side, so it sees the
        # whole machine incl. WSL /mnt/c, /home, etc.
        d = os.path.abspath(path) if path else os.path.expanduser("~")
        if os.path.isfile(d):
            d = os.path.dirname(d)
        if not os.path.isdir(d):
            d = os.path.expanduser("~")
        tex_only = (target or "").lower() == "tex"
        entries = []
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
        return _compile(path, synctex=synctex)

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
