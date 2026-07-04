"""Menu-bar entry point for the packaged macOS app (SPEC DM-3/DM-5/DM-7).

Wraps the existing `create_app()` server with a `rumps` NSStatusItem: no Dock
icon, no windows, just Open in browser / Copy URL / Quit. The CLI (`cli.py`,
`fused-render`) is unaffected and remains the dev entry point.

`rumps` is macOS-only and is not a core dependency (see the `app` extra in
pyproject.toml) — it is imported lazily, inside `main()`, so that
`import fused_render.app` never fails on another platform or in CI.
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from fused_render.server import create_app

APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/fused-render")
PIDFILE = os.path.join(APP_SUPPORT_DIR, "server.pid")
PORTFILE = os.path.join(APP_SUPPORT_DIR, "server.port")

DEFAULT_PORT = 8765
MAX_PORT = 8775


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def find_running_server() -> tuple[int, int] | None:
    """Return (pid, port) of an already-live fused-render instance, or None.

    "Live" means: the recorded pid is running AND it serves the shell page
    on the recorded port. Probing "/" (not /api/config) matters: "/" reads
    shell.html from disk, so a zombie whose bundle files were deleted or
    replaced (e.g. a build-dir instance clobbered by a rebuild) fails the
    probe and a fresh healthy instance gets started instead.
    """
    pid = _read_int(PIDFILE)
    port = _read_int(PORTFILE)
    if pid is None or port is None or not _is_process_alive(pid):
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
            if resp.status == 200:
                return pid, port
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pick_port(start: int = DEFAULT_PORT, end: int = MAX_PORT) -> int:
    for port in range(start, end + 1):
        if not _port_in_use(port):
            return port
    raise RuntimeError(f"no free port between {start} and {end}; is something hogging the whole range?")


def _wait_until_ready(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    return False


def _write_pidfile(port: int) -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    with open(PORTFILE, "w") as f:
        f.write(str(port))


def _remove_pidfile() -> None:
    for path in (PIDFILE, PORTFILE):
        try:
            os.remove(path)
        except OSError:
            pass


def _start_server_thread(port: int) -> uvicorn.Server:
    """Start uvicorn serving create_app(start_dir=home) on a daemon thread."""
    home = os.path.expanduser("~")
    app = create_app(start_dir=home)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


def main() -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)

    existing = find_running_server()
    if existing is not None:
        _, port = existing
        # Another instance already owns the menu bar and the pidfile; don't
        # start a second server, just point the browser at it and exit.
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return

    port = pick_port()
    url = f"http://127.0.0.1:{port}/"

    import rumps  # macOS-only; see module docstring

    icon_path = os.path.join(os.path.dirname(__file__), "assets", "menubar-template.png")

    # Startup ordering matters (learned the hard way): the AppKit run loop
    # starts FIRST and the server boots in the background AFTER it. Document
    # open events (Finder double-click) are delivered once the run loop is
    # up; the server takes seconds to become ready. Deciding home-vs-file
    # AFTER server readiness therefore happens long after any launch document
    # event has arrived — no timing race, unlike every timer-window variant.
    state = {
        "ready": False,      # server answers; safe to open browser tabs
        "docs": False,       # at least one document open event arrived
        "pending": [],       # file views requested before the server was ready
        "server": None,      # uvicorn.Server, set by the bootstrap thread
    }

    def open_file_view(fs_path: str) -> None:
        from urllib.parse import quote

        target = f"http://127.0.0.1:{port}/view{quote(fs_path)}"
        if state["ready"]:
            webbrowser.open(target)
        else:
            state["pending"].append(target)

    # ---- Finder "Open with FusedRender" -------------------------------------
    # AppKit delivers double-clicked documents to the app delegate's
    # application:openFiles:. rumps's delegate (rumps.rumps.NSApp, a pyobjc
    # NSObject subclass) doesn't implement it — adding the method to the class
    # is all that's needed; pyobjc registers the selector automatically.
    def application_openFiles_(self, _app, filenames):
        state["docs"] = True
        for name in filenames:
            open_file_view(str(name))

    rumps.rumps.NSApp.application_openFiles_ = application_openFiles_

    def _bootstrap_server() -> None:
        server = _start_server_thread(port)
        state["server"] = server
        if not _wait_until_ready(port):
            print(f"fused-render: server did not become ready on port {port}", flush=True)
            rumps.quit_application()
            return
        _write_pidfile(port)
        state["ready"] = True
        pending, state["pending"] = state["pending"], []
        for target in pending:
            webbrowser.open(target)
        # Home tab only when this launch wasn't a document double-click.
        if not state["docs"]:
            webbrowser.open(url)

    class FusedRenderStatusApp(rumps.App):
        def __init__(self):
            # Template icon (black+alpha) — macOS recolors it for menu bar
            # appearance. Icon beats a text title: recognizable and compact
            # in a crowded (notched) menu bar.
            super().__init__("fused-render", icon=icon_path, template=True, quit_button=None)
            self.menu = ["Open in browser", "Copy URL", "Quit"]

        @rumps.clicked("Open in browser")
        def open_browser(self, _sender):
            webbrowser.open(url)

        @rumps.clicked("Copy URL")
        def copy_url(self, _sender):
            subprocess.run(["pbcopy"], input=url.encode(), check=False)

        @rumps.clicked("Quit")
        def quit(self, _sender):
            if state["server"] is not None:
                state["server"].should_exit = True
            _remove_pidfile()
            rumps.quit_application()

    def _kickoff(timer):
        # One-shot, fired right after the run loop starts.
        timer.stop()
        threading.Thread(target=_bootstrap_server, daemon=True).start()

    boot_timer = rumps.Timer(_kickoff, 0.1)
    boot_timer.start()

    FusedRenderStatusApp().run()


if __name__ == "__main__":
    main()
