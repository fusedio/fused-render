"""Minimal fixture daemon for the background-apps engine_host contract.

The contract reference for the authoring skill: stdlib-only, argv
`--status/--cache/--version` (cribbed from `fused_render/engine_worker.py`),
binds :0, publishes `{port, token}` to the status file, answers `/ping` with
its version, and honors `/quit` for a graceful stop. Real background apps are
free to serve whatever else they like on top of this.

Also serves POST `/count` — a side-effecting endpoint (an in-memory counter)
that is what `fused.daemon.call()` actually exercises: the runtime hardcodes
POST, so a fixture with only `do_GET` (the original shape here) would answer
every real `fused.daemon.call()` with a 501 and never actually get exercised by
the JS API it is the reference implementation for. `/count`'s count is also
what a retry-safety test reads: a re-sent POST (the exact failure a heal
retrying an at-most-once call would cause) shows up as the counter advancing
by more than the number of calls a test actually made.
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


#: Calls served by POST /count, process-wide (this fixture is single-process
#: per spawn, so a plain module global plus a lock is enough — no need for the
#: real thing's persistence).
_count = 0
_count_lock = threading.Lock()


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
        if path == "/health":
            # A page resource distinct from /ping — the engine_host proxy
            # (routers/engines.py) refuses "ping" as private, so this is what
            # an end-to-end smoke through /api/engines/<id>/proxy/health hits.
            self._json(200, {"ok": True, "pid": os.getpid()})
            return
        if path == "/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        if path == "/count":
            # The one side-effecting endpoint: fused.daemon.call("/count", body)
            # hits this. Echoes the posted body back alongside the new count
            # so a test can also confirm a POST body actually arrived intact.
            global _count
            with _count_lock:
                _count += 1
                count = _count
            try:
                echo = json.loads(raw) if raw else None
            except ValueError:
                echo = None
            self._json(200, {"ok": True, "count": count, "echo": echo})
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
