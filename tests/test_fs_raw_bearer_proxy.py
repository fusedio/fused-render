"""Private-GCS bearer proxy on /api/fs/raw (Task 4, fused_render/server.py).

A token-only credentialed GCS remote can't hand the client a signed link — the
bearer token must never appear in a URL, log, or response header. So for a COLD
mount-backed read, when upstream_url_for returns None but bearer_upstream_for
returns (url, {Authorization: Bearer ...}), the handler proxies the store's
bytes through the shared keep-alive httpx pool with the Authorization header
attached OUT-OF-BAND — regardless of the &pooled flag, since there is nothing to
307 to.

These tests pin the contract:
  * the outbound request to the store carries the Authorization header;
  * the response is 200/206 with the bytes and NO 307;
  * the token never appears in any response header (it rides the outbound
    request only).

A threaded localhost server stands in for the private store; it records the
Authorization header it saw. upstream_url_for is stubbed to None and
bearer_upstream_for to point at it. prefetch is stubbed so the read stays cold.

Shared fixtures/helpers (home, _mount) live in _mount_safe_helpers, mirroring
test_fs_raw_pooled_proxy.
"""
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

_TOKEN = "ya29.SECRET-BEARER-TOKEN"


class _FakeStore:
    """Stands in for the private GCS object URL: serves a fixed blob, honours
    Range with a 206, and records the Authorization header of each request so a
    test can confirm the proxy attached it out-of-band."""

    def __init__(self, blob: bytes):
        self.blob = blob
        self.auth_seen = []
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                store.auth_seen.append(self.headers.get("Authorization"))
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
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/private/object.parquet"

    def close(self):
        self._srv.shutdown()


@pytest.fixture()
def cold_bearer(home, monkeypatch):
    """A TestClient over a cold mount-backed file whose remote is token-only
    private GCS: a serve is armed, prefetch is stubbed cold, upstream_url_for
    returns None (no 307-able URL) and bearer_upstream_for points at a
    _FakeStore with an Authorization header. Yields (client, file_path, store)."""
    import fused_render.shell.prefetch as prefetch
    monkeypatch.setattr(prefetch, "schedule", lambda *a, **k: None)
    monkeypatch.setattr(prefetch, "is_done", lambda *a, **k: False)

    store = _FakeStore(b"GCS-PRIVATE-BYTES-" + bytes(range(64)) * 4)

    mp = _mount("gcp", read_only=False)
    from fused_render.shell import storage
    storage.write_json(mounts_mod.serves_path(), {mp: "http://127.0.0.1:1"})
    monkeypatch.setattr(mounts_mod, "upstream_url_for", lambda p: None)
    monkeypatch.setattr(
        mounts_mod, "bearer_upstream_for",
        lambda p: (store.url, {"Authorization": f"Bearer {_TOKEN}"}))

    file_path = os.path.join(mp, "data.parquet")
    try:
        with TestClient(create_app(start_dir=str(home))) as client:
            yield client, file_path, store
    finally:
        store.close()


def test_bearer_read_proxies_bytes_with_auth_header(cold_bearer):
    client, file_path, store = cold_bearer
    r = client.get("/api/fs/raw", params={"path": file_path},
                   follow_redirects=False)
    assert r.status_code == 200  # proxied, never a 307
    assert r.content == store.blob
    # The Authorization header rode the OUTBOUND request to the store.
    assert store.auth_seen == [f"Bearer {_TOKEN}"]


def test_bearer_read_forwards_range_206(cold_bearer):
    client, file_path, store = cold_bearer
    r = client.get("/api/fs/raw", params={"path": file_path},
                   headers={"Range": "bytes=4-11"}, follow_redirects=False)
    assert r.status_code == 206
    assert r.content == store.blob[4:12]
    assert store.auth_seen[-1] == f"Bearer {_TOKEN}"


def test_bearer_token_never_in_response(cold_bearer):
    client, file_path, store = cold_bearer
    r = client.get("/api/fs/raw", params={"path": file_path},
                   follow_redirects=False)
    assert r.status_code == 200
    assert "location" not in r.headers  # no redirect carrying anything
    # The token must never be echoed back to the client in any header.
    for name, value in r.headers.items():
        assert _TOKEN not in value
        assert name.lower() != "authorization"
