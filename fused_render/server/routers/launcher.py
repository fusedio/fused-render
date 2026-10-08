"""`/launcher` and GET /api/launcher?q= — the launcher's page and search
(fused_render/launcher.py).

The panel's page (`/launcher`, a Vite page: frontend/launcher.html →
shell-dist/launcher.html, hosted by launcher_panel.py) asks the search per
keystroke. ``running`` comes from the window manager through
`launcher.native_hooks["open_keys"]` — a plain attribute read, safe from
this thread — and is empty wherever there are no windows. Under Fused Bot
the rows are bots and there are no file rows at all (the index is not
asked). The settings are part of `/api/prefs` (shell/prefs.py: wire names
`launcher_hotkey`, `launcher_row_modifier`, stored under the flavor's
keys), not a route of their own.
"""
import os

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import FileResponse, PlainTextResponse

from fused_render import _flavor, launcher
from fused_render.server.common import STATIC_DIR, _error, _require_fused

router = APIRouter()


@router.get("/launcher")
def launcher_page():
    page = os.path.join(STATIC_DIR, "shell-dist", "launcher.html")
    if not os.path.isfile(page):
        return PlainTextResponse(
            "launcher page not built (frontend/launcher.html → shell-dist/launcher.html); "
            "run: cd frontend && npm run build", status_code=503)
    return FileResponse(page, headers={"Cache-Control": "no-store"})


def _running() -> set[str]:
    hook = launcher.native_hooks.get("open_keys")
    if hook is None:
        return set()
    try:
        return set(hook())
    except Exception:  # noqa: BLE001 — a dot is not worth a failed search
        return set()


@router.post("/api/launcher/suspend")
def api_launcher_suspend(body: dict = Body(default={}),
                         x_fused: str | None = Header(default=None)):
    """Preferences is recording a shortcut (``{"on": true}``): the app
    unbinds the live launcher and row shortcuts until ``{"on": false}``, so
    the keys being pressed reach the recorder. A no-op with no panel."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    on = body.get("on")
    if not isinstance(on, bool):
        return _error("'on' must be a boolean")
    hook = launcher.native_hooks.get("suspend")
    if hook is not None:
        hook(on)
    return {"ok": True, "suspended": on if hook is not None else False}


@router.get("/api/launcher")
def api_launcher(q: str = Query(default="")):
    """``apps`` as before; ``files`` (and the index's coverage ``reason``
    when it had none to give) for a non-empty query. Sync on purpose: the
    index query blocks, and FastAPI runs a plain ``def`` in its threadpool.
    Under Fused Bot ``apps`` holds bot rows and ``files`` is always empty:
    the Bot app has no explorer to open a file in."""
    apps = launcher.results(q, _running())
    if _flavor.is_bot():
        return {"query": q, "apps": apps, "files": [], "files_reason": ""}
    files = launcher.file_results(q, exclude=[a["path"] for a in apps if a.get("path")])
    return {"query": q, "apps": apps, "files": files["files"],
            "files_reason": files["reason"]}
