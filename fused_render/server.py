"""FastAPI app: static shell, filesystem API, HTML rendering, Python execution.

No path restriction anywhere — the whole filesystem is in scope by design
(see DECISIONS.md D2/D3). All `path` query params are absolute filesystem
paths. Endpoints are sync `def` so FastAPI dispatches them to its threadpool,
giving free concurrency for blocking filesystem/subprocess work.
"""
import asyncio
import json
import logging
import mimetypes
import os
import stat as stat_mod
import tempfile
import traceback

from fastapi import Body, FastAPI, Header, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from fused_render.executor import run_python

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
TEMPLATES_DIR = os.path.join(HERE, "templates")

# ext -> ordered list of template names, first = default (SPEC PT-7, D60).
# A name is a folder name (fused_render/templates/<name>/), never a filename.
TEMPLATES = {
    ".parquet": ["table"],  # binary — code mode would be garbage
    ".csv": ["csv", "code"],
    ".tsv": ["csv", "code"],
    ".xlsx": ["xlsx"],
    ".json": ["tree", "code"],
    ".geojson": ["tree", "code"],
    ".md": ["markdown", "code"],
    ".svg": ["image", "code"],
    ".png": ["image"],
    ".jpg": ["image"],
    ".jpeg": ["image"],
    ".gif": ["image"],
    ".webp": ["image"],
    ".pdf": ["pdf"],
    # audio/video — one template branches on extension
    ".mp4": ["media"],
    ".mov": ["media"],
    ".m4v": ["media"],
    ".webm": ["media"],
    ".mp3": ["media"],
    ".wav": ["media"],
    ".m4a": ["media"],
    ".ogg": ["media"],
    ".flac": ["media"],
    # source code — CodeMirror, mode chosen by extension (note: .json routes to
    # the JSON tree template above, not here)
    # python — code default, with the swagger-style `api` run form (D63)
    # available as a second mode.
    ".py": ["code", "api"],
    ".js": ["code"],
    ".ts": ["code"],
    ".sh": ["code"],
    ".yaml": ["code"],
    ".yml": ["code"],
    ".toml": ["code"],
    ".css": ["code"],
    # plain text
    ".txt": ["text", "code"],
    ".log": ["text", "code"],
    # scientific rasters / arrays — decoded in-browser by vendored ESM bundles
    # (see scripts/vendor-sci); no reader.py involved. Binary formats, so no
    # code mode.
    ".tif": ["geotiff"],
    ".tiff": ["geotiff"],
    ".nc": ["netcdf"],
    ".nc4": ["netcdf"],
    ".cdf": ["netcdf"],
    # hardcoded, registry-exempt (SPEC CT-4): users can't rebind .html/.htm
    # or drop _render. _render is a shell sentinel (PT-12, D62) — no
    # template folder behind it.
    ".html": ["_render", "code"],
    ".htm": ["_render", "code"],
}

# Directory templates: a DIRECTORY whose basename carries one of these
# extensions resolves through the same ordered-name model as files (e.g. a
# `.zarr` store is one logical dataset spread across many chunk files, so it
# previews as a dataset rather than as a folder listing). Same {ext: [names]}
# shape as TEMPLATES; names resolve through `_resolve_name` identically.
DIR_TEMPLATES = {
    ".zarr": ["zarr"],
}


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
            "for shell sentinel modes (SPEC PT-12) and cannot be used in "
            "the registry"
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


def _resolve_mode_list(names, allow_sentinel=False):
    """Resolve an ordered list of template names into `templates` stat
    entries (SPEC PT-8). Per-entry validation (SPEC CT-6): a name that can't
    resolve is dropped; `error` is the first dropped name's message.

    `allow_sentinel` is set only when `names` is a built-in list (SPEC
    PT-12): a `_`-prefixed name there is a shell sentinel and is emitted as
    `{"mode": name, "path": None, "icon": None}` without touching the
    filesystem. When False (registry-resolved names), a `_`-prefixed name
    falls through to `_resolve_name`, which rejects it — the sentinel
    namespace is shell-owned, not user-addressable (CT-4/CT-6).
    """
    entries = []
    error = None
    for name in names:
        if allow_sentinel and isinstance(name, str) and name.startswith("_"):
            entries.append({"mode": name, "path": None, "icon": None})
            continue
        path, err = _resolve_name(name)
        if path is None:
            if error is None:
                error = err
            continue
        entries.append({"mode": name, "path": path, "icon": _icon_for(path)})
    return entries, error


def _user_names_for(filename: str, builtin_names: list):
    """Resolve the registry-configured template name list for filename
    against ~/.fused-render/registry.json (SPEC §16, CT-10/CT-11).

    Returns (names, disabled, error). names: ordered list[str] of (possibly
    still-unresolved) template names, or None when no registry binding
    applies at all. disabled: True when the matching value is `null` (SPEC
    CT-2) — no template at all, no error. error: a parse/shape-level problem
    (unreadable registry.json, non-dict, value not list/string/null, >1
    "..." splice) — the caller falls back to the built-in list and surfaces
    this as `template_error` so typos aren't silent. Read per call: a tiny
    local file, and it makes registry edits apply on the next stat with no
    restart and no cache to invalidate.
    """
    try:
        with open(USER_REGISTRY, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except FileNotFoundError:
        return None, False, None
    except (OSError, ValueError) as e:
        return None, False, f"cannot read registry.json: {e}"
    if not isinstance(registry, dict):
        return None, False, "registry.json must be a JSON object"

    # Longest-suffix match, case-insensitive. Dotted keys are what make
    # compound extensions (".tar.gz") expressible — the built-in table can't.
    lower = filename.lower()
    best = None
    for key in registry:
        k = str(key).lower()
        if k.startswith(".") and len(k) > 1 and lower.endswith(k):
            if best is None or len(k) > len(best[0]):
                best = (k, registry[key])
    if best is None:
        return None, False, None

    ext, value = best
    if value is None:
        return None, True, None
    if isinstance(value, str):
        # String = exactly today's meaning: a single-mode list (D50).
        return [value], False, None
    if isinstance(value, list):
        # "..." splices the built-in list in place (CT-11); >1 is invalid.
        splice_count = sum(1 for entry in value if entry == "...")
        if splice_count > 1:
            return None, False, f"{ext}: more than one '...' splice in template list"
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
    return None, False, f"{ext}: registry value must be a list, string, or null"


def _templates_for(path: str, is_dir: bool):
    """Returns (templates: list[dict], template_error: str|None) — SPEC PT-8.

    Precedence: user registry (longest suffix) > built-in table. .html/.htm
    skip the registry entirely (SPEC §4/CT-4, D62) — renderable HTML is the
    core semantic, so their mode list is hardcoded — but still resolve
    through the ordinary built-in-list path like any other extension,
    `allow_sentinel=True` so `_render` emits without touching the fs.

    Directories resolve against `DIR_TEMPLATES` by the extension on their
    basename (a `.zarr` store), through the same `_resolve_mode_list` path so
    entries come out identically shaped. Directory templates are
    PACKAGE-ONLY: the user registry (`_user_names_for`) is a per-file suffix
    match — it walks a filename against dotted registry keys — and there is no
    coherent way for such a file-suffix rule to bind a directory, so the
    registry deliberately does not apply here (D65). A directory with no
    DIR_TEMPLATES match returns empty, i.e. the plain listing view.
    """
    if is_dir:
        ext = os.path.splitext(os.path.basename(os.path.normpath(path)))[1].lower()
        return _resolve_mode_list(DIR_TEMPLATES.get(ext, []))
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    builtin_names = TEMPLATES.get(ext, [])
    if ext in (".html", ".htm"):
        return _resolve_mode_list(builtin_names, allow_sentinel=True)

    names, disabled, err = _user_names_for(filename, builtin_names)
    if disabled:
        return [], None
    if names is None:
        # No registry binding, or a parse/shape-level problem — either way
        # fall back to the plain built-in list (CT-6); `err` is None in the
        # former case, set in the latter.
        entries, _ = _resolve_mode_list(builtin_names, allow_sentinel=True)
        return entries, err

    entries, entry_err = _resolve_mode_list(names)
    error = err or entry_err
    if not entries:
        # The user's value resolved to nothing at all -> built-in fallback.
        entries, _ = _resolve_mode_list(builtin_names, allow_sentinel=True)
    return entries, error


def create_app(start_dir: str) -> FastAPI:
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

    @app.middleware("http")
    async def no_cache(request, call_next):
        # App code changes between restarts and user files change on disk;
        # stale browser caches of shell/runtime JS cause confusing half-old UIs.
        response = await call_next(request)
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

    @app.get("/api/fs/raw")
    def api_fs_raw(path: str):
        if not os.path.isfile(path):
            return _error(f"no such file: {path}", status=404)
        media_type, _ = mimetypes.guess_type(path)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @app.get("/api/fs/events")
    async def api_fs_events(path: list[str] = Query(default=[])):
        # SSE change feed (SPEC §13.2). Async def on purpose: a sync def would pin
        # a threadpool thread per open view for the lifetime of the page. Polling
        # stat every 200ms is dependency-free and cheap at local scale; upgrading
        # to real FS events later is internal to this endpoint.
        def mtime_of(p):
            try:
                return os.stat(p).st_mtime
            except OSError:
                return None

        async def stream():
            last = {p: mtime_of(p) for p in path}
            ticks = 0
            while True:
                await asyncio.sleep(0.2)
                for p in path:
                    m = mtime_of(p)
                    if m != last[p]:
                        last[p] = m
                        yield f"data: {json.dumps({'path': p, 'mtime': m})}\n\n"
                ticks += 1
                if ticks % 75 == 0:  # 75 × 200ms = keepalive every 15 s (WF-3)
                    yield ": keepalive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

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
    def api_run(body: dict = Body(...), x_fused: str | None = Header(default=None)):
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

        result = run_python(resolved, params)
        # Tell the runtime which absolute file actually ran so it can watch it
        # for auto-reload (LR-2). Set on failed runs too, so a broken py that
        # gets fixed still triggers a reload.
        result["resolved_py"] = resolved
        return JSONResponse(result)

    return app
