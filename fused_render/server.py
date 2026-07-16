"""FastAPI app: static shell, filesystem API, HTML rendering, Python execution.

No path restriction anywhere — the whole filesystem is in scope by design
(see DECISIONS.md D2/D3). All `path` query params are absolute filesystem
paths. Endpoints are sync `def` so FastAPI dispatches them to its threadpool,
giving free concurrency for blocking filesystem/subprocess work; /api/run is
async (the fused engine is async; the built-in executor is offloaded).

Execution engine (D69/D70): /api/run runs the built-in executor by **default**,
whether or not the `fused` package is installed — set `FUSED_RENDER_ENGINE=auto`
(use fused if importable, else fall back) or `=fused` (require it — fail loudly
at startup if missing) to opt in to the local compute backend (`engine.py`).
"""
import asyncio
import codecs
import email.utils
import json
import logging
from collections import deque
import mimetypes
import os
import shutil
import stat as stat_mod
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import Body, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from fused_render import __version__
from fused_render.account import router as account_router
from fused_render.core_templates import ensure_core_templates
from fused_render.deploy import router as deploy_router
from fused_render.executor import run_python
from fused_render.shell import prefs as shell_prefs
from fused_render.shell import storage
from fused_render.shell.bookmarks import router as bookmarks_router
from fused_render.shell.prefs import router as prefs_router
from fused_render.shell.recents import router as recents_router
from fused_render.shell.seed import fused_dir
from fused_render.shell.storage import home_dir

logger = logging.getLogger(__name__)


def _forced_engine() -> str | None:
    """The process-level engine override, or None when unset (D69/D70 + §20).

    FUSED_RENDER_ENGINE forces the /api/run engine for the whole process:
    `builtin` never touches the `fused` package even if importable; `auto`
    opts in to it iff importable; `fused` demands it (a missing package is a
    startup error, not a silent fallback). **Unset returns None** — the
    engine then follows the persisted preference (shell/prefs.py, default
    builtin — D70 stands), re-read per request so the Preferences page's
    switch applies without a restart. Logged either way — engine choice
    changes the code contract, so it must never be silent.
    """
    raw = os.environ.get("FUSED_RENDER_ENGINE")
    if raw is None:
        logger.info(
            "execution engine: following the preference (~/.fused-render/prefs.json, "
            "default builtin); FUSED_RENDER_ENGINE overrides it for this process"
        )
        return None
    requested = raw.strip().lower()
    if requested not in ("auto", "fused", "builtin"):
        raise RuntimeError(
            f"FUSED_RENDER_ENGINE={requested!r} is not one of: auto, fused, builtin"
        )
    if requested == "builtin":
        logger.info("execution engine: builtin (forced by FUSED_RENDER_ENGINE)")
        return "builtin"
    try:
        from fused_render import engine as _engine

        ok = _engine.available()
    except ImportError:
        ok = False
    if ok:
        logger.info("execution engine: fused (forced by FUSED_RENDER_ENGINE)")
        return "fused"
    if requested == "fused":
        raise RuntimeError(
            "FUSED_RENDER_ENGINE=fused but the `fused` package is not importable; "
            "install it (pip install 'fused-render[fused]') or unset the override"
        )
    logger.info("execution engine: builtin (FUSED_RENDER_ENGINE=auto, `fused` not installed)")
    return "builtin"

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
# Core templates ship in the package but are staged into
# ~/.fused-render/.core-templates on startup (reset-on-release); the server
# reads every built-in template/registry/helper from that copy, not the bundle.
TEMPLATES_DIR = ensure_core_templates()

# Built-in extension → mode-list bindings ship as data, not code (D73):
# templates/registry.json, exactly the user-registry format (SPEC §16). Keys
# are dot-anchored suffix patterns — ".csv", compound ".xyz.json", wildcard
# ".*.json" (`*` = one whole dot-segment) — and a trailing "/" marks a
# directory key (".zarr/": a zarr store is one logical dataset spread across
# many chunk files, so it previews as a dataset rather than a listing).
# Values are ordered lists of template names, first = default (SPEC PT-7,
# D60). A name is a folder name (fused_render/templates/<name>/), never a
# filename. Rationale per mapping lives in the SPEC PT-7 table.
BUILTIN_REGISTRY = os.path.join(TEMPLATES_DIR, "registry.json")

# Shell sentinel modes (SPEC PT-12): implemented by the shell, no template
# folder behind them. The only `_`-prefixed names a registry mode list may
# reference (D73); any other `_` name is invalid (CT-6). `_listing` is the
# shell's built-in directory listing — the default of the universal `/`
# directory key (D81).
KNOWN_SENTINELS = {"_render", "_listing"}

# Recursive-walk cap (/api/fs/walk): stop collecting after this many entries.
# With the streamed BFS walk this is a memory/latency safety valve, not a
# coverage budget — shallow entries (the ones a search almost always wants)
# are all emitted long before the cap can bite. Module-level so tests can
# shrink it.
WALK_MAX_ENTRIES = 200_000
# Much smaller cap when the walked path sits under a mount mountpoint
# (shell/mounts.py): there every directory listing is a remote LIST call
# (S3 etc.), so an unbounded walk over a bucket is a slow, potentially paid
# API storm. The walk truncates early and the existing `truncated` flag tells
# the client search was bounded.
WALK_MAX_ENTRIES_REMOTE = 2_000
# Max entries per NDJSON batch line in the streamed walk — a framing CAP, not
# the streaming lever (WALK_FLUSH_INTERVAL_S below is). Kept large so a big
# local walk emits few lines; the timer guarantees timely flushing regardless.
WALK_BATCH_SIZE = 500
# Flush whatever has accumulated this long after the last flush, even if the
# batch isn't full. This is what makes the walk actually STREAM: without it, a
# tree smaller than one batch (a bucket prefix is often dozens–hundreds of
# objects) buffers entirely and arrives as one end-of-walk lump, so the
# client's incremental scoring/paint never runs and results appear only once
# the whole walk finishes. With it, entries paint per directory as the walk
# descends, on mounts and locally alike. Checked between yielded entries
# (best-effort — a single blocking listdir can't be interrupted mid-call).
WALK_FLUSH_INTERVAL_S = 0.15
# Directory names never descended into by the walk, checked against the bare
# name so it also applies under hidden=1 (".git" is machine noise, not
# "hidden data"). This is only the UNIVERSAL floor — inside a git repository
# the walk additionally prunes whatever the repo's own .gitignore ignores
# (see _IgnoreOracle), which is what actually catches dist/, build/, .next/,
# target/ and friends without hardcoding every ecosystem's junk dir. The
# floor still matters outside repos (a stray node_modules in ~/Downloads)
# and for .git itself, which git never reports as ignored.
WALK_IGNORE_DIRS = {"node_modules", "__pycache__", "venv", ".venv", ".git", "site-packages"}
# Cap on concurrently open check-ignore co-processes during one walk (a home
# walk crosses dozens of repos; each oracle holds a git subprocess).
WALK_MAX_ORACLES = 8
# macOS package directories: emitted as a single (dir) entry but never
# descended — their internals are implementation details (Finder hides them
# too), and one Electron .app alone can be thousands of files.
WALK_LEAF_DIR_SUFFIXES = (".app", ".framework", ".bundle", ".photoslibrary")


# Lazily-created empty git dir backing check-ignore for NON-repo directories
# that still carry a .gitignore (an un-inited project, an Obsidian vault…).
# With GIT_DIR pointing here and GIT_WORK_TREE at the directory, git applies
# that tree's .gitignore files exactly as it would inside a real repo. One
# per process, a few KB, left for the OS tempdir cleanup.
_EMPTY_GIT_DIR: str | None | bool = None  # None = not tried, False = failed


def _empty_git_dir():
    global _EMPTY_GIT_DIR
    if _EMPTY_GIT_DIR is None:
        try:
            root = tempfile.mkdtemp(prefix="fused-render-emptygit-")
            subprocess.run(
                ["git", "init", "-q", root],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            _EMPTY_GIT_DIR = os.path.join(root, ".git")
        except (OSError, subprocess.SubprocessError):
            _EMPTY_GIT_DIR = False
    return _EMPTY_GIT_DIR or None


class _IgnoreOracle:
    """One repository's `git check-ignore` as a streaming co-process.

    `git check-ignore --stdin -z -v -n` answers path queries incrementally on
    one long-lived subprocess (measured ~14µs/query), so the walk can ask
    about every directory's children as it reaches them — no subprocess per
    directory, no giant upfront batch. `-v -n` makes git echo all four
    NUL-terminated fields for EVERY query (matching or not), which is what
    makes the stream pairable: query order in = verdict order out.

    Any failure (git missing, repo gone mid-walk, pipe breakage) marks the
    oracle broken and it answers "nothing ignored" from then on — gitignore
    pruning is an optimization, never a hard dependency (same posture as
    _git_ignored's dimming).
    """

    # Queries per write/read cycle: bounded so git's stdout can't fill the
    # pipe while we are still writing stdin (classic co-process deadlock).
    CHUNK = 200

    def __init__(self, repo_root):
        self.root = repo_root
        self.broken = False
        # Real repo (a .git exists at or above the root): plain `git -C`.
        # Standalone-.gitignore directory (no repo): graft the dir onto a
        # shared empty GIT_DIR as its work tree, which makes check-ignore
        # honor the tree's .gitignore files without a repository.
        env = None
        if not os.path.exists(os.path.join(repo_root, ".git")):
            empty = _empty_git_dir()
            if empty is None:
                self.proc = None
                self.broken = True
                self._buf = b""
                return
            env = {**os.environ, "GIT_DIR": empty, "GIT_WORK_TREE": repo_root}
        try:
            self.proc = subprocess.Popen(
                ["git", "-C", repo_root, "check-ignore", "--stdin", "-z", "-v", "-n"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError:
            self.proc = None
            self.broken = True
        self._buf = b""

    def _read_field(self):
        while True:
            cut = self._buf.find(b"\0")
            if cut != -1:
                field = self._buf[:cut]
                self._buf = self._buf[cut + 1:]
                return field
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                raise OSError("check-ignore stream closed")
            self._buf += chunk

    def ignored(self, rel_paths):
        """Subset of `rel_paths` (POSIX, relative to repo root) git ignores."""
        if self.broken or not rel_paths:
            return set()
        out = set()
        try:
            for i in range(0, len(rel_paths), self.CHUNK):
                chunk = rel_paths[i : i + self.CHUNK]
                payload = b"".join(os.fsencode(r) + b"\0" for r in chunk)
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
                for r in chunk:
                    # <source> NUL <linenum> NUL <pattern> NUL <path> NUL.
                    # Empty source = no pattern matched (not ignored). A
                    # NEGATED pattern ("!keep.log") is also echoed with its
                    # source under -v — that match means explicitly NOT
                    # ignored, so test the pattern's sign, not mere presence.
                    source = self._read_field()
                    self._read_field()
                    pattern = self._read_field()
                    self._read_field()
                    if source and not pattern.startswith(b"!"):
                        out.add(r)
            return out
        except OSError:
            self.broken = True
            self.close()
            return set()

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()
            self.proc = None


def _repo_toplevel(path):
    """The git work-tree root containing `path`, or None. One call per walk —
    covers walking a SUBDIRECTORY of a repo, where no `.git` marker is ever
    seen during the walk itself."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = os.fsdecode(proc.stdout.strip())
    return top or None


def _walk_bfs(path, include_hidden):
    """Level-order walk of `path` yielding /api/fs/walk entry dicts.

    Breadth-first via a FIFO of pending directories: every entry at depth N is
    yielded before any entry at depth N+1, so a caller that stops early (cap,
    client disconnect) always has complete shallow coverage. Within one parent,
    dirs come first, then files, each sorted by name (the old walk's per-level
    order). Symlinks are yielded but never descended; classification and stat
    follow the link (matching os.walk/os.stat), so a broken symlink is skipped
    like any other unstatable entry. Unreadable directories are skipped
    silently (matches /api/fs/list).

    Inside a git repository, entries the repo's own gitignore rules ignore are
    pruned entirely — not emitted, not descended (the generic answer to
    build/cache junk; WALK_IGNORE_DIRS is just the non-repo floor). Each
    directory inherits its parent's repo root through the queue; a child
    directory containing a `.git` entry (dir or worktree/submodule gitfile)
    starts a nested repo with its own rules. Verdicts come from one streaming
    check-ignore co-process per repo (_IgnoreOracle), capped at
    WALK_MAX_ORACLES concurrently, all closed when the walk ends.
    """
    oracles = {}  # repo root -> _IgnoreOracle, insertion order = LRU

    def oracle_for(repo):
        oracle = oracles.pop(repo, None)
        if oracle is None:
            oracle = _IgnoreOracle(repo)
            while len(oracles) >= WALK_MAX_ORACLES:
                oracles.pop(next(iter(oracles))).close()
        oracles[repo] = oracle  # re-insert = mark most-recently-used
        return oracle

    try:
        # (abs dir, rel from walk root, repo root or None, rel from repo root)
        top = _repo_toplevel(path)
        top_rel = "" if top is None else os.path.relpath(path, top).replace(os.sep, "/")
        queue = deque([(path, "", top, "" if top_rel == "." else top_rel)])
        while queue:
            current, rel_base, repo, repo_rel_base = queue.popleft()
            try:
                with os.scandir(current) as it:
                    children = list(it)
            except OSError:
                continue  # unreadable dir skipped silently
            # A .git entry (dir, or gitfile for worktrees/submodules) marks a
            # nested repository: its own gitignore rules take over below here.
            # A .gitignore WITHOUT any repo in scope marks a standalone
            # ignore root (un-inited project, vault, …): same pruning, backed
            # by the empty-GIT_DIR graft (see _IgnoreOracle). Not applied
            # inside a real repo — there git already cascades nested
            # .gitignore files itself.
            names = {c.name for c in children}
            if ".git" in names and current != repo:
                repo, repo_rel_base = current, ""
            elif repo is None and ".gitignore" in names:
                repo, repo_rel_base = current, ""
            dirs = []
            files = []
            for child in children:
                name = child.name
                if not include_hidden and name.startswith("."):
                    continue
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                if is_dir:
                    if name in WALK_IGNORE_DIRS:
                        continue
                    dirs.append(child)
                else:
                    files.append(child)
            if repo is not None and (dirs or files):
                prefix = repo_rel_base + "/" if repo_rel_base else ""
                ignored = oracle_for(repo).ignored(
                    [prefix + c.name for c in dirs + files]
                )
                if ignored:
                    dirs = [c for c in dirs if prefix + c.name not in ignored]
                    files = [c for c in files if prefix + c.name not in ignored]
            dirs.sort(key=lambda e: e.name)
            files.sort(key=lambda e: e.name)
            for child, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
                try:
                    st = child.stat()
                except OSError:
                    continue  # unreadable entries skipped silently
                rel = rel_base + "/" + child.name if rel_base else child.name
                yield {
                    "rel": rel,
                    "is_dir": is_dir,
                    "size": None if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                }
                if is_dir:
                    try:
                        is_link = child.is_symlink()
                    except OSError:
                        is_link = True  # can't tell — safer not to descend
                    if not is_link and not child.name.lower().endswith(WALK_LEAF_DIR_SUFFIXES):
                        repo_rel = (
                            (repo_rel_base + "/" + child.name if repo_rel_base else child.name)
                            if repo is not None
                            else ""
                        )
                        queue.append((os.path.join(current, child.name), rel, repo, repo_rel))
    finally:
        for oracle in oracles.values():
            oracle.close()


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _git_ignored(cwd: str, rel_names: list[str]) -> set[str]:
    """Return the subset of `rel_names` git would ignore, relative to `cwd`.

    Shells out to `git check-ignore` — the authority on gitignore semantics
    (nested .gitignore, .git/info/exclude, the global excludesfile, negation).
    One batched call covers a whole listing. Returns an empty set when `cwd`
    is not in a work tree, git is missing, or anything else goes wrong:
    dimming is a display hint, never a hard dependency on git.

    The `.git` directory (or the gitfile of a worktree/submodule) is folded in
    too: git never reports it via check-ignore, but it is repository plumbing
    the user rarely wants to browse, so we dim it exactly when we know git is
    present and `cwd` is a work tree — i.e. only after a successful call.
    """
    if not rel_names:
        return set()
    try:
        # --stdin -z: NUL-separated in and out, so names with newlines or
        # non-UTF-8 bytes round-trip intact. check-ignore exits 0 when some
        # path is ignored, 1 when none are (not an error), 128 on real
        # failure incl. "not a git repository".
        payload = b"".join(os.fsencode(n) + b"\0" for n in rel_names)
        proc = subprocess.run(
            ["git", "-C", cwd, "check-ignore", "--stdin", "-z"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode not in (0, 1):
        return set()
    ignored = {os.fsdecode(chunk) for chunk in proc.stdout.split(b"\0") if chunk}
    # Return code 0/1 (not 128) proves this is a work tree with git available,
    # so dim `.git` itself. Match the basename so both the top-level entry
    # (".git", from /api/fs/list) and a nested one ("sub/.git", from walk) go.
    ignored.update(n for n in rel_names if n == ".git" or n.endswith("/.git"))
    return ignored


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Guard for the mutating/executing POSTs. Read endpoints are already safe
    # cross-origin because the browser blocks a foreign page from reading our
    # response; but a POST can be fired blind (no-cors fetch) by any website,
    # with no way to read the reply. Requiring a custom request header forces a
    # CORS preflight, which fails cross-origin since we return no CORS headers —
    # so only our own same-origin pages get through. Not authentication (D3
    # stands): it only blocks blind cross-origin POSTs, nothing more.
    if x_fused != "1":
        return _error("missing or invalid X-Fused header", status=403)
    return None


# Per-file sidecar <file>.json (shared with the claude chat template, which
# owns "claudeSessions", and bookmarks, which own "bookmarkHistory" — see
# templates/claude/agent.py and shell/bookmarks.py). Read/merge/write preserves
# every other key so the writers never clobber each other (single local user,
# last-write-wins on a true interleave — D3).
def _sidecar_path(file: str) -> str:
    return file + ".json"


def _read_sidecar(file: str) -> dict:
    # read_json returns None on missing/corrupt; a non-dict (a stray JSON list)
    # is treated as empty so a merge can't crash.
    data = storage.read_json(_sidecar_path(file))
    return data if isinstance(data, dict) else {}


def _has_non_mode_param(search: str) -> bool:
    # A "qualifying" query has at least one key other than _mode (mirrors the
    # frontend hasQualifyingParam). keep_blank_values so "?city=" still counts.
    return any(k != "_mode" for k, _ in parse_qsl(search, keep_blank_values=True))


def _session_get(path: str):
    if not os.path.isfile(path):
        return _error(f"no such file: {path}", status=404)
    last = _read_sidecar(path).get("lastSession")
    return {"lastSession": last if isinstance(last, dict) else None}


def _session_put(body: dict, x_fused: str | None):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path")
    search = body.get("search")
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    if not os.path.isfile(path):
        return _error(f"no such file: {path}", status=404)
    if not isinstance(search, str):
        return _error("'search' must be a string")
    # Read-merge-write the whole dict so claudeSessions / bookmarkHistory
    # survive alongside lastSession (see _read_sidecar comment).
    data = _read_sidecar(path)
    # LSN-3 gate (authoritative, server-side): a _mode-only or empty query must
    # not START a session, but once one exists we DO record _mode-only updates
    # so the file's last _mode is remembered. Save when the query carries a
    # non-_mode param, OR (query is non-empty AND a lastSession already exists).
    # Empty query never clobbers an existing session down to "".
    has_session = isinstance(data.get("lastSession"), dict)
    if not (_has_non_mode_param(search) or (search != "" and has_session)):
        return {"ok": True, "skipped": True}
    data["lastSession"] = {"search": search, "updated_at": time.time()}
    try:
        storage.write_json(_sidecar_path(path), data)
    except OSError as e:
        return _error(f"cannot write sidecar for {path}: {e}", status=400)
    return {"ok": True}


# User templates + their registry live under the shell home dir's templates/
# subdir (D76) — ~/.fused-render/templates/<name>/ and .../templates/registry.json
# — one level below the home dir that also holds bookmarks.json (shell/storage).
# home_dir() itself nests per branch ref (shell/storage), so branch isolation
# comes for free here — no branch logic needed in server.
USER_TEMPLATES_DIR = os.path.join(home_dir(), "templates")
USER_REGISTRY = os.path.join(USER_TEMPLATES_DIR, "registry.json")


def _resolve_name(name):
    """Single template-name resolution rule, used identically for built-in
    table entries and registry entries (SPEC PT-6): `<name>` resolves to
    `~/.fused-render/templates/<name>/template.html` if present, else the staged
    core template `<TEMPLATES_DIR>/<name>/template.html` (core_templates), else
    unusable. A user
    folder shadows a built-in of the same name — the deliberate override
    channel. Returns (abs template.html path | None, error | None).
    """
    # The name is joined into a filesystem path, so it must be one plain
    # segment — a stray "../x" must not stat arbitrary locations. Correctness
    # guard, not auth (D3 stands). `.` is banned outright (SPEC CT-6): it
    # keeps names unambiguous against the "..." splice sigil and dotted
    # registry keys.
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or "." in name
    ):
        return None, f"invalid template name: {name!r}"
    if name.startswith("_"):
        return None, (
            f"invalid template name: {name!r} — the '_' prefix is reserved "
            "for shell sentinel modes (SPEC PT-12); the only referenceable "
            "sentinel is '_render'"
        )
    user = os.path.join(USER_TEMPLATES_DIR, name, "template.html")
    if os.path.isfile(user):
        return user, None
    builtin = os.path.join(TEMPLATES_DIR, name, "template.html")
    if os.path.isfile(builtin):
        return builtin, None
    return None, f"no template.html for {name!r} (looked in ~/.fused-render/templates/{name}/ and core {TEMPLATES_DIR}/{name}/)"


def _icon_for(template_path: str):
    """abs icon.svg beside the resolved template.html, or None (SPEC PT-11)."""
    icon = os.path.join(os.path.dirname(template_path), "icon.svg")
    return icon if os.path.isfile(icon) else None


def _condition_file(template_path: str):
    """The template folder's `condition.py` path, or None when it has no gate.

    A template folder may ship a `condition.py` defining `def main(path):
    bool` — the gate that decides whether the template shows for a given file
    (SPEC CT-12). No file -> the template is unconditional (the common case).
    Split from evaluation so `_apply_conditions` can cheaply tell which entries
    need running before paying to load any code.
    """
    condition_file = os.path.join(os.path.dirname(template_path), "condition.py")
    return condition_file if os.path.isfile(condition_file) else None


def _run_condition(condition_file: str, target_path: str):
    """Load+exec a `condition.py` and call `main(target_path)`. Returns
    (allowed: bool, error: str|None).

    The module is loaded fresh per call (like the registries, so an edit applies
    on the next stat with no restart) and never inserted into `sys.modules` — so
    concurrent calls with the fixed spec name get independent module objects and
    are safe to run in parallel (same rationale as executor._run_in_process). A
    broken condition — no callable `main`, or any raised exception — drops the
    template and surfaces the reason as `template_error`, mirroring how an
    unresolvable name is dropped (SPEC CT-6): a template gated by code that
    can't decide is not silently shown.
    """
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location(
            "__fused_condition__", condition_file
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "main", None)
        if not callable(fn):
            return False, f"{condition_file}: does not define a callable 'main'"
        return bool(fn(target_path)), None
    except BaseException as e:  # never let a bad condition tear down the stat
        return False, f"{condition_file}: {e}"


def _mark_conditions(entries: list):
    """Flag resolved template entries whose folder carries a `condition.py`
    gate with `"conditional": True` (SPEC PT-8/CT-12). Sentinel entries
    (`path is None`, D73) and folders with no gate are left untouched.

    Stat no longer *evaluates* gates — a gate may do real I/O (the H3 gate
    reads a parquet footer), and over a remote mount that stalled every stat
    of the extension. Marking is just an isfile() per entry (~1µs); the client
    renders unconditional templates immediately and resolves the marked ones
    in the background via /api/fs/conditions. A conditional entry is never the
    client's default when an unconditional one exists.
    """
    for entry in entries:
        path = entry.get("path")
        if path is not None and _condition_file(path) is not None:
            entry["conditional"] = True


def _evaluate_conditions(gated: list, target_path: str):
    """Evaluate `condition.py` gates: `gated` is [(key, condition_file)];
    returns {key: (allowed: bool, error: str|None)}.

    Gates are independent and may be slow (user code — filesystem reads,
    remote I/O), so they are evaluated **concurrently**: the cost is the
    slowest single gate, not their sum. Results are keyed, so ordering and
    error precedence are the caller's, unaffected by completion order.
    """
    results = {}  # key -> (allowed, error)

    def _serial():
        for k, cf in gated:
            results[k] = _run_condition(cf, target_path)

    if len(gated) == 1:
        _serial()
    elif gated:
        # Bounded fan-out — an extension has at most a handful of conditional
        # templates (SPEC CT-12), so one worker per gate is fine. The pool
        # machinery itself (thread creation, submit, result) lives OUTSIDE
        # _run_condition's catch-all, so an OS refusing a new thread under load
        # would otherwise escape and 500 the request — breaking the fail-closed
        # guarantee. Contain it: on any pool failure, fall back to serial
        # evaluation, which is wholly inside _run_condition's catch-all.
        try:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=len(gated)) as pool:
                futures = {pool.submit(_run_condition, cf, target_path): k for k, cf in gated}
                for fut, k in futures.items():
                    results[k] = fut.result()
        except BaseException:
            results.clear()  # drop any partial results, re-evaluate cleanly
            _serial()

    return results


def _conditions_payload(path: str):
    """The /api/fs/conditions shape: resolve the path's templates, evaluate
    only the gated ones, and report {"conditions": {mode: bool}, "error"}.

    This is the deferred half of SPEC CT-12: stat marks gated entries
    `conditional` without running them; the client calls this endpoint in the
    background while the first unconditional template already renders. `error`
    carries the first gate error in list order (a broken gate reports False —
    fail closed — with the reason), matching stat's `template_error` posture.
    """
    try:
        st = os.stat(path)
    except OSError:
        return _error(f"no such file or directory: {path}", status=404)
    entries, _ = _templates_for(path, stat_mod.S_ISDIR(st.st_mode))

    gated = []  # [(mode, condition_file)] — mode keys are unique per list
    for entry in entries:
        if entry.get("conditional"):
            cf = _condition_file(entry["path"])
            if cf is not None:
                gated.append((entry["mode"], cf))

    results = _evaluate_conditions(gated, path)
    conditions, error = {}, None
    for mode, _cf in gated:
        allowed, err = results[mode]
        conditions[mode] = allowed
        if err and error is None:
            error = err

    payload = {"path": path, "conditions": conditions}
    if error:
        payload["error"] = error
    return payload


def _resolve_mode_list(names):
    """Resolve an ordered list of template names into `templates` stat
    entries (SPEC PT-8). Per-entry validation (SPEC CT-6): a name that can't
    resolve is dropped; `error` is the first dropped name's message.

    A known sentinel (SPEC PT-12, `KNOWN_SENTINELS`) is emitted as
    `{"mode": name, "path": None, "icon": None}` without touching the
    filesystem — referenceable from the built-in and the user registry alike
    (D73). Any other `_`-prefixed name falls through to `_resolve_name`,
    which rejects it: the rest of the sentinel namespace stays shell-owned
    (CT-6).
    """
    entries = []
    error = None
    for name in names:
        if name in KNOWN_SENTINELS:
            entries.append({"mode": name, "path": None, "icon": None})
            continue
        path, err = _resolve_name(name)
        if path is None:
            if error is None:
                error = err
            continue
        entries.append({"mode": name, "path": path, "icon": _icon_for(path)})
    return entries, error


def _load_registry(path: str, label: str):
    """Read one registry file → (dict | None, error | None). Missing file is
    a clean no-op (SPEC CT-5). Read per call: a tiny local file, and it makes
    registry edits apply on the next stat with no restart and no cache to
    invalidate — the built-in registry rides the same loader (D73), which
    also gives editable installs live edits for free. `label` distinguishes
    the two files in errors (both basenames are registry.json).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, f"cannot read {label}: {e}"
    if not isinstance(registry, dict):
        return None, f"{label} must be a JSON object"
    return registry, None


def _key_segments(key, is_dir: bool):
    """Parse a registry key into its match segments, or None when the key
    cannot apply to this stat. Keys are dot-anchored suffix patterns (SPEC
    CT-3): ".csv", compound ".xyz.json", wildcard ".*.json" — `*` matches
    exactly one whole dot-segment, partial wildcards (".geo*") are invalid. A
    trailing "/" marks a directory key (".zarr/", D73); dir keys match only
    directories, others only files. The bare "/" is the universal directory
    key (D81): zero segments, matches any directory — returned as `[]`
    (distinct from None), ranked lowest by `_match_registry`. A key of the
    wrong shape (no leading dot, empty segment) never matches — same
    silent-ignore the no-leading-dot rule always had.
    """
    key = str(key).lower()
    dir_key = key.endswith("/")
    if dir_key != is_dir:
        return None
    if dir_key:
        key = key[:-1]
        if key == "":
            return []  # universal directory key ("/"): matches any directory
    if not key.startswith(".") or len(key) < 2:
        return None
    segs = key[1:].split(".")
    for seg in segs:
        if not seg or ("*" in seg and seg != "*"):
            return None
    return segs


def _match_registry(registry: dict, basename: str, is_dir: bool):
    """Best-matching (key, value) for basename against registry keys, or
    None. Longest-suffix semantics generalized to patterns (SPEC CT-3, D73):
    a key with more segments beats one with fewer; at equal length, comparing
    from the rightmost segment, a literal beats a `*` (`.xyz.json` >
    `.*.json` > `.json`). The universal `/` directory key (zero segments, D81)
    ranks below every dot-anchored key (`.zarr/` > `/`) and its stem is the
    whole basename. A match needs a non-empty stem before the matched suffix,
    so a dotfile named exactly like a key (a file literally called ".json")
    does not match. Case-insensitive throughout.
    """
    fsegs = basename.lower().split(".")
    best = None  # (n_segments, literal-mask right-to-left, key, value)
    for key, value in registry.items():
        ksegs = _key_segments(key, is_dir)
        if ksegs is None:
            continue
        n = len(ksegs)
        if n == 0:
            # Universal directory key: matches any directory (stem = whole
            # basename, non-empty), lowest specificity so any real key wins.
            rank = (0, ())
        else:
            if len(fsegs) <= n:
                continue
            if not ".".join(fsegs[:-n]):
                continue
            tail = fsegs[-n:]
            if any(not (k == f or (k == "*" and f)) for k, f in zip(ksegs, tail)):
                continue
            rank = (n, tuple(s != "*" for s in reversed(ksegs)))
        if best is None or rank > best[0]:
            best = (rank, key, value)
    if best is None:
        return None
    return best[1], best[2]


def _names_from_value(key, value, builtin_names: list):
    """Interpret one matched registry value (SPEC CT-2/CT-10/CT-11).

    Returns (names, disabled, error). names: ordered list[str] of (possibly
    still-unresolved) template names, or None when the value disables previews.
    disabled: True for `null` **and for an empty list** (`[]`) — both mean "no
    template at all for this type", no error, no built-in fallback. error: a
    shape-level problem (value not list/string/null) — surfaced as
    `template_error` so typos aren't silent.

    There is no `"..."` splice: the token is treated as an ordinary name that
    resolves to no folder (a dangling ref, surfaced broken), not a splice into
    the built-in list. `builtin_names` is unused, kept for signature stability.
    """
    if value is None:
        return None, True, None
    if isinstance(value, str):
        # String = exactly a single-mode list (D50).
        return [value], False, None
    if isinstance(value, list):
        # Empty list disables previews, identical to `null` (owner 2026-07-09).
        if not value:
            return None, True, None
        # Names pass through verbatim; any that resolve to no folder are kept
        # and surfaced as broken (dangling refs), never spliced or expanded.
        return list(value), False, None
    return None, False, f"{key}: registry value must be a list, string, or null"


_TEXT_SNIFF_BYTES = 8192


def _looks_like_text(path: str) -> bool:
    """Best-effort "is this a text file" sniff for the no-binding fallback.

    Reads a small prefix: a NUL byte means binary; otherwise the prefix must
    decode as UTF-8 (the encoding the text/code viewers assume). Decoding is
    incremental with ``final=False`` so a multibyte char split by the read
    boundary isn't mistaken for binary. Any read error (permission, gone, not a
    regular file) -> False, so the caller keeps the metadata card. An empty
    file counts as text (harmless to open in the viewer).
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(_TEXT_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(chunk, final=False)
    except UnicodeDecodeError:
        return False
    return True


def _templates_for(path: str, is_dir: bool):
    """Returns (templates: list[dict], template_error: str|None) — SPEC PT-8.

    Both binding tables are registries in one format (D73): the built-in
    templates/registry.json and the user ~/.fused-render/templates/registry.json, both
    resolved by `_match_registry` — dot-anchored suffix patterns with `*`
    wildcard segments and trailing-"/" directory keys. Directories therefore
    resolve exactly like files (a `.zarr` store matches the ".zarr/" key),
    and the user registry binds them too (D73 revises D65). Precedence: any
    user match > built-in match (CT-3). .html/.htm are ordinary keys (D73
    revises CT-4): the user can rebind them, listing `_render` explicitly to
    keep it reachable. A path with no match in either registry returns empty —
    unmapped file, or the plain listing view for a directory.
    """
    basename = os.path.basename(os.path.normpath(path))

    builtin_names = []
    builtin_reg, error = _load_registry(BUILTIN_REGISTRY, "built-in registry.json")
    if builtin_reg is not None:
        matched = _match_registry(builtin_reg, basename, is_dir)
        if matched is not None:
            names, disabled, err = _names_from_value(*matched, builtin_names=[])
            error = error or err
            if names and not disabled:
                builtin_names = names

    user_names, disabled = None, False
    user_reg, user_err = _load_registry(USER_REGISTRY, "registry.json")
    if user_reg is not None:
        matched = _match_registry(user_reg, basename, is_dir)
        if matched is not None:
            user_names, disabled, err = _names_from_value(*matched, builtin_names)
            user_err = user_err or err
    error = error or user_err

    if disabled:
        # The user explicitly bound this key to null (CT-2) — honor "no
        # template" and never second-guess it with the text sniff below.
        return [], error

    if user_names is None:
        # No user binding, or a parse/shape-level problem — either way fall
        # back to the built-in list (CT-6); `error` carries the problem.
        entries, entry_err = _resolve_mode_list(builtin_names)
        error = error or entry_err
    else:
        entries, entry_err = _resolve_mode_list(user_names)
        error = error or entry_err
        if not entries:
            # The user's value resolved to nothing at all -> built-in fallback.
            entries, _ = _resolve_mode_list(builtin_names)

    if not entries and not is_dir and _looks_like_text(path):
        # Nothing in either registry matched. Many config/dotfiles are plain
        # text the suffix matcher structurally can't reach — its keys are
        # dot-anchored *suffixes* needing a non-empty stem, so a whole-name
        # dotfile (".gitignore", ".gitconfig", ".npmrc") never matches, and
        # extensionless files ("Makefile", "LICENSE") have no suffix at all.
        # Rather than the bare metadata card, sniff the bytes and, when they're
        # text, offer the same viewers .txt gets. Binary keeps the metadata
        # fallback (empty list).
        entries, _ = _resolve_mode_list(["text", "code"])

    # Conditional templates (SPEC PT-8): a template folder may gate itself on
    # the file with a `condition.py`. Mark after resolution so gating is
    # orthogonal to the registry — it applies to whatever list survived,
    # built-in or user, main path or text-sniff fallback. Evaluation is
    # deferred to /api/fs/conditions so a slow gate never stalls the stat.
    _mark_conditions(entries)
    return entries, error


def _writable(path: str) -> bool:
    """True iff /api/fs/write would accept this path. An existing target needs
    W_OK on itself — the atomic os.replace would otherwise bypass a read-only
    bit via the parent directory — and a new file needs W_OK on its parent.
    Templates read this off the stat payload to render read-only mode up
    front; keep the two in agreement.

    Paths under a read-only mount are never writable, whatever the permission
    bits say: the rclone VFS (CacheMode=full) takes any write into its local
    cache and only fails at the async upload, so W_OK is a lie there."""
    # Local import, like _stat_payload's: server -> shell.mounts only,
    # keeping shell ↛ server acyclic.
    from fused_render.shell.mounts import mount_read_only

    if mount_read_only(path):
        return False
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    return os.access(os.path.dirname(path) or ".", os.W_OK)


# Response headers forwarded from the rclone serve on a proxied /api/fs/raw.
# Content-Length/-Range/Accept-Ranges make ranged readers (duckdb httpfs)
# work; Last-Modified/ETag let their caches revalidate.
_PROXY_HEADERS = ("content-length", "content-range", "content-type",
                  "accept-ranges", "last-modified", "etag")


def _proxy_raw(upstream: str, request: Request):
    """Forward one GET/HEAD (with its Range header) to a mount's localhost
    rclone serve and stream the answer back. None when the serve can't be
    reached at all — the caller then reads the file the ordinary way; an HTTP
    error from a live serve passes through as-is (a 416/404 is an answer,
    not a reason to fall back to a different read path mid-protocol)."""
    headers = {}
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    req = urllib.request.Request(upstream, headers=headers, method=request.method)
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        # Error responses carry protocol-level headers too — a 416's
        # `Content-Range: bytes */<size>` is how a range client learns the
        # file's length — so forward the same header set as on success.
        out = {k: v for k, v in e.headers.items() if k.lower() in _PROXY_HEADERS}
        try:
            payload = b"" if request.method == "HEAD" else e.read()
        finally:
            e.close()
        return Response(content=payload, status_code=e.code, headers=out)
    except OSError:
        return None
    out = {k: v for k, v in r.headers.items() if k.lower() in _PROXY_HEADERS}
    if request.method == "HEAD":
        r.close()
        return Response(status_code=r.status, headers=out)

    def body():
        try:
            while chunk := r.read(256 * 1024):
                yield chunk
        finally:
            r.close()

    return StreamingResponse(body(), status_code=r.status, headers=out)


def _stat_or_none(path: str) -> os.stat_result | None:
    """stat() for /api/fs/raw's 404 gate: None for missing paths and
    non-regular files alike (a directory has no raw bytes to serve)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if stat_mod.S_ISREG(st.st_mode) else None


def _stat_payload(path: str, is_dir: bool, st: os.stat_result | None = None) -> dict:
    """The /api/fs/stat shape. /api/fs/write returns it too, so the editor can
    re-arm its optimistic lock from a save response. Pass a pre-fetched `st` to
    avoid a redundant stat() — one remote round-trip under a mount."""
    if st is None:
        st = os.stat(path)
    templates, template_error = _templates_for(path, is_dir)
    # Local import, like api_fs_walk's: server -> shell.mounts only, keeping
    # shell ↛ server acyclic.
    from fused_render.shell.mounts import is_mount_backed

    payload = {
        "path": path,
        "name": os.path.basename(path) or path,
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "writable": _writable(path),
        # Bytes come from a remote (the path sits under a mount). Pages use
        # this to prefer ranged HTTP reads (/api/fs/raw) over local file I/O.
        "remote": is_mount_backed(path),
        "templates": templates,
    }
    if template_error:
        payload["template_error"] = template_error
    return payload


def _fs_stat(path: str):
    # One stat, not the exists()+isdir()+stat() trio: over a remote mount each
    # is a round-trip, so a plain metadata fetch cost 3 LISTs. os.path.exists()
    # returns False for any OSError, so mirror that -> 404.
    try:
        st = os.stat(path)
    except OSError:
        return _error(f"no such file or directory: {path}", status=404)
    return _stat_payload(path, stat_mod.S_ISDIR(st.st_mode), st)


def _fs_write(body: dict, x_fused: str | None):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    content = body.get("content")
    expected_mtime = body.get("expected_mtime")
    # New File / "must not clobber" callers set create=true: an existing path
    # is a 409 conflict (same wire string as rename/copy/mkdir) instead of a
    # silent overwrite.
    create = bool(body.get("create"))

    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    if not isinstance(content, str):
        return _error("'content' must be a string")
    if os.path.isdir(path):
        return _error(f"path is a directory: {path}")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return _error(f"parent directory does not exist: {parent}", status=404)

    # Read-only guard: refuse before touching anything. The atomic write
    # below replaces the target via the PARENT directory, so without this
    # check a chmod -w file would be silently overwritten. The bare "readonly"
    # error string is a wire contract — runtime.js writeFile turns it into a
    # typed error, like "conflict" below.
    if not _writable(path):
        return JSONResponse({"error": "readonly"}, status_code=403)

    # Optimistic lock: the editor sends the mtime it last saw; if the file
    # changed (or was deleted) underneath it, refuse so the edit doesn't
    # clobber someone else's write. Compare against the raw st_mtime float
    # that /api/fs/stat returns, with a tolerance for float round-tripping.
    exists = os.path.exists(path)
    if create and exists:
        return JSONResponse({"error": "conflict"}, status_code=409)
    if expected_mtime is not None:
        if not exists:
            return JSONResponse({"error": "conflict", "mtime": None}, status_code=409)
        current = os.stat(path).st_mtime
        if abs(current - expected_mtime) >= 1e-6:
            return JSONResponse({"error": "conflict", "mtime": current}, status_code=409)

    # Preserve the target's permission bits across an overwrite.
    mode = stat_mod.S_IMODE(os.stat(path).st_mode) if exists else None

    # Atomic write: land the bytes in a temp file in the same directory,
    # fsync, then os.replace onto the target so a reader never sees a
    # half-written file (and a crash leaves the original intact).
    fd, tmp = tempfile.mkstemp(dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return _error(f"cannot write {path}: {e}", status=400)

    return _stat_payload(path, False)


def _fs_mkdir(body: dict, x_fused: str | None):
    # Create a single directory. Parents are NOT auto-created (no mkdir -p):
    # a missing parent is a 400 so a typo'd path can't silently spawn a deep
    # tree. Mirrors _fs_write's guard order — X-Fused, absolute path, then
    # the filesystem-shape checks — and returns the /api/fs/stat payload so
    # the client can render the new folder without a follow-up stat.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return _error(f"parent directory does not exist: {parent}")
    if os.path.exists(path):
        return JSONResponse({"error": "conflict"}, status_code=409)
    # Read-only guard: the "readonly" wire string matches _fs_write's — the
    # parent must accept a new entry (_writable falls back to the parent's
    # W_OK for a path that does not yet exist).
    if not _writable(path):
        return JSONResponse({"error": "readonly"}, status_code=403)

    try:
        os.mkdir(path)
    except OSError as e:
        return _error(f"cannot create directory {path}: {e}")
    return _stat_payload(path, True)


def _trash_supported() -> bool:
    # Move-to-Trash is macOS-only (a ~/.Trash + Finder concept). Isolated so
    # tests can force it on/off without touching the global sys.platform.
    return sys.platform == "darwin"


def _trash_dest_name(name: str, counter: int) -> str:
    # Finder-style dedupe for a name already present in ~/.Trash: the first
    # occurrence keeps its name, later ones gain a " N" suffix before the
    # extension ("report.csv" -> "report 2.csv"); dirs / extensionless /
    # dotfile names take the suffix at the end ("folder" -> "folder 2").
    if counter <= 1:
        return name
    dot = name.rfind(".")
    if dot > 0:
        return f"{name[:dot]} {counter}{name[dot:]}"
    return f"{name} {counter}"


def _move_to_trash(path: str) -> None:
    # Move `path` into the user's ~/.Trash (macOS). A plain os.rename into
    # ~/.Trash is the fast path, with a " N" dedupe suffix when a name is
    # already there. A rename ACROSS devices (or any other OSError) can't be
    # done by rename, so it falls back to Finder via osascript, which copies +
    # removes itself. Raises on total failure so the caller reports it and the
    # frontend can fall back to a hard delete.
    trash = Path.home() / ".Trash"
    name = os.path.basename(path.rstrip("/"))
    try:
        trash.mkdir(parents=True, exist_ok=True)
        counter = 1
        dest = trash / _trash_dest_name(name, counter)
        while dest.exists():
            counter += 1
            dest = trash / _trash_dest_name(name, counter)
        os.rename(path, dest)
    except OSError:
        subprocess.run(
            [
                "osascript",
                "-e",
                f"tell application \"Finder\" to delete POSIX file {json.dumps(path)}",
            ],
            check=True,
            capture_output=True,
        )


def _fs_delete(body: dict, x_fused: str | None):
    # Remove a file or directory. With trash=true the target is moved to the
    # user's Trash instead of being erased (recoverable, macOS only). Otherwise
    # a hard delete: a directory needs recursive=true unless it is empty (an
    # empty dir is a plain os.rmdir); a non-empty dir without the flag is a 409
    # so a stray click can't wipe a subtree. Read-only targets are refused with
    # the same "readonly" contract as _fs_write.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    recursive = bool(body.get("recursive", False))
    trash = bool(body.get("trash", False))
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    if not os.path.exists(path):
        return _error(f"no such file or directory: {path}", status=404)
    if not _writable(path):
        return JSONResponse({"error": "readonly"}, status_code=403)

    if trash:
        # Non-darwin (or Trash otherwise unavailable) → a distinct 501 so the
        # frontend can fall back to the confirm-then-hard-delete flow.
        if not _trash_supported():
            return JSONResponse({"error": "trash unsupported"}, status_code=501)
        try:
            _move_to_trash(path)
        except Exception as e:  # noqa: BLE001 — rename OSError or osascript failure
            # A FAILED trash on a supported platform is a plain error, not the
            # 501 "unsupported" signal — that one routes the client into the
            # irreversible hard-delete fallback, which must never be the
            # response to a recoverable-delete attempt that merely failed.
            return _error(f"cannot move to Trash: {e}", status=500)
        return {"deleted": path, "trashed": True}

    try:
        # A symlink is removed as the link itself, never followed: rmtree on a
        # symlink-to-dir raises, and following it would delete the TARGET's
        # contents. Mirrors the `not os.path.islink` guard _fs_rename/_fs_copy
        # apply before their own rmtree.
        if os.path.isdir(path) and not os.path.islink(path):
            if recursive:
                shutil.rmtree(path)
            elif os.listdir(path):
                return JSONResponse(
                    {"error": "conflict", "message": "directory not empty"},
                    status_code=409,
                )
            else:
                os.rmdir(path)
        else:
            os.remove(path)
    except OSError as e:
        return _error(f"cannot delete {path}: {e}")
    return {"deleted": path, "trashed": False}


def _fs_rename(body: dict, x_fused: str | None):
    # Move/rename src -> dst. dst must be absolute and its parent writable
    # (same "outside"/readonly guards as elsewhere). An existing dst is a 409
    # unless overwrite=true; a missing src is a 404. shutil.move handles the
    # cross-device case os.replace can't; overwrite removes dst first so a
    # dir-over-dir move can't nest into it.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    src = body.get("src")
    dst = body.get("dst")
    overwrite = bool(body.get("overwrite", False))
    if not src or not os.path.isabs(src):
        return _error("'src' must be an absolute filesystem path")
    if not dst or not os.path.isabs(dst):
        return _error("'dst' must be an absolute filesystem path")
    # dst's parent must already exist — a rename never creates intermediate
    # dirs. Without this, a missing parent falls through to _writable (which
    # walks up to the nearest existing ancestor) and surfaces a misleading
    # "readonly" 403; a 400 is the honest error, same as _fs_write/_fs_mkdir.
    dst_parent = os.path.dirname(dst)
    if not os.path.isdir(dst_parent):
        return _error(f"parent directory does not exist: {dst_parent}")
    if not os.path.exists(src):
        return _error(f"no such file or directory: {src}", status=404)
    if os.path.isdir(src):
        # Same self/descendant guard as copy: moving a directory into itself
        # (or a child) would build the destination inside the source.
        s = os.path.abspath(src)
        d = os.path.abspath(dst)
        if d == s or d.startswith(s + os.sep):
            return _error("cannot move a directory into itself or a descendant")

    dst_exists = os.path.exists(dst)
    if dst_exists and not overwrite:
        return JSONResponse({"error": "conflict"}, status_code=409)
    # A move deletes the source, so the source must be writable too — otherwise
    # a rename could lift entries off a read-only mount (delete/write refuse).
    if not _writable(src) or not _writable(dst):
        return JSONResponse({"error": "readonly"}, status_code=403)

    try:
        if dst_exists:
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        shutil.move(src, dst)
    except OSError as e:
        return _error(f"cannot rename {src} -> {dst}: {e}")
    return _stat_payload(dst, os.path.isdir(dst))


def _fs_copy(body: dict, x_fused: str | None):
    # Copy src -> dst. File via copy2 (preserves metadata), dir via copytree.
    # Same error contract as rename (400 relative, 404 missing src, 409 dst
    # exists w/o overwrite, 403 readonly dst). Copying a directory into itself
    # or a descendant is refused (400) — copytree would otherwise recurse into
    # the destination it is still writing.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    src = body.get("src")
    dst = body.get("dst")
    overwrite = bool(body.get("overwrite", False))
    if not src or not os.path.isabs(src):
        return _error("'src' must be an absolute filesystem path")
    if not dst or not os.path.isabs(dst):
        return _error("'dst' must be an absolute filesystem path")
    # dst's parent must already exist — a copy never creates intermediate dirs.
    # Without this, a missing parent falls through to _writable (which walks up
    # to the nearest existing ancestor) and surfaces a misleading "readonly"
    # 403; a 400 is the honest error, same as _fs_write/_fs_mkdir.
    dst_parent = os.path.dirname(dst)
    if not os.path.isdir(dst_parent):
        return _error(f"parent directory does not exist: {dst_parent}")
    if not os.path.exists(src):
        return _error(f"no such file or directory: {src}", status=404)

    src_is_dir = os.path.isdir(src)
    if src_is_dir:
        # Normalize both ends so "self/descendant" catches ./ and trailing
        # slashes; the sep suffix stops /a/b matching /a/bc.
        s = os.path.abspath(src)
        d = os.path.abspath(dst)
        if d == s or d.startswith(s + os.sep):
            return _error("cannot copy a directory into itself or a descendant")

    dst_exists = os.path.exists(dst)
    if dst_exists and not overwrite:
        return JSONResponse({"error": "conflict"}, status_code=409)
    if not _writable(dst):
        return JSONResponse({"error": "readonly"}, status_code=403)

    try:
        if src_is_dir:
            if dst_exists:
                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            shutil.copytree(src, dst)
        else:
            # copy2 onto an existing dir would drop the file inside it; a
            # dir dst must be replaced wholesale to mean "become this file".
            if dst_exists and os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            shutil.copy2(src, dst)
    except OSError as e:
        return _error(f"cannot copy {src} -> {dst}: {e}")
    return _stat_payload(dst, os.path.isdir(dst))


def create_app(start_dir: str) -> FastAPI:
    # Engine (D69/D70 + SPEC §20): validate any FUSED_RENDER_ENGINE override
    # ONCE at startup — this raises on a bad value and fails loudly for
    # `=fused` when the package is missing, and logs the choice. Dispatch
    # itself goes through the single live resolver (`prefs.effective_engine`,
    # which re-reads the override + pref + availability per request), so the
    # Preferences switch and a mid-session install both apply with no restart
    # and the page's "running" label never drifts from what actually runs.
    _forced_engine()

    def current_engine() -> str:
        return shell_prefs.effective_engine()

    app = FastAPI(title="fused-render")

    @app.exception_handler(Exception)
    async def unhandled_exception(request, exc):
        # A bare "Internal Server Error" with an empty body is undebuggable on
        # a DMG install: Finder-launched apps have no visible stderr, so the
        # traceback used to vanish (e.g. a right-click "Open with FusedRender"
        # that 500s on /render or /api/run leaves nothing to report). Put the
        # traceback in the response body (local single-user tool, D3 — the
        # only reader owns the machine) AND in the log file so a later
        # `Open logs` gives the full story. Log with the request line so a
        # noisy log still pins the failure to a URL.
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.error(
            "unhandled error on %s %s\n%s", request.method, request.url.path, tb
        )
        return _error(
            f"fused-render internal error on {request.method} "
            f"{request.url.path}:\n\n{tb}",
            status=500,
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Vendored JS libraries (marked, CodeMirror) that templates load by absolute
    # URL. Templates render at /render?path=… so a relative <script src> in a
    # template would resolve against /render, not the templates dir — hence a
    # dedicated absolute mount. Everything here is a committed local file: the
    # product has no network at runtime (no CDNs anywhere).
    app.mount(
        "/template-assets",
        StaticFiles(directory=os.path.join(TEMPLATES_DIR, "vendor")),
        name="template-assets",
    )
    # First-party ESM shared by the sci preview templates (geotiff/netcdf
    # sciViz core — colormaps, stretch/stats/histogram, canvas draw helpers, UI
    # kit). Same absolute-URL rationale as /template-assets above. A dedicated
    # mount (rather than nesting under templates/vendor/) keeps vendor/ strictly
    # third-party; templates/shared/ has no template.html, so it can never be
    # resolved as a template name.
    app.mount(
        "/template-shared",
        StaticFiles(directory=os.path.join(TEMPLATES_DIR, "shared")),
        name="template-shared",
    )

    # Static asset mounts are high-volume (every preview pulls runtime.js,
    # icons, vendored bundles) and almost never the cause of an "Internal
    # Server Error" or a bad right-click-open — logging them would churn the
    # rotating file and push the interesting lines out. The request flow that
    # matters (/view, /render, /api/*) is everything else.
    _LOG_SKIP_PREFIXES = ("/static/", "/template-assets/", "/template-shared/")

    @app.middleware("http")
    async def no_cache_and_log(request, call_next):
        # App code changes between restarts and user files change on disk;
        # stale browser caches of shell/runtime JS cause confusing half-old UIs.
        # Also the browser request log (SPEC SV-3): one INFO line per request
        # with status + duration, so the log reconstructs the sequence of calls
        # a page made — the context you need to see *which* request 500'd and
        # what led to it. A 500 raised in a route escapes call_next; log the
        # request line before re-raising so the access trail stays complete
        # (the catch-all handler then logs the traceback).
        path = request.url.path
        logged = not path.startswith(_LOG_SKIP_PREFIXES)
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            if logged:
                dur = (time.monotonic() - start) * 1000
                logger.info("%s %s -> 500 (%.0f ms)", request.method, path, dur)
            raise
        if logged:
            dur = (time.monotonic() - start) * 1000
            logger.info(
                "%s %s -> %s (%.0f ms)", request.method, path, response.status_code, dur
            )
        response.headers["Cache-Control"] = "no-cache"
        return response

    # React shell (D52/D54): built by Vite from frontend/ into static/
    # shell-dist/. The output is NOT committed — dev machines build it
    # themselves; wheels/DMG builds run it via the hatch hook
    # (scripts/hatch_build.py). Fail at startup with the fix, not with a
    # bare 404 on first page load.
    shell_path = os.path.join(STATIC_DIR, "shell-dist", "index.html")
    if not os.path.exists(shell_path):
        raise RuntimeError(
            "React shell not built (fused_render/static/shell-dist/ missing). "
            "Run: cd frontend && npm install && npm run build"
        )

    @app.get("/")
    def shell_root():
        return FileResponse(shell_path)

    @app.get("/view/{path:path}")
    def shell_view(path: str):
        return FileResponse(shell_path)

    @app.get("/embed/{path:path}")
    def shell_embed(path: str):
        return FileResponse(shell_path)

    # Shell-specific state backends live in fused_render/shell/ (bookmarks,
    # prefs, recents), kept out of this module's fs/render internals.
    app.include_router(bookmarks_router)
    app.include_router(prefs_router)
    app.include_router(recents_router)
    # Mounts: remote storage mounted as local paths via rclone rcd
    # (shell/mounts.py). startup() remounts every mount in a background
    # thread; mounts deliberately survive server restarts.
    from fused_render.shell import mounts as shell_mounts
    from fused_render.shell import prefetch as shell_prefetch

    app.include_router(shell_mounts.router)
    shell_mounts.startup()
    # GitHub deep links (SPEC §26, D110): GET /clone confirm page +
    # POST /api/clone sparse-clone into ~/Documents/Fused. deeplink.py never
    # imports server, so the include stays acyclic like shell/*.
    from fused_render.deeplink import router as deeplink_router

    app.include_router(deeplink_router)
    # Deploy (hosted publish through the fused CLI) — export + `fused share`
    # orchestration and the per-page deployment pointer store (deploy.py).
    app.include_router(deploy_router)
    # Fused account (in-app `fused cloud login/logout`, account.py) — the
    # sign-in the managed-env deploys need, without a terminal.
    app.include_router(account_router)
    # Template management (templates_api.py) — the Templates view backend:
    # inventory across sources, registry bindings edit, import/export. It owns
    # GET /api/templates/registry (the extended §2.2 shape). Imported here
    # (not at module top) because templates_api reads server helpers/dirs —
    # a lazy include keeps the server<->templates_api import acyclic.
    from fused_render.templates_api import router as templates_router

    app.include_router(templates_router)

    # Per-file session restore (LSN-*): a viewed file remembers its last URL
    # query in the "lastSession" key of its <file>.json sidecar. GET is a read
    # endpoint (no X-Fused guard); PUT mutates so it carries the D36 guard.
    @app.get("/api/session")
    def api_session_get(path: str):
        return _session_get(path)

    @app.put("/api/session")
    def api_session_put(
        body: dict = Body(...), x_fused: str | None = Header(default=None)
    ):
        return _session_put(body, x_fused)

    @app.get("/api/config")
    def api_config():
        return {
            "start_dir": start_dir,
            "home": os.path.expanduser("~"),
            # The Fused workspace dir (~/Documents/Fused, D81) — the sidebar's
            # "Fused" entry navigates here. Path only; the dir is created + seeded
            # at the process entry points (cli/app), not on this read.
            "fused_dir": fused_dir(),
            # The fused-render package version, surfaced in the sidebar brand.
            "version": __version__,
            # Which /api/run engine is in effect (D69/§20): "fused" | "builtin".
            # Read per request — it can change under the Preferences switch.
            "engine": current_engine(),
        }

    # GET /api/templates/registry moved to templates_api.py (extended §2.2
    # shape) and registered via templates_router above.

    @app.get("/api/fs/stat")
    def api_fs_stat(path: str):
        return _fs_stat(path)

    @app.get("/api/fs/conditions")
    def api_fs_conditions(path: str):
        # Deferred condition.py evaluation (SPEC CT-12): stat marks gated
        # templates `conditional`; this resolves them while the client already
        # renders the first unconditional template.
        return _conditions_payload(path)

    @app.get("/api/fs/list")
    def api_fs_list(path: str):
        if not os.path.isdir(path):
            return _error(f"not a directory: {path}", status=400)
        entries = []
        # scandir over listdir+per-entry stat/isdir: the readdir already carries
        # each entry's type, so is_dir() is free and stat() is a single call —
        # the old loop did two stats per entry (os.stat + os.path.isdir's own
        # stat), i.e. 2N remote round-trips under a mount. Both follow symlinks,
        # matching the previous os.stat/os.path.isdir behavior.
        try:
            with os.scandir(path) as it:
                dents = list(it)
        except OSError as e:
            broken = shell_mounts.broken_mount_error(path)
            if broken:
                return _error(broken, status=503)
            return _error(f"cannot read directory {path}: {e}", status=400)
        if not dents:
            # A dead mount leaves a plain empty dir (or a wedged NFS mount
            # serving nothing) at the mountpoint — an empty listing under
            # mounts/ is only trustworthy while the mount is healthy.
            broken = shell_mounts.broken_mount_error(path)
            if broken:
                return _error(broken, status=503)
        for de in dents:
            try:
                st = de.stat()
                is_dir = de.is_dir()
            except OSError:
                continue  # unreadable entries skipped silently
            entries.append(
                {
                    "name": de.name,
                    "is_dir": is_dir,
                    "size": None if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        ignored = _git_ignored(path, [e["name"] for e in entries])
        for e in entries:
            e["ignored"] = e["name"] in ignored
        # Case-insensitive primary order, then exact name as a deterministic
        # tiebreak so names differing only by case get a stable order instead of
        # falling back to arbitrary os.listdir() order (which changes per call).
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower(), e["name"]))
        return {"path": path, "entries": entries}

    @app.get("/api/fs/walk")
    def api_fs_walk(path: str, hidden: str = "0", stream: str = "0"):
        # Recursive listing of a directory subtree, for the explorer search
        # (flat, ranked client-side). Walks BREADTH-FIRST so shallow entries —
        # the ones a search almost always targets — are all emitted before any
        # deep subtree can exhaust the WALK_MAX_ENTRIES cap (the old
        # depth-first walk let one big sibling starve every later one). Prunes
        # WALK_IGNORE_DIRS entirely, prunes gitignored entries inside git
        # repositories (see _walk_bfs — which is why walk entries carry no
        # `ignored` dimming flag: nothing ignored survives to be dimmed),
        # emits WALK_LEAF_DIR_SUFFIXES packages without descending, never
        # follows symlinks, and skips unreadable entries silently (matches
        # /api/fs/list). `rel` is posix-relative to `path`.
        #
        # `hidden=1` (explicit intent: the user typed a dot-leading query)
        # includes dot-files and descends into dot-dirs. WALK_IGNORE_DIRS and
        # gitignore pruning apply regardless — those trees are noise, not
        # "hidden data", and letting hidden=1 descend into .git/node_modules
        # would flood the results with machine-managed junk.
        #
        # `stream=1` returns NDJSON: zero or more `{"entries": [...]}` batch
        # lines (WALK_BATCH_SIZE each) followed by exactly one terminal
        # `{"done": true, "truncated": bool, "total": n}` line. The client
        # scores batches as they arrive, so first results paint while the walk
        # is still running. Closing the connection cancels the walk (Starlette
        # closes the generator on disconnect). Without `stream=1` the response
        # is the original single-JSON shape, unchanged for old clients.
        include_hidden = hidden == "1"
        if not os.path.isdir(path):
            return _error(f"not a directory: {path}", status=400)
        walker = _walk_bfs(path, include_hidden)
        # Remote-mount clamp: under a mount mountpoint every directory is
        # a remote LIST round-trip, so the cap drops to WALK_MAX_ENTRIES_REMOTE
        # (see the constant's comment). Resolved per request — the mounts dir
        # follows home_dir()'s env-based redirection.
        from fused_render.shell.mounts import mounts_dir as _mounts_dir

        mroot = os.path.abspath(_mounts_dir())
        under_mount = os.path.abspath(path).startswith(mroot + os.sep)
        max_entries = WALK_MAX_ENTRIES_REMOTE if under_mount else WALK_MAX_ENTRIES

        if stream != "1":
            entries = []
            truncated = False
            for entry in walker:
                entries.append(entry)
                if len(entries) >= max_entries:
                    truncated = True
                    break
            return {"path": path, "entries": entries, "truncated": truncated}

        def ndjson():
            batch = []
            total = 0
            truncated = False
            last_flush = time.monotonic()
            for entry in walker:
                batch.append(entry)
                total += 1
                now = time.monotonic()
                if len(batch) >= WALK_BATCH_SIZE or now - last_flush >= WALK_FLUSH_INTERVAL_S:
                    yield json.dumps({"entries": batch}) + "\n"
                    batch = []
                    last_flush = now
                if total >= max_entries:
                    truncated = True
                    break
            if batch:
                yield json.dumps({"entries": batch}) + "\n"
            yield json.dumps({"done": True, "truncated": truncated, "total": total}) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    @app.api_route("/api/fs/raw", methods=["GET", "HEAD"])
    async def api_fs_raw(path: str, request: Request):
        # Mount-backed file with a live HTTP serve: proxy the bytes from
        # rclone instead of reading through the kernel mount. Concurrent
        # ranged reads (duckdb's httpfs) through the NFS mount stall its 1s
        # RPC timeout and get the whole mount dropped; the same reads proxied
        # over HTTP are merely slow. Explicit HEAD support matters: httpfs
        # HEADs for the length first, and Starlette's implicit HEAD-on-GET
        # would run the full upstream GET just to drop the body.
        #
        # No stat() before a serve-backed GET: on a mount that's a VFS
        # getattr — a full remote round trip (~1s cold), paid serially
        # before the read even starts, per never-listed object. The serve
        # and the store both 404 a missing object themselves (_proxy_raw
        # passes error statuses through), so existence falls out of the
        # read. Only HEAD (answered from st_size) and the local-file
        # fallback below still stat.
        upstream = await asyncio.to_thread(shell_mounts.serve_url_for, path)
        if upstream is not None:
            # Every remote read flows through here, so this is where the
            # shell learns a mounted file is in use: kick off (or just
            # touch) its background whole-file prefetch. Cheap no-op after
            # the first call; templates stay mount-agnostic (prefetch.py).
            shell_prefetch.schedule(path, upstream)
            # HEAD answered from a VFS getattr rather than proxied: ranged
            # clients (duckdb httpfs, fsspec/zarr, geotiff) probe the length
            # before reading, and proxying that probe is a full remote round
            # trip for headers the getattr already knows. The serve reads
            # the same rclone remote, so the sizes agree. In a thread — a
            # cold getattr would otherwise stall the event loop.
            if request.method == "HEAD":
                st = await asyncio.to_thread(_stat_or_none, path)
                if st is None:
                    return _error(f"no such file: {path}", status=404)
                media_type, _ = mimetypes.guess_type(path)
                return Response(status_code=200, headers={
                    "content-length": str(st.st_size),
                    "content-type": media_type or "application/octet-stream",
                    "accept-ranges": "bytes",
                    "last-modified": email.utils.formatdate(
                        st.st_mtime, usegmt=True),
                })
            # Cold reads go straight to the store: the serve's VFS layer
            # serializes concurrent uncached range reads of one file (an
            # analytical scan pays ~0.25s per seek) and its per-file open
            # ceremony dwarfs a small metadata fetch (zarr.json), while the
            # store answers the same GETs in parallel. Once the prefetch
            # has landed the whole file in the serve cache, the serve
            # replays ranges from local disk and wins again. A 307 rather
            # than a proxied fetch: the client (duckdb httpfs, fsspec)
            # re-issues each GET against the store itself with its own
            # pooled parallel connections — proxying here paid a fresh TLS
            # handshake per range read (measured 2x on the point-read
            # phase) and streamed every byte through this process twice.
            # Whole-file GETs redirect too (zarr stores read many tiny
            # metadata files whole; schedule() above warms the serve cache
            # regardless). GET only: presigned links are minted for GET (a
            # HEAD fails their signature). Native clients only: a browser
            # fetch would follow the redirect cross-origin and die on CORS
            # — browsers always send Sec-Fetch-Mode, duckdb's httpfs never
            # does, so its absence is the gate.
            if ("sec-fetch-mode" not in request.headers
                    and not shell_prefetch.is_done(path)):
                direct = await asyncio.to_thread(
                    shell_mounts.upstream_url_for, path)
                if direct:
                    return RedirectResponse(direct, status_code=307)
            # Not redirected (browser, warm read, or no direct URL): proxy the
            # bytes. Guard non-files here — the cold redirect path above is the
            # never-listed-object hot path and stays stat-free, but a directory
            # proxied through rclone serve comes back as a 200 HTML listing, so
            # stat before serving (warm getattr is cheap; a directory 404s).
            st = await asyncio.to_thread(_stat_or_none, path)
            if st is None:
                return _error(f"no such file: {path}", status=404)
            resp = await asyncio.to_thread(_proxy_raw, upstream, request)
            if resp is not None:
                return resp  # upstream unreachable -> plain file read below
        st = await asyncio.to_thread(_stat_or_none, path)
        if st is None:
            return _error(f"no such file: {path}", status=404)
        media_type, _ = mimetypes.guess_type(path)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @app.websocket("/api/fs/events")
    async def api_fs_events(ws: WebSocket):
        # File-change feed (SPEC §13.2), WebSocket not SSE (D74): every rendered
        # pane holds one of these open for the lifetime of the page, and SSE
        # rides ordinary HTTP/1.1 — Chrome caps those at 6 per origin, so a
        # 6-pane panel pinned every socket and all later fetches (/api/run!)
        # queued browser-side forever. WebSockets live in a separate, much
        # larger connection pool. Polling stat every 200ms is dependency-free
        # and cheap at local scale; upgrading to real FS events later is
        # internal to this endpoint. Messages are JSON: {path, mtime} on
        # change, {keepalive: true} every 15 s (WF-3).
        await ws.accept()
        paths = ws.query_params.getlist("path")

        def mtime_of(p):
            try:
                return os.stat(p).st_mtime
            except OSError:
                return None

        async def poll():
            last = {p: mtime_of(p) for p in paths}
            ticks = 0
            while True:
                await asyncio.sleep(0.2)
                for p in paths:
                    m = mtime_of(p)
                    if m != last[p]:
                        last[p] = m
                        await ws.send_text(json.dumps({"path": p, "mtime": m}))
                ticks += 1
                if ticks % 75 == 0:
                    await ws.send_text(json.dumps({"keepalive": True}))

        poller = asyncio.create_task(poll())
        try:
            # Drain the receive side purely to learn about disconnect; the
            # poll loop alone would only notice on its next send.
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            poller.cancel()

    @app.post("/api/fs/reveal")
    def api_fs_reveal(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        # Open the path in the OS file manager (Finder / Explorer / xdg).
        # Browsers block file:// navigation from http pages, so the breadcrumb's
        # reveal button goes through the server, which is local-only.
        # A file is revealed selected inside its folder; a directory is opened.
        guard = _require_fused(x_fused)
        if guard is not None:
            return guard

        path = body.get("path")
        if not path or not os.path.isabs(path):
            return _error("'path' must be an absolute filesystem path")
        if not os.path.exists(path):
            return _error(f"no such path: {path}", status=404)

        is_dir = os.path.isdir(path)
        if sys.platform == "darwin":
            cmd = ["open", path] if is_dir else ["open", "-R", path]
        elif os.name == "nt":
            # Explorer needs native backslash paths — forward slashes make
            # /select, silently open the default folder instead.
            win_path = os.path.normpath(path)
            cmd = ["explorer", win_path] if is_dir else f'explorer /select,"{win_path}"'
        else:
            cmd = ["xdg-open", path if is_dir else os.path.dirname(path)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return JSONResponse({"ok": True})

    @app.post("/api/fs/write")
    def api_fs_write(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        return _fs_write(body, x_fused)

    @app.post("/api/fs/mkdir")
    def api_fs_mkdir(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        return _fs_mkdir(body, x_fused)

    @app.post("/api/fs/delete")
    def api_fs_delete(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        return _fs_delete(body, x_fused)

    @app.post("/api/fs/rename")
    def api_fs_rename(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        return _fs_rename(body, x_fused)

    @app.post("/api/fs/copy")
    def api_fs_copy(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        return _fs_copy(body, x_fused)

    @app.get("/render")
    def render(path: str):
        if not os.path.isfile(path):
            return _error(f"no such file: {path}", status=404)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                html = f.read()
        except OSError as e:
            return _error(f"cannot read {path}: {e}", status=400)

        # Always inject the runtime.
        injection = '<script src="/static/runtime.js"></script>'
        lower = html.lower()
        head_idx = lower.find("<head>")
        if head_idx != -1:
            insert_at = head_idx + len("<head>")
            html = html[:insert_at] + injection + html[insert_at:]
        else:
            html = injection + html
        return HTMLResponse(html)

    @app.post("/api/run")
    async def api_run(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        guard = _require_fused(x_fused)
        if guard is not None:
            return guard

        py = body.get("py")
        html = body.get("html")
        params = body.get("params") or {}

        # Cold mount-backed reads: swap the raw-proxy source_url for the
        # store's own URL before the reader sees it. The /api/fs/raw 307
        # already sends cold ranged GETs to the store, but a redirect
        # defeats httpfs connection pooling — duckdb re-follows it per
        # range read and opens a fresh TLS connection to the store each
        # time (measured ~3x on a cold open: schema 8.5s vs 3.4s, a
        # 9-column page 14.5s vs 3.8s). Handing the reader the direct URL
        # up front lets httpfs pool its store connections normally. Done
        # here in the server, not in templates: pages keep sending the raw
        # URL and stay mount-agnostic. Warm files (prefetch landed) keep
        # the raw URL so the serve replays ranges from local disk; the
        # explicit schedule() below matters because a direct-reading run
        # never touches /api/fs/raw, which is otherwise the only place the
        # prefetch learns a file is in use.
        src = params.get("source_url")
        if isinstance(src, str):
            parts = urlsplit(src)
            fpath = dict(parse_qsl(parts.query)).get("path")
            if parts.path.endswith("/api/fs/raw") and fpath:
                upstream = shell_mounts.serve_url_for(fpath)
                if upstream is not None and not shell_prefetch.is_done(fpath):
                    shell_prefetch.schedule(fpath, upstream)
                    direct = await asyncio.to_thread(
                        shell_mounts.upstream_url_for, fpath)
                    if direct:
                        params = dict(params, source_url=direct)

        if not py:
            return _error("request body must include 'py': a path to a Python file")

        if os.path.isabs(py):
            resolved = py
        else:
            if not html:
                return _error(
                    "'py' is a relative path but 'html' was not provided; "
                    "either send an absolute 'py' path or include 'html' so it can be resolved"
                )
            resolved = os.path.normpath(os.path.join(os.path.dirname(html), py))

        # Engine dispatch (D69/§20): both paths return the same wire shape
        # ({ok, result, error:{type,message,traceback}, stdout} — the fused
        # engine adds stderr/duration_ms), so pages never see which ran.
        # Resolved per request: the Preferences switch applies to the next
        # run, no restart (a set FUSED_RENDER_ENGINE pins it instead).
        if current_engine() == "fused":
            from fused_render import engine as _engine

            result = await _engine.run_python(resolved, params)
        else:
            # The built-in executor blocks on a subprocess; keep the event
            # loop free (the endpoint is async now for the engine's sake).
            result = await asyncio.to_thread(run_python, resolved, params)
        # Tell the runtime which absolute file actually ran so it can watch it
        # for auto-reload (LR-2). Set on failed runs too, so a broken py that
        # gets fixed still triggers a reload.
        result["resolved_py"] = resolved
        return JSONResponse(result)

    @app.post("/api/export")
    def api_export(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        guard = _require_fused(x_fused)
        if guard is not None:
            return guard

        from fused_render.export import ExportError, export_page

        page = body.get("page")
        out = body.get("out")
        if not page or not os.path.isabs(page):
            return _error("'page' must be an absolute path to the .html page")
        if not out or not os.path.isabs(out):
            return _error("'out' must be an absolute path to the output directory")

        # Optional file selection (same as the Deploy modal): extra files to bundle
        # beyond the literal-call scan, and files to drop from it. Absent -> auto-only.
        include = body.get("include") or []
        exclude = body.get("exclude") or []
        for name, value in (("include", include), ("exclude", exclude)):
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                return _error(f"'{name}' must be an array of relative file paths")

        try:
            plan = export_page(page, out, include=include, exclude=exclude)
        except ExportError as e:
            return _error(str(e))

        return {
            "out": os.path.abspath(out),
            "entrypoints": [{"path": e.path, "name": e.name, "file": e.file} for e in plan.entrypoints],
            "assets": [{"path": a.path, "name": a.name, "file": a.file} for a in plan.assets],
            "warnings": plan.warnings,
        }

    return app
