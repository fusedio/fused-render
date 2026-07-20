"""Menu-bar entry point for the packaged macOS app (SPEC DM-3/DM-5/DM-7).

Wraps the existing `create_app()` server with a `rumps` NSStatusItem whose
single surface is the pinned-view popover (menubar_pin.py, SPEC §25 D98):
header row of app actions + a WKWebView of the pinned file. The rumps menu is
only a fallback if the popover controller fails (PV-8). The CLI (`cli.py`,
`fused-render`) is unaffected and remains the dev entry point.

`rumps` is macOS-only and is not a core dependency (see the `app` extra in
pyproject.toml) — it is imported lazily, inside `main()`, so that
`import fused_render.app` never fails on another platform or in CI.
"""
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from fused_render._branch import branch_port, branch_ref
from fused_render.logs import log_path, setup_logging
from fused_render.server import create_app
from fused_render.shell.seed import ensure_fused_dir_and_landing

logger = logging.getLogger("fused_render")

_APP_SUPPORT_BASE = os.path.expanduser("~/Library/Application Support/fused-render")
APP_SUPPORT_DIR = (
    _APP_SUPPORT_BASE if not branch_ref() else os.path.join(_APP_SUPPORT_BASE, branch_ref())
)
PIDFILE = os.path.join(APP_SUPPORT_DIR, "server.pid")
PORTFILE = os.path.join(APP_SUPPORT_DIR, "server.port")

DEFAULT_PORT = branch_port()
MAX_PORT = DEFAULT_PORT + 10


def view_url_path(fs_path: str) -> str:
    """Shell URL path for a Finder-opened file (SB-9, D99).

    A `.bookmark` file is not previewed — it routes to the `_bookmark`
    sentinel, which reads the file server-side and redirects to the view it
    describes (the frontend resolves its relative paths against the file's
    own directory). Everything else opens as a plain `/view/<path>`.
    Module-level (not a closure) so it is testable without AppKit.
    """
    from urllib.parse import quote

    if fs_path.lower().endswith(".bookmark"):
        return "/view/_bookmark?file=" + quote(fs_path, safe="")
    return "/view" + quote(fs_path)


def clone_url_path(raw_url: str) -> str:
    """Shell URL path for an OS-delivered `fused-render://` deep link (SPEC
    §26, D110): the /clone confirm page with the raw link as ?src=. Parsing
    and validation happen server-side (deeplink.py); this only ferries the
    string. Module-level (not a closure) so it is testable without AppKit."""
    from urllib.parse import quote

    return "/clone?src=" + quote(raw_url, safe="")


def openurls_target_path(raw_url: str) -> str:
    """Shell URL path for an `application:openURLs:` event (SPEC §26, D110).

    AppKit delivers both `fused-render://` deep links AND plain document
    opens (e.g. a Finder double-click on a registered `.bookmark` file, as
    a `file://` URL) through this one selector — unlike `openFiles:`, which
    only ever gets plain paths. Only a `fused-render:` URL is a deep link;
    anything else is a file open and must resolve the same way
    `application_openFiles_` does, via `view_url_path`. Module-level (not a
    closure) so it is testable without AppKit.
    """
    if raw_url.lower().startswith("fused-render:"):
        return clone_url_path(raw_url)

    from urllib.parse import unquote, urlparse

    return view_url_path(unquote(urlparse(raw_url).path))


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


def _start_server_thread(port: int) -> tuple[uvicorn.Server, str | None]:
    """Start uvicorn serving create_app(start_dir=Fused dir) on a daemon thread.
    Also returns the first-launch landing path (the seeded showcase page's /view/
    URL) when THIS run performed the one-time example seed, else None."""
    # First-run onboarding (D81): create ~/Documents/Fused and seed it once.
    start_dir, landing = ensure_fused_dir_and_landing()
    app = create_app(start_dir=start_dir)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, landing


def main() -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    setup_logging()  # first: everything after this can crash-report to the file
    logger.info("app starting (pid %s)", os.getpid())

    existing = find_running_server()
    if existing is not None:
        pid, port = existing
        # Another instance already owns the menu bar and the pidfile; don't
        # start a second server, just point the browser at it and exit.
        logger.info("found live server (pid %s, port %s); reusing it", pid, port)
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
        "pin": None,         # menubar_pin.PinController, built after run loop start
    }

    def open_file_view(fs_path: str) -> None:
        target = f"http://127.0.0.1:{port}" + view_url_path(fs_path)
        if state["ready"]:
            logger.info("opening file view: %s", target)
            webbrowser.open(target)
        else:
            logger.info("queuing file view until server is ready: %s", target)
            state["pending"].append(target)

    # ---- Finder "Open with FusedRender" -------------------------------------
    # AppKit delivers double-clicked documents to the app delegate's
    # application:openFiles:. rumps's delegate (rumps.rumps.NSApp, a pyobjc
    # NSObject subclass) doesn't implement it — adding the method to the class
    # is all that's needed; pyobjc registers the selector automatically.
    def application_openFiles_(self, _app, filenames):
        # This is the "Right-Click open" path: Finder "Open with FusedRender".
        # Log the raw filenames the OS handed us — if a view later 500s, the
        # log ties the failing URL back to the file the user actually clicked.
        names = [str(n) for n in filenames]
        logger.info("Finder open-files event: %s", names)
        state["docs"] = True
        for name in names:
            open_file_view(name)

    rumps.rumps.NSApp.application_openFiles_ = application_openFiles_

    # ---- fused-render:// deep links (SPEC §26, D110) -------------------------
    # AppKit delivers URL-scheme opens (CFBundleURLTypes in the py2app plist)
    # to application:openURLs:. Same delegate-patch mechanism as openFiles
    # above; the /clone confirm page does all parsing and asks before any
    # clone, so this handler only ferries the raw URL to the server.
    #
    # AppKit also routes plain document opens (Finder double-click on a
    # registered file type, e.g. .bookmark) through this same selector as a
    # file:// URL on some launches, not through application:openFiles:.
    # openurls_target_path tells the two apart (mirrors the scheme check in
    # winopen.py's _open()).
    def application_openURLs_(self, _app, urls):
        raws = [str(u.absoluteString()) for u in urls]
        logger.info("deep-link open-URLs event: %s", raws)
        state["docs"] = True  # a deep-link launch shouldn't also open the home tab
        for raw in raws:
            target = f"http://127.0.0.1:{port}" + openurls_target_path(raw)
            if state["ready"]:
                logger.info("opening open-URLs target: %s", target)
                webbrowser.open(target)
            else:
                logger.info("queuing open-URLs target until server is ready: %s", target)
                state["pending"].append(target)

    rumps.rumps.NSApp.application_openURLs_ = application_openURLs_

    # ---- Dock icon click on the running app ---------------------------------
    # AppKit sends applicationShouldHandleReopen:hasVisibleWindows: when the
    # user clicks the Dock icon (or double-clicks the app in Finder) while the
    # app is already running. rumps's delegate doesn't implement it, so without
    # this patch a Dock click does nothing. Open the home tab; if the server is
    # still booting, queue it on the same pending list the bootstrap flushes.
    # Must return a BOOL — returning None here breaks the pyobjc bridge.
    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):
        logger.info("dock reopen event (server ready=%s)", state["ready"])
        if state["ready"]:
            webbrowser.open(url)
        else:
            state["pending"].append(url)
        return True

    rumps.rumps.NSApp.applicationShouldHandleReopen_hasVisibleWindows_ = (
        applicationShouldHandleReopen_hasVisibleWindows_
    )

    def _bootstrap_server() -> None:
        logger.info("starting server on port %s", port)
        server, landing = _start_server_thread(port)
        state["server"] = server
        if not _wait_until_ready(port):
            # Log file, not print: Finder-launched apps have no visible stderr.
            logger.error("server did not become ready on port %s", port)
            rumps.quit_application()
            return
        _write_pidfile(port)
        state["ready"] = True
        logger.info("server ready on port %s", port)
        if state["pin"] is not None:
            # AppKit is main-thread-only; this bootstrap runs on a worker.
            from PyObjCTools import AppHelper

            AppHelper.callAfter(state["pin"].server_ready)
        pending, state["pending"] = state["pending"], []
        for target in pending:
            webbrowser.open(target)
        # Home tab only when this launch wasn't a document double-click. A
        # brand-new install's very first launch lands on the seeded showcase
        # page instead of the workspace root.
        if not state["docs"] and not os.environ.get("FUSED_RENDER_NO_BROWSER"):
            webbrowser.open(f"http://127.0.0.1:{port}{landing}" if landing else url)

    class FusedRenderStatusApp(rumps.App):
        def __init__(self):
            # Template icon (black+alpha) — macOS recolors it for menu bar
            # appearance. Icon beats a text title: recognizable and compact
            # in a crowded (notched) menu bar.
            # This menu is normally never seen: the popover controller strips
            # it from the status item and carries these actions in its header
            # row (SPEC §25 PV-3, D98). It stays built as the fallback surface
            # if the controller fails to construct (PV-8) — the app must never
            # be left unquittable.
            super().__init__("fused-render", icon=icon_path, template=True, quit_button=None)
            self.menu = ["Open in browser", "Copy URL", "Open logs", "Quit"]

        @rumps.clicked("Open in browser")
        def open_browser(self, _sender):
            _open_browser()

        @rumps.clicked("Copy URL")
        def copy_url(self, _sender):
            _copy_url()

        @rumps.clicked("Open logs")
        def open_logs(self, _sender):
            _open_logs()

        @rumps.clicked("Quit")
        def quit(self, _sender):
            _do_quit()

    def _open_browser():
        webbrowser.open(url)

    def _copy_url():
        subprocess.run(["pbcopy"], input=url.encode(), check=False)

    def _open_logs():
        # Reveal in Finder rather than opening the file: users are asked to
        # zip/attach it, and Console.app (the .log default handler) confuses
        # more than it helps.
        subprocess.run(["open", "-R", log_path()], check=False)

    def _do_quit():
        if state["server"] is not None:
            state["server"].should_exit = True
        _remove_pidfile()
        rumps.quit_application()

    status_app = FusedRenderStatusApp()

    def _kickoff(timer):
        # One-shot, fired right after the run loop starts — the status item
        # (status_app._nsapp.nsstatusitem) exists only from this point on.
        timer.stop()
        try:
            # Lazy + guarded: pyobjc-framework-WebKit may be missing in an
            # older [app] env; on failure the rumps menu stays attached and
            # the app runs menu-only (PV-8).
            from fused_render.menubar_pin import PinController

            state["pin"] = PinController(
                status_app._nsapp.nsstatusitem,
                port,
                APP_SUPPORT_DIR,
                actions={
                    "open_browser": _open_browser,
                    "copy_url": _copy_url,
                    "open_logs": _open_logs,
                    "quit": _do_quit,
                },
            )
        except Exception:
            logger.exception("popover unavailable; falling back to the status-item menu")
        threading.Thread(target=_bootstrap_server, daemon=True).start()

    boot_timer = rumps.Timer(_kickoff, 0.1)
    boot_timer.start()

    status_app.run()


if __name__ == "__main__":
    main()
