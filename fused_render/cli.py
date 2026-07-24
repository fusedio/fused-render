"""Command-line entry point.

Two subcommands:
  * ``fused-render serve`` (the default when no subcommand is given, preserving the
    original ``fused-render [--start-dir DIR] [--port N]`` invocation) — the local
    127.0.0.1 file explorer.
  * ``fused-render calls`` — read the app call log (calls.py) from a terminal.
    Reads the store directly off disk, so it works with no server running.

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
import time
import webbrowser

from fused_render._branch import branch_port, branch_ref
from fused_render.logs import setup_logging
from fused_render.shell.seed import ensure_fused_dir_and_landing, fused_dir

logger = logging.getLogger("fused_render")

DEFAULT_PORT = branch_port()

# Subcommand names; anything else as argv[1] falls through to the implicit `serve`
# so the historical bare `fused-render --port 9000` invocation keeps working.
_SUBCOMMANDS = ("serve", "calls")


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

    calls = sub.add_parser(
        "calls",
        help="read the app call log (what API calls your pages made)",
        description="Read the app call log written by the running server "
                    "(~/.fused-render/calls). Prints a digest by default; use "
                    "--verbose for whole records.",
    )
    calls.add_argument("--page", default="", help="only calls made by this page (absolute path)")
    calls.add_argument("--since", default="1h",
                       help="window: 30s / 15m / 2h / 7d (default: 1h; 'all' for everything)")
    calls.add_argument("--failed", action="store_true", help="only errors and conflicts")
    calls.add_argument("--entrypoint", default="", help="only calls whose target contains this")
    calls.add_argument("--limit", type=int, default=20, help="records to show (default: 20)")
    calls.add_argument("--since-cursor", default="", metavar="CALL_ID",
                       help="only records newer than this call_id — pass back the "
                            "'cursor' from a previous run to get exactly what is new")
    calls.add_argument("--json", action="store_true", dest="as_json",
                       help="machine-readable output (a digest plus failing records)")
    calls.add_argument("--verbose", action="store_true",
                       help="with --json, include every record, not just failures")
    calls.add_argument("--follow", action="store_true",
                       help="wait for new records to appear, then print and exit")
    calls.add_argument("--timeout", type=float, default=60.0,
                       help="seconds --follow waits before giving up (default: 60)")
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
    # Publish the real bound origin so runPython children (e.g. the zarr_aoi
    # tile daemon) read store bytes from THIS port, not the branch default.
    from fused_render.server import set_server_origin_env
    set_server_origin_env(port, host=_HOST)

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


_AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_age(text: str) -> float:
    """``30s`` / ``15m`` / ``2h`` / ``7d`` -> seconds. ``all``/empty -> 0 (no bound)."""
    text = (text or "").strip().lower()
    if not text or text in ("all", "0"):
        return 0.0
    unit = _AGE_UNITS.get(text[-1])
    try:
        return float(text[:-1]) * unit if unit else float(text)
    except (TypeError, ValueError):
        raise SystemExit(
            f"could not read --since {text!r}; use forms like 30s, 15m, 2h, 7d, or 'all'"
        ) from None


def _run_calls(args: argparse.Namespace) -> None:
    """Print the call log: a digest, then the records that matter.

    Digest-first is deliberate and is the whole point of this surface (design
    §9.2c): the caller is usually an agent verifying a page it just wrote, and
    dumping hundreds of raw records at it burns the context that the actual
    diagnosis needs. Failures print in full because that is what you came for;
    successes collapse into the per-target rollup.
    """
    import json as _json

    from fused_render import calls as call_log

    since = _parse_age(args.since)
    filters = {
        "page": os.path.abspath(os.path.expanduser(args.page)) if args.page else None,
        "entrypoint": args.entrypoint or None,
        "since": (time.time() - since) if since else None,
        "failed": args.failed,
    }

    if args.follow:
        # Wait for something new rather than making the caller guess how long
        # the human took to open the page (design §9.2d). Polls the store: it is
        # a bounded read of a local file, and a watcher would need a running
        # server this command deliberately does not require.
        deadline = time.monotonic() + max(1.0, args.timeout)
        baseline = call_log.query(limit=1, **filters)["cursor"]
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if call_log.query(limit=1, **filters)["cursor"] != baseline:
                break
        else:
            if not args.as_json:
                print(f"no new calls within {args.timeout:g}s")

    page = call_log.query(limit=args.limit, cursor=args.since_cursor or None, **filters)
    overview = call_log.overview(**filters)
    targets = call_log.targets(**filters)["targets"]
    records = page["records"]
    page_errors = [r for r in records if r.get("kind") == "page-error"]
    # Page errors are reported in their own section below (they are not call
    # failures — they are what happened instead of a call), so keep them out of
    # this list rather than printing each one twice.
    failures = [r for r in records
                if r.get("outcome") in ("error", "conflict") and r.get("kind") != "page-error"]

    if args.as_json:
        print(_json.dumps({
            "overview": overview,
            "targets": targets,
            "cursor": page["cursor"],
            "records": records if args.verbose else failures,
            "records_omitted": 0 if args.verbose else len(records) - len(failures),
        }, indent=2, default=str))
        return

    outcomes = overview["outcomes"]
    if not overview["total"]:
        print("no calls recorded" + (f" for {filters['page']}" if filters["page"] else "")
              + f" in the last {args.since}")
        print(f"store: {call_log.store_dir()}"
              + ("" if call_log.enabled() else "  (capture is OFF)"))
        return

    print(f"{overview['total']} record(s) · " + " · ".join(
        f"{name} {count}" for name, count in sorted(outcomes.items())))
    if overview.get("dropped"):
        print(f"  ({overview['dropped']} record(s) dropped — rate cap or queue full)")

    if targets:
        print("\ntarget                          calls    p50    p95    max  errors")
        for row in targets[:15]:
            def ms(value):
                return "     —" if value is None else f"{value:6.0f}"
            print(f"  {row['name'][:28]:<28}  {row['count']:>5}"
                  f" {ms(row['p50'])} {ms(row['p95'])} {ms(row['max'])}"
                  f"  {row['errors'] or '-':>6}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for rec in failures:
            error = rec.get("error") or {}
            print(f"\n  {rec.get('occurred_at')}  {rec.get('entrypoint_name') or rec.get('route')}")
            print(f"    {error.get('type', 'Error')}: {error.get('message', '')}")
            if rec.get("params"):
                print(f"    params: {rec['params']}")
            if error.get("traceback"):
                for line in str(error["traceback"]).rstrip().splitlines()[-8:]:
                    print(f"    | {line}")

    if page_errors:
        # The page's JS threw — so it may have made no calls at all. This is the
        # signal that separates "broken page" from "page nobody opened".
        print(f"\n{len(page_errors)} page error(s) — the page's own JS, not Python:")
        for rec in page_errors:
            error = rec.get("error") or {}
            where = rec.get("source") or "?"
            if rec.get("line"):
                where += f":{rec['line']}"
            print(f"  {error.get('type', 'Error')}: {error.get('message', '')}  ({where})")

    print(f"\ncursor: {page['cursor']}   (pass to --since-cursor for only what is new)")


def main() -> None:
    parser = _build_parser()

    # Preserve the historical bare invocation: `fused-render`, `fused-render --port N`,
    # etc. default to `serve`. Only inject the default when the first token is not a
    # subcommand and not the top-level -h/--help.
    argv = sys.argv[1:]
    if not argv or (argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv = ["serve", *argv]

    args = parser.parse_args(argv)
    if args.command == "calls":
        _run_calls(args)
        return
    _run_serve(args)


if __name__ == "__main__":
    main()
