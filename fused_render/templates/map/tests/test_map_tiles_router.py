"""Map tiles ride the stable :1777 origin (fused_render/server/routers/map_tiles.py).

The assertion the old architecture could not pass: kill the tile daemon
mid-session and the SAME tile URL still answers, because the server restarts
the child and replays its described sources underneath the request.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_map_tiles_router.py -o addopts=""
"""
import time

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(tmp_path, monkeypatch, map_engine):
    from fused_render.server.app import create_app
    from fused_render.shell import mounts

    # create_app's body starts mount threads that reach for a real rclone;
    # neutralized exactly as the root tests/conftest.py does.
    monkeypatch.setattr(mounts, "startup", lambda: None)
    monkeypatch.setattr(mounts, "start_health_monitor", lambda: None)
    app = create_app(str(tmp_path))
    # No context manager: the startup events (schedule loop, AI reaper, index
    # scan) must not run; map_engine teardown is the map_engine fixture's.
    return fastapi_testclient.TestClient(app)


def _ensure(client, stub_python, stub_daemon, tmp_path, version="v1"):
    response = client.post("/api/map/ensure", json={
        "python": stub_python,
        "daemon": stub_daemon,
        "cache": str(tmp_path / "cache"),
        "version": version,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text
    return response.json()


def _describe(client, target="scene.tif"):
    response = client.post("/api/map/describe", json={"target": target},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text
    return response.json()


def _tile_url(descriptor):
    return descriptor["data"]["tile_url"].replace("{z}", "0").replace("{x}", "0").replace("{y}", "0")


def test_ensure_requires_the_x_fused_header(client, stub_python, stub_daemon, tmp_path):
    response = client.post("/api/map/ensure", json={
        "python": stub_python, "daemon": stub_daemon,
        "cache": str(tmp_path), "version": "v1",
    })
    assert response.status_code == 403


def test_describe_rewrites_the_live_urls_to_the_server_origin(
    client, stub_python, stub_daemon, tmp_path,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    data = descriptor["data"]
    assert data["tile_url"].startswith("/api/map/tiles/")
    assert data["job_url"].startswith("/api/map/jobs/")
    assert data["optimize_url"].startswith("/api/map/optimize/")
    for key in ("tile_url", "job_url", "optimize_url"):
        assert "127.0.0.1" not in data[key]
        assert "t=" not in data[key], "the child token leaked to the page"


def test_a_tile_returns_png_bytes_through_the_server(
    client, stub_python, stub_daemon, tmp_path,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    response = client.get(_tile_url(descriptor))
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")
    assert response.headers["content-type"] == "image/png"


def test_jobs_and_optimize_ride_the_same_origin(
    client, stub_python, stub_daemon, tmp_path,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    assert client.get(descriptor["data"]["job_url"]).status_code == 200
    response = client.post(descriptor["data"]["optimize_url"],
                           headers={"X-Fused": "1"})
    assert response.status_code == 200


def test_killing_the_child_heals_the_same_tile_url(
    client, stub_python, stub_daemon, tmp_path, map_engine,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    url = _tile_url(descriptor)
    assert client.get(url).status_code == 200

    child = map_engine.current()
    child.proc.kill()
    child.proc.wait(timeout=10)

    healed = client.get(url)
    assert healed.status_code == 200, healed.text
    assert healed.content.startswith(b"\x89PNG")
    assert map_engine.current().pid != child.pid


def test_a_wedged_child_is_restarted_by_the_retry(
    client, stub_python, stub_daemon, tmp_path, map_engine, monkeypatch, child_post,
):
    from fused_render.server.routers import map_tiles

    monkeypatch.setattr(map_tiles, "PROXY_TIMEOUT_S", 3.0)
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    url = _tile_url(descriptor)
    assert client.get(url).status_code == 200

    child = map_engine.current()
    child_post(child, "/wedge")

    started = time.monotonic()
    healed = client.get(url)
    assert healed.status_code == 200, healed.text
    assert map_engine.current().pid != child.pid
    assert time.monotonic() - started < 60


def test_a_tile_without_a_running_engine_says_so(client):
    response = client.get("/api/map/tiles/nope/0/0/0.png")
    assert response.status_code == 409
