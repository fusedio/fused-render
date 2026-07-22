"""Command-line entry point.

One subcommand:
  * ``fused-render serve`` (the default when no subcommand is given, preserving the
    original ``fused-render [--start-dir DIR] [--port N]`` invocation) — the local
    127.0.0.1 file explorer.

Packing a renderable page into a portable bundle for hosted serving is a
``POST /api/export`` call on the running server (see server.py/export.py), not a
CLI subcommand — it needs no separate offline step.
"""
import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser

from fused_render._branch import branch_port, branch_ref
from fused_render.logs import setup_logging
from fused_render.shell.seed import ensure_fused_dir_and_landing, fused_dir

logger = logging.getLogger("fused_render")

DEFAULT_PORT = branch_port()

# Subcommand names; anything else as argv[1] falls through to the implicit `serve`
# so the historical bare `fused-render --port 9000` invocation keeps working.
_SUBCOMMANDS = ("serve",)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fused-render", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the local file explorer (default)")
    serve.add_argument(
        "--start-dir",
        default=fused_dir(),
        help="initial directory shown in the browser (default: ~/Documents/Fused). "
        "The whole filesystem remains browsable.",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port to bind (default: {DEFAULT_PORT}; startup fails if the port is "
        "already in use rather than silently picking another)",
    )
    serve.add_argument(
        "--no-browser", action="store_true", help="do not open a browser tab on startup"
    )
    return parser


_HOST = "127.0.0.1"


def _port_free(port: int) -> bool:
    """True if uvicorn could bind ``port`` on the loopback right now.

    Mirror uvicorn's own bind by setting SO_REUSEADDR so the probe agrees with
    it in both directions: an active listener (a stale server) still makes bind
    fail — SO_REUSEADDR does not permit two live binds to the same address, that
    needs SO_REUSEPORT — so a real collision is still caught, while a port merely
    lingering in TIME_WAIT after a clean shutdown reads as free (uvicorn, which
    also sets SO_REUSEADDR, would bind it). A plain bind here would reject those
    TIME_WAIT ports and wrongly block an immediate dev.sh restart.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((_HOST, port))
            return True
        except OSError:
            return False


def _check_port_free(port: int) -> None:
    """Fail loudly if ``port`` is already taken.

    Probing before uvicorn binds keeps the browser tab (opened a beat later)
    from landing on a leftover server: with per-branch ports (see
    fused_render._branch) a collision means a stale server for this same branch
    is already running, so we stop with a clear message rather than silently
    drifting to another port the tab wouldn't point at.
    """
    if not _port_free(port):
        raise SystemExit(
            f"port {port} is already in use — a server (likely a stale dev instance "
            "for this branch) is running there. Stop it, or pass a different --port."
        )


def _run_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from fused_render.server import create_app
    from fused_render.windows_process import install_no_window_policy

    install_no_window_policy()
    log_file = setup_logging()
    # First-run onboarding (D81): create ~/Documents/Fused and seed it once. Runs
    # regardless of --start-dir — seeding is about the Fused dir, not the start dir.
    # On the very first run, `landing` is the seeded showcase page and the browser
    # opens there instead of the workspace root.
    _, landing = ensure_fused_dir_and_landing()
    start_dir = os.path.abspath(os.path.expanduser(args.start_dir))
    app = create_app(start_dir=start_dir)

    port = args.port if args.port is not None else DEFAULT_PORT
    _check_port_free(port)

    url = f"http://{_HOST}:{port}/"
    branch_note = f" (branch {branch_ref()})" if branch_ref() else ""
    print(f"fused-render serving at {url}{branch_note}")
    print(f"start dir: {start_dir}")
    print(f"log file: {log_file}")
    # Explicit startup marker in the log (the boot line already timestamps it,
    # but this records the bind + start dir a session is running with).
    logger.info("serving at %s%s (start dir %s)", url, branch_note, start_dir)

    if not args.no_browser:
        open_url = url.rstrip("/") + landing if landing else url
        threading.Timer(1.0, lambda: webbrowser.open(open_url)).start()

    server = uvicorn.Server(uvicorn.Config(app, host=_HOST, port=port))
    app.state.uvicorn_server = server
    server.run()


def main() -> None:
    parser = _build_parser()

    # Preserve the historical bare invocation: `fused-render`, `fused-render --port N`,
    # etc. default to `serve`. Only inject the default when the first token is not a
    # subcommand and not the top-level -h/--help.
    argv = sys.argv[1:]
    if not argv or (argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv = ["serve", *argv]

    args = parser.parse_args(argv)
    _run_serve(args)


if __name__ == "__main__":
    main()
