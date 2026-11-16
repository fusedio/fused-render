"""A local stand-in for an S3 bucket that speaks Range, ETag and If-Match,
plus the `bucket` fixture that points `shared/remote_range.py` at it.
Shared by tests/test_geotiff_stream.py and tests/test_duckdb_stream.py.

The range reader speaks https to the real bucket; this bucket is plain http
on loopback, so the fixture swaps `remote_range._connect` (the one function
that opens a socket) and `fused_s3.sign` (the server round trip). Everything
above the socket is the real code.
"""
import hashlib
import http.client
import importlib.util
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fused_render

_SHARED = os.path.join(os.path.dirname(fused_render.__file__), "templates", "shared")


def remote_range():
    """The shared range reader, loaded exactly as the templates load it (by
    path, under `_fused_remote_range`), so a monkeypatch here is the one the
    template code sees."""
    name = "_fused_remote_range"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_SHARED, "remote_range.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


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
                    a, b = int(a), min(int(b), len(data) - 1)
                    if a >= len(data):
                        return self._send(416, b"")
                    return self._send(206, data[a:b + 1], etag,
                                      f"bytes {a}-{b}/{len(data)}")
                return self._send(200, data, etag)

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
        # A real loopback URL, so DuckDB's httpfs can read it too (the range
        # reader's socket is swapped below and ignores the host).
        return f"http://127.0.0.1:{self.port}/{key}"


@pytest.fixture
def bucket(monkeypatch):
    b = _Bucket()

    # The buffer speaks https to the bucket; the test bucket is plain http on
    # loopback. Everything above the socket is the real code.
    RF = remote_range()
    monkeypatch.setattr(RF, "_connect", lambda url: http.client.HTTPConnection(
        "127.0.0.1", b.port, timeout=10))
    signs = []

    def sign(address):
        signs.append(address)
        return {"url": b.url(address.split("/", 3)[3]), "expires_at": None}

    monkeypatch.setattr(RF.fused_s3, "sign", sign)
    b.signs = signs
    yield b
    b.server.shutdown()


