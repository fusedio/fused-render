"""Mount-safety of /api/fs/raw (fused_render/server.py) — Task 2.

  * HEAD for a serve-backed mount path is answered through the rclone rcd
    (rc_list_dir), never a kernel os.stat: a missing-sidecar HEAD (.zmetadata,
    .ovr) is exactly the cold-negative probe that would enumerate the whole
    remote prefix and wedge the mount. Confirmed-missing/non-regular -> 404,
    indeterminate (rcd down/timeout) -> 503.
  * When serve_url_for returns None (or the proxied read fails) but the path IS
    mount-backed, the rclone serve is down/respawning — the handler returns 503
    rather than falling through to a local-file kernel read of the mount.

Shared fixtures/helpers (home, _mount, _entry, _no_kernel_on_mount,
_list_returns, _list_raises) live in _mount_safe_helpers.
"""

import os

import pytest
from _mount_safe_helpers import (  # noqa: F401 — `home` is a reused fixture
    _entry,
    _list_raises,
    _list_returns,
    _mount,
    _no_kernel_on_mount,
    home,
)
from fastapi.testclient import TestClient

import fused_render.shell.mounts as mounts_mod
from fused_render.server import create_app


@pytest.fixture()
def raw_client(home, monkeypatch):
    """A TestClient plus a factory that arms a live serve for a mountpoint and
    stubs prefetch (whose background reader would otherwise touch the file)."""
    import fused_render.shell.prefetch as prefetch

    monkeypatch.setattr(prefetch, "schedule", lambda *a, **k: None)
    monkeypatch.setattr(prefetch, "is_done", lambda *a, **k: True)
    client = TestClient(create_app(start_dir=str(home)))

    def arm_serve(mp):
        from fused_render.shell import storage

        storage.write_json(mounts_mod.serves_path(), {mp: "http://127.0.0.1:1"})

    return client, arm_serve


def test_raw_head_missing_sidecar_404_via_rc(raw_client, home, monkeypatch):
    client, arm_serve = raw_client
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    # Parent lists the store, but the .zmetadata sidecar is absent -> 404,
    # answered without a kernel os.stat (the cold-negative wedge).
    _list_returns(monkeypatch, [_entry("zarr.json", size=10)])
    r = client.head("/api/fs/raw", params={"path": os.path.join(mp, ".zmetadata")})
    assert r.status_code == 404


def test_raw_head_present_file_200_via_rc(raw_client, home, monkeypatch):
    client, arm_serve = raw_client
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("zarr.json", size=42)])
    r = client.head("/api/fs/raw", params={"path": os.path.join(mp, "zarr.json")})
    assert r.status_code == 200
    assert r.headers["content-length"] == "42"


def test_raw_head_unknown_size_reports_zero_not_negative(raw_client, home, monkeypatch):
    client, arm_serve = raw_client
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    # rclone reports Size:-1 for an object of unknown length; `-1 or 0` is -1,
    # which would emit an invalid content-length: -1. It must clamp to 0.
    _list_returns(
        monkeypatch,
        [{"Name": "blob.bin", "IsDir": False, "Size": -1, "ModTime": "2024-01-02T03:04:05Z"}],
    )
    r = client.head("/api/fs/raw", params={"path": os.path.join(mp, "blob.bin")})
    assert r.status_code == 200
    assert r.headers["content-length"] == "0"


def test_raw_head_indeterminate_is_503(raw_client, home, monkeypatch):
    client, arm_serve = raw_client
    mp = _mount("rw", read_only=False)
    arm_serve(mp)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, mounts_mod.RcListUnavailable("rcd down"))
    r = client.head("/api/fs/raw", params={"path": os.path.join(mp, ".zmetadata")})
    assert r.status_code == 503


def test_raw_serve_lost_but_mount_backed_returns_503_not_kernel_read(raw_client, home, monkeypatch):
    client, _ = raw_client
    mp = _mount("rw", read_only=False)
    # No serve armed -> serve_url_for returns None; the path IS mount-backed,
    # so the handler must 503 rather than fall through to a kernel file read.
    _no_kernel_on_mount(monkeypatch, mp)
    r = client.get("/api/fs/raw", params={"path": os.path.join(mp, "data.parquet")})
    assert r.status_code == 503
