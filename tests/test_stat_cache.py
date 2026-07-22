"""Tests for the /api/fs/stat TTL cache.

Stat'ing a path on a remote mount goes _fs_stat -> _mount_safe_stat ->
_mount_probe -> rc_list_dir(parent) — a full cold LIST of the parent prefix over
rclone/S3 (~1.6s) just to describe one child. Re-navigating to a sibling repaid
it every time. api_fs_stat now caches success payloads for MOUNT-backed paths
for a short TTL. These tests monkeypatch server._fs_stat with a call-counting
stub (and force is_mount_backed True) so cache hits vs recomputes can be
asserted directly, mirroring tests/test_conditions_cache.py.
"""

import pytest
from fastapi.testclient import TestClient

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


def test_write_invalidates_cache(client, monkeypatch):
    path = "/mnt/proj/main.py"
    calls = {"n": 0}

    def stat_stub(p):
        calls["n"] += 1
        return {"path": p, "is_dir": False, "mtime": float(calls["n"])}

    monkeypatch.setattr(server, "_fs_stat", stat_stub)

    # Prime the cache.
    client.get("/api/fs/stat", params={"path": path})
    assert calls["n"] == 1
    assert path in server._STAT_CACHE

    # A write must drop the entry so the editor's post-write stat re-reads the
    # fresh mtime instead of the stale cached one. Monkeypatching _fs_write
    # bypasses the auth guard and the actual filesystem mutation.
    monkeypatch.setattr(
        server, "_fs_write", lambda body, x_fused: {"path": body["path"], "mtime": 99.0}
    )
    r = client.post("/api/fs/write", json={"path": path, "content": "x"})
    assert r.status_code == 200
    assert path not in server._STAT_CACHE

    # Subsequent stat recomputes.
    client.get("/api/fs/stat", params={"path": path})
    assert calls["n"] == 2


def test_midflight_invalidation_is_not_refilled(client, monkeypatch):
    # TOCTOU race guard: a slow _fs_stat (cold mount LIST, GIL released during
    # I/O) can start BEFORE a concurrent mutation, then finish AFTER that
    # mutation's _invalidate_stat_cache popped the key. Without a generation
    # guard, api_fs_stat would unconditionally write its pre-mutation payload
    # back, undoing the invalidation and serving stale metadata to the editor's
    # post-write optimistic-lock re-stat. We reproduce it deterministically with
    # no real threads: the stub invalidates DURING its own call (as a mutation
    # completing mid-flight would), then returns a stale payload.
    path = "/mnt/proj/main.py"
    calls = {"n": 0}

    def racing_stub(p):
        calls["n"] += 1
        # A mutation lands and invalidates while this stat is "in flight".
        server._invalidate_stat_cache(p)
        return {"path": p, "is_dir": False, "mtime": float(calls["n"])}

    monkeypatch.setattr(server, "_fs_stat", racing_stub)

    r = client.get("/api/fs/stat", params={"path": path})
    assert r.status_code == 200
    # The raced result must NOT be cached — the invalidation wins.
    assert path not in server._STAT_CACHE
    # And a subsequent stat recomputes rather than serving a stale hit.
    client.get("/api/fs/stat", params={"path": path})
    assert calls["n"] == 2


def test_rename_invalidates_src_and_dst(client, monkeypatch):
    src = "/mnt/proj/old.py"
    dst = "/mnt/proj/new.py"

    def stat_stub(p):
        return {"path": p, "is_dir": False, "mtime": 1.0}

    monkeypatch.setattr(server, "_fs_stat", stat_stub)

    # Prime both.
    client.get("/api/fs/stat", params={"path": src})
    client.get("/api/fs/stat", params={"path": dst})
    assert src in server._STAT_CACHE
    assert dst in server._STAT_CACHE

    monkeypatch.setattr(
        server, "_fs_rename", lambda body, x_fused: {"path": body["dst"], "mtime": 5.0}
    )
    r = client.post("/api/fs/rename", json={"src": src, "dst": dst})
    assert r.status_code == 200
    assert src not in server._STAT_CACHE
    assert dst not in server._STAT_CACHE
