"""Command-line entry point: `fused-render [--start-dir DIR] [--port N] [--no-browser]`."""
import argparse
import os
import threading
import webbrowser

import uvicorn

from fused_render.logs import setup_logging
from fused_render.server import create_app

DEFAULT_PORT = 8765


def main() -> None:
    parser = argparse.ArgumentParser(prog="fused-render", description=__doc__)
    parser.add_argument(
        "--start-dir",
        default=os.path.expanduser("~"),
        help="initial directory shown in the browser (default: home). "
        "The whole filesystem remains browsable.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab on startup")
    args = parser.parse_args()

    log_file = setup_logging()
    start_dir = os.path.abspath(os.path.expanduser(args.start_dir))
    app = create_app(start_dir=start_dir)

    url = f"http://127.0.0.1:{args.port}/"
    print(f"fused-render serving at {url}")
    print(f"start dir: {start_dir}")
    print(f"log file: {log_file}")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
