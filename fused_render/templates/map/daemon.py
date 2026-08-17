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
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

# Frozen desktop launchers do not consistently put a directly executed data
# script's directory on sys.path. Resolve sibling template modules from this
# file explicitly so daemon startup is independent of cwd and launcher shape.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker
from raster_engine import RasterEngine
from vector_engine import VectorEngine


# 0 disables the idle exit, which is the default: every tile URL the page holds
# embeds this process's port, so exiting silently breaks every raster already on
# the map — and the page cannot tell that from a real tile error. Staying
# resident costs one idle process; exiting cost a map that never recovers.
IDLE_TIMEOUT = int(os.environ.get("MAP_VIEWER_IDLE_TIMEOUT", "0"))

# A rendered tile is expensive and the URL that names it already carries every
# input: the source fingerprint, the style revision, and this process's port. So
# the browser may keep it, and panning back over ground it has already drawn
# costs nothing. Metadata routes report live progress and stay uncacheable.
TILE_CACHE_CONTROL = "public, max-age=86400"


class MapServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server: MapServer
    # HTTP/1.0 closes the socket after every response, so a viewport of tiles
    # cost a viewport of TCP handshakes. Every response here carries an accurate
    # Content-Length, which is what keep-alive needs; the timeout reaps the
    # thread an idle connection would otherwise park forever.
    protocol_version = "HTTP/1.1"
    timeout = 65

    def log_message(self, _format, *_args):
        return

    @property
    def engine(self) -> RasterEngine:
        return self.server.engine  # type: ignore[attr-defined]

    @property
    def vectors(self) -> VectorEngine:
        return self.server.vectors  # type: ignore[attr-defined]

    def _touch(self):
        self.server.last_hit = time.time()  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        supplied = query.get("t", [""])[0] or self.headers.get("X-Map-Token", "")
        expected = self.server.token  # type: ignore[attr-defined]
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _headers(
        self, status: int, content_type: str, length: int, cache: str = "no-store"
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Map-Token")
        self.send_header("Cache-Control", cache)
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

    def _bytes(self, status: int, body: bytes, content_type: str, cache: str = "no-store"):
        self._headers(status, content_type, len(body), cache)
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
                self._bytes(200, tile, "image/png", TILE_CACHE_CONTROL)
            return

        if len(parts) == 5 and parts[0] == "vtiles" and parts[4].endswith(".pbf"):
            try:
                source_id = parts[1]
                z, x = int(parts[2]), int(parts[3])
                y = int(parts[4][:-4])
            except ValueError:
                self._json(400, {"error": "invalid tile coordinate"})
                return
            try:
                tile = self.vectors.tile(source_id, z, x, y)
            except Exception as error:
                traceback.print_exc()
                self._json(
                    500,
                    {
                        "status": "error",
                        "message": f"{type(error).__name__}: {error}",
                    },
                )
                return
            if tile is None:
                self._json(404, {"error": "unknown source"})
            elif not tile:
                self._bytes(
                    204, b"", "application/vnd.mapbox-vector-tile", TILE_CACHE_CONTROL
                )
            else:
                self._bytes(
                    200, tile, "application/vnd.mapbox-vector-tile", TILE_CACHE_CONTROL
                )
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        self._touch()
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        if parsed.path == "/shutdown":
            self._json(200, {"ok": True, "pid": os.getpid()})
            # serve_forever runs on the main thread; shutdown() blocks until it
            # returns, so it cannot be called from the thread serving this
            # request.
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        try:
            if parsed.path == "/describe":
                request = self._read_json()
                descriptor = worker.main(
                    request,
                    raster_engine=self.engine,
                    vector_engine=self.vectors,
                )
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


def _already_serving(state_path: Path, version: str) -> bool:
    """Whether a healthy service for this exact build is already registered.

    The caller serializes spawns on a lock, but it cannot serialize its own
    death: a render killed by its timeout while waiting for a cold start
    releases the lock without ever recording the daemon it began, and the next
    render starts another. Each survivor then holds a full geospatial runtime
    and competes for the same work, which is how one slow start turns into a
    machine full of daemons. Checking here ends it — whoever loses simply
    exits.
    """
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("version") != version or int(state["pid"]) == os.getpid():
            return False
        url = (
            f"http://127.0.0.1:{int(state['port'])}/ping"
            f"?t={quote(str(state['token']), safe='')}"
        )
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response).get("version") == version
    except (OSError, ValueError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    if _already_serving(Path(args.state), args.version):
        return

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
    server.vectors = VectorEngine(  # type: ignore[attr-defined]
        base_url=f"http://127.0.0.1:{port}",
        token=token,
        locator=server.engine.locator,
        cache_dir=args.cache,
    )

    state_path = Path(args.state)
    _write_state(
        state_path,
        {
            "version": args.version,
            # Which copy of the template this process is running, so that only
            # its own replacement retires it. A second checkout sharing this
            # cache directory is a different service, not a stale one.
            "home": str(HERE),
            "port": port,
            "token": token,
            "pid": os.getpid(),
            "started_at": time.time(),
        },
    )
    if IDLE_TIMEOUT > 0:
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
