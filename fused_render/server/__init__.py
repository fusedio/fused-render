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
import re
import shutil
import stat as stat_mod
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import time
import traceback
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

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
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from fused_render import __version__
from fused_render import calls as shell_calls
from fused_render._view_url_codec import canonical_fs_path
from fused_render.account import router as account_router
from fused_render.core_templates import ensure_core_templates
from fused_render.deploy import router as deploy_router
from fused_render.executor import dumps_result, run_python
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

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
# Depth cap for a mount-backed walk, enforced INSIDE _walk_bfs so the generator
# stops DESCENDING (stops enqueuing deeper dirs), not just the consumer. The
# entry-count cap alone doesn't bound a deep, LOW-fan-out tree (e.g. NAIP
# state/year/quad/tile): each level is one more remote LIST round-trip, and a
# handful of children per level never trips the entry cap while the walk marches
# arbitrarily deep. Kept generous enough for a real search (a few levels below a
# bucket prefix) but finite so a search-as-you-type over a mount root can't kick
# off an unbounded remote enumeration. Root is depth 0. Module-level so tests
# can shrink it.
WALK_MAX_DEPTH_REMOTE = 6
# Depth cap for a LOCAL walk. Local listings are cheap kernel calls, so this is a
# generous runaway guard (a symlink-free but pathologically deep tree) rather
# than a budget — a normal project never approaches it. Module-level so tests can
# shrink it.
WALK_MAX_DEPTH_LOCAL = 40
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


from .walk import (
    _IgnoreOracle,
    _repo_toplevel,
    _RcDirEntry,
    _mount_list_item,
    _win_protected,
    _sort_entries,
    _list_response,
    _list_direct,
    _WALK_TRUNCATED,
    _walk_bfs,
    _git_ignored,
    _mount_list_error_response,
)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# /api/fs/conditions evaluates template condition.py gates, which over a remote
# mount costs ~6.8s and was recomputed on every call. A small check-on-read TTL
# cache lets re-navigation to the same directory reuse the verdict. Only success
# payloads (plain dicts) are cached; error/404 responses are JSONResponse and are
# never stored. No background eviction — a stale entry is overwritten on the next
# miss. _CONDITIONS_TTL_S is a module attribute so tests can monkeypatch it.
_CONDITIONS_TTL_S = 60.0
# path -> (inserted_monotonic, prefs_mtime, payload). Gates may read the
# preference store (the reader template's condition.py does), so a cached
# verdict is only valid while prefs.json is unchanged — otherwise flipping a
# Preferences toggle looks dead for a full TTL.
_CONDITIONS_CACHE: dict[str, tuple[float, float, dict]] = {}


def _prefs_mtime() -> float:
    # Local import keeps module import order unchanged; shell never imports
    # server so this direction is safe.
    from fused_render.shell import storage
    try:
        return os.path.getmtime(os.path.join(storage.home_dir(), "prefs.json"))
    except OSError:
        return 0.0


# /api/fs/stat on a MOUNT path goes _mount_safe_stat -> _mount_probe ->
# rc_list_dir(parent), a full cold LIST of the parent prefix over rclone/S3
# (~1.6s) just to describe one child; opening a folder fires it, and
# re-navigating to a sibling repaid it uncached. A short check-on-read TTL cache
# (same shape as _CONDITIONS_CACHE) serves a recent stat instead. Only
# MOUNT-backed success payloads are cached — see api_fs_stat for the scope
# rationale and mutation-invalidation contract. Separate TTL from conditions so
# each can be tuned/monkeypatched on its own; kept a module attribute so tests
# can override it.
_STAT_TTL_S = 60.0
_STAT_CACHE: dict[str, tuple[float, dict]] = {}  # path -> (inserted_monotonic, payload)
# Monotonic invalidation counter for the TOCTOU guard in api_fs_stat. Every
# _invalidate_stat_cache bump happens-before the post-compute check in a stat
# that reads the generation first, so any mutation that completes while a slow
# _fs_stat is in flight is observed and blocks that stale result from refilling
# the cache. A single global counter is deliberately conservative: a concurrent
# mutation to ANY path just skips caching this one in-flight stat (rare and
# harmless) rather than requiring per-path bookkeeping.
_STAT_CACHE_GEN = 0


def _invalidate_stat_cache(*paths: object) -> None:
    # Drop cached /api/fs/stat entries for paths a mutation just touched, plus
    # their parent directories (creating/deleting a child moves the parent's
    # mtime on many backends). The editor re-stats a path right after
    # write/rename/copy/mkdir to re-arm its optimistic lock, so a stale hit here
    # would be a real clobber bug — invalidation is the correctness backbone of
    # this cache, not an optimization. Popping a key that was never cached (or a
    # non-string/None body field) is a harmless no-op, so callers can pass raw
    # body values and invalidate unconditionally without inspecting the result.
    #
    # Bump the generation UNCONDITIONALLY (even for a no-op pop): a stat for a
    # not-yet-cached path may be mid-flight, and the bump is what tells its
    # post-compute check that a mutation raced it so it must not cache a
    # pre-mutation payload. See api_fs_stat for the guard.
    global _STAT_CACHE_GEN
    _STAT_CACHE_GEN += 1
    for p in paths:
        if isinstance(p, str) and p:
            _STAT_CACHE.pop(p, None)
            _STAT_CACHE.pop(os.path.dirname(p), None)


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


# --- /api/ai — inference through the Claude Code CLI --------------------------
#
# fused.ai(prompt, opts) lands here. The shell invokes the `claude` binary the
# user already has (Claude Code — its login is the credential) rather than
# pages fetching a model directly: the page stays origin-clean (no API key or
# endpoint baked into authored HTML), and the server is one place to grow
# config/limits later. Wire shape is the house {ok, result,
# error:{type,message}} contract /api/run set; {"stream": true} switches the
# response to NDJSON chunks (see _ai_relay).
#
# The CLI is driven as a bare completion engine: --tools= disables every
# built-in tool, --setting-sources= skips user/project settings and
# CLAUDE.md, --system-prompt-file REPLACES the shipped agent prompt, and
# --no-session-persistence keeps everything off disk.
#
# LATENCY (D168/D169): the CLI is a Node program whose startup alone costs
# ~1.5-2.5s — it dominated every call at haiku sizes. ONE persistent process
# is therefore kept alive in --input-format stream-json mode and RECONFIGURED
# per request over its stdin protocol (all probed on 2.1.220):
#   /clear (a plain user message)        -> wipes conversation context, ~0.7s
#   set_model control_request            -> swaps model AND system_prompt, ~0ms
#   set_max_thinking_tokens ctrl_request -> 0 clamps thinking on ANY model,
#                                           null resets to session default
#   apply_flag_settings control_request  -> sets effortLevel, ~10ms
# Every call therefore sees an empty context (the /clear is what preserves
# D159's isolation property), and only the first call after a crash or server
# start pays a spawn. Requests are SERIALIZED through the one process (a
# local single-user app; calls are seconds) rather than pooled.

# `effort` medium/high/xhigh passes through to Claude Code's own effort
# semantics (the same code path as the interactive /effort command) — only
# effort-capable models (sonnet/opus class) honor effortLevel. Absent or
# "low" means NO THINKING, enforced with the thinking-budget clamp, which
# works on every model including haiku (the default, which otherwise thinks
# by default in stream-json mode). See _AiSession.configure.
_AI_EFFORTS = ("low", "medium", "high", "xhigh")
_AI_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_AI_DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
_AI_TIMEOUT_S = 600.0
# A reconfiguration step (/clear, set_model, effort) is local work; one that
# takes longer than this means a wedged process — kill and respawn.
_AI_CTRL_TIMEOUT_S = 10.0
_AI_BIN_ENV = "FUSED_RENDER_CLAUDE_BIN"
# Model ids/aliases are a closed charset. This is a SECURITY boundary, not
# just validation: on the Windows .cmd-shim path argv is re-parsed by cmd.exe
# (whose quoting cannot be escaped reliably), so every argv element must be a
# static literal, a tempdir path, or a value this regex admitted.
_AI_MODEL_RE = re.compile(r"[A-Za-z0-9._-]+")



# Where Claude Code installs `claude`, for when it isn't on the PATH this
# process inherited — the packaged app's PATH is the supervisor's, not a
# shell's, and a Finder/Dock-launched .app misses ~/.local/bin and Homebrew.
# On Windows it is worse: a GUI launch inherits the PATH of its login session,
# so an install that appended to the *user* PATH afterwards stays invisible
# until the next sign-in.
#
# The claude chat template (templates/claude/agent.py) resolves the CLI the
# same way and this list looks much like its own. That is deliberate
# duplication, not a missing import: a template is standalone user-forkable
# code, and the only thing the server and a template share is the fused api.
# Neither side is authoritative for the other, so neither is held to the
# other's list.
#
# Ordered most-canonical first, `.exe` ahead of any `.cmd` shim: a shim has to
# be run through cmd.exe, which re-parses the command line (see _popen_argv).
_CLAUDE_WINDOWS_CANDIDATES = (
    # native installer (irm https://claude.ai/install.ps1 | iex) — recommended
    r"%USERPROFILE%\.local\bin\claude.exe",
    # winget install Anthropic.ClaudeCode, via winget's own shim dir
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe",
    # npm install -g @anthropic-ai/claude-code, in npm's global prefix
    r"%APPDATA%\npm\claude.exe",
    r"%APPDATA%\npm\claude.cmd",
    # legacy local npm install, written by older Claude Code versions
    r"%USERPROFILE%\.claude\local\claude.exe",
)
_CLAUDE_POSIX_CANDIDATES = ("~/.local/bin/claude", "/opt/homebrew/bin/claude",
                            "/usr/local/bin/claude")


from .ai_relay import (
    _ai_error,
    _claude_bin,
    _needs_cmd_shim,
    _cmd_quote,
    _popen_cmd,
    _kill_process_tree,
    _ai_cmd,
    _spawn_claude_stream,
    _ai_spawn,
    _ai_reap,
    _AiProcFailure,
    _AiSession,
    _ai_drive,
    _ai_result_payload,
    _ai_relay,
    _ai_usage,
)


_AI_SESSION = _AiSession()


from .session import (
    _sidecar_path,
    _read_sidecar,
    _has_non_mode_param,
    _is_file_mount_safe,
    _session_get,
    _session_put,
)


# User templates + their registry live under the shell home dir's templates/
# subdir (D76) — ~/.fused-render/templates/<name>/ and .../templates/registry.json
# — one level below the home dir that also holds bookmarks.json (shell/storage).
# home_dir() itself nests per branch ref (shell/storage), so branch isolation
# comes for free here — no branch logic needed in server.
USER_TEMPLATES_DIR = os.path.join(home_dir(), "templates")
USER_REGISTRY = os.path.join(USER_TEMPLATES_DIR, "registry.json")


# Per-gate probe budget (SPEC CT-12 fail-closed). One condition gate evaluation
# shares this wall-clock deadline across ALL its mount probes. On a
# non-direct-capable mount each operations/stat can burn the full rc timeout
# resolving a miss (rclone lists the whole parent prefix), so a gate's serialized
# probes would otherwise stack to N * that timeout. 5s bounds a whole gate to
# roughly one slow probe; direct-capable mounts probe in ~1s and rarely reach it.
GATE_PROBE_BUDGET_S = 5.0

# One bounded direct-listing page fed to the gate seed (fix #3/#4). All zarr
# group-root markers are immediate children of the store dir, so a COMPLETE
# (untruncated) page of the dir's children answers all three marker isfile
# probes with zero extra network calls; 1000 keys comfortably covers a store
# root's immediate children in one unsigned request.
_GATE_LIST_MAX_KEYS = 1000


from .conditions import (
    _GateSeed,
    _mount_gate_builtins,
    _run_condition,
    _mark_conditions,
    _evaluate_conditions,
    _conditions_payload,
)


_TEXT_SNIFF_BYTES = 8192


# Response headers forwarded from the rclone serve on a proxied /api/fs/raw.
# Content-Length/-Range/Accept-Ranges make ranged readers (duckdb httpfs)
# work; Last-Modified/ETag let their caches revalidate.
_PROXY_HEADERS = ("content-length", "content-range", "content-type",
                  "accept-ranges", "last-modified", "etag")


# Media types a browser will EXECUTE as a document rather than display as data.
_SCRIPTABLE_MEDIA = frozenset((
    "text/html", "application/xhtml+xml", "image/svg+xml"))

# Fetch destinations that make the response a document on THIS origin: a
# top-level navigation, or a frame of one.
_DOCUMENT_DESTS = frozenset(("document", "iframe", "frame", "embed", "object"))


from .raw_proxy import (
    _harden_raw,
    _proxy_raw,
    _proxy_raw_pooled,
    _pooled_send,
    _pooled_response,
    _bearer_status_passes,
    _proxy_raw_bearer,
)

from .mount_probe import (
    _MountProbe,
    _mount_probe,
    _mount_stat_payload,
    _probe_path,
    _mutation_result_payload,
    _stat_or_none,
    _stat_payload,
    _mount_safe_stat,
    _writable,
)

from .templates import (
    _resolve_name,
    _icon_for,
    _condition_file,
    _resolve_mode_list,
    _load_registry,
    _key_segments,
    _match_registry,
    _names_from_value,
    _looks_like_text,
    _templates_for,
)



from .fs_ops import (
    _fs_stat,
    _fs_write,
    _fs_mkdir,
    _fs_delete,
    _fs_rename,
    _fs_copy,
    _trash_supported,
    _trash_dest_name,
    _move_to_trash,
)


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
# Mount-backed paths on a remote that is NOT direct_list_capable (e.g.
# source.coop's custom S3 endpoint, not recognized as plain AWS S3): change
# detection there costs a full rc_list_dir of the prefix, not a bounded unsigned
# page. Poll such paths far less often to cut standing remote pressure, and skip
# listing a mount ROOT entirely (see _mount_signal) — fs/events P1 #4.
_MOUNT_SLOW_POLL_S = 60.0
_STAT_TIMEOUT_S = 4.0  # a stat outliving this reports "unchanged" for this tick

# Sentinel distinct from every real mtime signal (float, RFC3339 str, or None
# meaning "deleted"): _read() returns it for "no change / could not determine",
# which must NOT be confused with None (a real local-deletion signal, LR-6).
_UNCHANGED = object()


from .watch import _mtime_or_none, _hash_listing, _WatchEntry, _WatchRegistry


_WATCH_REGISTRY = _WatchRegistry()


def set_server_origin_env(port: int, host: str = "127.0.0.1") -> str:
    """Publish the server's ACTUAL bound origin so in-process runPython
    children read store bytes from the port the server is really on.

    The zarr_aoi tile daemon (and any other child that fetches bytes back
    through ``/api/fs/raw``) reads the origin from ``FUSED_RENDER_ORIGIN``.
    Without this, it falls back to ``_branch.branch_port()`` — the baseline
    default ``1777`` — which is wrong under any ``--port`` override (e.g. the
    desktop launcher's auto-picked free port), sending every read to a dead
    port and surfacing "No group found in store" from zarr. Set it before the
    server starts serving so every child process inherits the correct origin.
    """
    origin = f"http://{host}:{port}"
    os.environ["FUSED_RENDER_ORIGIN"] = origin
    return origin


def export_app_env() -> None:
    """Publish the resolved shell dirs so template children can find them
    WITHOUT importing ``fused_render`` (SPEC PY-15 / D166).

    Templates learn their environment through ``templates/shared/appenv.py``,
    which reads only env vars. That indirection exists because the fused local
    execution backend strips ``PYTHONPATH`` from child processes: a template's
    guarded ``from fused_render.shell.mounts import ...`` then silently takes its
    fallback branch and a mount-backed path gets treated as local. Env vars cross
    that boundary intact.

    Both values are exported ALREADY RESOLVED — ``home_dir()`` includes the
    per-branch nesting (``FUSED_RENDER_BRANCH``) and ``mounts_dir()`` is
    normpath'd — so no consumer re-implements those rules. Called from the same
    place as ``set_server_origin_env``, i.e. before the server starts serving, so
    every child process inherits them; the read-only mount list is exported
    separately by ``shell.mounts.export_ro_mounts_env`` because it has to be
    refreshed on every store write, not just at startup.
    """
    from fused_render.shell import mounts as shell_mounts
    from fused_render.shell import storage as shell_storage

    os.environ["FUSED_RENDER_HOME_DIR"] = shell_storage.home_dir()
    os.environ["FUSED_RENDER_MOUNTS_DIR"] = shell_mounts.mounts_dir()
    shell_mounts.export_ro_mounts_env()


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

    # Shared keep-alive HTTP pool for the opt-in pooled /api/fs/raw proxy
    # (TASK F). The pyramid/geotiff workers range-read a store's signed URL one
    # ~64KB block at a time; a per-block urllib GET (and, before this, a 307
    # they re-followed per block) pays a fresh TLS handshake every read — serial,
    # multi-second cold. One AsyncClient with a connection pool lets those range
    # reads reuse sockets to the store. Created at startup, closed at shutdown,
    # stashed on app.state so api_fs_raw can await through it.
    @app.on_event("startup")
    async def _open_pooled_client():
        app.state.pooled_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0),
            limits=httpx.Limits(max_keepalive_connections=32,
                                max_connections=64),
        )

    @app.on_event("shutdown")
    async def _close_pooled_client():
        client = getattr(app.state, "pooled_client", None)
        if client is not None:
            await client.aclose()

    # Warm claude instance for fused.ai (D168/D169): pay the ~2s Node/CLI
    # startup before the first request instead of inside it. Fire-and-forget —
    # server readiness never waits on it, and a missing binary just skips it.
    @app.on_event("startup")
    async def _prewarm_ai():
        _AI_SESSION.prewarm_default()

    @app.on_event("shutdown")
    async def _shutdown_ai_session():
        await _AI_SESSION.shutdown()

    @app.exception_handler(Exception)
    async def unhandled_exception(request, exc):
        # A bare "Internal Server Error" with an empty body is undebuggable on
        # a DMG install: Finder-launched apps have no visible stderr, so the
        # traceback used to vanish (e.g. a right-click "Open with FusedRender"
        # that 500s on /render or /api/run leaves nothing to report). Put the
        # traceback in the response body (local single-user tool, D3 — the
        # only reader owns the machine) AND in the log file so a later
        # `Open app logs` gives the full story. Log with the request line so a
        # noisy log still pins the failure to a URL.
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        # The id the middleware already stamped onto this request's call record
        # (it runs first, then re-raises past us). Echoing it here is what makes
        # `err_id` in the call log an actual join key rather than a dead field:
        # a screenshot of this 500 and the record in the log name each other.
        err_id = getattr(request.state, "fused_err_id", None)
        logger.error(
            "unhandled error on %s %s%s\n%s", request.method, request.url.path,
            f" [err_id {err_id}]" if err_id else "", tb
        )
        return _error(
            f"fused-render internal error on {request.method} "
            f"{request.url.path}"
            + (f" (err_id {err_id})" if err_id else "")
            + f":\n\n{tb}",
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
    # NOT IMPLEMENTED: detecting that the client hung up mid-request, which would
    # let an abandoned run be recorded as `disconnected` instead of a served
    # request (SPEC CL-5a's named gap). Both obvious approaches are dead ends
    # under this app's shape, verified rather than assumed:
    #
    #   * From a ROUTE: `BaseHTTPMiddleware` (which `@app.middleware("http")`
    #     builds) wraps the downstream `receive`, so `request.is_disconnected()`
    #     inside a handler never observes `http.disconnect` — a watcher there
    #     waits forever. Without the middleware in front it fires immediately,
    #     which is what makes this easy to "verify" wrongly.
    #   * From HERE: the middleware's own request CAN see the disconnect, but
    #     `is_disconnected()` peeks by CONSUMING a message off the receive
    #     channel (starlette.requests, an immediately-cancelled CancelScope
    #     around `_receive()`). Polling it steals the `http.request` body message
    #     the downstream route is waiting for, so every request with a body —
    #     /api/run included — hangs. A body-less spike hides this completely.
    #
    # Doing it properly means converting this middleware to pure ASGI so it can
    # tee the receive channel instead of racing the route for it. That is a
    # change to the hottest path in the server and belongs in its own commit,
    # not riding along with the call log. Meanwhile a supersession IS reported by
    # the page (CL-5a), which covers the common slider case; a closed tab or
    # reload still records as `ok`.

    @app.middleware("http")
    async def no_cache_and_log(request, call_next):
        # The app call log's single write point (calls.py, design §4.5). begin()
        # returns a record only for a request carrying runtime.js's
        # X-Fused-Page header — so the shell's own /api/fs/list, the conditions
        # probe, and every non-page caller are excluded by construction rather
        # than by an endpoint blocklist that would drift. Route handlers enrich
        # the same dict through request.state.fused_call; only finish() writes.
        call = shell_calls.begin(request)
        request.state.fused_call = call
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
        except asyncio.CancelledError:
            # The client went away mid-request — overwhelmingly a runPython
            # superseded by a newer call for the same .py (D114/RH-9) or a
            # closed tab. Recorded as its own outcome and kept out of every
            # latency statistic: a slider scrub would otherwise report dozens
            # of "slow" calls for what the user experienced as one request.
            if call is not None:
                shell_calls.finish(
                    call, status=None, elapsed_ms=(time.monotonic() - start) * 1000,
                    outcome="disconnected",
                )
            raise
        except Exception:
            if logged:
                dur = (time.monotonic() - start) * 1000
                logger.info("%s %s -> 500 (%.0f ms)", request.method, path, dur)
            # An unhandled exception escapes call_next: @app.exception_handler
            # runs in ServerErrorMiddleware, OUTSIDE user middleware, so this
            # except branch is the only place the record can be closed out.
            # Mint the correlation id here (we run before the handler) and stash
            # it so the handler can echo the same id into the 500 body.
            err_id = uuid.uuid4().hex[:12]
            request.state.fused_err_id = err_id
            if call is not None:
                shell_calls.finish(
                    call, status=500, elapsed_ms=(time.monotonic() - start) * 1000,
                    outcome="error", err_id=err_id,
                )
            raise
        if logged:
            dur = (time.monotonic() - start) * 1000
            logger.info(
                "%s %s -> %s (%.0f ms)", request.method, path, response.status_code, dur
            )
        if call is not None:
            shell_calls.finish(
                call,
                status=response.status_code,
                elapsed_ms=(time.monotonic() - start) * 1000,
                content_length=response.headers.get("content-length"),
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
    # The app call log (calls.py): GET /api/calls/config + the page-error
    # event POST. The records themselves are written by the middleware above.
    app.include_router(shell_calls.router)
    # Mounts: remote storage mounted as local paths via rclone rcd
    # (shell/mounts.py). startup() remounts every mount in a background
    # thread; mounts deliberately survive server restarts.
    from fused_render.shell import mounts as shell_mounts
    from fused_render.shell import prefetch as shell_prefetch

    app.include_router(shell_mounts.router)
    shell_mounts.startup()
    # Background mount-health monitor (shell/mounts.py): polls every mount on a
    # timer, auto-reconnects a wedged/disconnected NFS mount ONCE per disconnect
    # episode, and records an event log the Mounts panel polls. Started AFTER
    # startup() so the automount thread owns the initial attach — the monitor
    # only acts on a later healthy->disconnected transition.
    shell_mounts.start_health_monitor()

    # Mount-health telemetry the Mounts panel polls: current per-mount state
    # plus the auto-reconnect event log. A read — no X-Fused guard.
    @app.get("/api/mounts/health")
    def api_mounts_health():
        return shell_mounts.health_snapshot()
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
            # Whether the builtin learn mount record exists (D123) — the
            # sidebar's Learn entry only renders when this is true, so it's
            # never a dead link (BUGBOT: an unpackaged run with no
            # FUSED_RENDER_LEARN_ZIP, or the brief window before the
            # background automount thread upserts the record on a packaged
            # run, would otherwise show a link to a path that doesn't exist).
            "learn_mount_ready": shell_mounts.learn_mount_ready(),
            # The call-log store (calls.py). Same job as `mounts_root` above and
            # for a sharper reason: a call-log file is APPENDED TO by the act of
            # viewing it, so a page watching one reloads, re-reads, appends, and
            # reloads again. Watching it is never useful either — the viewers that
            # want live updates (log_studio's Tail) poll instead, precisely so a
            # reload cannot rebuild the frame mid-poll. Keyed off this prefix +
            # suffix so generic templates (code, duckdb, tree) need to know
            # nothing about the call log.
            #
            # Canonicalized on the way out: `abspath` is backslashed on Windows
            # while every path the runtime holds is forward-slashed, so the
            # prefix test in `isCallLog` would never fire there. (`mounts_root`
            # above has the same shape and is deliberately left alone — changing
            # it would newly ENABLE an exclusion on Windows, which is a mount
            # behaviour change and belongs with the mount code, not here.)
            "calls_dir": canonical_fs_path(os.path.abspath(shell_calls.store_dir())),
            "calls_suffix": shell_calls.SUFFIX,
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
        # Short check-on-read TTL cache (mirrors api_fs_conditions) to avoid
        # re-paying the ~1.6s cold parent-prefix LIST that a mount stat costs
        # (see _STAT_CACHE). Only MOUNT-backed paths are cached: a local stat is
        # a cheap kernel call, and a local file can be mutated out-of-band (git,
        # another editor) with no hook for us to invalidate — caching those
        # would risk handing back a stale mtime the editor's optimistic lock
        # trusts, for no latency win. Mount paths are mutated only through this
        # server's fs/write|rename|copy|delete|mkdir handlers, which call
        # _invalidate_stat_cache, so their only staleness is the same bounded
        # external-change window the conditions cache already accepts. Only
        # success payloads (plain dicts) are stored; _fs_stat's 404/503 branches
        # return _error -> JSONResponse and are always recomputed.
        from fused_render.shell.mounts import is_mount_backed

        cached = _STAT_CACHE.get(path)
        if cached is not None and time.monotonic() - cached[0] < _STAT_TTL_S:
            return cached[1]
        # Snapshot the invalidation generation BEFORE the (slow) stat. _fs_stat
        # releases the GIL on its cold mount LIST, so a concurrent mutation can
        # complete _invalidate_stat_cache — popping this key AND bumping the
        # generation — while we're in flight. Caching unconditionally here would
        # write our now-stale payload back, undoing that invalidation and
        # handing the editor's post-write optimistic-lock re-stat pre-mutation
        # metadata (a real clobber bug). So only cache if the generation is
        # UNCHANGED: the bump happens-before this check for any invalidation
        # that finished before our write, closing the TOCTOU window. If it
        # moved, return the fresh result WITHOUT caching it.
        gen = _STAT_CACHE_GEN
        result = _fs_stat(path)
        if isinstance(result, dict) and is_mount_backed(path) and _STAT_CACHE_GEN == gen:
            _STAT_CACHE[path] = (time.monotonic(), result)
        return result

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
        #
        # Re-navigating to the same directory would otherwise re-pay the full
        # gate-evaluation cost, so a short check-on-read TTL cache serves a
        # recent verdict. Only success payloads (plain dicts) are cached; error
        # responses (_error -> JSONResponse) are always recomputed.
        pm = _prefs_mtime()
        cached = _CONDITIONS_CACHE.get(path)
        if (cached is not None
                and time.monotonic() - cached[0] < _CONDITIONS_TTL_S
                and cached[1] == pm):
            return cached[2]
        result = _conditions_payload(path)
        if isinstance(result, dict):
            _CONDITIONS_CACHE[path] = (time.monotonic(), pm, result)
        return result

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
                if _win_protected(de):
                    # Windows hidden+system entries (e.g. the deny-ACL
                    # Documents\My Videos compat junction) — Explorer hides
                    # these, and scandir'ing into them raises WinError 5.
                    continue
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

    # Poll request.is_disconnected() once every this many walked entries. The
    # explorer search fires a fresh /api/fs/walk on each keystroke and abandons
    # the previous one; without this the superseded walk would keep enumerating
    # (over a mount, keep issuing remote LISTs) until it hit a cap. Checked
    # between entries only — a single blocking directory listing can't be
    # interrupted mid-call (same best-effort caveat as WALK_FLUSH_INTERVAL_S).
    WALK_DISCONNECT_CHECK_EVERY = 64

    @app.get("/api/fs/walk")
    async def api_fs_walk(request: Request, path: str, hidden: str = "0", stream: str = "0"):
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
        # The walk is bounded on three axes so a search-as-you-type over a big
        # (esp. mount) root can't kick off an unbounded enumeration: entry count
        # and DEPTH (both enforced inside _walk_bfs — see its caps), and the HTTP
        # request lifetime (if the client abandons this keystroke we stop pulling
        # from the walk; see WALK_DISCONNECT_CHECK_EVERY). The blocking walk runs
        # in a threadpool (iterate_in_threadpool) so this async route can poll for
        # disconnect without stalling the event loop.
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
        # is still running. Without `stream=1` the response is the original
        # single-JSON shape, unchanged for old clients.
        include_hidden = hidden == "1"
        # Under a mount, os.path.isdir is itself a kernel GETATTR on the mount
        # we route around; _walk_bfs lists mount dirs via the rc API and simply
        # yields nothing for a non-directory root, so the guard is local-only.
        under_mount = shell_mounts.is_mount_backed(path)
        if not under_mount and not os.path.isdir(path):
            return _error(f"not a directory: {path}", status=400)
        # Remote-mount clamp: under a mount mountpoint every directory is a
        # remote LIST round-trip, so both caps drop to their _REMOTE values (see
        # the constants' comments). The caps are enforced INSIDE _walk_bfs so the
        # walk terminates early instead of the consumer draining a huge tree.
        max_entries = WALK_MAX_ENTRIES_REMOTE if under_mount else WALK_MAX_ENTRIES
        max_depth = WALK_MAX_DEPTH_REMOTE if under_mount else WALK_MAX_DEPTH_LOCAL
        walker = _walk_bfs(path, include_hidden, max_entries=max_entries, max_depth=max_depth)

        # Force the ROOT listing eagerly (the first next() runs it) so a dead
        # mount / down rcd / timed-out or not-a-directory root fails with
        # fs/list's status codes instead of streaming a 200-empty body. Only the
        # ROOT raises out of _walk_bfs; deeper per-dir failures skip-and-continue
        # (feeding the truncated flag via the _WALK_TRUNCATED sentinel). Run in a
        # threadpool: the root listing is blocking (a remote LIST under a mount).
        def _pull_first():
            try:
                return next(walker), True
            except StopIteration:
                return None, False

        try:
            first, have_first = await run_in_threadpool(_pull_first)
        except shell_mounts.RcListError as e:
            return _mount_list_error_response(path, e)

        def _items():
            if have_first:
                yield first
            yield from walker

        if stream != "1":
            entries = []
            truncated = False
            seen = 0
            async for entry in iterate_in_threadpool(_items()):
                seen += 1
                if seen % WALK_DISCONNECT_CHECK_EVERY == 0 and await request.is_disconnected():
                    break  # client abandoned this keystroke — stop the walk
                if entry is _WALK_TRUNCATED:
                    truncated = True  # a dir was cut / skipped (partial coverage)
                    continue
                entries.append(entry)
                if len(entries) >= max_entries:
                    truncated = True
                    break
            return {"path": path, "entries": entries, "truncated": truncated}

        async def ndjson():
            batch = []
            total = 0
            truncated = False
            seen = 0
            last_flush = time.monotonic()
            async for entry in iterate_in_threadpool(_items()):
                seen += 1
                if seen % WALK_DISCONNECT_CHECK_EVERY == 0 and await request.is_disconnected():
                    return  # client abandoned this keystroke — stop the walk
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
    async def api_fs_raw(path: str, request: Request, base: str | None = None,
                         pooled: str | None = None):
        # Every response this route can produce goes through _harden_raw: the
        # read has four exits (HEAD, the 307, the proxied mount read, and the
        # local file) and three of them can put a scriptable content-type on
        # this origin, so the hardening lives at the single choke point rather
        # than being repeated — and cannot be missed by a new exit.
        return _harden_raw(
            await _api_fs_raw_read(path, request, base, pooled), request)

    async def _api_fs_raw_read(path: str, request: Request, base: str | None,
                               pooled: str | None):
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
                    # OPT-IN pooled proxy (TASK F): the pyramid/geotiff workers
                    # read this file with a plain per-block urllib GET and would
                    # otherwise re-follow the 307 to the store on EVERY ~64KB
                    # block — a fresh TLS handshake + redirect round trip per
                    # read, serial, multi-second cold. When they set &pooled=1
                    # we stream the same signed URL back through a shared
                    # keep-alive httpx pool (sockets reused across range reads).
                    # duckdb/parquet never set the flag, so they still get the
                    # 307 and their own pooled parallel connections — no
                    # regression. A pooled fetch that can't reach the store
                    # returns None and falls through to the 307.
                    if pooled:
                        resp = await _proxy_raw_pooled(
                            request.app.state.pooled_client, direct, request)
                        if resp is not None:
                            return resp
                    return RedirectResponse(direct, status_code=307)
                # No 307-able URL: a token-only private GCS remote can't hand
                # the client a signed link (the bearer token must never appear
                # in a URL), so proxy the bytes through the pooled client with
                # the Authorization header attached out-of-band — regardless of
                # the &pooled flag, there is nothing to redirect to. A stale
                # token (401/403) self-heals via one re-resolve + retry inside
                # _proxy_raw_bearer; a still-denied or unreachable read returns
                # None and falls through to the serve.
                resp = await _proxy_raw_bearer(request, path)
                if resp is not None:
                    return resp
            # Not redirected (browser, warm read, or no direct URL): proxy the
            # bytes. Guard non-files here — a directory proxied through rclone
            # serve comes back as a 200 HTML listing, so resolve shape before
            # serving. But NOT with a kernel os.stat on a mount-backed path: this
            # branch is reached once the prefetch has landed (is_done) or for a
            # browser read, and a cold GETATTR on a never-listed object forces
            # rclone to enumerate the whole parent prefix (~28s on a 44k-entry
            # dir), past the NFS deadman -> the mount is dropped. Answer
            # existence/shape through the rcd (_mount_probe) like the HEAD branch
            # above; only a local path stats the kernel.
            if shell_mounts.is_mount_backed(path):
                try:
                    pr = await asyncio.to_thread(_mount_probe, path)
                except (shell_mounts.RcListUnavailable,
                        shell_mounts.RcListTimeout) as e:
                    return _mount_list_error_response(os.path.dirname(path), e)
                if not pr.exists or pr.is_dir:
                    return _error(f"no such file: {path}", status=404)
            else:
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
        entries = [await _WATCH_REGISTRY.subscribe(p, queue) for p in paths]

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

    # Every mutation endpoint invalidates the /api/fs/stat cache for the paths it
    # touches (and their parents, via _invalidate_stat_cache) so the editor's
    # immediate post-mutation stat re-reads fresh metadata. Invalidation runs
    # unconditionally after the handler — a no-op on error/409 costs nothing, and
    # doing it here (not inside each _fs_* helper's many return branches) keeps
    # the contract in one obvious place per route.
    #
    # RESIDUAL: a RECURSIVE delete / overwriting rename of a directory does not
    # walk the (now-gone) subtree to evict individually-cached child stats. Those
    # entries simply age out within _STAT_TTL_S — the same bounded staleness the
    # cache accepts for out-of-band changes — and the editor navigates top-down,
    # so it re-lists the parent (fresh) before it would re-stat a vanished child.
    @app.post("/api/fs/write")
    def api_fs_write(request: Request, body: dict = Body(...),
                     x_fused: str | None = Header(default=None)):
        result = _fs_write(body, x_fused)
        _invalidate_stat_cache(body.get("path"))
        # What the app wrote and how big — never the content (calls.py).
        # `_fs_write` returns a stat payload on success and a JSONResponse on
        # every refusal, so the status has to come off the response object.
        shell_calls.enrich_write(
            getattr(request.state, "fused_call", None),
            path=body.get("path") if isinstance(body.get("path"), str) else "",
            content=body.get("content"),
            status=getattr(result, "status_code", 200),
            # Both refusals are 403; only a read-only target is `readonly`.
            # Re-asking the guard rather than re-spelling `x_fused != "1"` here,
            # so there is still one rule (it allocates nothing when it passes).
            unauthorized=_require_fused(x_fused) is not None,
        )
        return result

    @app.post("/api/fs/mkdir")
    def api_fs_mkdir(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        result = _fs_mkdir(body, x_fused)
        _invalidate_stat_cache(body.get("path"))
        return result

    @app.post("/api/fs/delete")
    def api_fs_delete(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        result = _fs_delete(body, x_fused)
        _invalidate_stat_cache(body.get("path"))
        return result

    @app.post("/api/fs/rename")
    def api_fs_rename(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        result = _fs_rename(body, x_fused)
        # A move changes both ends: src disappears, dst appears.
        _invalidate_stat_cache(body.get("src"), body.get("dst"))
        return result

    @app.post("/api/fs/copy")
    def api_fs_copy(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        result = _fs_copy(body, x_fused)
        # A copy only writes dst; src is untouched, so its cached stat stays valid.
        _invalidate_stat_cache(body.get("dst"))
        return result

    @app.get("/render")
    def render(path: str):
        if not _is_file_mount_safe(path):
            return _error(f"no such file: {path}", status=404)
        # Mount-backed pages read through the rclone serve like /api/fs/raw:
        # the kernel mount's first cold read can fail (EINVAL) mid-warmup.
        upstream = shell_mounts.serve_url_for(path)
        if upstream is not None:
            try:
                with urllib.request.urlopen(upstream, timeout=120) as r:
                    html = r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                e.close()
                return _error(f"cannot read {path}: HTTP {e.code}",
                              status=404 if e.code == 404 else 400)
            except OSError:
                return _error("mount serve unavailable", status=503)
        elif shell_mounts.is_mount_backed(path):
            return _error("mount serve unavailable", status=503)
        else:
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
    async def api_run(request: Request, body: dict = Body(...),
                      x_fused: str | None = Header(default=None)):
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
        engine_used = current_engine()
        if engine_used == "fused":
            from fused_render import engine as _engine

            work = _engine.run_python(resolved, params)
        else:
            # The built-in executor blocks on a subprocess; keep the event
            # loop free (the endpoint is async now for the engine's sake).
            work = asyncio.to_thread(run_python, resolved, params)
        result = await work
        # Hand the run's detail to the in-flight call record (calls.py): the
        # resolved .py, the params, the engine, and — on failure — the
        # traceback and output tails a user has since clicked away from. The
        # handler enriches; the middleware writes. (Whether the client hung up
        # mid-run is decided by the middleware — a route CANNOT see it; the
        # NOT IMPLEMENTED note above `no_cache_and_log` says why.)
        shell_calls.enrich_run(
            getattr(request.state, "fused_call", None),
            resolved=resolved, params=params, engine=engine_used, result=result,
        )
        # Tell the runtime which absolute file actually ran so it can watch it
        # for auto-reload (LR-2). Set on failed runs too, so a broken py that
        # gets fixed still triggers a reload.
        result["resolved_py"] = resolved
        # dumps_result, not JSONResponse: the in-process executor path already
        # serialized the payload (it has to, to validate it), so this reuses
        # that string instead of encoding a multi-MB result a second time. The
        # bytes are identical to JSONResponse's for every other result.
        return Response(content=dumps_result(result), media_type="application/json")

    @app.post("/api/ai")
    async def api_ai(body: dict = Body(...), x_fused: str | None = Header(default=None)):
        # fused.ai() — validation and the claude CLI hop live in _ai_relay
        # (module-level so tests can drive it with the subprocess mocked).
        guard = _require_fused(x_fused)
        if guard is not None:
            return guard
        return await _ai_relay(body)

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
