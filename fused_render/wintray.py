"""Windows system-tray controller for the packaged app.

A tray icon that starts (or reuses) a detached fused-render server and lets the
user open it in the browser or stop it and quit — the Windows analog of the
macOS menu-bar app. The installer's Start Menu shortcut launches this
(`pythonw -m fused_render.wintray`).

`pystray`/`Pillow` are only in the Windows bundle (the `windows` extra), so they
are imported inside `main()` — `import fused_render.wintray` stays safe on any
platform and in CI.
"""
import logging
import subprocess
import sys
import webbrowser

from fused_render import winopen
from fused_render.logs import setup_logging

logger = logging.getLogger("fused_render")


def _kill_server() -> None:
    """Stop the detached server by the pid winopen recorded, if any."""
    pid = winopen._read_int(winopen.PIDFILE)
    if pid is not None:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True,
        )
    winopen._remove_pidfile()


def main() -> None:
    import pystray
    from PIL import Image

    setup_logging()
    try:
        port = winopen._ensure_server(None)
    except Exception as exc:  # noqa: BLE001 - surface any startup failure to the user
        logger.exception("tray: could not start server")
        winopen.ctypes.windll.user32.MessageBoxW(0, f"fused-render: {exc}", "fused-render", 0x10)
        raise SystemExit(1)

    def on_open(icon, item):
        webbrowser.open(f"http://127.0.0.1:{port}/")

    def on_quit(icon, item):
        _kill_server()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open fused-render", on_open, default=True),
        pystray.MenuItem("Stop server and quit", on_quit),
    )
    icon = pystray.Icon(
        "fused-render",
        Image.open(winopen._ICON_PATH),
        f"fused-render (port {port})",
        menu,
    )
    webbrowser.open(f"http://127.0.0.1:{port}/")
    icon.run()


if __name__ == "__main__":
    main()
