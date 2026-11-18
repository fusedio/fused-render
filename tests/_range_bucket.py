"""A local stand-in for an S3 bucket that speaks Range, ETag and If-Match,
plus the `bucket` fixture that serves it on loopback over plain http (which
DuckDB's httpfs and GDAL's /vsicurl/ read like any URL). Used by
tests/test_duckdb_stream.py and tests/test_geotiff_stream.py.
"""
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Bucket:
    """Objects served with Range / ETag / If-Match like S3, and a log."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.log: list = []
        self.deny_next = 0       # answer this many requests with 403
        self.ignore_range = False
        bucket = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                data = bucket.objects.get(self.path.split("?")[0])
                rng = self.headers.get("Range")
                bucket.log.append((self.path, rng, self.headers.get("If-Match")))
                if bucket.deny_next:
                    bucket.deny_next -= 1
                    return self._send(403, b"<Error>AccessDenied</Error>")
                if data is None:
                    return self._send(404, b"")
                etag = hashlib.md5(data).hexdigest()
                im = self.headers.get("If-Match")
                if im and im.strip('"') != etag:
                    return self._send(412, b"")
                if rng and not bucket.ignore_range:
                    a, _, b = rng[6:].partition("-")
                    a, b = ((int(a), min(int(b), len(data) - 1)) if a
                            else (max(0, len(data) - int(b)), len(data) - 1))
                    if a >= len(data):
                        return self._send(416, b"")
                    return self._send(206, data[a:b + 1], etag,
                                      f"bytes {a}-{b}/{len(data)}")
                return self._send(200, data, etag)

            def do_HEAD(self):
                data = bucket.objects.get(self.path.split("?")[0])
                bucket.log.append((self.path, "HEAD", None))
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", f'"{hashlib.md5(data).hexdigest()}"')
                self.end_headers()

            def _send(self, status, body, etag=None, cr=None):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if etag:
                    self.send_header("ETag", f'"{etag}"')
                if cr:
                    self.send_header("Content-Range", cr)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, key):
        return f"http://127.0.0.1:{self.port}/{key}"


@pytest.fixture
def bucket():
    b = _Bucket()
    yield b
    b.server.shutdown()
