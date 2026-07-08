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
import json
import logging
import mimetypes
import os
import stat as stat_mod
import subprocess
import sys
import tempfile
import time
import traceback

from fastapi import Body, FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from fused_render.executor import run_python

logger = logging.getLogger(__name__)


def _select_engine() -> str:
    """Pick the /api/run engine for this process: "fused" or "builtin".

    Availability-driven (D69), builtin-by-default (D70): unset/`builtin` never
    touches the `fused` package even if it's importable; `auto` opts in to it
    iff importable; `fused` demands it (a missing package is a startup error,
    not a silent fallback). Logged either way — engine choice changes the code
    contract, so it must never be silent.
    """
    is_default = "FUSED_RENDER_ENGINE" not in os.environ
    requested = os.environ.get("FUSED_RENDER_ENGINE", "builtin").strip().lower()
    if requested not in ("auto", "fused", "builtin"):
        raise RuntimeError(
            f"FUSED_RENDER_ENGINE={requested!r} is not one of: auto, fused, builtin"
        )
    if requested == "builtin":
        if is_default:
            logger.info(
                "execution engine: builtin (default; set FUSED_RENDER_ENGINE=auto or "
                "=fused to opt in to the fused engine)"
            )
        else:
            logger.info("execution engine: builtin (forced by FUSED_RENDER_ENGINE)")
        return "builtin"
    try:
        from fused_render import engine as _engine

        ok = _engine.available()
    except ImportError:
        ok = False
    if ok:
        logger.info("execution engine: fused (local compute backend)")
        return "fused"
    if requested == "fused":
        raise RuntimeError(
            "FUSED_RENDER_ENGINE=fused but the `fused` package is not importable; "
            "install it (pip install 'fused-render[fused]') or unset the override"
        )
    logger.info("execution engine: builtin (`fused` package not installed)")
    return "builtin"

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
TEMPLATES_DIR = os.path.join(HERE, "templates")

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
# reference (D73); any other `_` name is invalid (CT-6).
KNOWN_SENTINELS = {"_render"}

# Recursive-walk cap (/api/fs/walk): stop collecting after this many entries so
# a search over a huge tree stays bounded. Module-level so tests can shrink it.
WALK_MAX_ENTRIES = 20000
# Directories never descended into by the walk (nor emitted as entries): heavy,
# machine-managed trees that only add noise to a file search.
WALK_IGNORE_DIRS = {"node_modules", "__pycache__", "venv"}


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


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


USER_TEMPLATES_DIR = os.path.expanduser("~/.fused-render")
USER_REGISTRY = os.path.join(USER_TEMPLATES_DIR, "registry.json")


def _resolve_name(name):
    """Single template-name resolution rule, used identically for built-in
    table entries and registry entries (SPEC PT-6): `<name>` resolves to
    `~/.fused-render/<name>/template.html` if present, else
    `fused_render/templates/<name>/template.html`, else unusable. A user
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
    return None, f"no template.html for {name!r} (looked in ~/.fused-render/{name}/ and built-in templates/{name}/)"


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
    directories, others only files. A key of the wrong shape (no leading dot,
    empty segment) never matches — same silent-ignore the no-leading-dot rule
    always had.
    """
    key = str(key).lower()
    dir_key = key.endswith("/")
    if dir_key != is_dir:
        return None
    if dir_key:
        key = key[:-1]
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
    `.*.json` > `.json`). A match needs a non-empty stem before the matched
    suffix, so a dotfile named exactly like a key (a file literally called
    ".json") does not match. Case-insensitive throughout.
    """
    fsegs = basename.lower().split(".")
    best = None  # (n_segments, literal-mask right-to-left, key, value)
    for key, value in registry.items():
        ksegs = _key_segments(key, is_dir)
        if ksegs is None or len(fsegs) <= len(ksegs):
            continue
        if not ".".join(fsegs[: -len(ksegs)]):
            continue
        tail = fsegs[-len(ksegs):]
        if any(not (k == f or (k == "*" and f)) for k, f in zip(ksegs, tail)):
            continue
        rank = (len(ksegs), tuple(s != "*" for s in reversed(ksegs)))
        if best is None or rank > best[0]:
            best = (rank, key, value)
    if best is None:
        return None
    return best[1], best[2]


def _names_from_value(key, value, builtin_names: list):
    """Interpret one matched registry value (SPEC CT-2/CT-10/CT-11).

    Returns (names, disabled, error). names: ordered list[str] of (possibly
    still-unresolved) template names, or None when the value is unusable.
    disabled: True when the value is `null` (CT-2) — no template at all, no
    error. error: a shape-level problem (value not list/string/null, >1 "..."
    splice) — surfaced as `template_error` so typos aren't silent.
    """
    if value is None:
        return None, True, None
    if isinstance(value, str):
        # String = exactly a single-mode list (D50).
        return [value], False, None
    if isinstance(value, list):
        # "..." splices the built-in list in place (CT-11); >1 is invalid.
        splice_count = sum(1 for entry in value if entry == "...")
        if splice_count > 1:
            return None, False, f"{key}: more than one '...' splice in template list"
        if splice_count == 0:
            return list(value), False, None
        explicit = {e for e in value if isinstance(e, str) and e != "..."}
        names = []
        for entry in value:
            if entry == "...":
                for bname in builtin_names:
                    if bname not in explicit and bname not in names:
                        names.append(bname)
            else:
                names.append(entry)
        return names, False, None
    return None, False, f"{key}: registry value must be a list, string, or null"


def _templates_for(path: str, is_dir: bool):
    """Returns (templates: list[dict], template_error: str|None) — SPEC PT-8.

    Both binding tables are registries in one format (D73): the built-in
    templates/registry.json and the user ~/.fused-render/registry.json, both
    resolved by `_match_registry` — dot-anchored suffix patterns with `*`
    wildcard segments and trailing-"/" directory keys. Directories therefore
    resolve exactly like files (a `.zarr` store matches the ".zarr/" key),
    and the user registry binds them too (D73 revises D65). Precedence: any
    user match > built-in match (CT-3). .html/.htm are ordinary keys (D73
    revises CT-4): the user can rebind them; "..." keeps `_render` reachable.
    A path with no match in either registry returns empty — unmapped file, or
    the plain listing view for a directory.
    """
    basename = os.path.basename(os.path.normpath(path))

    builtin_names = []
    builtin_reg, error = _load_registry(BUILTIN_REGISTRY, "built-in registry.json")
    if builtin_reg is not None:
        matched = _match_registry(builtin_reg, basename, is_dir)
        if matched is not None:
            # Splices are meaningless in the built-in registry (nothing to
            # splice into) — "..." there expands to nothing, harmless.
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
        return [], error
    if user_names is None:
        # No user binding, or a parse/shape-level problem — either way fall
        # back to the built-in list (CT-6); `error` carries the problem.
        entries, entry_err = _resolve_mode_list(builtin_names)
        return entries, error or entry_err

    entries, entry_err = _resolve_mode_list(user_names)
    error = error or entry_err
    if not entries:
        # The user's value resolved to nothing at all -> built-in fallback.
        entries, _ = _resolve_mode_list(builtin_names)
    return entries, error


def create_app(start_dir: str) -> FastAPI:
    engine_name = _select_engine()  # "fused" | "builtin" (D69/D70); raises on a bad override
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

    @app.get("/api/config")
    def api_config():
        return {
            "start_dir": start_dir,
            "home": os.path.expanduser("~"),
            # Which /api/run engine this process uses (D69): "fused" | "builtin".
            "engine": engine_name,
        }

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
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"path": path, "entries": entries}

    @app.get("/api/fs/walk")
    def api_fs_walk(path: str):
        # Recursive listing of a directory subtree, for the explorer search
        # (flat, ranked client-side). Prunes hidden entries (leading `.`) and
        # WALK_IGNORE_DIRS from descent, skips hidden files, and never follows
        # symlinks. Capped at WALK_MAX_ENTRIES; `rel` is posix-relative to
        # `path`. Unreadable entries are skipped silently (matches /api/fs/list).
        if not os.path.isdir(path):
            return _error(f"not a directory: {path}", status=400)
        entries = []
        truncated = False
        for root, dirs, files in os.walk(path, followlinks=False):
            # Mutating dirs in place both drops them from descent and, since we
            # emit from the (already pruned) list, keeps them out of the results.
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and d not in WALK_IGNORE_DIRS
            )
            batch = [(d, True) for d in dirs] + [
                (f, False) for f in sorted(files) if not f.startswith(".")
            ]
            for name, is_dir in batch:
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue  # unreadable entries skipped silently
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                entries.append(
                    {
                        "rel": rel,
                        "is_dir": is_dir,
                        "size": None if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
                if len(entries) >= WALK_MAX_ENTRIES:
                    truncated = True
                    break
            if truncated:
                break
        return {"path": path, "entries": entries, "truncated": truncated}

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
        # "Open in Finder" button goes through the server, which is local-only.
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
            cmd = ["explorer", path] if is_dir else ["explorer", "/select," + path]
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
    def render(path: str, annotate: str | None = Query(default=None, alias="_annotate")):
        if not os.path.isfile(path):
            return _error(f"no such file: {path}", status=404)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                html = f.read()
        except OSError as e:
            return _error(f"cannot read {path}: {e}", status=400)

        # Always inject the runtime. Only when the iframe URL carries `_annotate=1`
        # (annotate mode, AN-4/AN-15) is the annotation overlay injected alongside
        # it — normal pages pay zero cost. annotate.js self-activates off the same
        # flag on its own window.location, so this conditional and the flag must
        # agree; ordering after runtime.js is intentional (the overlay reuses the
        # same shell-URL replaceState channel the runtime establishes, §17.4).
        injection = '<script src="/static/runtime.js"></script>'
        if annotate == "1":
            injection += '<script src="/static/annotate.js"></script>'
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

        # Engine dispatch (D69): both paths return the same wire shape
        # ({ok, result, error:{type,message,traceback}, stdout} — the fused
        # engine adds stderr/duration_ms), so pages never see which ran.
        if engine_name == "fused":
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
