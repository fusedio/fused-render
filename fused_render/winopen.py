"""Windows Explorer "Open with" entry point (SPEC: Windows opener).

Invoked as `fused-render-open [--port N] [FILE]` (the gui-scripts entry point;
`pythonw.exe -m fused_render.winopen` is equivalent) by the right-click
"Open with fused-render" verb / OpenWithProgids registration that
scripts/windows/register_open_with.ps1 writes into HKCU. Finds an
already-running server (portfile + HTTP probe, mirroring find_running_server()
in app.py) or starts one detached, waits for it to become ready, then opens
the browser at the file's /view URL (or at / with no FILE). Runs windowless
under pythonw.exe, so failures surface via a message box, not a terminal.

Kept stdlib-only and light at import time: this module never imports uvicorn/
fastapi/the server directly, it only spawns `python -m fused_render.cli serve`
as a subprocess.
"""
import argparse
import ctypes
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import quote

from fused_render._branch import branch_port, branch_ref
from fused_render.logs import setup_logging

logger = logging.getLogger("fused_render")

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
_APP_SUPPORT_BASE = os.path.join(_LOCALAPPDATA, "fused-render")
APP_SUPPORT_DIR = (
    _APP_SUPPORT_BASE if not branch_ref() else os.path.join(_APP_SUPPORT_BASE, branch_ref())
)
PIDFILE = os.path.join(APP_SUPPORT_DIR, "server.pid")
PORTFILE = os.path.join(APP_SUPPORT_DIR, "server.port")
SERVER_LOG = os.path.join(APP_SUPPORT_DIR, "server.out.log")

DEFAULT_PORT = branch_port()
MAX_PORT = DEFAULT_PORT + 10


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _probe(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_running_server() -> int | None:
    """Return the port of an already-live fused-render instance, or None.

    Liveness is decided by the HTTP probe alone, not the pidfile: on Windows
    os.kill(pid, 0) doesn't check liveness, it calls TerminateProcess (there is
    no signal-0 no-op like on POSIX), so using it here would kill a live
    server instead of checking it. The pidfile is still written on start, for
    humans/debugging, but never read back for this decision.
    """
    port = _read_int(PORTFILE)
    if port is None or not _probe(port):
        return None
    return port


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


def _write_pidfile(pid: int, port: int) -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(pid))
    with open(PORTFILE, "w", encoding="utf-8") as f:
        f.write(str(port))


def _start_server(port: int) -> subprocess.Popen:
    """Spawn `python -m fused_render.cli serve` detached from this process.

    sys.executable under pythonw.exe IS pythonw.exe (fine here, don't swap to
    python.exe): its own stdout/stderr are None, but cli.py print()s at
    startup, so the child needs a real stream — redirect both to
    server.out.log rather than inherit them.
    """
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    log_file = open(SERVER_LOG, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "fused_render.cli", "serve", "--no-browser", "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        ),
    )
    log_file.close()
    return proc


def _view_url(port: int, path: str | None) -> str:
    """Build the URL frontend/src/lib/router.ts's codec decodes back to `path`.

    Mirrors urlForFsPath(): backslashes become forward slashes for a
    drive-letter path, then each "/"-segment is percent-encoded on its own
    (so "C:" -> "C%3A") and rejoined with a literal "/" — matching
    encodeURIComponent segment-by-segment, not a whole-path quote.
    """
    if not path:
        return f"http://127.0.0.1:{port}/"
    fs_path = os.path.abspath(path).replace("\\", "/")
    segments = [quote(seg, safe="") for seg in fs_path.split("/") if seg]
    return f"http://127.0.0.1:{port}/view/" + "/".join(segments)


def _fail(msg: str) -> None:
    logger.error(msg)
    ctypes.windll.user32.MessageBoxW(0, msg, "fused-render", 0x10)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m fused_render.winopen",
        description="Windows Explorer 'Open with' entry point for fused-render.",
    )
    parser.add_argument("--port", type=int, default=None, help="port to use/reuse (default: autodetect)")
    parser.add_argument("path", nargs="?", default=None, help="file to open in /view (default: home)")
    args = parser.parse_args()

    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    setup_logging()  # first: everything after this can crash-report to the file
    logger.info("winopen starting (pid %s, path=%r, port=%r)", os.getpid(), args.path, args.port)

    if args.path and not os.path.exists(args.path):
        _fail(f"fused-render: file not found:\n{args.path}")
        return

    if args.port is not None:
        port, alive = args.port, _probe(args.port)
    else:
        port = find_running_server()
        alive = port is not None
        if port is None:
            port = pick_port()

    if not alive:
        # Re-probe once right before spawning: two rapid double-clicks race
        # here, and the loser's bind fails while this probe finds the winner.
        # No lock, that's an acceptable outcome.
        alive = _probe(port)

    if not alive:
        proc = _start_server(port)
        logger.info("starting server (pid %s, port %s)", proc.pid, port)
        if not _wait_until_ready(port):
            _fail(f"fused-render: server did not start on port {port}.\nSee log: {SERVER_LOG}")
            return
        _write_pidfile(proc.pid, port)

    url = _view_url(port, args.path)
    logger.info("opening %s", url)
    webbrowser.open(url)


if __name__ == "__main__":
    main()
