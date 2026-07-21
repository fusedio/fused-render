"""Tests for the /api/fs/stat TTL cache.

Stat'ing a path on a remote mount goes _fs_stat -> _mount_safe_stat ->
_mount_probe -> rc_list_dir(parent) — a full cold LIST of the parent prefix over
rclone/S3 (~1.6s) just to describe one child. Re-navigating to a sibling repaid
it every time. api_fs_stat now caches success payloads for MOUNT-backed paths
for a short TTL. These tests monkeypatch server._fs_stat with a call-counting
stub (and force is_mount_backed True) so cache hits vs recomputes can be
asserted directly, mirroring tests/test_conditions_cache.py.
"""

from fastapi.testclient import TestClient

import pytest

from fused_render import server
from fused_render.shell import mounts


@pytest.fixture(autouse=True)
def _clear_stat_cache():
    server._STAT_CACHE.clear()
    yield
    server._STAT_CACHE.clear()


@pytest.fixture(autouse=True)
def _force_mount_backed(monkeypatch):
    # Only mount-backed paths are cached; make the test paths look mount-backed
    # so the cache engages (api_fs_stat imports is_mount_backed from this module
    # at call time, so patching the module attribute is picked up).
    monkeypatch.setattr(mounts, "is_mount_backed", lambda path: True)


@pytest.fixture
def client():
    return TestClient(server.create_app(start_dir="/"))


def test_cache_hit_within_ttl(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return {"path": path, "is_dir": True, "mtime": 1.0}

    monkeypatch.setattr(server, "_fs_stat", stub)

    r1 = client.get("/api/fs/stat", params={"path": "/mnt/some/dir"})
    r2 = client.get("/api/fs/stat", params={"path": "/mnt/some/dir"})

    assert calls["n"] == 1
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()


def test_distinct_paths_cached_separately(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return {"path": path, "is_dir": True, "mtime": 1.0}

    monkeypatch.setattr(server, "_fs_stat", stub)

    client.get("/api/fs/stat", params={"path": "/mnt/dir/a"})
    client.get("/api/fs/stat", params={"path": "/mnt/dir/b"})

    assert calls["n"] == 2


def test_stale_ttl_recomputes(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return {"path": path, "is_dir": True, "mtime": 1.0}

    monkeypatch.setattr(server, "_fs_stat", stub)
    monkeypatch.setattr(server, "_STAT_TTL_S", 0.0)

    client.get("/api/fs/stat", params={"path": "/mnt/some/dir"})
    client.get("/api/fs/stat", params={"path": "/mnt/some/dir"})

    assert calls["n"] == 2


def test_errors_are_not_cached(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return server._error("no such file", status=404)

    monkeypatch.setattr(server, "_fs_stat", stub)

    r1 = client.get("/api/fs/stat", params={"path": "/mnt/missing"})
    r2 = client.get("/api/fs/stat", params={"path": "/mnt/missing"})

    assert calls["n"] == 2
    assert r1.status_code == 404
    assert r2.status_code == 404


def test_non_mount_paths_not_cached(client, monkeypatch):
    # Local paths are cheap kernel stats and can be mutated out-of-band, so they
    # are deliberately never cached — recompute every call.
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return {"path": path, "is_dir": True, "mtime": 1.0}

    monkeypatch.setattr(server, "_fs_stat", stub)
    monkeypatch.setattr(mounts, "is_mount_backed", lambda path: False)

    client.get("/api/fs/stat", params={"path": "/local/dir"})
    client.get("/api/fs/stat", params={"path": "/local/dir"})

    assert calls["n"] == 2
