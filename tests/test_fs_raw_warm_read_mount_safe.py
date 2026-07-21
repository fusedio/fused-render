"""Mount-safety of the WARM-READ fallthrough on /api/fs/raw (server.py).

Regression for the fsraw-mount-stat wedge: once a mount-backed file's background
prefetch has completed (prefetch.is_done True) — or for a browser read — the
redirect/pooled block is skipped and the handler falls through to its "not
redirected, proxy the bytes" branch. That branch used to guard directories with a
raw kernel os.stat(path). On a mount-backed file whose parent prefix isn't
VFS-cached that cold GETATTR forces rclone to enumerate the whole S3 prefix (~28s
on a 44k-entry dir), blows past the macOS NFS deadman, and drops the mount.

The fix mirrors the HEAD branch: a mount-backed path resolves existence/shape
through the rclone rcd (_mount_probe), never the kernel. These tests pin that:

  * a present mount-backed file on the warm-read path proxies (200) WITHOUT any
    kernel os.stat / _stat_or_none of the mount (existence came from the rc probe);
  * a mount-backed directory still 404s (answered via the rc probe);
  * an indeterminate rc probe (rcd down/timeout) maps to 503, never "missing";
  * a local (non-mount) read is unchanged (still a kernel-backed FileResponse).

Shared fixtures/helpers live in _mount_safe_helpers.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import fused_render.server as server_mod
import fused_render.shell.mounts as mounts_mod
from fused_render.server import create_app
from _mount_safe_helpers import (  # noqa: F401 — `home` is a reused fixture
    _entry,
    _list_raises,
    _list_returns,
    _mount,
    _no_kernel_on_mount,
    home,
)


class _FakeServe:
    """Stands in for a mount's localhost rclone serve: answers any GET with a
    fixed blob so the warm-read proxy branch returns 200 without touching the
    kernel mount."""

    def __init__(self, blob: bytes):
        self.blob = blob
        serve = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(serve.blob)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(serve.blob)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self._srv.shutdown()


@pytest.fixture()
def warm_raw(home, monkeypatch):
    """A TestClient on the WARM-READ path: prefetch.is_done True (so the
    redirect/pooled block is skipped and execution reaches the proxy
    fallthrough), plus a factory that arms a reachable serve for a mountpoint and
    a spy that makes any _stat_or_none call fail loudly (proves the mount path
    resolved existence via the rc probe, never the kernel)."""
    import fused_render.shell.prefetch as prefetch
    monkeypatch.setattr(prefetch, "schedule", lambda *a, **k: None)
    # Warm: prefetch already landed -> the handler is past its redirect phase and
    # falls through to the proxy branch (the one that used to kernel-stat).
    monkeypatch.setattr(prefetch, "is_done", lambda *a, **k: True)

    serve = _FakeServe(b"WARM-MOUNT-FILE-BYTES")

    def arm_serve(mp):
        from fused_render.shell import storage
        storage.write_json(mounts_mod.serves_path(), {mp: serve.base})

    client = TestClient(create_app(start_dir=str(home)))
    try:
        yield client, arm_serve, serve
    finally:
        serve.close()


def test_warm_present_file_proxies_200_without_kernel_stat(warm_raw, monkeypatch):
    client, arm_serve, serve = warm_raw
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    # A kernel _stat_or_none of the mount is the wedge — make it fail loudly so a
    # silent regression to the old code path can't pass this test.
    def _boom_stat(path):
        raise AssertionError(f"_stat_or_none({path}) touched the mount")
    monkeypatch.setattr(server_mod, "_stat_or_none", _boom_stat)
    # Parent lists the store; the file is present and a regular object.
    _list_returns(monkeypatch, [_entry("cog.tif", size=len(serve.blob))])

    r = client.get("/api/fs/raw",
                   params={"path": os.path.join(mp, "cog.tif")},
                   follow_redirects=False)
    assert r.status_code == 200
    assert r.content == serve.blob


def test_warm_directory_still_404s_via_rc(warm_raw, monkeypatch):
    client, arm_serve, serve = warm_raw
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    # A directory proxied through the serve would come back as a 200 HTML
    # listing; the rc probe reports IsDir -> 404, answered without a kernel stat.
    _list_returns(monkeypatch, [_entry("subdir", is_dir=True)])
    r = client.get("/api/fs/raw",
                   params={"path": os.path.join(mp, "subdir")},
                   follow_redirects=False)
    assert r.status_code == 404


def test_warm_missing_file_404s_via_rc(warm_raw, monkeypatch):
    client, arm_serve, serve = warm_raw
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    # Parent listable, child absent -> 404 via the rc probe, no kernel stat.
    _list_returns(monkeypatch, [_entry("other.tif", size=1)])
    r = client.get("/api/fs/raw",
                   params={"path": os.path.join(mp, "cog.tif")},
                   follow_redirects=False)
    assert r.status_code == 404


def test_warm_indeterminate_probe_is_503(warm_raw, monkeypatch):
    client, arm_serve, serve = warm_raw
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    # rcd down/timeout -> existence is INDETERMINATE; the handler must 503, never
    # fall back to a kernel read or report "missing".
    _list_raises(monkeypatch, mounts_mod.RcListUnavailable("rcd down"))
    r = client.get("/api/fs/raw",
                   params={"path": os.path.join(mp, "cog.tif")},
                   follow_redirects=False)
    assert r.status_code == 503


def test_warm_local_path_unchanged(warm_raw, home, monkeypatch):
    client, _arm, _serve = warm_raw
    # A local (non-mount) file has no serve, so serve_url_for is None and the
    # handler serves it with an ordinary kernel-backed FileResponse — unchanged
    # by the mount guard.
    local = home / "local.txt"
    local.write_text("hello-local")
    r = client.get("/api/fs/raw", params={"path": str(local)})
    assert r.status_code == 200
    assert r.content == b"hello-local"
