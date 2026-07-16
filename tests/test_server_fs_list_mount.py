"""Tests for mount-backed GET /api/fs/list and /api/fs/walk
(fused_render/server.py).

A directory under the mounts dir must be listed via the rclone rcd rc API
(operations/list), never a kernel os.scandir: a READDIR on a flat S3 prefix
with millions of keys forces rclone's VFS to enumerate the whole directory
before the kernel gets its first entry, blows past the macOS NFS deadman, and
kills the mount (the mur-sst incident). A too-huge directory must become a
failed HTTP request, never a dead mount.

Real rclone is never invoked — the StubRcd from test_shell_mounts answers the
rc calls, and FUSED_RENDER_HOME is redirected per test.
"""
import os

import pytest
from fastapi.testclient import TestClient

import fused_render.server as server
import fused_render.shell.mounts as mounts_mod
from fused_render.server import create_app
from test_shell_mounts import StubRcd


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "mounts").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    # These tests add a mount BEFORE create_app, so create_app's startup would
    # spawn an automount daemon thread with real work to do. That thread reads
    # FUSED_RENDER_HOME lazily, so if it outlives the test it corrupts the next
    # test's home — and the endpoints under test don't need automount anyway.
    monkeypatch.setattr(mounts_mod, "startup", lambda: None)
    return h


@pytest.fixture()
def rcd(home):
    stub = StubRcd()
    mounts_mod.write_rcd_state(stub.port, 4242)
    yield stub
    stub.close()


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _entry(name, is_dir=False, size=0, mtime="2024-01-02T03:04:05Z"):
    return {"Name": name, "IsDir": is_dir,
            "Size": -1 if is_dir else size, "ModTime": mtime}


# -- fs/list -----------------------------------------------------------------


def test_list_mount_backed_routes_through_rc_not_kernel(home, rcd, tmp_path, monkeypatch):
    c = mounts_mod.add_mount("s3demo", "remote:bucket/prefix")
    sub = os.path.join(mounts_mod.mountpoint(c), "data")
    rcd.responses["operations/list"] = {"list": [
        _entry("zeta.txt", size=3),
        _entry("Alpha", is_dir=True),
        _entry("beta.parquet", size=10),
    ]}

    # The mount path must never be touched through the kernel — record every
    # scandir/stat and assert none landed under the mountpoint.
    mp = mounts_mod.mountpoint(c)
    scanned, statted = [], []
    real_scandir, real_stat = os.scandir, os.stat
    monkeypatch.setattr(os, "scandir",
                        lambda p, *a, **k: (scanned.append(os.fspath(p)),
                                            real_scandir(p, *a, **k))[1])
    monkeypatch.setattr(os, "stat",
                        lambda p, *a, **k: (statted.append(os.fspath(p)),
                                            real_stat(p, *a, **k))[1])

    data = _client(tmp_path).get("/api/fs/list", params={"path": sub}).json()

    assert not any(str(p).startswith(mp) for p in scanned)
    assert not any(str(p).startswith(mp) for p in statted)
    # Dirs group first, then case-insensitive by name — same as a local listing.
    assert [e["name"] for e in data["entries"]] == ["Alpha", "beta.parquet", "zeta.txt"]
    by = {e["name"]: e for e in data["entries"]}
    assert by["Alpha"]["is_dir"] is True and by["Alpha"]["size"] is None
    assert by["beta.parquet"]["is_dir"] is False and by["beta.parquet"]["size"] == 10
    assert isinstance(by["beta.parquet"]["mtime"], float)
    assert all(e["ignored"] is False for e in data["entries"])
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/list"]
    assert body["fs"] == "remote:bucket/prefix" and body["remote"] == "data"


def test_list_mount_root_normalizes_rel_to_empty(home, rcd, tmp_path):
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    rcd.responses["operations/list"] = {"list": []}
    resp = _client(tmp_path).get("/api/fs/list",
                                 params={"path": mounts_mod.mountpoint(c)})
    assert resp.status_code == 200
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/list"]
    assert body["remote"] == ""  # "." normalized to the fs root


def test_list_mount_file_is_not_a_directory(home, rcd, tmp_path, monkeypatch):
    # The mount is HEALTHY but operations/list errors on a file remote (stub
    # 404s the unset method) -> 400, the mount-safe stand-in for os.path.isdir.
    # (A broken mount takes the 503 branch instead — see the dead-mount test in
    # test_shell_mounts.)
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp, exist_ok=True)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    resp = _client(tmp_path).get(
        "/api/fs/list", params={"path": mp + "/f.parquet"})
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["error"]


def test_list_mount_rcd_down_returns_503_broken(home, tmp_path):
    # No live rcd -> the mount can't be trusted; surface the broken-mount 503
    # ("reconnect from the Mounts page"), never a kernel fallback.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    resp = _client(tmp_path).get(
        "/api/fs/list", params={"path": mounts_mod.mountpoint(c) + "/data"})
    assert resp.status_code == 503
    assert "reconnect" in resp.json()["error"].lower()


def test_list_mount_timeout_returns_503(home, rcd, tmp_path, monkeypatch):
    # A directory too large to enumerate hits the hard timeout -> 503, not a
    # wedged mount.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    rcd.responses["operations/list"] = {"list": []}
    rcd.delay["operations/list"] = 1.0
    monkeypatch.setattr(mounts_mod, "RC_LIST_TIMEOUT_S", 0.2)
    resp = _client(tmp_path).get(
        "/api/fs/list", params={"path": mounts_mod.mountpoint(c) + "/huge"})
    assert resp.status_code == 503
    assert "timed out" in resp.json()["error"]


# -- fs/walk -----------------------------------------------------------------


def test_walk_mount_backed_lists_each_dir_via_rc(home, rcd, tmp_path):
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    # BFS: the root is listed first, then its "sub" child — a per-call response
    # sequence (last repeats) hands each its own listing.
    rcd.responses["operations/list"] = [
        {"list": [_entry("a.txt", size=1), _entry("sub", is_dir=True)]},
        {"list": [_entry("b.txt", size=2)]},
    ]
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    assert data["truncated"] is False
    rels = {e["rel"] for e in data["entries"]}
    assert rels == {"a.txt", "sub", "sub/b.txt"}  # descended into the subdir
    by = {e["rel"]: e for e in data["entries"]}
    assert by["sub"]["is_dir"] is True and by["sub"]["size"] is None
    assert by["a.txt"]["size"] == 1


def test_walk_mount_skips_failing_subdir_and_continues(home, tmp_path, monkeypatch):
    # A subdir that times out (or otherwise fails to list) is skipped — its
    # entry is still emitted, but the walk neither descends it nor aborts.
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    root = [_entry("a.txt", size=1), _entry("big", is_dir=True),
            _entry("ok", is_dir=True)]

    def fake_list(path, timeout=None):
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail == "big":
            raise mounts_mod.RcListTimeout("too many entries")
        if tail == "ok":
            return [_entry("c.txt", size=1)]
        return root

    monkeypatch.setattr(mounts_mod, "rc_list_dir", fake_list)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    rels = {e["rel"] for e in data["entries"]}
    assert {"a.txt", "big", "ok", "ok/c.txt"} <= rels  # walk continued past "big"
    assert not any(r.startswith("big/") for r in rels)  # timed-out dir not descended
    assert data["truncated"] is False


def test_walk_mount_clamped_to_remote_cap(home, tmp_path, monkeypatch):
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    entries = [_entry(f"f{i}.txt", size=1) for i in range(10)]
    monkeypatch.setattr(mounts_mod, "rc_list_dir", lambda p, timeout=None: entries)
    monkeypatch.setattr(server, "WALK_MAX_ENTRIES_REMOTE", 3)
    data = _client(tmp_path).get("/api/fs/walk", params={"path": mp}).json()
    assert data["truncated"] is True
    assert len(data["entries"]) == 3
