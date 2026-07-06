"""Command-line entry point.

Two subcommands:
  * ``fused-render serve`` (the default when no subcommand is given, preserving the
    original ``fused-render [--start-dir DIR] [--port N]`` invocation) — the local
    127.0.0.1 file explorer.
  * ``fused-render export <page.html> --out <dir>`` — an offline build step that packs
    a renderable page into a portable bundle for hosted serving (see export.py). It
    starts no server and touches no network.
"""
import argparse
import os
import sys
import threading
import webbrowser

DEFAULT_PORT = 8765

# Subcommand names; anything else as argv[1] falls through to the implicit `serve`
# so the historical bare `fused-render --port 9000` invocation keeps working.
_SUBCOMMANDS = ("serve", "export")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fused-render", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the local file explorer (default)")
    serve.add_argument(
        "--start-dir",
        default=os.path.expanduser("~"),
        help="initial directory shown in the browser (default: home). "
        "The whole filesystem remains browsable.",
    )
    serve.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port to bind (default: {DEFAULT_PORT})"
    )
    serve.add_argument(
        "--no-browser", action="store_true", help="do not open a browser tab on startup"
    )

    export = sub.add_parser(
        "export", help="pack a renderable page into a portable bundle for hosted serving"
    )
    export.add_argument("page", help="path to the .html page to export")
    export.add_argument(
        "--out", "-o", required=True, help="output directory for the bundle (created if absent)"
    )
    return parser


def _run_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from fused_render.server import create_app

    start_dir = os.path.abspath(os.path.expanduser(args.start_dir))
    app = create_app(start_dir=start_dir)

    url = f"http://127.0.0.1:{args.port}/"
    print(f"fused-render serving at {url}")
    print(f"start dir: {start_dir}")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port)


def _run_export(args: argparse.Namespace) -> None:
    from fused_render.export import ExportError, export_page

    try:
        plan = export_page(args.page, args.out)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1) from None

    out = os.path.abspath(args.out)
    print(f"exported {os.path.basename(args.page)} -> {out}")
    print(f"  {len(plan.entrypoints)} runPython entrypoint(s), {len(plan.assets)} asset(s)")
    for e in plan.entrypoints:
        print(f"    runPython {e.path} -> route {e.name!r}")
    for a in plan.assets:
        print(f"    asset     {a.path} -> {a.name}")


def main() -> None:
    parser = _build_parser()

    # Preserve the historical bare invocation: `fused-render`, `fused-render --port N`,
    # etc. default to `serve`. Only inject the default when the first token is not a
    # subcommand and not the top-level -h/--help.
    argv = sys.argv[1:]
    if not argv or (argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv = ["serve", *argv]

    args = parser.parse_args(argv)
    if args.command == "export":
        _run_export(args)
    else:
        _run_serve(args)


if __name__ == "__main__":
    main()
