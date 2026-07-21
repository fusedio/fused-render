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
import hashlib
import itertools
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
from types import SimpleNamespace
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
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
# Flat cap on a single /api/fs/list response, across all three routes (direct,
# rc, local). An unbounded listing of a directory with a million entries builds
# and serializes a million-entry JSON response — slow to produce, slow to render.
# The response's `truncated` flag (and, on the resumable direct route, its
# `cursor`) tells the client the listing is partial. Module-level so tests can
# shrink it.
LIST_MAX_ENTRIES = 10_000
# Per-request cap for the RESUMABLE direct (S3/GCS) listing route. Deliberately
# one store page: each page runs seconds on a slow bucket (mur-sst ~2s), so a
# bigger first paint just multiplies the wait, and unlike the local/rc routes
# the client can always fetch the next 1000 via the cursor (Load more). Module-
# level so tests can shrink it.
S3_LIST_MAX_ENTRIES = 1_000
# Much smaller cap when the walked path sits under a mount mountpoint
# (shell/mounts.py): there every directory listing is a remote LIST call
# (S3 etc.), so an unbounded walk over a bucket is a slow, potentially paid
# API storm. The walk truncates early and the existing `truncated` flag tells
# the client search was bounded.
WALK_MAX_ENTRIES_REMOTE = 2_000
# Per-directory hard timeout for the rc listing of a mount-backed dir during a
# walk (see _walk_bfs). Shorter than the interactive fs/list timeout: a walk
# fans out across many directories, so a single slow/huge one is skipped (the
# walk moves on) rather than stalling the whole subtree — same "dead mount ->
# skipped dir" safety, without failing the request.
WALK_RC_LIST_TIMEOUT_S = 10.0
# Overall wall-clock budget for accumulating direct (S3/GCS) pages into ONE
# /api/fs/list response. The per-page timeout (mounts.S3_LIST_TIMEOUT_S /
# GCS_LIST_TIMEOUT_S, 15s) bounds a single
# page, but page COUNT is unbounded — a prefix that returns few keys per page
# could run many pages and stall a request for minutes. On budget exhaustion the
# accumulator stops and returns what it has with the last continuation token, a
# valid resumable page (truncated=True, cursor set), NOT an error. Kept well
# under the rc timeouts because this is FIRST-PAINT latency: on a slow bucket
# (mur-sst pages run ~2s each) the user waits this long for the partial listing,
# and Load more resumes from the cursor. Module-level so tests can shrink it.
S3_LIST_OVERALL_TIMEOUT_S = 8.0
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


class _RcDirEntry:
    """os.DirEntry-shaped view of one rclone operations/list entry, so
    _walk_bfs can consume mount-backed listings (fetched via the rcd rc API,
    off the kernel NFS mount) through the exact same loop as local os.scandir
    entries. Remote listings carry no symlinks (is_symlink is always False) and
    the size/mtime an os.stat would return are already in the entry, so stat()
    never touches the kernel."""

    __slots__ = ("name", "_is_dir", "_stat")

    def __init__(self, entry, mtime):
        self.name = entry.get("Name")
        self._is_dir = bool(entry.get("IsDir"))
        self._stat = SimpleNamespace(st_size=entry.get("Size"), st_mtime=mtime)

    def is_dir(self):
        return self._is_dir

    def is_symlink(self):
        return False

    def stat(self):
        return self._stat


def _mount_list_item(de):
    """Map one rc/direct listing entry (Name/Size/IsDir/ModTime, the shared shape
    of rc_list_dir and the direct S3/GCS pagers) to an /api/fs/list item.
    `ignored` is always
    False under a mount: there's no git repo there, and `git check-ignore`
    against a mount path is the very kernel I/O these routes avoid."""
    from fused_render.shell import mounts as shell_mounts

    is_dir = bool(de.get("IsDir"))
    return {
        "name": de.get("Name"),
        "is_dir": is_dir,
        "size": None if is_dir else de.get("Size"),
        "mtime": shell_mounts.rc_modtime_epoch(de.get("ModTime")),
        "ignored": False,
    }


def _sort_entries(entries):
    """Sort /api/fs/list items in place and return them: dirs first, then
    case-insensitive by name with the exact name as a deterministic tiebreak so
    case-only variants get a stable order. The single sort key for all three
    list routes (direct, rc, local)."""
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower(), e["name"]))
    return entries


def _list_response(path, entries, truncated, cursor):
    """The single /api/fs/list response shape, shared by all three routes."""
    return {"path": path, "entries": entries,
            "truncated": truncated, "cursor": cursor}


def _accumulate_direct_pages(path, cursor, max_entries, *,
                             page_timeout=None, overall_timeout=None):
    """Accumulate raw direct-listing entries (Name/Size/IsDir/ModTime dicts, the
    shared rc/direct shape) for a mount-backed dir on an anonymous S3 or GCS
    remote, up to `max_entries`. The one page-accumulation loop shared by
    /api/fs/list and the walk; the backend (S3 ListObjectsV2 vs GCS
    objects.list) is picked per-path by shell_mounts.direct_list_page.

    Each page requests only min(1000, remaining) keys, so the accumulation never
    overshoots `max_entries` (a whole extra 1000-key page could otherwise push a
    LIST_MAX_ENTRIES=10k cap to 10,999) while the returned continuation token
    still resumes cleanly. `overall_timeout` bounds total wall time across pages
    (page count is otherwise unbounded); on exhaustion the loop stops mid-listing
    and returns the last token, a valid resume point.

    Returns (raw_entries, next_token). next_token is non-None exactly when more
    entries remain (cap hit, budget hit with more pending, or the listing was
    truncated) — i.e. the listing is partial. Raises shell_mounts.DirectListError
    only when the FIRST page fails (nothing fetched, so the rc fallback is worth
    trying); a failure after at least one page returns what was accumulated with
    the failed page's continuation token as the resume point — on a slow bucket
    the per-page timeout shrinks toward the budget's deadline and the last
    page routinely times out, and discarding thousands of fetched entries to
    re-list via rc (which can't paginate at all) would turn a partial success
    into a guaranteed 503."""
    from fused_render.shell import mounts as shell_mounts

    entries: list = []
    token = cursor or None
    deadline = None if overall_timeout is None else time.monotonic() + overall_timeout
    while True:
        remaining = max_entries - len(entries)
        if remaining <= 0:
            break
        t = page_timeout
        # The first page always runs with the full page timeout (the progress
        # guarantee — even a zero budget returns one page). Later pages shrink
        # to the budget's remainder, and stop before it reaches zero: a
        # non-positive timeout would hit urlopen as a ValueError, not a
        # DirectListError (Bugbot), and token is already a valid resume point.
        if deadline is not None and entries:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            t = left if t is None else min(t, left)
        try:
            page, next_token = shell_mounts.direct_list_page(
                path, max_keys=min(1000, remaining), continuation=token, timeout=t)
        except shell_mounts.DirectListError:
            if not entries:
                raise
            logger.warning("direct-listing page for %r failed mid-listing; "
                           "returning %d accumulated entries as partial",
                           path, len(entries))
            return entries, token
        token = next_token
        entries.extend(page)
        if token is None:
            break
        # Budget checked AFTER a page so the loop always makes progress; the
        # token from the last fetched page is a valid resume point.
        if deadline is not None and time.monotonic() >= deadline:
            break
    return entries, token


def _list_direct(path, cursor):
    """Accumulate direct-listing pages (S3 ListObjectsV2 / GCS objects.list) for
    a mount-backed dir on an anonymous S3 or GCS remote into sorted /api/fs/list
    items, up to S3_LIST_MAX_ENTRIES within an overall time budget — a
    deliberately small per-request cap, since this route is resumable (Load more
    pages in the rest). Returns (entries, next_token); a non-None token means the
    listing is partial and resumable. Raises shell_mounts.DirectListError on any
    page failure so the caller can fall back to the rc route."""
    raw, token = _accumulate_direct_pages(
        path, cursor, S3_LIST_MAX_ENTRIES,
        overall_timeout=S3_LIST_OVERALL_TIMEOUT_S)
    # Sorted over what was fetched, not the whole directory — a truncated
    # listing is honestly partial (see the endpoint's sort caveat). Skip any
    # entry missing a Name (a malformed page must not 500 the request).
    entries = _sort_entries([_mount_list_item(de) for de in raw if de.get("Name")])
    return entries, token


# Yielded by _walk_bfs when a directory's listing was cut short (direct S3/GCS
# pages stopped early, rc listing over the per-dir cap, or a per-dir rc/direct
# failure skipped it). The walk's `truncated` flag counts YIELDED entries, but
# dotfile /
# gitignore filtering means a dir cut at the per-dir cap can yield fewer than the
# cap while thousands of keys went unlisted — so incompleteness is signalled
# out-of-band with this sentinel rather than inferred from the entry count. The
# endpoint sets truncated=True on it and emits nothing.
_WALK_TRUNCATED = object()


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
    from fused_render.shell import mounts as shell_mounts

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
        # A mount-backed root gets no repo: mounts hold no git repositories, and
        # `git -C <mount> rev-parse` (like every gitignore check below) is kernel
        # I/O on the mount we're deliberately routing around.
        top = None if shell_mounts.is_mount_backed(path) else _repo_toplevel(path)
        top_rel = "" if top is None else os.path.relpath(path, top).replace(os.sep, "/")
        queue = deque([(path, "", top, "" if top_rel == "." else top_rel)])
        while queue:
            current, rel_base, repo, repo_rel_base = queue.popleft()
            # Mount-backed dir: list it via the rcd rc API, off the kernel mount
            # (see rc_list_dir / the mur-sst incident). A dir that times out or
            # can't be listed is skipped and the walk moves on, rather than
            # failing the whole request or wedging the mount.
            mount_backed = shell_mounts.is_mount_backed(current)
            if mount_backed:
                is_root = current == path
                listed = None
                dir_cut = False  # this dir's listing stopped short (1.3)
                # Anonymous S3/GCS dir: page the store's own listing API (fast,
                # non-timeout-prone) up to the remote walk cap instead of the rc
                # listing rclone can't paginate. On any failure, fall back to rc
                # for THIS dir (same skip-on-failure semantics as below).
                if shell_mounts.direct_list_capable(current):
                    try:
                        listed, direct_token = _accumulate_direct_pages(
                            current, None, WALK_MAX_ENTRIES_REMOTE,
                            page_timeout=WALK_RC_LIST_TIMEOUT_S,
                            overall_timeout=WALK_RC_LIST_TIMEOUT_S)
                        if direct_token is not None:
                            dir_cut = True  # more keys remained unlisted
                    except shell_mounts.DirectListError:
                        listed = None  # fall back to the rc path for this dir
                if listed is None:
                    try:
                        listed = shell_mounts.rc_list_dir(
                            current, timeout=WALK_RC_LIST_TIMEOUT_S)
                    except shell_mounts.RcListError:
                        # The ROOT listing failing is fatal — surface it with the
                        # same status codes fs/list uses (see api_fs_walk, which
                        # pulls the first item eagerly to catch this). A non-root
                        # dir keeps skip-and-continue, but marks the walk
                        # truncated so the client knows coverage is partial.
                        if is_root:
                            raise
                        yield _WALK_TRUNCATED
                        continue
                    # rclone can't paginate, so a huge dir comes back whole: cap
                    # it at the per-dir remote budget and flag the cut.
                    if len(listed) > WALK_MAX_ENTRIES_REMOTE:
                        dir_cut = True
                        listed = listed[:WALK_MAX_ENTRIES_REMOTE]
                if dir_cut:
                    yield _WALK_TRUNCATED
                children = [
                    _RcDirEntry(e, shell_mounts.rc_modtime_epoch(e.get("ModTime")))
                    for e in listed if e.get("Name")
                ]
            else:
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
            # .gitignore files itself. Skipped entirely for mount-backed dirs:
            # they hold no repos, and check-ignore is kernel I/O on the mount.
            if not mount_backed:
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
            if repo is not None and not mount_backed and (dirs or files):
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


def _mount_list_error_response(path, exc):
    """Map an RcList* failure to the same HTTP response /api/fs/list returns, so
    fs/walk surfaces a failed ROOT listing identically (timeout/down rcd/broken
    mount -> 503, a file or otherwise-not-a-directory -> 400) instead of a
    200-empty body. Subclasses are checked before the RcListError base."""
    from fused_render.shell import mounts as shell_mounts

    if isinstance(exc, shell_mounts.RcListTimeout):
        return _error(
            f"directory listing timed out — too many entries to list ({path})",
            status=503)
    if isinstance(exc, shell_mounts.RcListUnavailable):
        broken = shell_mounts.broken_mount_error(path)
        return _error(broken or f"cannot list directory {path}", status=503)
    broken = shell_mounts.broken_mount_error(path)
    if broken:
        return _error(broken, status=503)
    return _error(f"not a directory: {path}", status=400)


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
    # A file browsed inside a read-only remote mount can never take the
    # sidecar write: with CacheMode=full the doomed PutObject lands in the VFS
    # cache and 403-loops forever (the sidecar-write incident). Skip before
    # even reading the sidecar (that read is a network stat too) — reopening
    # the file just restores the default view. Same "skipped" shape as the
    # LSN-3 gate below.
    from fused_render.shell.mounts import mount_read_only
    if mount_read_only(path):
        return {"ok": True, "skipped": True}
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


# Per-gate probe budget (SPEC CT-12 fail-closed). One condition gate evaluation
# shares this wall-clock deadline across ALL its mount probes. On a
# non-direct-capable mount each operations/stat can burn the full rc timeout
# resolving a miss (rclone lists the whole parent prefix), so a gate's serialized
# probes would otherwise stack to N * that timeout. 5s bounds a whole gate to
# roughly one slow probe; direct-capable mounts probe in ~1s and rarely reach it.
GATE_PROBE_BUDGET_S = 5.0


def _mount_gate_builtins(target_path: str):
    """Custom `__builtins__` for a condition gate whose target is MOUNT-backed,
    so the gate's own filesystem primitives route through the rclone rc API
    instead of the kernel NFS mount.

    Kernel NFS is the enemy: a cold NEGATIVE os.path.isfile over an rclone-NFS
    mount is a kernel LOOKUP miss that forces rclone to LIST the whole parent S3
    prefix to resolve it (~18-24s on a world-scale store), tripping the macOS NFS
    deadman so the mount is declared dead. This is the same "route via rc, never
    the kernel" hardening api_fs_list / rc_list_dir already carry; the gate path
    never got it because gates run raw os.path against the mount.

    The gate is exec'd stdlib-only and calls os.path directly (we can't ask
    arbitrary gate code to call an rc helper), so we intercept at the os / open
    layer. `import os` inside the gate resolves through __import__, so a fake
    `os` injected into the module globals would just be overwritten by the real
    one — instead we override __import__ (and open) in the gate module's OWN
    __builtins__. That dict is built per _run_condition call on a fresh module,
    so this is thread-safe under the concurrent ThreadPoolExecutor fan-out
    (_evaluate_conditions) — NEVER a global monkeypatch of os.path.*, which would
    race across threads and the rest of the server.

    Fail-closed (SPEC CT-12): any rc error / timeout / unreachable rcd makes the
    routed call behave as the kernel exception would today (isfile/isdir/exists
    -> False, os.stat -> OSError, open -> OSError), so the gate returns False
    quietly. A mount path NEVER falls back to the kernel os.* — that reintroduces
    the wedge. Non-mount paths a gate might also touch pass straight through to
    the real os / open.
    """
    import builtins
    import io

    from fused_render.shell import mounts

    real_os = os

    # The gate's probes run SERIALLY in this one thread; give them ONE shared
    # deadline (GATE_PROBE_BUDGET_S from now). Each probe is bounded to the budget
    # REMAINING, and once it is spent every further probe fails closed instantly
    # (isfile/isdir/exists -> False, stat -> OSError) instead of issuing another
    # slow rc call — so a hung/slow backend can't stack timeouts across a gate.
    deadline = time.monotonic() + GATE_PROBE_BUDGET_S

    def _probe_budget():
        return deadline - time.monotonic()

    def _isfile(p):
        if not mounts.is_mount_backed(p):
            return real_os.path.isfile(p)
        left = _probe_budget()
        if left <= 0:
            return False  # budget spent -> fail closed
        return mounts.rc_kind_for(p, timeout=left) == "file"

    def _isdir(p):
        if not mounts.is_mount_backed(p):
            return real_os.path.isdir(p)
        left = _probe_budget()
        if left <= 0:
            return False
        return mounts.rc_kind_for(p, timeout=left) == "dir"

    def _exists(p):
        if not mounts.is_mount_backed(p):
            return real_os.path.exists(p)
        left = _probe_budget()
        if left <= 0:
            return False
        return mounts.rc_kind_for(p, timeout=left) in ("file", "dir")

    def _stat(p, *a, **k):
        if not mounts.is_mount_backed(p):
            return real_os.stat(p, *a, **k)
        left = _probe_budget()
        if left <= 0:
            raise OSError(f"probe budget exhausted for {p}")
        return mounts.rc_stat_result(p, timeout=left)

    def _listdir(p="."):
        # A kernel listing over a mount is the mur-sst wedge; the gate is
        # forbidden from enumerating anyway (constant-time by design), so fail
        # closed rather than route a listing it should never issue.
        if mounts.is_mount_backed(p):
            raise OSError(f"listing not permitted for mount path {p} in a gate")
        return real_os.listdir(p)

    def _scandir(p="."):
        if mounts.is_mount_backed(p):
            raise OSError(f"scandir not permitted for mount path {p} in a gate")
        return real_os.scandir(p)

    class _OsPathShim:
        # Instance attrs win over __getattr__, so only these three route via rc;
        # join / basename / everything else delegate to the real os.path.
        isfile = staticmethod(_isfile)
        isdir = staticmethod(_isdir)
        exists = staticmethod(_exists)

        def __getattr__(self, name):
            return getattr(real_os.path, name)

    class _OsShim:
        path = _OsPathShim()
        stat = staticmethod(_stat)
        listdir = staticmethod(_listdir)
        scandir = staticmethod(_scandir)

        def __getattr__(self, name):
            return getattr(real_os, name)

    os_shim = _OsShim()
    real_import = builtins.__import__
    real_open = open

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        # Route every import form of `os`/`os.path` to the shim. __import__'s
        # return-value contract differs by form: `import os` / `import os as o`
        # (name "os") and `import os.path` (name "os.path", empty fromlist) bind
        # the TOP package, then the import machinery walks .path off it via
        # getattr — so return os_shim. `from os import ...` (name "os", non-empty
        # fromlist) also wants the top package. Only `from os.path import x`
        # (name "os.path", non-empty fromlist) wants the SUBMODULE — return the
        # shim's path object so the names bind to shimmed functions.
        # NOTE: this covers os / os.path only. A gate reaching the mount through
        # a different stdlib module (pathlib, io, glob, ...) would still hit the
        # kernel — a known, deliberately out-of-scope escape (low likelihood;
        # the builtin gates use os).
        if name == "os":
            return os_shim
        if name == "os.path":
            return os_shim.path if fromlist else os_shim
        return real_import(name, globals, locals, fromlist, level)

    def _open(file, *args, **kwargs):
        if isinstance(file, str) and mounts.is_mount_backed(file):
            # The one bounded gate read (zarr.json node_type). Ranged HTTP read
            # over the mount's serve — never a kernel open. OSError -> the gate's
            # own except -> fail closed.
            data = mounts.rc_read_bounded(file)
            mode = args[0] if args else kwargs.get("mode", "r")
            if "b" in mode:
                return io.BytesIO(data)
            return io.StringIO(data.decode(kwargs.get("encoding") or "utf-8"))
        return real_open(file, *args, **kwargs)

    b = dict(vars(builtins))
    b["__import__"] = _import
    b["open"] = _open
    return b


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

    For a MOUNT-backed target the gate runs under a per-call, thread-safe shim
    (_mount_gate_builtins) that routes its os.path / os.stat / open off the
    kernel NFS mount and onto the rclone rc API — a cold negative os.path.isfile
    over a mount otherwise lists the whole S3 prefix and wedges the mount.
    Templates stay mount-agnostic; all mount-awareness lives here.
    """
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location(
            "__fused_condition__", condition_file
        )
        mod = importlib.util.module_from_spec(spec)
        # Local import keeps shell ↛ server acyclic; resolves the attr at call
        # time so the mount routing is monkeypatchable in tests.
        from fused_render.shell.mounts import is_mount_backed
        if is_mount_backed(target_path):
            mod.__dict__["__builtins__"] = _mount_gate_builtins(target_path)
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
    from fused_render.shell.mounts import is_mount_backed, rc_kind_for

    if is_mount_backed(path):
        # A mount is_dir probe off the kernel (a kernel os.stat here is a
        # GETATTR that can force an S3 re-list and wedge the mount). "missing" is
        # a trustworthy 404; "indeterminate" (rcd down / rc error / probe budget
        # exhausted) must NOT 404 a path the user just opened — proceed treating
        # it as a dir, and the gates then fail closed on their own indeterminate
        # probes, so the endpoint still returns 200 with all-False conditions
        # rather than a spurious 404. The probe is bounded by GATE_PROBE_BUDGET_S
        # so a stalled non-direct-capable backend can't hang this endpoint before
        # the gates (each also budgeted) even start.
        kind = rc_kind_for(path, timeout=GATE_PROBE_BUDGET_S)
        if kind == "missing":
            return _error(f"no such file or directory: {path}", status=404)
        is_dir = kind != "file"
    else:
        try:
            st = os.stat(path)
        except OSError:
            return _error(f"no such file or directory: {path}", status=404)
        is_dir = stat_mod.S_ISDIR(st.st_mode)
    entries, _ = _templates_for(path, is_dir)

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
    from fused_render.shell.mounts import is_mount_backed, mount_read_only

    if mount_read_only(path):
        return False
    if is_mount_backed(path):
        # A writable (not read-only) mount: the rclone VFS (CacheMode=full) takes
        # writes into its local cache, so a path under it is writable regardless
        # of kernel permission bits. Return True WITHOUT a kernel
        # os.path.exists/os.access — a cold negative lookup over the mount lists
        # the whole S3 prefix and wedges it, the same trap fs/stat's os.stat is
        # routed off the kernel to avoid (mount_read_only reads mounts.json only).
        return True
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    return os.access(os.path.dirname(path) or ".", os.W_OK)


# ---------------------------------------------------------------------------
# Mount-safe existence/shape probes for the fs mutation handlers + /api/fs/raw.
#
# An rclone-backed NFS mount has no cheap point lookup: a cold NEGATIVE kernel
# probe (os.stat / os.path.exists / os.path.isdir / os.listdir) forces rclone
# to enumerate the ENTIRE parent S3 prefix (measured: 44k entries, ~64s), which
# blows the macOS NFS deadman and DROPS the mount — server threads then block
# uninterruptibly. So a mutation handler must never touch a mount-backed path
# through the kernel to decide whether it exists or what shape it is. These
# helpers answer that via the rclone rcd (operations/list, bounded by a hard
# timeout: a too-huge directory becomes a failed request, never a dead mount).


class _MountProbe:
    """Result of a mount-safe existence/shape probe. `parent_is_dir` is whether
    the path's parent is a listable directory (a mount-safe stand-in for
    os.path.isdir(parent)); `exists`/`is_dir`/`size`/`mtime` describe the path
    itself. Size/mtime come from the rc listing entry (None when absent)."""

    __slots__ = ("parent_is_dir", "exists", "is_dir", "size", "mtime")

    def __init__(self, parent_is_dir, exists, is_dir=False, size=None, mtime=None):
        self.parent_is_dir = parent_is_dir
        self.exists = exists
        self.is_dir = is_dir
        self.size = size
        self.mtime = mtime


def _mount_probe(path: str) -> _MountProbe:
    """Existence + shape of a MOUNT-BACKED path, answered by the rclone rcd
    (rc_list_dir of the parent + membership match), doing ZERO kernel FS I/O on
    the mount. The parent listing is bounded by rc_list_dir's hard timeout, so a
    huge remote directory raises RcListTimeout rather than wedging the mount.

    Returns a _MountProbe. Raises RcListUnavailable (rcd down / broken mount) or
    RcListTimeout (directory too large to enumerate) when existence is
    INDETERMINATE — the caller maps those to 503 (via _mount_list_error_response),
    never to "missing"."""
    from fused_render.shell import mounts as m

    # The mounts container and each individual mountpoint are LOCAL directories
    # the shell created to host mounts; they always exist as directories and
    # their own parent has no single mount record to list. Answer directly.
    if m.is_mounts_root(path):
        return _MountProbe(True, True, is_dir=True)
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    if m.is_mounts_root(parent):
        # A direct child of the container is a mountpoint only if a mount
        # RECORD carries its name — an unknown/removed name is a phantom and
        # must read as absent (mounts.json only, no I/O on any mount).
        exists = any(rec.get("name") == name for rec in m.list_mounts())
        return _MountProbe(True, exists, is_dir=exists)
    try:
        entries = m.rc_list_dir(parent)
    except (m.RcListUnavailable, m.RcListTimeout):
        raise  # indeterminate -> caller returns 503
    except m.RcListError:
        # The rcd rejected the listing: the parent is a file or is missing, so
        # the child cannot exist. Mount-safe equivalent of a False os.path.isdir.
        return _MountProbe(False, False)
    for ent in entries:
        if ent.get("Name") == name:
            return _MountProbe(True, True, is_dir=bool(ent.get("IsDir")),
                               size=ent.get("Size"),
                               mtime=m.rc_modtime_epoch(ent.get("ModTime")))
    return _MountProbe(True, False)  # parent listable, child absent


def _mount_stat_payload(path: str, is_dir: bool, size, mtime) -> dict:
    """The /api/fs/stat payload for a MOUNT-BACKED path, built from an rc probe
    (size/mtime) with NO kernel os.stat/os.access on the mount. `writable` is
    True by construction — the caller only reaches this after clearing the
    read-only gate (mount_read_only False)."""
    templates, template_error = _templates_for(path, is_dir)
    payload = {
        "path": path,
        "name": os.path.basename(path) or path,
        "is_dir": is_dir,
        "size": None if is_dir else size,
        "mtime": mtime,
        "writable": True,
        "remote": True,
        "templates": templates,
    }
    if template_error:
        payload["template_error"] = template_error
    return payload


def _probe_path(path: str) -> _MountProbe:
    """Existence + shape of `path`, mount-safe: a mount-backed path is answered
    through the rclone rcd (_mount_probe, zero kernel I/O), a local path with a
    plain kernel stat. Used by _fs_rename/_fs_copy, which may mix a local and a
    mount-backed side. Raises RcListUnavailable/RcListTimeout for an
    indeterminate mount probe (caller maps to 503)."""
    from fused_render.shell import mounts as m

    if m.is_mount_backed(path):
        return _mount_probe(path)
    parent_is_dir = os.path.isdir(os.path.dirname(path) or ".")
    if not os.path.exists(path):
        return _MountProbe(parent_is_dir, False)
    return _MountProbe(parent_is_dir, True, is_dir=os.path.isdir(path))


def _mutation_result_payload(path: str, is_dir: bool) -> dict:
    """The /api/fs/stat payload returned after a successful mutation, mount-safe:
    a mount-backed path is described from a fresh rc probe (no kernel os.stat),
    a local path via the ordinary _stat_payload."""
    from fused_render.shell import mounts as m

    if not m.is_mount_backed(path):
        return _stat_payload(path, is_dir)
    try:
        pr = _mount_probe(path)
    except (m.RcListUnavailable, m.RcListTimeout):
        pr = None
    size = pr.size if pr and pr.exists else None
    mtime = pr.mtime if pr and pr.exists else None
    return _mount_stat_payload(path, is_dir, size, mtime)


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
    # Local import, like api_fs_walk's: server -> shell.mounts only, keeping
    # shell ↛ server acyclic.
    from fused_render.shell.mounts import is_mount_backed

    if st is None:
        st = _mount_safe_stat(path)
    templates, template_error = _templates_for(path, is_dir)

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


def _mount_safe_stat(path: str) -> os.stat_result:
    """os.stat for a path that may be mount-backed, off the kernel for mounts.

    A kernel os.stat on a mount is a GETATTR that can force an S3 re-list and
    wedge the mount (the stat-storm / deadman incident); route mount paths
    through the rclone rc API (rc_stat_result) instead. It raises OSError /
    FileNotFoundError exactly like the kernel os.stat it replaces, so callers'
    existing OSError->404 handling holds — and it NEVER falls back to that kernel
    GETATTR, which is the call that killed the mount."""
    from fused_render.shell.mounts import is_mount_backed, rc_stat_result

    if is_mount_backed(path):
        return rc_stat_result(path)
    return os.stat(path)


def _fs_stat(path: str):
    # One stat, not the exists()+isdir()+stat() trio: over a remote mount each
    # is a round-trip, so a plain metadata fetch cost 3 LISTs. _mount_safe_stat
    # keeps a mount stat off the kernel (rc API / direct probe).
    #
    # 404 vs 503: FileNotFoundError is a CONFIRMED miss (kernel ENOENT, or a
    # healthy backend's trustworthy negative) -> 404, matching os.path.exists()'s
    # OSError->False for a local path. A bare OSError on a MOUNT path is
    # rc_stat_result's "indeterminate" (rcd unreachable, rc timeout, mount slow /
    # unresponsive) — NOT proof the path is gone. Mapping it to 404 tells the
    # client a path it just opened has vanished; surface it as a retryable 503
    # instead. A non-mount OSError keeps the historical exists()->False -> 404.
    from fused_render.shell.mounts import is_mount_backed

    try:
        st = _mount_safe_stat(path)
    except FileNotFoundError:
        return _error(f"no such file or directory: {path}", status=404)
    except OSError:
        if is_mount_backed(path):
            return _error(
                f"mount is slow or unresponsive, could not stat {path}",
                status=503)
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
    parent = os.path.dirname(path)

    # Mount-backed target: gate on read-only-ness and answer existence/shape via
    # the rclone rcd BEFORE any kernel probe — a cold negative os.stat here is
    # the exact enumerate-the-whole-prefix call that wedges the mount.
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(path):
        # Read-only mount: refuse first, before touching anything (the same
        # "readonly" wire contract as the local guard below).
        if shell_mounts.mount_read_only(path):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            pr = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(parent, e)  # indeterminate -> 503
        if pr.exists and pr.is_dir:
            return _error(f"path is a directory: {path}")
        if not pr.parent_is_dir:
            return _error(f"parent directory does not exist: {parent}", status=404)
        if create and pr.exists:
            return JSONResponse({"error": "conflict"}, status_code=409)
        if expected_mtime is not None:
            if not pr.exists:
                return JSONResponse({"error": "conflict", "mtime": None},
                                    status_code=409)
            # Cross-source compare: expected_mtime is a KERNEL /api/fs/stat
            # st_mtime, but pr.mtime is the rclone rcd ModTime — the two round
            # a mount's timestamp differently and disagree sub-second, so the
            # 1e-6 tolerance the local branch uses would 409 every save on a
            # writable mount. Tolerate < 1s here; a larger gap is a real change.
            if pr.mtime is None or abs(pr.mtime - expected_mtime) >= 1.0:
                return JSONResponse({"error": "conflict", "mtime": pr.mtime},
                                    status_code=409)
        # The write itself goes through the rclone VFS (acceptable — it is the
        # negative/list probes, not the mutation, that wedge the mount): atomic
        # temp-write + os.replace in the parent, same as the local path. No mode
        # preservation (a remote object has no unix mode, and reading it would
        # be an extra kernel getattr on the mount).
        #
        # RESIDUAL RISK: tempfile.mkstemp(dir=parent) + os.replace still do
        # kernel negative LOOKUPs on the mount (as do os.mkdir/os.remove/
        # shutil.move in the sibling handlers) — the rc probe above answers
        # existence but does NOT warm the kernel dircache, so on a huge parent
        # these lookups can still trigger the full-prefix enumeration this
        # module exists to avoid. Follow-up: route the mutations themselves
        # through rclone rc operations (uploadfile / deletefile / movefile),
        # not the kernel VFS, so no mutation touches the mount through a LOOKUP.
        fd, tmp = tempfile.mkstemp(dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return _error(f"cannot write {path}: {e}", status=400)
        # Re-arm the client's optimistic lock from a fresh rc probe; fall back to
        # the written length if the rcd can't answer (never kernel-stat).
        try:
            after = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout):
            after = None
        size = after.size if after and after.exists else len(content.encode("utf-8"))
        mtime = after.mtime if after and after.exists else None
        return _mount_stat_payload(path, False, size, mtime)

    if os.path.isdir(path):
        return _error(f"path is a directory: {path}")
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


# ---------------------------------------------------------------------------
# fs/events watch registry
#
# Incident this exists to prevent: a read-only S3-backed rclone NFS mount died
# with the macOS "Server connections interrupted" dialog. Root cause was the
# /api/fs/events WebSocket poller calling os.stat() on every watched path every
# 200ms for the life of each socket. Each stat is a kernel NFS GETATTR; when
# the attribute cache expires it forces rclone to re-list the directory on S3,
# and for a world-scale .zarr on a slow bucket that re-list blows past the
# macOS NFS client's timeo*retrans ceiling (~2min) -> the kernel declares the
# mount dead. During the incident ~5 sockets (open preview panes + the Listing
# view) ran these loops at once, several on paths under the mount.
#
# This registry fixes the whole class of problem:
#   * ONE stat ticker per unique path, refcounted, fanned out to every socket
#     watching it (so N panes watching the same file = 1 stat/interval, not N).
#   * Stats run OFF the event loop (asyncio.to_thread) with a hard timeout, so
#     a hung NFS stat can never freeze the server's event loop. A timed-out or
#     errored stat reports "unchanged".
#   * A path with a stat still in flight never gets a second stat queued on top
#     of it — a stat hung for minutes must not spawn a thread every tick.
#   * Mount-backed paths poll slowly (5s vs 200ms) and answer via the rclone rc
#     API (mounts.rc_mtime_for), not the kernel, removing NFS from the loop
#     entirely. Local paths keep the cheap 200ms os.stat behavior.
# ---------------------------------------------------------------------------

_LOCAL_POLL_S = 0.2   # local files: cheap os.stat, snappy reload
_MOUNT_POLL_S = 5.0   # mount-backed files: rc stat, far less remote pressure
_STAT_TIMEOUT_S = 4.0  # a stat outliving this reports "unchanged" for this tick

# Sentinel distinct from every real mtime signal (float, RFC3339 str, or None
# meaning "deleted"): _read() returns it for "no change / could not determine",
# which must NOT be confused with None (a real local-deletion signal, LR-6).
_UNCHANGED = object()


def _mtime_or_none(path: str):
    """Local-file mtime signal for the poller: st_mtime, or None when the path
    is gone. None is a real change signal (deletion -> reload, LR-6), distinct
    from the _UNCHANGED sentinel returned on timeout."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _hash_listing(listed) -> str:
    """A stable change signal for a mount-backed DIRECTORY watch: a hash of its
    shallow (Name, Size, ModTime) tuples. Unlike a directory's own ModTime — a
    constant sentinel for synthetic S3 dirs — this moves whenever a child is
    created, deleted, renamed, or resized. The "L" prefix keeps it disjoint from
    a file watch's numeric/str ModTime signal."""
    h = hashlib.sha1()
    for e in listed:
        h.update(repr((e.get("Name"), e.get("Size"), e.get("ModTime"))).encode())
    return "L" + h.hexdigest()


class _WatchEntry:
    """One coalesced stat ticker for a single path, fanning changes out to
    every subscribed socket. Classified once at creation as local or
    mount-backed, which fixes both the poll interval and the stat strategy."""

    def __init__(self, path: str):
        from fused_render.shell import mounts as shell_mounts

        self.path = path
        self.is_mount = shell_mounts.is_mount_backed(path)
        self.interval = _MOUNT_POLL_S if self.is_mount else _LOCAL_POLL_S
        self.subscribers: set = set()  # asyncio.Queue per socket
        self.last = _UNCHANGED  # primed by the first successful read
        self._inflight = None   # in-progress stat task; guards against pile-up
        self.task = None        # the ticker task

    async def _stat_signal(self):
        """The change signal for this path, off the event loop. Never raises:
        any error becomes _UNCHANGED so a transient failure never masquerades
        as a change (which would spuriously reload the pane)."""
        try:
            if self.is_mount:
                # rc API, NOT the kernel — a slow answer here can't kill the
                # mount. We deliberately do NOT fall back to os.stat, which is
                # the GETATTR that caused the incident.
                return await asyncio.to_thread(self._mount_signal)
            return await asyncio.to_thread(_mtime_or_none, self.path)
        except Exception:
            return _UNCHANGED

    def _mount_signal(self):
        """Change signal for a mount-backed watch, off the event loop.

        A DIRECTORY's rclone ModTime is a constant sentinel (2000-01-01) for
        synthetic S3/GCS dirs, so create/delete/rename of children never moves
        it — the mount-dir auto-refresh (Listing LS-1) was silently dead. So for
        a directory the signal is a hash of a BOUNDED shallow listing instead:
        one direct_list_page (anonymous S3/GCS) or a short-timeout rc_list_dir.

        A FILE reaches the ModTime path differently by branch:
          - direct-listable (anonymous S3/GCS): direct_list_capable is a pure
            path/config check that can't tell a file from a directory, and
            direct_list_page on a file KEY returns an EMPTY page (the file's own
            key != the "<key>/" listing prefix). An empty page is
            indistinguishable from an empty directory, so we fall back to the
            file's operations/stat ModTime — a real, changing signal for a file;
            harmless for a genuinely empty directory, since the moment a child
            appears the page is non-empty and the listing hash takes over.
          - rc route: rc rejects listing a file as not-a-directory (RcListError),
            which likewise falls back to operations/stat ModTime.
        Any failure/timeout -> _UNCHANGED (never an error storm)."""
        from fused_render.shell import mounts as shell_mounts

        try:
            if shell_mounts.direct_list_capable(self.path):
                page, _ = shell_mounts.direct_list_page(
                    self.path, max_keys=1000, timeout=4)
                if not page:
                    # Empty: a file (its key isn't under the "<key>/" prefix) or
                    # an empty dir. Use the rc ModTime — moves for a file's
                    # content, constant-but-harmless for an empty dir.
                    m = shell_mounts.rc_mtime_for(self.path)
                    return _UNCHANGED if m is None else m
                return _hash_listing(page)
            listed = shell_mounts.rc_list_dir(self.path, timeout=4)
            return _hash_listing(listed)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout):
            return _UNCHANGED  # down / too big to list -> treat as unchanged
        except shell_mounts.RcListError:
            # Not a directory (a file): fall back to the file's ModTime.
            m = shell_mounts.rc_mtime_for(self.path)
            return _UNCHANGED if m is None else m
        except Exception:
            return _UNCHANGED  # DirectListError, etc.

    async def _read(self):
        """One tick's read with a hard timeout and in-flight de-duplication.

        asyncio.wait_for cancels its awaitable on timeout, but the underlying
        stat/listing runs in a thread that cannot be cancelled — so we shield
        the task and, on timeout, leave it running and report _UNCHANGED. The
        still-running task then guards the NEXT tick: while it is hung (possibly
        for minutes) we never stack a second thread on top of it. But once it
        FINISHES (a slow stat that outlived its wait_for), the next tick must
        CONSUME its result rather than discard a done future and start over —
        otherwise a path whose stat always takes >_STAT_TIMEOUT_S never primes
        and 100% of the work is wasted."""
        if self._inflight is not None:
            if not self._inflight.done():
                return _UNCHANGED  # previous read still hanging; skip this tick
            sig = self._inflight.result()  # _stat_signal never raises
            self._inflight = None
            return sig
        self._inflight = asyncio.ensure_future(self._stat_signal())
        try:
            sig = await asyncio.wait_for(
                asyncio.shield(self._inflight), _STAT_TIMEOUT_S)
        except asyncio.TimeoutError:
            return _UNCHANGED  # leave _inflight running; consumed on a later tick
        self._inflight = None
        return sig

    def _broadcast(self, sig):
        msg = json.dumps({"path": self.path, "mtime": sig})
        for q in list(self.subscribers):
            q.put_nowait(msg)

    async def run(self):
        # First read primes the baseline WITHOUT broadcasting, so connecting a
        # socket never triggers an immediate reload. A late subscriber joining
        # an already-running ticker inherits the current baseline the same way.
        while True:
            sig = await self._read()
            if sig is not _UNCHANGED:
                if self.last is not _UNCHANGED and sig != self.last:
                    self._broadcast(sig)
                self.last = sig
            await asyncio.sleep(self.interval)


class _WatchRegistry:
    """Module-level map of path -> _WatchEntry, refcounted by subscriber count.
    subscribe() attaches a socket's queue (starting the ticker on the first
    subscriber); unsubscribe() detaches it (stopping the ticker on the last)."""

    def __init__(self):
        self._entries: dict = {}

    def subscribe(self, path: str, queue):
        entry = self._entries.get(path)
        if entry is None:
            entry = _WatchEntry(path)
            self._entries[path] = entry
            entry.task = asyncio.create_task(entry.run())
        entry.subscribers.add(queue)
        return entry

    def unsubscribe(self, entry, queue):
        entry.subscribers.discard(queue)
        if not entry.subscribers:
            if entry.task is not None:
                entry.task.cancel()
            self._entries.pop(entry.path, None)


_WATCH_REGISTRY = _WatchRegistry()


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

    # Mount-backed target: read-only refusal first, then existence/shape via the
    # rclone rcd — never a kernel probe (see _fs_write's mount branch).
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(path):
        if shell_mounts.mount_read_only(path):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            pr = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(parent, e)  # indeterminate -> 503
        if not pr.parent_is_dir:
            return _error(f"parent directory does not exist: {parent}")
        if pr.exists:
            return JSONResponse({"error": "conflict"}, status_code=409)
        try:
            os.mkdir(path)  # through the rclone VFS
        except OSError as e:
            return _error(f"cannot create directory {path}: {e}")
        return _mount_stat_payload(path, True, None, None)

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

    # Mount-backed target: read-only refusal first; then answer shape via the
    # rclone rcd. A DIRECTORY delete (the non-recursive os.listdir emptiness
    # check or a recursive shutil.rmtree) would kernel-enumerate/walk the remote
    # tree — refused, out of scope. A single-file delete goes through the VFS.
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(path):
        if shell_mounts.mount_read_only(path):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            pr = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(os.path.dirname(path), e)  # 503
        if not pr.exists:
            return _error(f"no such file or directory: {path}", status=404)
        if pr.is_dir:
            return _error(
                "cannot delete a directory on a remote mount: directory-tree "
                "operations are not supported over mounts", status=400)
        if trash:
            # Move-to-Trash lifts the file OFF the mount, which reads the whole
            # file through the kernel; report it unsupported so the client
            # falls back to the confirm-then-hard-delete flow (same 501 signal
            # a non-darwin platform returns).
            return JSONResponse({"error": "trash unsupported"}, status_code=501)
        try:
            os.remove(path)  # single VFS unlink
        except OSError as e:
            return _error(f"cannot delete {path}: {e}")
        return {"deleted": path, "trashed": False}

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
    dst_parent = os.path.dirname(dst)

    # A mount is involved on either side: gate mount-safely BEFORE any kernel
    # probe. A move deletes src and writes dst, so a read-only mount on EITHER
    # side refuses (readonly first, as the mount contract). Existence/shape is
    # answered through the rclone rcd; a DIRECTORY on a mount side is refused
    # (a rmtree/copytree-style walk of a remote tree is out of scope).
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(src) or shell_mounts.is_mount_backed(dst):
        if shell_mounts.mount_read_only(src) or shell_mounts.mount_read_only(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            src_pr = _probe_path(src)
            dst_pr = _probe_path(dst)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(
                os.path.dirname(src) if shell_mounts.is_mount_backed(src)
                else dst_parent, e)
        if not src_pr.exists:
            return _error(f"no such file or directory: {src}", status=404)
        if src_pr.is_dir or (dst_pr.exists and dst_pr.is_dir):
            return _error(
                "cannot move a directory to or from a remote mount: "
                "directory-tree operations are not supported over mounts",
                status=400)
        if not dst_pr.parent_is_dir:
            return _error(f"parent directory does not exist: {dst_parent}")
        if dst_pr.exists and not overwrite:
            return JSONResponse({"error": "conflict"}, status_code=409)
        # The mount read-only gate above only covers the mount side(s). A LOCAL
        # side still needs the ordinary _writable check: a move deletes src and
        # writes dst, so a chmod-protected local src or a non-writable local dst
        # must 403 "readonly" (same contract as the all-local branch below).
        # Never _writable a mount side — for a writable mount that kernel-probes
        # W_OK on the mount, the exact stat this whole path exists to avoid.
        if not shell_mounts.is_mount_backed(src) and not _writable(src):
            return JSONResponse({"error": "readonly"}, status_code=403)
        if not shell_mounts.is_mount_backed(dst) and not _writable(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            if dst_pr.exists:
                os.remove(dst)  # single file (a dir dst was refused above)
            shutil.move(src, dst)
        except OSError as e:
            return _error(f"cannot rename {src} -> {dst}: {e}")
        return _mutation_result_payload(dst, False)

    # dst's parent must already exist — a rename never creates intermediate
    # dirs. Without this, a missing parent falls through to _writable (which
    # walks up to the nearest existing ancestor) and surfaces a misleading
    # "readonly" 403; a 400 is the honest error, same as _fs_write/_fs_mkdir.
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
    dst_parent = os.path.dirname(dst)

    # A mount is involved on either side: gate mount-safely BEFORE any kernel
    # probe. A copy writes dst (only the dst mount must be writable — readonly
    # first, as the mount contract) and never modifies src. A DIRECTORY on a
    # mount side is refused (a copytree walk of a remote tree is out of scope);
    # a single-file copy proceeds (its sequential read/write is slow, not fatal).
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(src) or shell_mounts.is_mount_backed(dst):
        if shell_mounts.mount_read_only(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            src_pr = _probe_path(src)
            dst_pr = _probe_path(dst)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(
                os.path.dirname(src) if shell_mounts.is_mount_backed(src)
                else dst_parent, e)
        if not src_pr.exists:
            return _error(f"no such file or directory: {src}", status=404)
        if src_pr.is_dir or (dst_pr.exists and dst_pr.is_dir):
            return _error(
                "cannot copy a directory to or from a remote mount: "
                "directory-tree operations are not supported over mounts",
                status=400)
        if not dst_pr.parent_is_dir:
            return _error(f"parent directory does not exist: {dst_parent}")
        if dst_pr.exists and not overwrite:
            return JSONResponse({"error": "conflict"}, status_code=409)
        # See _fs_rename: the mount read-only gate covers only the mount side. A
        # copy writes dst (never src), so a LOCAL dst still needs _writable —
        # matching the all-local branch, which checks dst only. Never _writable a
        # mount side (it kernel-stats a writable mount).
        if not shell_mounts.is_mount_backed(dst) and not _writable(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            if dst_pr.exists:
                os.remove(dst)  # single file (a dir dst was refused above)
            shutil.copy2(src, dst)
        except OSError as e:
            return _error(f"cannot copy {src} -> {dst}: {e}")
        return _mutation_result_payload(dst, False)

    # dst's parent must already exist — a copy never creates intermediate dirs.
    # Without this, a missing parent falls through to _writable (which walks up
    # to the nearest existing ancestor) and surfaces a misleading "readonly"
    # 403; a 400 is the honest error, same as _fs_write/_fs_mkdir.
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
    def api_config(
        token: str | None = Header(default=None, alias="X-Fused-Desktop-Token"),
    ):
        from fused_render.paths import desktop_instance

        config = {
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
            # Root of the mounts dir (~/.fused-render/mounts). The rendered
            # page's auto-reload watcher (static/runtime.js) uses this to skip
            # watching mount-backed data files: they live on read-only remote
            # buckets that never change, so watching them buys nothing and every
            # poll is remote traffic — the stat storm that killed a mount in the
            # fs/events incident. Templates stay mount-agnostic; the skip lives
            # in runtime internals, keyed off this server-provided prefix.
            "mounts_root": os.path.abspath(shell_mounts.mounts_dir()),
        }
        if instance := desktop_instance():
            config["desktop_instance"] = {"id": instance[0]}
            if token == instance[1]:
                config["desktop_instance"]["token"] = instance[1]
        return config

    @app.post("/api/desktop/shutdown")
    def api_desktop_shutdown(
        token: str | None = Header(default=None, alias="X-Fused-Desktop-Token"),
    ):
        from fused_render.paths import desktop_instance

        instance = desktop_instance()
        if instance is None:
            raise HTTPException(status_code=404, detail="desktop supervisor is not active")
        if token != instance[1]:
            raise HTTPException(status_code=403, detail="invalid desktop supervisor token")
        uvicorn_server = getattr(app.state, "uvicorn_server", None)
        if uvicorn_server is None:
            raise HTTPException(status_code=503, detail="server shutdown is not ready")
        uvicorn_server.should_exit = True
        return {"ok": True}

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
        #
        # This does NOT gate first paint, on either side. Server-side it's a
        # sync `def`, so FastAPI runs it in the threadpool — its cold os.stat
        # over the mount (~1.6s) never blocks the event loop or other requests.
        # Client-side the frontend fetches it from a background useEffect
        # (Preview.tsx `useConditions`) and renders every unconditional
        # template while the verdict is still `null` — the gated ones just show
        # a spinner until it lands. So no change on the render path is needed.
        return _conditions_payload(path)

    @app.get("/api/fs/list")
    def api_fs_list(path: str, cursor: str | None = None):
        # A mount-backed listing must never issue kernel filesystem I/O: both
        # os.path.isdir and os.scandir below are kernel READDIR/GETATTR calls,
        # and on a flat remote prefix with millions of keys rclone's VFS must
        # enumerate the WHOLE directory before the kernel gets its first entry
        # — minutes of blocking that trips the macOS NFS deadman and kills the
        # mount (the mur-sst incident). Route off the kernel instead, so a
        # too-huge directory is a failed/partial request, never a wedged mount.
        #
        # Every response carries `truncated` (the listing is a partial page) and
        # `cursor` (an opaque resume token, non-None only on the direct S3/GCS
        # route — rclone and a local scandir can't resume). Fallback ladder for a
        # mount path: direct -> rc -> 503.
        if shell_mounts.is_mounts_root(path):
            # The mounts container is a LOCAL directory whose children are the
            # mountpoints. is_mount_backed is true for it (so no kernel readdir
            # touches it), yet it sits under no single mount record, so the
            # rc/S3 routes below have nothing to list and 503 ("cannot list
            # directory"). Enumerate the mount records directly instead — the
            # authoritative mount list, with zero kernel or remote I/O and no
            # sidecar files (mounts.json, per-mount *.json) leaking in.
            entries = _sort_entries([
                {"name": m["name"], "is_dir": True, "size": None,
                 "mtime": None, "ignored": False}
                for m in shell_mounts.list_mounts() if m.get("name")
            ])
            return _list_response(path, entries, False, None)
        if shell_mounts.is_mount_backed(path):
            # Direct fast path: for anonymous plain AWS S3 / anonymous GCS — the
            # backends that dominate our mounts — page the store's own listing
            # API (rclone can't paginate its listing at any layer, so a
            # million-key prefix times out on the rc route). On any page failure,
            # log and fall through to the rc route below.
            if shell_mounts.direct_list_capable(path):
                try:
                    entries, next_token = _list_direct(path, cursor)
                except shell_mounts.DirectListError:
                    # A cursored request can't fall through to rc: rc re-serves
                    # page 1 with cursor=None (the frontend dedupes it to zero
                    # rows and pagination dies) or 503s on a huge dir. Return a
                    # retryable error so the client resumes the SAME cursor.
                    if cursor is not None:
                        return _error(
                            "listing continuation failed — retry", status=503)
                    logger.warning("direct listing of %s failed; falling "
                                   "back to rc", path, exc_info=True)
                else:
                    return _list_response(path, entries,
                                          next_token is not None, next_token)
            try:
                listed = shell_mounts.rc_list_dir(path)
            except shell_mounts.RcListTimeout:
                return _error(
                    f"directory listing timed out — too many entries to list "
                    f"({path})", status=503)
            except shell_mounts.RcListUnavailable:
                # rcd down or path under no known mount: the mount can't be
                # trusted. Prefer the specific broken-mount wording when we have
                # it (it tells the user to reconnect from the Mounts page).
                broken = shell_mounts.broken_mount_error(path)
                return _error(broken or f"cannot list directory {path}",
                              status=503)
            except shell_mounts.RcListError:
                # rcd answered but rejected the listing. Two causes look alike
                # here: a genuinely broken mount (dead/stale/disconnected — the
                # empty-mountpoint bug this endpoint already guards), and a path
                # that is simply a file. broken_mount_error distinguishes them:
                # a message means the mount is unhealthy (503, reconnect); no
                # message means the mount is fine and the path just isn't a
                # directory (400, the mount-safe stand-in for os.path.isdir).
                broken = shell_mounts.broken_mount_error(path)
                if broken:
                    return _error(broken, status=503)
                return _error(f"not a directory: {path}", status=400)
            # rcd answered but with nothing: a stale/dead mount (rcd alive, the
            # kernel mount gone) lists empty, and pre-Phase-1 an empty listing
            # consulted broken_mount_error before it was trusted. Restore that
            # so a dead mount 503s ("reconnect") instead of rendering as an
            # ordinary empty folder.
            if not listed:
                broken = shell_mounts.broken_mount_error(path)
                if broken:
                    return _error(broken, status=503)
            # rclone can't resume a listing, so cap and flag rather than page:
            # the client sees the first LIST_MAX_ENTRIES entries, `truncated`
            # tells it there are more, and `cursor` stays None (no Load more).
            # Sort the WHOLE listing THEN cap, so the capped page is the true
            # sorted-first N rather than rclone's arbitrary order sliced. Skip
            # any entry missing a Name (a malformed rc entry must not 500).
            entries = _sort_entries(
                [_mount_list_item(de) for de in listed if de.get("Name")])
            truncated = len(entries) > LIST_MAX_ENTRIES
            return _list_response(path, entries[:LIST_MAX_ENTRIES], truncated, None)
        if not os.path.isdir(path):
            return _error(f"not a directory: {path}", status=400)
        entries = []
        # scandir over listdir+per-entry stat/isdir: the readdir already carries
        # each entry's type, so is_dir() is free and stat() is a single call —
        # the old loop did two stats per entry (os.stat + os.path.isdir's own
        # stat), i.e. 2N remote round-trips under a mount. Both follow symlinks,
        # matching the previous os.stat/os.path.isdir behavior.
        #
        # islice caps consumption at LIST_MAX_ENTRIES: a directory with a
        # million entries would otherwise build a million-entry JSON response.
        # Read one past the cap to detect overflow, then trim.
        try:
            with os.scandir(path) as it:
                dents = list(itertools.islice(it, LIST_MAX_ENTRIES + 1))
        except OSError as e:
            broken = shell_mounts.broken_mount_error(path)
            if broken:
                return _error(broken, status=503)
            return _error(f"cannot read directory {path}: {e}", status=400)
        truncated = len(dents) > LIST_MAX_ENTRIES
        if truncated:
            dents = dents[:LIST_MAX_ENTRIES]
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
        # _sort_entries: dirs first, case-insensitive by name, exact name as a
        # deterministic tiebreak (same order for all three list routes).
        return _list_response(path, _sort_entries(entries), truncated, None)

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
        # Under a mount, os.path.isdir is itself a kernel GETATTR on the mount
        # we route around; _walk_bfs lists mount dirs via the rc API and simply
        # yields nothing for a non-directory root, so the guard is local-only.
        under_mount = shell_mounts.is_mount_backed(path)
        if not under_mount and not os.path.isdir(path):
            return _error(f"not a directory: {path}", status=400)
        walker = _walk_bfs(path, include_hidden)
        # Remote-mount clamp: under a mount mountpoint every directory is a
        # remote LIST round-trip, so the cap drops to WALK_MAX_ENTRIES_REMOTE
        # (see the constant's comment).
        max_entries = WALK_MAX_ENTRIES_REMOTE if under_mount else WALK_MAX_ENTRIES

        # Force the ROOT listing eagerly (the first next() runs it) so a dead
        # mount / down rcd / timed-out or not-a-directory root fails with
        # fs/list's status codes instead of streaming a 200-empty body. Only the
        # ROOT raises out of _walk_bfs; deeper per-dir failures skip-and-continue
        # (feeding the truncated flag via the _WALK_TRUNCATED sentinel).
        try:
            first = next(walker)
            have_first = True
        except StopIteration:
            first, have_first = None, False
        except shell_mounts.RcListError as e:
            return _mount_list_error_response(path, e)

        def _items():
            if have_first:
                yield first
            yield from walker

        if stream != "1":
            entries = []
            truncated = False
            for entry in _items():
                if entry is _WALK_TRUNCATED:
                    truncated = True  # a dir was cut / skipped (partial coverage)
                    continue
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
            for entry in _items():
                if entry is _WALK_TRUNCATED:
                    truncated = True
                    continue
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
    async def api_fs_raw(path: str, request: Request, base: str | None = None):
        # Page-relative resolution (SPEC RH-1): a *relative* `path` is resolved against
        # the directory of `base` — the page's own absolute path, sent by the runtime's
        # fused.rawUrl(), the same contract /api/run uses via `html` (see the resolve at
        # the top of api_run). An absolute `path` is used verbatim (base ignored). This is
        # what lets one `fused.rawUrl("data/x.json")` call resolve locally here AND, when
        # the page is hosted, against the bundle's _asset route by the same key.
        if base and not os.path.isabs(path):
            path = os.path.normpath(os.path.join(os.path.dirname(base), path))
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
                if shell_mounts.is_mount_backed(path):
                    # A missing-sidecar HEAD (.zmetadata, .ovr) is exactly the
                    # cold-negative that a kernel os.stat would turn into a
                    # full-prefix enumeration and a wedged mount — answer it
                    # through the rclone rcd instead. Confirmed-missing/
                    # non-regular -> 404; indeterminate (rcd down/timeout) ->
                    # 503, never "missing".
                    try:
                        pr = await asyncio.to_thread(_mount_probe, path)
                    except (shell_mounts.RcListUnavailable,
                            shell_mounts.RcListTimeout) as e:
                        return _mount_list_error_response(os.path.dirname(path), e)
                    if not pr.exists or pr.is_dir:
                        return _error(f"no such file: {path}", status=404)
                    # rclone reports Size:-1 for an object of unknown length;
                    # `-1 or 0` is -1, which is an invalid content-length. Clamp
                    # a missing/negative size to 0 (keep the mtime fallback).
                    size = pr.size if pr.size is not None and pr.size >= 0 else 0
                    mtime = pr.mtime or 0.0
                else:
                    st = await asyncio.to_thread(_stat_or_none, path)
                    if st is None:
                        return _error(f"no such file: {path}", status=404)
                    size, mtime = st.st_size, st.st_mtime
                media_type, _ = mimetypes.guess_type(path)
                return Response(status_code=200, headers={
                    "content-length": str(size),
                    "content-type": media_type or "application/octet-stream",
                    "accept-ranges": "bytes",
                    "last-modified": email.utils.formatdate(mtime, usegmt=True),
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
        # Reached when serve_url_for returned None (no live serve) or the
        # proxied read failed. For a mount-backed path that means the rclone
        # serve died or is respawning: reading through the kernel mount here is
        # the wedge this whole module exists to avoid, so refuse with 503 rather
        # than fall back to a local file read.
        if shell_mounts.is_mount_backed(path):
            return _error("mount serve unavailable", status=503)
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
        # larger connection pool. Messages are JSON: {path, mtime} on change,
        # {keepalive: true} every 15 s (WF-3).
        #
        # Stat mechanics live in the module-level _WATCH_REGISTRY, NOT here:
        # every socket watching a given path shares ONE ticker (so a panel of
        # panes previewing the same mounted file makes one stat per interval,
        # not one per pane), stats run off the event loop with a hard timeout
        # so a hung NFS stat can't freeze the server, mount-backed paths poll
        # at 5s via the rclone rc API instead of the kernel, and a stat already
        # in flight is never stacked on. This all exists because a stat storm
        # on a slow S3-backed NFS mount killed the mount — see the registry's
        # header comment. This handler just plumbs each path's queue to the
        # socket and emits keepalives.
        await ws.accept()
        paths = ws.query_params.getlist("path")

        queue: asyncio.Queue = asyncio.Queue()
        entries = [_WATCH_REGISTRY.subscribe(p, queue) for p in paths]

        async def pump():
            # Forward change messages; a 15s idle gap emits a keepalive (WF-3).
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    await ws.send_text(json.dumps({"keepalive": True}))
                    continue
                await ws.send_text(msg)

        pumper = asyncio.create_task(pump())
        try:
            # Drain the receive side purely to learn about disconnect; the
            # pump loop alone would only notice on its next send.
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            pumper.cancel()
            for entry in entries:
                _WATCH_REGISTRY.unsubscribe(entry, queue)

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

        from fused_render.export import ExportError, _asset_key, export_page

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

        cache_max_age = body.get("cache_max_age") or "0s"

        try:
            plan = export_page(
                page, out, include=include, exclude=exclude, cache_max_age=cache_max_age
            )
        except ExportError as e:
            return _error(str(e))

        # Mirror the v2 manifest shape (entrypoints carry the payload-relative `key`, assets
        # just `path`+`name`) so a caller sees the same fields the bundle's manifest.json has.
        return {
            "out": os.path.abspath(out),
            "entrypoints": [
                {"path": e.path, "name": e.name, "key": _asset_key(e.path)}
                for e in plan.entrypoints
            ],
            "assets": [{"path": a.path, "name": a.name} for a in plan.assets],
            "warnings": plan.warnings,
        }

    return app
