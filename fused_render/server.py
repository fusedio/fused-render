"""FastAPI app: static shell, filesystem API, HTML rendering, Python execution.

No path restriction anywhere — the whole filesystem is in scope by design
(see DECISIONS.md D2/D3). All `path` query params are absolute filesystem
paths. Endpoints are sync `def` so FastAPI dispatches them to its threadpool,
giving free concurrency for blocking filesystem/subprocess work.
"""
import mimetypes
import os

from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
}


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _template_for(path: str, is_dir: bool) -> str | None:
    if is_dir:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".htm"):
        return None
    name = TEMPLATES.get(ext)
    return os.path.join(TEMPLATES_DIR, name) if name else None


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

    @app.get("/api/config")
    def api_config():
        return {"start_dir": start_dir, "home": os.path.expanduser("~")}

    @app.get("/api/fs/stat")
    def api_fs_stat(path: str):
        if not os.path.exists(path):
            return _error(f"no such file or directory: {path}", status=404)
        is_dir = os.path.isdir(path)
        st = os.stat(path)
        return {
            "path": path,
            "name": os.path.basename(path) or path,
            "is_dir": is_dir,
            "size": None if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "template": _template_for(path, is_dir),
        }

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
    def api_run(body: dict = Body(...)):
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
        return JSONResponse(result)

    return app
