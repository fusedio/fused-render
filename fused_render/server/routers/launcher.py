"""GET /api/launcher?q= — the launcher's search (fused_render/launcher.py).

The panel's page (`static/launcher.html`, served by the /static mount) asks
this per keystroke. ``running`` comes from the window manager through
`launcher.native_hooks["open_keys"]` — a plain attribute read, safe from
this thread — and is empty wherever there are no windows. The settings are
part of `/api/prefs` (shell/prefs.py: `launcher_hotkey`,
`launcher_row_modifier`), not a route of their own.
"""
from fastapi import APIRouter, Query

from fused_render import launcher

router = APIRouter()


def _running() -> set[str]:
    hook = launcher.native_hooks.get("open_keys")
    if hook is None:
        return set()
    try:
        return set(hook())
    except Exception:  # noqa: BLE001 — a dot is not worth a failed search
        return set()


@router.get("/api/launcher")
def api_launcher(q: str = Query(default="")):
    """``apps`` as before; ``files`` (and the index's coverage ``reason``
    when it had none to give) for a non-empty query. Sync on purpose: the
    index query blocks, and FastAPI runs a plain ``def`` in its threadpool."""
    apps = launcher.results(q, _running())
    files = launcher.file_results(q, exclude=[a["path"] for a in apps if a.get("path")])
    return {"query": q, "apps": apps, "files": files["files"],
            "files_reason": files["reason"]}
