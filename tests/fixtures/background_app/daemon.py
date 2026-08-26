"""Minimal fixture daemon for the background-apps engine_host contract.

The contract reference for the authoring skill: stdlib-only, argv
`--status/--cache/--version` (cribbed from `fused_render/engine_worker.py`),
binds :0, publishes `{port, token}` to the status file, answers `/ping` with
its version, and honors `/quit` for a graceful stop. Real background apps are
free to serve whatever else they like on top of this.
"""
import argparse
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format, *args):  # noqa: A002 — overrides BaseHTTPRequestHandler's own param name
        return

    def _authorized(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        supplied = query.get("t", [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.server.token)  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        if path == "/ping":
            self._json(200, {"ok": True,
                             "version": self.server.version,  # type: ignore[attr-defined]
                             "pid": os.getpid()})
            return
        if path == "/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._json(404, {"error": "not found"})


def _write_status(path: str, payload: dict) -> None:
    """Atomic publish: temp file + os.replace, matching engine_worker.py, so
    the parent never reads a half-written status file while polling."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.token = token  # type: ignore[attr-defined]
    server.version = args.version  # type: ignore[attr-defined]
    port = int(server.server_address[1])

    _write_status(args.status, {"port": port, "token": token, "pid": os.getpid()})
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
