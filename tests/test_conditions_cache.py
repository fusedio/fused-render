"""Tests for the /api/fs/conditions TTL cache.

Evaluating template condition.py gates over a remote mount is slow (~6.8s) and
was recomputed on every call. api_fs_conditions now caches success payloads for
a short TTL. These tests monkeypatch server._conditions_payload with a
call-counting stub so cache hits vs recomputes can be asserted directly.
"""

import pytest
from fastapi.testclient import TestClient

from fused_render import server


@pytest.fixture(autouse=True)
def _clear_conditions_cache():
    server._CONDITIONS_CACHE.clear()
    yield
    server._CONDITIONS_CACHE.clear()


@pytest.fixture
def client():
    return TestClient(server.create_app(start_dir="/"))


def test_cache_hit_within_ttl(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return {"path": path, "conditions": {}}

    monkeypatch.setattr(server, "_conditions_payload", stub)

    r1 = client.get("/api/fs/conditions", params={"path": "/some/dir"})
    r2 = client.get("/api/fs/conditions", params={"path": "/some/dir"})

    assert calls["n"] == 1
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()


def test_distinct_paths_cached_separately(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return {"path": path, "conditions": {}}

    monkeypatch.setattr(server, "_conditions_payload", stub)

    client.get("/api/fs/conditions", params={"path": "/dir/a"})
    client.get("/api/fs/conditions", params={"path": "/dir/b"})

    assert calls["n"] == 2


def test_stale_ttl_recomputes(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return {"path": path, "conditions": {}}

    monkeypatch.setattr(server, "_conditions_payload", stub)
    monkeypatch.setattr(server, "_CONDITIONS_TTL_S", 0.0)

    client.get("/api/fs/conditions", params={"path": "/some/dir"})
    client.get("/api/fs/conditions", params={"path": "/some/dir"})

    assert calls["n"] == 2


def test_errors_are_not_cached(client, monkeypatch):
    calls = {"n": 0}

    def stub(path):
        calls["n"] += 1
        return server._error("no such file", status=404)

    monkeypatch.setattr(server, "_conditions_payload", stub)

    r1 = client.get("/api/fs/conditions", params={"path": "/missing"})
    r2 = client.get("/api/fs/conditions", params={"path": "/missing"})

    assert calls["n"] == 2
    assert r1.status_code == 404
    assert r2.status_code == 404
