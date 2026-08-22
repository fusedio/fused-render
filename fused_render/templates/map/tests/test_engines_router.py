"""Managed engines ride the stable :1777 origin (fused_render/server/routers/engines.py).

The assertion the old architecture could not pass: kill the daemon mid-session
and the SAME proxied URL still answers, because the server restarts the child
and replays its reinit requests underneath the request. Driven through the map
engine id, the router's first user; the descriptor URL rewrite now lives in the
template (map_render), so it is not exercised here.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_engines_router.py -o addopts=""
"""
import time
from urllib.parse import urlsplit

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

ENGINE = "map"
ENSURE = f"/api/engines/{ENGINE}/ensure"
REINIT = f"/api/engines/{ENGINE}/reinit"
DESCRIBE = f"/api/engines/{ENGINE}/proxy/describe"
PROXY = f"/api/engines/{ENGINE}/proxy"


@pytest.fixture
def client(tmp_path, monkeypatch, engine_host):
    from fused_render.server.app import create_app
    from fused_render.shell import mounts

    # create_app's body starts mount threads that reach for a real rclone;
    # neutralized exactly as the root tests/conftest.py does.
    monkeypatch.setattr(mounts, "startup", lambda: None)
    monkeypatch.setattr(mounts, "start_health_monitor", lambda: None)
    app = create_app(str(tmp_path))
    # No context manager: the startup events (schedule loop, AI reaper, index
    # scan) must not run; child teardown is the engine_host fixture's.
    return fastapi_testclient.TestClient(app)


def _ensure(client, stub_python, stub_daemon, tmp_path, version="v1"):
    response = client.post(ENSURE, json={
        "python": stub_python,
        "daemon": stub_daemon,
        "cache": str(tmp_path / "cache"),
        "version": version,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text
    return response.json()


def _describe(client, target="scene.tif", register=True):
    response = client.post(DESCRIBE, json={"target": target}, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text
    descriptor = response.json()
    if register:
        source = descriptor["data"]["source_id"]
        client.post(REINIT, json={"key": source, "path": "/describe",
                                  "payload": {"target": target}},
                    headers={"X-Fused": "1"}).raise_for_status()
    return descriptor


def _proxy_tile(descriptor):
    """The child's absolute tile URL as the stable proxy path the page would hold."""
    path = urlsplit(descriptor["data"]["tile_url"]).path
    return (PROXY + path).replace("{z}", "0").replace("{x}", "0").replace("{y}", "0")


def test_ensure_requires_the_x_fused_header(client, stub_python, stub_daemon, tmp_path):
    response = client.post(ENSURE, json={
        "python": stub_python, "daemon": stub_daemon,
        "cache": str(tmp_path), "version": "v1",
    })
    assert response.status_code == 403


def test_a_proxied_post_requires_the_x_fused_header(client, stub_python, stub_daemon, tmp_path):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    response = client.post(DESCRIBE, json={"target": "scene.tif"})
    assert response.status_code == 403


def test_describe_reaches_the_child_and_returns_its_urls(
    client, stub_python, stub_daemon, tmp_path,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client, register=False)
    assert descriptor["status"] == "ok"
    # The router does not rewrite; the child's own absolute URLs come back and
    # the template rewrites them. The router only hides the port on GETs.
    assert descriptor["data"]["source_id"]


def test_a_tile_returns_png_bytes_through_the_server(
    client, stub_python, stub_daemon, tmp_path,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    response = client.get(_proxy_tile(descriptor))
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")
    assert response.headers["content-type"] == "image/png"


def test_killing_the_child_heals_the_same_proxy_url(
    client, stub_python, stub_daemon, tmp_path, engine_host,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    url = _proxy_tile(descriptor)
    assert client.get(url).status_code == 200

    child = engine_host.current(ENGINE)
    child.proc.kill()
    child.proc.wait(timeout=10)

    healed = client.get(url)
    assert healed.status_code == 200, healed.text
    assert healed.content.startswith(b"\x89PNG")
    assert engine_host.current(ENGINE).pid != child.pid


def test_a_wedged_child_is_restarted_by_the_retry(
    client, stub_python, stub_daemon, tmp_path, engine_host, monkeypatch, child_post,
):
    from fused_render.server.routers import engines

    monkeypatch.setattr(engines, "GET_TIMEOUT_S", 3.0)
    _ensure(client, stub_python, stub_daemon, tmp_path)
    descriptor = _describe(client)
    url = _proxy_tile(descriptor)
    assert client.get(url).status_code == 200

    child = engine_host.current(ENGINE)
    child_post(child, "/wedge")

    started = time.monotonic()
    healed = client.get(url)
    assert healed.status_code == 200, healed.text
    assert engine_host.current(ENGINE).pid != child.pid
    assert time.monotonic() - started < 60


def test_a_tile_without_a_running_engine_says_so(client):
    response = client.get(f"{PROXY}/tiles/nope/0/0/0.png")
    assert response.status_code == 409
