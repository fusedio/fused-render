"""Opt-in pooled proxy on /api/fs/raw (TASK F, fused_render/server.py).

For a COLD mount-backed read, /api/fs/raw 307-redirects native clients to the
store's signed URL (duckdb/fsspec re-issue GETs with their own pooled parallel
connections — proxying there paid a fresh TLS handshake per range read). But the
pyramid/geotiff workers read block-by-block with a plain urllib GET and would
re-follow that 307 on EVERY block. So those workers now tack `&pooled=1` onto the
raw URL, opting that read into a server-side proxy that streams the same signed
URL back through a shared keep-alive httpx pool (sockets reused across ranges).

These tests pin the endpoint contract:
  * `/api/fs/raw?...&pooled=1` on a cold mount path returns 200/206 with the
    bytes and NO 307 (the opt-in proxy fired);
  * WITHOUT the flag the same request still 307s to the store (no regression for
    duckdb/parquet).

A threaded localhost server stands in for the signed store URL; upstream_url_for
is stubbed to point at it, and a serve is armed so the serve-backed branch is
entered. prefetch is stubbed so the read stays "cold" (redirect-eligible) and no
background reader touches the file.

Shared fixtures/helpers (home, _mount, _no_kernel_on_mount) live in
_mount_safe_helpers, mirroring test_server_fs_raw_mount_safe.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import fused_render.shell.mounts as mounts_mod
from fused_render.server import create_app
from _mount_safe_helpers import (  # noqa: F401 — `home` is a reused fixture
    _mount,
    home,
)


class _FakeStore:
    """Stands in for the store's signed S3 URL: serves a fixed blob and honours
    Range with a 206 (200 whole-body otherwise). Records how many GETs it saw,
    so a test can confirm the proxy actually fetched from it."""

    def __init__(self, blob: bytes):
        self.blob = blob
        self.gets = 0
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                store.gets += 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    s, _, e = rng[6:].partition("-")
                    s = int(s)
                    e = int(e) if e else len(store.blob) - 1
                    chunk = store.blob[s:e + 1]
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     f"bytes {s}-{e}/{len(store.blob)}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(store.blob)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(store.blob)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/signed/object.tif"

    def close(self):
        self._srv.shutdown()


@pytest.fixture()
def cold_raw(home, monkeypatch):
    """A TestClient over a cold mount-backed file: a serve is armed (so the
    serve-backed branch is entered), prefetch is stubbed so the read stays cold
    (redirect-eligible) and touches nothing, and a _FakeStore stands in for the
    signed URL. Yields (client, mountpoint, file_path, store)."""
    import fused_render.shell.prefetch as prefetch
    monkeypatch.setattr(prefetch, "schedule", lambda *a, **k: None)
    # Cold: not yet prefetched -> the handler is in its redirect-to-store phase.
    monkeypatch.setattr(prefetch, "is_done", lambda *a, **k: False)

    store = _FakeStore(b"PYRAMID-COG-BYTES-" + bytes(range(64)) * 8)

    mp = _mount("rw", read_only=False)
    # Arm a live serve for the mount so serve_url_for(path) is non-None (its
    # base need not be reachable: the pooled/redirect branch fires before any
    # proxy to the serve).
    from fused_render.shell import storage
    storage.write_json(mounts_mod.serves_path(), {mp: "http://127.0.0.1:1"})
    # The store's signed URL for any path under the mount.
    monkeypatch.setattr(mounts_mod, "upstream_url_for", lambda p: store.url)

    file_path = os.path.join(mp, "cog.tif")
    try:
        # Context-manager form so Starlette runs the app's startup event and
        # creates app.state.pooled_client (the pool the opt-in proxy awaits
        # through); a bare TestClient(...) skips lifespan.
        with TestClient(create_app(start_dir=str(home))) as client:
            yield client, mp, file_path, store
    finally:
        store.close()


def test_pooled_flag_proxies_bytes_no_redirect(cold_raw):
    client, mp, file_path, store = cold_raw
    # Full-body GET with the opt-in flag: the proxy streams the store's bytes
    # back (200), never a 307. follow_redirects=False so a stray 307 would show.
    r = client.get("/api/fs/raw",
                   params={"path": file_path, "pooled": "1"},
                   follow_redirects=False)
    assert r.status_code == 200
    assert r.content == store.blob
    assert store.gets == 1  # the proxy fetched from the store itself


def test_pooled_flag_forwards_range_206(cold_raw):
    client, mp, file_path, store = cold_raw
    # A ranged read (what _HttpRangeFile issues per block) comes back as a 206
    # window through the pool, not a redirect.
    r = client.get("/api/fs/raw",
                   params={"path": file_path, "pooled": "1"},
                   headers={"Range": "bytes=4-11"},
                   follow_redirects=False)
    assert r.status_code == 206
    assert r.content == store.blob[4:12]
    assert r.headers["content-range"] == f"bytes 4-11/{len(store.blob)}"


def test_no_flag_still_redirects_to_store(cold_raw):
    client, mp, file_path, store = cold_raw
    # WITHOUT the flag the cold read still 307s to the signed store URL — the
    # untouched duckdb/parquet path. The proxy never runs (store sees no GET).
    r = client.get("/api/fs/raw",
                   params={"path": file_path},
                   follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == store.url
    assert store.gets == 0
