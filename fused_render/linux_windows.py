"""Linux native windows, server side.

The Linux supervisor runs a WebKitGTK window host (`supervisor/_linux/
window_host.py`) and tells this server where to reach it through
`window_host_ipc.ENV_SOCKET`, plus whether a host could run here at all
through `ENV_LAUNCHABLE` (set regardless of whether one is actually running —
the preference that starts it may be off). With `ENV_SOCKET` set, `install`
fills `window_policy.native_hooks` the way `app.py` does for macOS, so the
shell's "open this app in its own window" (`POST /api/windows/open`) and the
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
    launchable = environ.get(ipc.ENV_LAUNCHABLE) == "1"

    def apply(on: bool) -> bool:
        """True unless the host is reachable and explicitly refused ``on``
        (the one case prefs.py must not record as a clean switch — the live
        state and prefs.json would then disagree for as long as that host
        keeps running). A host that is not reachable at all is not a
        failure: the host may not have started yet (the lazy-start design,
        core.py), and it reads this same preference for itself at its own
        startup, so the write still lands."""
        try:
            reply = ipc.request(sock, {"cmd": "set_enabled", "on": bool(on)},
                                 timeout=ipc.CALLER_TIMEOUT_S)
        except ipc.HostUnavailable as error:
            logger.info("window host not reachable for the windows preference "
                        "(applies once it starts): %s", error)
            return True
        if not reply.get("ok"):
            logger.warning("window host refused the windows preference: %s",
                           reply.get("reason"))
            return False
        return True

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
        # True once the host answers, but also when it merely COULD run here
        # (the supervisor's launchability check) so the Preferences section
        # survives a restart with the preference off and no host up to ping.
        "usable": lambda: launchable or ipc.ping(sock),
    })
    return True
