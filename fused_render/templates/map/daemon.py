"""Cross-platform Map Viewer service.

One loopback HTTP process owns the warm geospatial runtime, raster source
registry, XYZ tile cache, and background COG jobs.  The service is intentionally
independent of DuckDB: a missing vector extension can never prevent a raster
from opening.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import worker
from raster_engine import RasterEngine


IDLE_TIMEOUT = int(os.environ.get("MAP_VIEWER_IDLE_TIMEOUT", "1800"))


class MapServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server: MapServer

    def log_message(self, _format, *_args):
        return

    @property
    def engine(self) -> RasterEngine:
        return self.server.engine  # type: ignore[attr-defined]

    def _touch(self):
        self.server.last_hit = time.time()  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        supplied = query.get("t", [""])[0] or self.headers.get("X-Map-Token", "")
        expected = self.server.token  # type: ignore[attr-defined]
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _headers(self, status: int, content_type: str, length: int):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Map-Token")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _upstream(self, parsed):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 4 or parts[0] != "upstream":
            return False
        result = self.engine.upstream(
            token=parts[1],
            source_key=parts[2],
            filename=unquote(parts[3]),
            method=self.command,
            range_header=self.headers.get("Range"),
        )
        if result is None:
            self._json(404, {"error": "unknown upstream source"})
            return True
        status, headers, body = result
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
        return True

    def _bytes(self, status: int, body: bytes, content_type: str):
        self._headers(status, content_type, len(body))
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

    def _json(self, status: int, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self._bytes(status, body, "application/json; charset=utf-8")

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 2 << 20:
            raise ValueError("request body is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Map-Token")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._touch()
        parsed = urlparse(self.path)
        if parsed.path.startswith("/upstream/") and self._upstream(parsed):
            return
        if parsed.path == "/ping":
            if not self._authorized():
                self._json(403, {"error": "forbidden"})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "version": self.server.version,  # type: ignore[attr-defined]
                    "pid": os.getpid(),
                },
            )
            return

        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "jobs":
            result = self.engine.job(parts[1])
            self._json(200 if result else 404, result or {"error": "unknown source"})
            return

        if len(parts) == 5 and parts[0] == "tiles" and parts[4].endswith(".png"):
            try:
                source_id = parts[1]
                z, x = int(parts[2]), int(parts[3])
                y = int(parts[4][:-4])
            except ValueError:
                self._json(400, {"error": "invalid tile coordinate"})
                return
            tile = self.engine.tile(source_id, z, x, y)
            if tile is None:
                self._json(404, {"error": "unknown source"})
            else:
                self._bytes(200, tile, "image/png")
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        self._touch()
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        try:
            if parsed.path == "/describe":
                request = self._read_json()
                descriptor = self.engine.try_describe(request)
                if descriptor is None:
                    descriptor = worker.main(request, raster_engine=self.engine)
                self._json(200, descriptor)
                return

            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0] == "optimize":
                result = self.engine.start_optimization(parts[1])
                self._json(
                    202 if result else 404,
                    result or {"error": "unknown source"},
                )
                return
        except Exception as error:
            self._json(
                500,
                {
                    "status": "error",
                    "message": f"{type(error).__name__}: {error}",
                    "error": {"traceback": traceback.format_exc()},
                },
            )
            return
        self._json(404, {"error": "not found"})


def _write_state(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def _idle_monitor(server: MapServer):
    while True:
        time.sleep(min(30, max(1, IDLE_TIMEOUT // 4)))
        if time.time() - server.last_hit >= IDLE_TIMEOUT:  # type: ignore[attr-defined]
            server.shutdown()
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)
    server = MapServer(("127.0.0.1", 0), Handler)
    server.token = token  # type: ignore[attr-defined]
    server.version = args.version  # type: ignore[attr-defined]
    server.last_hit = time.time()  # type: ignore[attr-defined]
    port = int(server.server_address[1])
    server.engine = RasterEngine(  # type: ignore[attr-defined]
        cache_dir=args.cache,
        base_url=f"http://127.0.0.1:{port}",
        token=token,
    )

    state_path = Path(args.state)
    _write_state(
        state_path,
        {
            "version": args.version,
            "port": port,
            "token": token,
            "pid": os.getpid(),
            "started_at": time.time(),
        },
    )
    threading.Thread(target=_idle_monitor, args=(server,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid():
                state_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
