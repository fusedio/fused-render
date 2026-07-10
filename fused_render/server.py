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
import json
import logging
from collections import deque
import mimetypes
import os
import stat as stat_mod
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import parse_qsl

from fastapi import Body, FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from fused_render import __version__
from fused_render.core_templates import ensure_core_templates
from fused_render.deploy import router as deploy_router
from fused_render.executor import run_python
from fused_render.shell import prefs as shell_prefs
from fused_render.shell import storage
from fused_render.shell.bookmarks import router as bookmarks_router
from fused_render.shell.prefs import router as prefs_router
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
# Entries per NDJSON batch line in the streamed walk: big enough that framing
# overhead is noise, small enough that the client gets its first scoreable
# batch within a few filesystem reads.
WALK_BATCH_SIZE = 500
# Directories never descended into by the walk: heavy, machine-managed trees
# that only add noise to a file search. Checked against the bare name, so it
# also applies under hidden=1 (".git" is machine noise, not "hidden data").
WALK_IGNORE_DIRS = {"node_modules", "__pycache__", "venv", ".venv", ".git"}
# macOS package directories: emitted as a single (dir) entry but never
# descended — their internals are implementation details (Finder hides them
# too), and one Electron .app alone can be thousands of files.
WALK_LEAF_DIR_SUFFIXES = (".app", ".framework", ".bundle", ".photoslibrary")


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
    """
    queue = deque([(path, "")])
    while queue:
        current, rel_base = queue.popleft()
        try:
            with os.scandir(current) as it:
                children = list(it)
        except OSError:
            continue  # unreadable dir skipped silently
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
                    queue.append((os.path.join(current, child.name), rel))


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
    return entries, error


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
    # prefs), kept out of this module's fs/render internals.
    app.include_router(bookmarks_router)
    app.include_router(prefs_router)
    # PROTOTYPE: remote-storage connectors (rclone-managed mounts as local
    # paths). Throwaway — see shell/connectors_prototype.py.
    from fused_render.shell.connectors_prototype import router as connectors_router

    app.include_router(connectors_router)
    # Deploy (hosted publish through the fused CLI) — export + `fused share`
    # orchestration and the per-page deployment pointer store (deploy.py).
    app.include_router(deploy_router)
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
        if not os.path.exists(path):
            return _error(f"no such file or directory: {path}", status=404)
        is_dir = os.path.isdir(path)
        st = os.stat(path)
        templates, template_error = _templates_for(path, is_dir)
        payload = {
            "path": path,
            "name": os.path.basename(path) or path,
            "is_dir": is_dir,
            "size": None if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "templates": templates,
        }
        if template_error:
            payload["template_error"] = template_error
        return payload

    @app.get("/api/fs/list")
    def api_fs_list(path: str):
        if not os.path.isdir(path):
            return _error(f"not a directory: {path}", status=400)
        entries = []
        try:
            names = os.listdir(path)
        except OSError as e:
            return _error(f"cannot read directory {path}: {e}", status=400)
        for name in names:
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
            except OSError:
                continue  # unreadable entries skipped silently
            is_dir = os.path.isdir(full)
            entries.append(
                {
                    "name": name,
                    "is_dir": is_dir,
                    "size": None if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        ignored = _git_ignored(path, [e["name"] for e in entries])
        for e in entries:
            e["ignored"] = e["name"] in ignored
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"path": path, "entries": entries}

    def _annotate_ignored(root: str, batch: list[dict]) -> None:
        # Gitignore dimming for walk entries (#75's list behavior, per batch in
        # the streamed walk — one `git check-ignore --stdin` call per batch).
        ignored = _git_ignored(root, [e["rel"] for e in batch])
        for e in batch:
            e["ignored"] = e["rel"] in ignored

    @app.get("/api/fs/walk")
    def api_fs_walk(path: str, hidden: str = "0", stream: str = "0"):
        # Recursive listing of a directory subtree, for the explorer search
        # (flat, ranked client-side). Walks BREADTH-FIRST so shallow entries —
        # the ones a search almost always targets — are all emitted before any
        # deep subtree can exhaust the WALK_MAX_ENTRIES cap (the old
        # depth-first walk let one big sibling starve every later one). Prunes
        # WALK_IGNORE_DIRS entirely, emits WALK_LEAF_DIR_SUFFIXES packages
        # without descending, never follows symlinks, and skips unreadable
        # entries silently (matches /api/fs/list). `rel` is posix-relative to
        # `path`.
        #
        # `hidden=1` (explicit intent: the user typed a dot-leading query)
        # includes dot-files and descends into dot-dirs. WALK_IGNORE_DIRS is
        # always pruned regardless — those trees are noise, not "hidden data",
        # and letting hidden=1 descend into .git/node_modules would flood the
        # results with machine-managed junk.
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

        if stream != "1":
            entries = []
            truncated = False
            for entry in walker:
                entries.append(entry)
                if len(entries) >= WALK_MAX_ENTRIES:
                    truncated = True
                    break
            _annotate_ignored(path, entries)
            return {"path": path, "entries": entries, "truncated": truncated}

        def ndjson():
            batch = []
            total = 0
            truncated = False
            for entry in walker:
                batch.append(entry)
                total += 1
                if len(batch) >= WALK_BATCH_SIZE:
                    _annotate_ignored(path, batch)
                    yield json.dumps({"entries": batch}) + "\n"
                    batch = []
                if total >= WALK_MAX_ENTRIES:
                    truncated = True
                    break
            if batch:
                _annotate_ignored(path, batch)
                yield json.dumps({"entries": batch}) + "\n"
            yield json.dumps({"done": True, "truncated": truncated, "total": total}) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    @app.get("/api/fs/raw")
    def api_fs_raw(path: str):
        if not os.path.isfile(path):
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
        guard = _require_fused(x_fused)
        if guard is not None:
            return guard

        path = body.get("path")
        content = body.get("content")
        expected_mtime = body.get("expected_mtime")

        if not path or not os.path.isabs(path):
            return _error("'path' must be an absolute filesystem path")
        if not isinstance(content, str):
            return _error("'content' must be a string")
        if os.path.isdir(path):
            return _error(f"path is a directory: {path}")
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            return _error(f"parent directory does not exist: {parent}", status=404)

        # Optimistic lock: the editor sends the mtime it last saw; if the file
        # changed (or was deleted) underneath it, refuse so the edit doesn't
        # clobber someone else's write. Compare against the raw st_mtime float
        # that /api/fs/stat returns, with a tolerance for float round-tripping.
        exists = os.path.exists(path)
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

        # Same shape as /api/fs/stat so the editor can re-arm its lock.
        st = os.stat(path)
        templates, template_error = _templates_for(path, False)
        payload = {
            "path": path,
            "name": os.path.basename(path) or path,
            "is_dir": False,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "templates": templates,
        }
        if template_error:
            payload["template_error"] = template_error
        return payload

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

        try:
            plan = export_page(page, out)
        except ExportError as e:
            return _error(str(e))

        return {
            "out": os.path.abspath(out),
            "entrypoints": [{"path": e.path, "name": e.name, "file": e.file} for e in plan.entrypoints],
            "assets": [{"path": a.path, "name": a.name, "file": a.file} for a in plan.assets],
        }

    return app
