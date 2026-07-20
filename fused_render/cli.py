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
        "--port", type=int, default=DEFAULT_PORT, help=f"port to bind (default: {DEFAULT_PORT})"
    )
    serve.add_argument(
        "--no-browser", action="store_true", help="do not open a browser tab on startup"
    )
    return parser


def _run_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from fused_render.server import create_app

    log_file = setup_logging()
    # First-run onboarding (D81): create ~/Documents/Fused and seed it once. Runs
    # regardless of --start-dir — seeding is about the Fused dir, not the start dir.
    # On the very first run, `landing` is the seeded showcase page and the browser
    # opens there instead of the workspace root.
    _, landing = ensure_fused_dir_and_landing()
    start_dir = os.path.abspath(os.path.expanduser(args.start_dir))
    app = create_app(start_dir=start_dir)

    url = f"http://127.0.0.1:{args.port}/"
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

    uvicorn.run(app, host="127.0.0.1", port=args.port)


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
