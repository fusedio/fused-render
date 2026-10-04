"""POST /api/windows/open — open an app in its own native window (macOS, Linux).

The shell calls this instead of navigating when it runs inside one of the
app's native windows (router.ts ``IS_NATIVE_WINDOW``) and the user clicks
an app: a card, the app page's Open. The window manager focuses the app's window
or opens one on its entry page as an embed (mac_window.py
``WindowManager.focus_or_open_app``), through
`window_policy.native_hooks["open_app"]`, which hops to the main thread.

On Linux the hook is `linux_windows.install`'s: it hands the URL to the
supervisor's WebKitGTK window host over a unix socket and falls back to a
browser tab when the host declines or is gone.

``404`` where there is no such hook — `fused-render serve`, Windows, a Linux
session whose window host never started — so the caller falls back to an in-page navigation.
"""
import os

from fastapi import APIRouter, Body, Header

from fused_render import window_policy
from fused_render.server.common import _error, _require_fused

router = APIRouter()


@router.post("/api/windows/open")
def api_windows_open(body: dict = Body(default={}),
                     x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return _error("'path' must be an absolute path")
    if not os.path.exists(path):
        return _error("no such path", 404)
    hook = window_policy.native_hooks.get("open_app")
    if hook is None:
        return _error("native windows are not available here", 404)
    hook(path)
    return {"ok": True}
