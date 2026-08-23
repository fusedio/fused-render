"""Cross-platform Map Viewer service.

One loopback HTTP process owns the warm geospatial runtime, raster source
registry, XYZ tile cache, and background COG jobs.  The service is intentionally
independent of DuckDB: a missing vector extension can never prevent a raster
from opening.

The fused-render server owns this process as a managed engine
(fused_render/server/engine_host.py): it hands over a --status path to publish
{port, token, pid, version} to, proxies every request through its own origin,
health-checks, restarts, and kills it at app shutdown. Nothing here manages its
own lifecycle any more.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import select
import socket
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as PoolTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# Frozen desktop launchers do not consistently put a directly executed data
# script's directory on sys.path. Resolve sibling template modules from this
# file explicitly so daemon startup is independent of cwd and launcher shape.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker
from multidim_engine import MultidimEngine
from raster_engine import RasterEngine
from vector_engine import VectorEngine


# A rendered tile is expensive and the URL that names it already carries every
# input: the source fingerprint, the style revision, and this process's port. So
# the browser may keep it, and panning back over ground it has already drawn
# costs nothing. Metadata routes report live progress and stay uncacheable.
TILE_CACHE_CONTROL = "public, max-age=86400"

# Every describe and tile render runs on these PERSISTENT threads, never on the
# per-connection handler threads. Two reasons, and the second is the fatal one:
# a viewport burst renders at CPU width instead of one thread per tile, and a
# thread that has done GDAL /vsicurl work DEADLOCKS THE WHOLE PROCESS when it
# exits (Windows: its DLL thread-detach ends up holding the loader lock against
# the GIL, so the next Thread.start() — every new connection — blocks forever).
# That exit-time wedge is "listening but not answering": reproduced with a
# 12-line script — open a remote COG in a thread, join it, start another thread.
# Handler threads exit per connection, so they must never touch the geo stack.
RENDER_POOL = ThreadPoolExecutor(
    max_workers=os.cpu_count() or 4, thread_name_prefix="render"
)
# Describe runs on its OWN persistent pool, not RENDER_POOL: a cold remote
# describe holds its worker for its whole multi-minute open, and sharing the
# tile pool let two of them starve tile rendering for an already-loaded layer.
DESCRIBE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="describe")
# Vector (MVT) tiles run ONE AT A TIME. Each tile is a spatial query over the
# GeoPackage's own SQLite/RTree; a viewport's worth in parallel just contends
# for the same file, and on a multi-GB GeoPackage that inflated each tile from
# ~1s to 3-10s (measured: six dense tiles took 7.4s one-at-a-time vs ~10s
# concurrent). Serialising keeps each query fast and the paint progressive.
# Persistent, like the others, so its worker never exits mid-render.
VTILE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vtile")


class _ClientGone(Exception):
    """The requester hung up before its tile finished."""


class MapServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # The stdlib backlog default is 5; a viewport of keep-alive connections
    # overflowed it, and refused connections read as "listening but not
    # answering" — /ping included.
    request_queue_size = 128


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

    @property
    def multidim(self) -> MultidimEngine:
        return self.server.multidim  # type: ignore[attr-defined]

    def _render_tile(self, source_id: str, z: int, x: int, y: int):
        tile = self.engine.tile(source_id, z, x, y)
        if tile is None:
            tile = self.multidim.tile(source_id, z, x, y)
        return tile

    def _client_gone(self) -> bool:
        """A tile request whose browser panned away shows up as a closed
        socket: readable with an empty peek."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _await_tile(self, future, cancel: threading.Event | None = None):
        """Poll instead of block. When the client hangs up mid-render, the
        pending work is cancelled (dequeued if not started, interrupted via
        `cancel` if it is) so an abandoned viewport cannot head-of-line-block
        the tile pools for other requests — the fix for both stale work on
        pan/zoom and one heavy layer starving every other tab."""
        while True:
            try:
                return future.result(timeout=0.25)
            except PoolTimeout:
                if not self._client_gone():
                    continue
                if cancel is not None:
                    cancel.set()
                future.cancel()
                raise _ClientGone()

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

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
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
            result = self.engine.job(parts[1]) or self.vectors.job(parts[1])
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
            try:
                tile = self._await_tile(
                    RENDER_POOL.submit(self._render_tile, source_id, z, x, y)
                )
            except _ClientGone:
                self.close_connection = True
                return
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
            cancel = threading.Event()
            try:
                tile = self._await_tile(
                    VTILE_POOL.submit(
                        self.vectors.tile, source_id, z, x, y, cancel
                    ),
                    cancel,
                )
            except _ClientGone:
                self.close_connection = True
                return
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
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return

        try:
            if parsed.path == "/describe":
                request = self._read_json()
                descriptor = DESCRIBE_POOL.submit(
                    worker.main,
                    request,
                    raster_engine=self.engine,
                    vector_engine=self.vectors,
                    multidim_engine=self.multidim,
                ).result()
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


def _write_status(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def _prewarm_geo_stack() -> None:
    """Import the geo stack on a persistent pool thread at startup so the first
    describe does not pay the ~2s cold import while the user waits. Handler
    threads must never touch the geo stack (they exit per connection and wedge
    the interpreter on exit), but a RENDER_POOL worker is persistent, like the
    threads that render every tile."""
    try:
        import pyogrio  # noqa: F401
        import pyproj  # noqa: F401
        import rasterio  # noqa: F401
        import shapely  # noqa: F401
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)
    server = MapServer(("127.0.0.1", 0), Handler)
    server.token = token  # type: ignore[attr-defined]
    server.version = args.version  # type: ignore[attr-defined]
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
    server.multidim = MultidimEngine(  # type: ignore[attr-defined]
        base_url=f"http://127.0.0.1:{port}",
        token=token,
    )

    _write_status(
        Path(args.status),
        {
            "version": args.version,
            "port": port,
            "token": token,
            "pid": os.getpid(),
        },
    )
    RENDER_POOL.submit(_prewarm_geo_stack)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
