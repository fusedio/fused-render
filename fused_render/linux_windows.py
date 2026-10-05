"""Linux native windows, server side.

The Linux supervisor runs a WebKitGTK window host (`supervisor/_linux/
window_host.py`) and tells this server where to reach it through
`window_host_ipc.ENV_SOCKET`. With that set, `install` fills
`window_policy.native_hooks` the way `app.py` does for macOS, so the shell's
"open this app in its own window" (`POST /api/windows/open`) and the
`native_windows_enabled` preference reach the host. Without it — `fused-render
serve`, macOS, Windows, a Linux session with no display — nothing is installed
and the hooks stay absent, exactly as before.

Every failure of the host degrades to a browser tab at the same address, never
to nothing and never to an exception in a request handler.
"""
from __future__ import annotations

import logging
import os
import sys
import webbrowser

from fused_render import window_host_ipc as ipc
from fused_render import window_policy

logger = logging.getLogger(__name__)


def _open_in_browser(url: str) -> None:
    webbrowser.open(url)


def install(port: int, environ=None, platform: str | None = None) -> bool:
    """Install the Linux hooks if a window host is reachable by configuration.
    Returns whether it did."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    sock = environ.get(ipc.ENV_SOCKET)
    if not platform.startswith("linux") or not sock:
        return False

    def apply(on: bool) -> None:
        try:
            ipc.request(sock, {"cmd": "set_enabled", "on": bool(on)}, timeout=ipc.CALLER_TIMEOUT_S)
        except ipc.HostUnavailable as error:
            logger.info("window host did not take the windows preference: %s", error)

    def open_app(fs_path: str) -> None:
        url = f"http://127.0.0.1:{port}" + window_policy.app_window_path(fs_path)
        try:
            shown = bool(ipc.request(sock, {"cmd": "open", "url": url},
                                     timeout=ipc.CALLER_TIMEOUT_S).get("ok"))
        except ipc.HostUnavailable as error:
            logger.info("window host unavailable (%s); opening a browser tab", error)
            shown = False
        if not shown:
            _open_in_browser(url)

    window_policy.native_hooks.update({
        "apply": apply,
        "open_app": open_app,
        "usable": lambda: ipc.ping(sock),
    })
    return True
