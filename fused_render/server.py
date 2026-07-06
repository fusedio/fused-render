"""FastAPI app: static shell, filesystem API, HTML rendering, Python execution.

No path restriction anywhere — the whole filesystem is in scope by design
(see DECISIONS.md D2/D3). All `path` query params are absolute filesystem
paths. Endpoints are sync `def` so FastAPI dispatches them to its threadpool,
giving free concurrency for blocking filesystem/subprocess work.
"""
import asyncio
import json
import mimetypes
import os
import stat as stat_mod
import tempfile

from fastapi import Body, FastAPI, Header, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from fused_render.executor import run_python

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
TEMPLATES_DIR = os.path.join(HERE, "templates")

TEMPLATES = {
    ".parquet": "parquet_template.html",
    ".png": "image_template.html",
    ".jpg": "image_template.html",
    ".jpeg": "image_template.html",
    ".gif": "image_template.html",
    ".webp": "image_template.html",
    ".svg": "image_template.html",
    ".md": "markdown_template.html",
    ".csv": "csv_template.html",
    ".tsv": "csv_template.html",
    ".json": "json_template.html",
    ".geojson": "json_template.html",
    ".xlsx": "xlsx_template.html",
    ".pdf": "pdf_template.html",
    # audio/video — one template branches on extension
    ".mp4": "media_template.html",
    ".mov": "media_template.html",
    ".m4v": "media_template.html",
    ".webm": "media_template.html",
    ".mp3": "media_template.html",
    ".wav": "media_template.html",
    ".m4a": "media_template.html",
    ".ogg": "media_template.html",
    ".flac": "media_template.html",
    # source code — CodeMirror, mode chosen by extension (note: .json routes to
    # the JSON tree template above, not here)
    ".py": "code_template.html",
    ".js": "code_template.html",
    ".ts": "code_template.html",
    ".sh": "code_template.html",
    ".yaml": "code_template.html",
    ".yml": "code_template.html",
    ".toml": "code_template.html",
    ".css": "code_template.html",
    # plain text
    ".txt": "text_template.html",
    ".log": "text_template.html",
    # scientific rasters / arrays — vendored decoders (see scripts/vendor-sci).
    # .zarr is a directory, not a file, so it lives in DIR_TEMPLATES below.
    ".tif": "geotiff_template.html",
    ".tiff": "geotiff_template.html",
    ".nc": "netcdf_template.html",
    ".nc4": "netcdf_template.html",
    ".cdf": "netcdf_template.html",
}

# Directory templates: a directory whose basename carries one of these
# extensions renders through a template instead of the listing view (e.g. a
# `.zarr` store is one logical dataset spread across many chunk files).
DIR_TEMPLATES = {
    ".zarr": "zarr_template.html",
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

# Sentinel distinguishing "registry maps this extension to null" (disable the
# built-in, render nothing) from "registry has no binding" (None).
_DISABLED = object()


def _user_template_for(filename: str):
    """Resolve a filename against ~/.fused-render/registry.json (SPEC §16).

    Returns (resolution, error). resolution: absolute template.html path,
    _DISABLED (extension bound to null), or None (no binding). error: string
    when a binding exists but is unusable — the caller falls back to the
    built-in and surfaces it as `template_error` so typos aren't silent.
    Read per call: a tiny local file, and it makes registry edits apply on
    the next stat with no restart and no cache to invalidate.
    """
    try:
        with open(USER_REGISTRY, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, f"cannot read registry.json: {e}"
    if not isinstance(registry, dict):
        return None, "registry.json must be a JSON object"

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
        return None, None

    ext, name = best
    if name is None:
        return _DISABLED, None
    # The name is joined into a filesystem path, so it must be one plain
    # segment — a stray "../x" must not stat arbitrary locations. Correctness
    # guard, not auth (D3 stands).
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or name in (".", "..")
    ):
        return None, f"invalid template folder name for {ext}: {name!r}"
    template = os.path.join(USER_TEMPLATES_DIR, name, "template.html")
    if not os.path.isfile(template):
        return None, f"{ext} -> {name}: no template.html in ~/.fused-render/{name}/"
    return template, None


def _template_for(path: str, is_dir: bool):
    """Returns (template path | None, template_error | None).

    Files: precedence is user registry (longest suffix) > built-in table.
    .html/.htm never route through a template — renderable HTML is the core
    semantic (SPEC §4) — so they skip the registry too.

    Directories: mapped only by the built-in `DIR_TEMPLATES` (e.g. a `.zarr`
    store). The user registry (M7/D50) is a per-file suffix match and does not
    extend to directory templates yet (D52); dir templates are package-only.
    """
    if is_dir:
        # A directory maps to a template by the extension on its basename (e.g. a
        # `.zarr` store). normpath strips any trailing slash so basename is real.
        ext = os.path.splitext(os.path.basename(os.path.normpath(path)))[1].lower()
        name = DIR_TEMPLATES.get(ext)
        return (os.path.join(TEMPLATES_DIR, name) if name else None), None
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".html", ".htm"):
        return None, None
    user, err = _user_template_for(filename)
    if user is _DISABLED:
        return None, None
    if isinstance(user, str):
        return user, None
    name = TEMPLATES.get(ext)
    return (os.path.join(TEMPLATES_DIR, name) if name else None), err


def create_app(start_dir: str) -> FastAPI:
    app = FastAPI(title="fused-render")
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

    @app.middleware("http")
    async def no_cache(request, call_next):
        # App code changes between restarts and user files change on disk;
        # stale browser caches of shell/runtime JS cause confusing half-old UIs.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    shell_path = os.path.join(STATIC_DIR, "shell.html")

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
            # The shell renders HTML "Source" view through this editable template
            # (code_template maps .html → CM.html()), so it needs the abs path.
            "source_template": os.path.join(TEMPLATES_DIR, "code_template.html"),
        }

    @app.get("/api/fs/stat")
    def api_fs_stat(path: str):
        if not os.path.exists(path):
            return _error(f"no such file or directory: {path}", status=404)
        is_dir = os.path.isdir(path)
        st = os.stat(path)
        template, template_error = _template_for(path, is_dir)
        payload = {
            "path": path,
            "name": os.path.basename(path) or path,
            "is_dir": is_dir,
            "size": None if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "template": template,
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
        template, template_error = _template_for(path, False)
        payload = {
            "path": path,
            "name": os.path.basename(path) or path,
            "is_dir": False,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "template": template,
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
