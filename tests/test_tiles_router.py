"""The public tile surface (fused_render/server/routers/tiles.py).

/api/tiles/* is the DOCUMENTED face of the internal engine host: it must return
same-origin /api/tiles URLs (never an /api/engines path, a child origin or a
token), forward tile/status GETs to the map engine through the shared proxy,
heal a dead daemon under a URL the page holds, and reject a layer id that is not
a bare token before it can reach the child.

/api/tiles/open runs map_render against the real engine; wiring that whole path
under TestClient is impractical, so `_describe_layer` (the one function open uses
to produce a descriptor) is monkeypatched to a canned descriptor, while the
child it forwards to is a live stub registered through engine_host — so the
rewrite, allowlist, forward, traversal and heal behaviour are all exercised.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest tests/test_tiles_router.py -o addopts=""
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

# Set before any fused_render.server import can stage core templates into the
# home dir (root tests/conftest.py normally wins; this is a belt-and-braces
# guard so the file is runnable on its own without touching the real home).
if "FUSED_RENDER_HOME" not in os.environ:
    _tmp = tempfile.mkdtemp(prefix="tiles-tests-home-")
    os.environ["FUSED_RENDER_HOME"] = _tmp
    atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.environ.setdefault("FUSED_RENDER_ENGINE", "builtin")


# The same stand-in daemon the engine-host tests use: it speaks the real
# daemon's token-guarded contract (/ping /describe /tiles /vtiles /jobs) and
# publishes {port, token, pid, version} to its status file.
STUB_DAEMON = '''\
import argparse, json, os, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PNG = b"\\x89PNG\\r\\n\\x1a\\n" + b"stub-tile"
PBF = b"\\x1a\\x02st"

parser = argparse.ArgumentParser()
parser.add_argument("--status", required=True)
parser.add_argument("--cache", required=True)
parser.add_argument("--version", required=True)
args = parser.parse_args()
token = secrets.token_urlsafe(8)
sources = set()
port = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, payload):
        self._send(200, json.dumps(payload).encode("utf-8"))

    def _authorized(self):
        return parse_qs(urlparse(self.path).query).get("t", [""])[0] == token

    def do_GET(self):
        if not self._authorized():
            self._send(403, b"{}")
            return
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/ping":
            self._ok({"ok": True, "version": args.version, "pid": os.getpid()})
        elif parts and parts[0] == "tiles":
            if parts[1] in sources:
                self._send(200, PNG, "image/png")
            else:
                self._send(404, b'{"error": "unknown source"}')
        elif parts and parts[0] == "vtiles":
            if parts[1] in sources:
                self._send(200, PBF, "application/vnd.mapbox-vector-tile")
            else:
                self._send(404, b'{"error": "unknown source"}')
        elif parts and parts[0] == "jobs":
            self._ok({"phase": "idle", "source": parts[1]})
        else:
            self._send(404, b"{}")

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._send(403, b"{}")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if parsed.path == "/describe":
            sid = "s" + "".join(
                c if c.isalnum() else "_" for c in str(body.get("target") or "x"))
            sources.add(sid)
            base = "http://127.0.0.1:%d" % port
            self._ok({"status": "ok", "kind": "raster_tiles", "bounds": [0, 0, 1, 1],
                      "data": {
                          "source_id": sid,
                          "tile_url": base + "/tiles/" + sid + "/{z}/{x}/{y}.png?t=" + token,
                      }})
        else:
            self._send(404, b"{}")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
port = server.server_address[1]
tmp = args.status + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"port": port, "token": token, "pid": os.getpid(),
               "version": args.version}, f)
os.replace(tmp, args.status)
server.serve_forever()
'''


@pytest.fixture(scope="session")
def stub_python():
    from fused_render.shell.storage import home_dir

    venv_dir = os.path.join(home_dir(), "venvs", "tiles-engine-stub")
    python = os.path.join(
        venv_dir, "Scripts" if os.name == "nt" else "bin",
        "python.exe" if os.name == "nt" else "python")
    if not os.path.exists(python):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv_dir],
                       check=True, capture_output=True)
    return python


@pytest.fixture(scope="session")
def stub_daemon():
    from fused_render.shell.storage import home_dir

    folder = os.path.join(home_dir(), "templates", "map")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "daemon.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(STUB_DAEMON)
    return path


@pytest.fixture
def engine_host():
    from fused_render.server import engine_host as module

    module.stop_all()
    yield module
    module.stop_all()


@pytest.fixture
def client(tmp_path, monkeypatch, engine_host):
    from fused_render.server.app import create_app
    from fused_render.shell import mounts

    monkeypatch.setattr(mounts, "startup", lambda: None)
    monkeypatch.setattr(mounts, "start_health_monitor", lambda: None)
    app = create_app(str(tmp_path))
    return fastapi_testclient.TestClient(app)


ENSURE = "/api/engines/map/ensure"
DESCRIBE = "/api/engines/map/proxy/describe"


def _ensure(client, stub_python, stub_daemon, tmp_path, version="v1"):
    response = client.post(ENSURE, json={
        "python": stub_python,
        "daemon": stub_daemon,
        "cache": str(tmp_path / "cache"),
        "version": version,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text


def _register_source(client, target):
    """Register a source + its reinit replay in the child, the way map_render
    does (describe through the proxy with X-Engine-Reinit)."""
    response = client.post(
        DESCRIBE, json={"target": target},
        headers={"X-Fused": "1", "X-Engine-Reinit": "rk-" + target})
    assert response.status_code == 200, response.text
    return response.json()["data"]["source_id"]


def _canned_raster(monkeypatch, sid, target, kind="raster_tiles"):
    from fused_render.server.routers import tiles

    async def fake(_target, _options):
        # A descriptor that even CARRIES an /api/engines proxy url, to prove the
        # public layer is built from the id and never leaks the internal path.
        return {
            "status": "ok",
            "kind": kind,
            "bounds": [0, 0, 1, 1],
            "minzoom": 0,
            "maxzoom": 18,
            "warnings": ["preparing"],
            "data": {
                "source_id": sid,
                "reinit_key": "rk-" + target,
                "tile_url": f"/api/engines/map/proxy/tiles/{sid}/{{z}}/{{x}}/{{y}}.png",
            },
        }

    monkeypatch.setattr(tiles, "_describe_layer", fake)


def test_open_requires_the_x_fused_header(client):
    response = client.post("/api/tiles/open", json={"target": "scene.tif"})
    assert response.status_code == 403


def test_open_returns_a_public_layer_with_no_engine_url(
    client, stub_python, stub_daemon, tmp_path, monkeypatch,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    sid = _register_source(client, "scene.tif")
    _canned_raster(monkeypatch, sid, "scene.tif")

    response = client.post("/api/tiles/open", json={"target": "scene.tif"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text
    layer = response.json()

    assert layer["id"] == sid
    assert layer["kind"] == "raster"
    assert layer["tileUrl"] == f"/api/tiles/{sid}/{{z}}/{{x}}/{{y}}.png"
    assert layer["tileUrl"].startswith("/api/tiles/")
    assert layer["closeToken"] == "rk-scene.tif"
    # Nothing internal leaks anywhere in the whole layer object.
    blob = json.dumps(layer)
    assert "/api/engines" not in blob
    assert "127.0.0.1" not in blob
    assert "t=" not in layer["tileUrl"]


def test_a_tile_returns_png_bytes_through_the_public_url(
    client, stub_python, stub_daemon, tmp_path, monkeypatch,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    sid = _register_source(client, "scene.tif")
    _canned_raster(monkeypatch, sid, "scene.tif")
    layer = client.post("/api/tiles/open", json={"target": "scene.tif"},
                        headers={"X-Fused": "1"}).json()

    url = layer["tileUrl"].replace("{z}", "0").replace("{x}", "0").replace("{y}", "0")
    response = client.get(url)
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")
    assert response.headers["content-type"] == "image/png"


def test_a_vector_layer_serves_pbf_tiles(
    client, stub_python, stub_daemon, tmp_path, monkeypatch,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    sid = _register_source(client, "roads.geojson")
    _canned_raster(monkeypatch, sid, "roads.geojson", kind="vector_tiles_mvt")

    layer = client.post("/api/tiles/open", json={"target": "roads.geojson"},
                        headers={"X-Fused": "1"}).json()
    assert layer["kind"] == "vector"
    assert layer["tileUrl"] is None
    assert layer["vectorTileUrl"] == f"/api/tiles/{sid}/{{z}}/{{x}}/{{y}}.pbf"

    url = layer["vectorTileUrl"].replace("{z}", "1").replace("{x}", "0").replace("{y}", "0")
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == b"\x1a\x02st"


def test_status_reaches_the_child_jobs_endpoint(
    client, stub_python, stub_daemon, tmp_path,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    sid = _register_source(client, "scene.tif")
    response = client.get(f"/api/tiles/{sid}/status")
    assert response.status_code == 200
    assert response.json()["phase"] == "idle"


def test_killing_the_child_heals_the_same_public_url(
    client, stub_python, stub_daemon, tmp_path, monkeypatch, engine_host,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    sid = _register_source(client, "scene.tif")
    _canned_raster(monkeypatch, sid, "scene.tif")
    layer = client.post("/api/tiles/open", json={"target": "scene.tif"},
                        headers={"X-Fused": "1"}).json()
    url = layer["tileUrl"].replace("{z}", "0").replace("{x}", "0").replace("{y}", "0")
    assert client.get(url).status_code == 200

    child = engine_host.current("map")
    child.proc.kill()
    child.proc.wait(timeout=10)

    healed = client.get(url)
    assert healed.status_code == 200, healed.text
    assert healed.content.startswith(b"\x89PNG")
    assert engine_host.current("map").pid != child.pid


def test_a_layer_id_that_is_not_a_bare_token_is_rejected(
    client, stub_python, stub_daemon, tmp_path,
):
    # A live engine with a real source, so a 404 is the guard rejecting the id
    # rather than there being no engine at all.
    _ensure(client, stub_python, stub_daemon, tmp_path)
    sid = _register_source(client, "scene.tif")
    assert client.get(f"/api/tiles/{sid}/0/0/0.png").status_code == 200

    # A dot in the token fails the bare-token guard before any forward.
    assert client.get("/api/tiles/a.b/0/0/0.png").status_code == 404
    # A traversal segment cannot even form the route.
    assert client.get("/api/tiles/%2e%2e/0/0/0.png").status_code in (400, 404)
    assert client.get("/api/tiles/..%2Fping/0/0/0.png").status_code in (400, 404)
    # The daemon's private /ping is unreachable through this namespace: there is
    # no bare-word route, so it 404s at routing and never touches the child.
    assert client.get("/api/tiles/ping").status_code == 404
    assert client.get("/api/tiles/ping/ping").status_code == 404


def test_open_preserves_the_error_descriptor(client, monkeypatch):
    from fused_render.server.routers import tiles

    async def fake(_target, _options):
        return {"status": "error", "message": "boom", "traceback": "tb"}

    monkeypatch.setattr(tiles, "_describe_layer", fake)
    response = client.post("/api/tiles/open", json={"target": "nope.tif"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["message"] == "boom"
    assert "/api/engines" not in json.dumps(body)


def test_close_forgets_the_layers_reinit(
    client, stub_python, stub_daemon, tmp_path, engine_host, monkeypatch,
):
    _ensure(client, stub_python, stub_daemon, tmp_path)
    sid = _register_source(client, "scene.tif")
    _canned_raster(monkeypatch, sid, "scene.tif")
    layer = client.post("/api/tiles/open", json={"target": "scene.tif"},
                        headers={"X-Fused": "1"}).json()

    calls = []
    monkeypatch.setattr(engine_host, "forget",
                        lambda engine_id, key: calls.append((engine_id, key)))
    response = client.post(f"/api/tiles/{layer['id']}/close",
                           json={"closeToken": layer["closeToken"]},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200
    assert calls == [("map", "rk-scene.tif")]


def test_close_requires_the_x_fused_header(client):
    response = client.post("/api/tiles/somesource/close", json={})
    assert response.status_code == 403
