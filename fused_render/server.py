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
    ".txt": "text_template.html",
    ".py": "text_template.html",
    ".js": "text_template.html",
    ".ts": "text_template.html",
    ".json": "text_template.html",
    ".md": "text_template.html",
    ".csv": "text_template.html",
    ".log": "text_template.html",
    ".yaml": "text_template.html",
    ".yml": "text_template.html",
    ".toml": "text_template.html",
    ".sh": "text_template.html",
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
