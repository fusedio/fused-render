"""Shared fixtures for the server-owned engine-host tests.

The host (fused_render/server/engine_host.py) only spawns an interpreter from
the home venv store and a daemon from <templates-root>/<engine_id>/daemon.py, so
the fixtures build both inside a throwaway FUSED_RENDER_HOME: a bare
`python -m venv` under <home>/venvs and a stdlib stand-in daemon under
<home>/templates/map. The stand-in speaks the real daemon's contract
(--status/--cache/--version, token-guarded /ping /describe /tiles /vtiles /jobs
/optimize) plus a /wedge switch that makes it stop answering — the failure mode
the whole architecture exists to heal.
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import pytest

# Set before any fused_render.server import can stage core templates into the
# home dir (root tests/conftest.py does the same, and wins when it ran first).
if "FUSED_RENDER_HOME" not in os.environ:
    _tmp = tempfile.mkdtemp(prefix="map-tests-home-")
    os.environ["FUSED_RENDER_HOME"] = _tmp
    atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
os.environ.setdefault("FUSED_RENDER_ENGINE", "builtin")

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
wedged = threading.Event()
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
        if wedged.is_set():
            time.sleep(3600)
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
        if parsed.path == "/wedge":
            wedged.set()
            self._ok({"ok": True})
            return
        if wedged.is_set():
            time.sleep(3600)
        if not self._authorized():
            self._send(403, b"{}")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/describe":
            sid = "s" + "".join(
                c if c.isalnum() else "_" for c in str(body.get("target") or "x"))
            sources.add(sid)
            base = "http://127.0.0.1:%d" % port
            self._ok({"status": "ok", "kind": "raster_tiles", "bounds": [0, 0, 1, 1],
                      "data": {
                          "source_id": sid,
                          "tile_url": base + "/tiles/" + sid + "/{z}/{x}/{y}.png?t=" + token,
                          "job_url": base + "/jobs/" + sid + "?t=" + token,
                          "optimize_url": base + "/optimize/" + sid + "?t=" + token,
                      }})
        elif parts and parts[0] == "optimize":
            self._ok({"ok": True, "source": parts[1]})
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
    """A real interpreter inside the home venv store, where validation looks."""
    from fused_render.shell.storage import home_dir

    venv_dir = os.path.join(home_dir(), "venvs", "map-engine-stub")
    python = os.path.join(
        venv_dir, "Scripts" if os.name == "nt" else "bin",
        "python.exe" if os.name == "nt" else "python")
    if not os.path.exists(python):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv_dir],
                       check=True, capture_output=True)
    return python


@pytest.fixture(scope="session")
def stub_daemon():
    """The stand-in daemon, at the map template's path inside the user templates
    root — where the host's validation looks for exactly map/daemon.py."""
    from fused_render.shell.storage import home_dir

    folder = os.path.join(home_dir(), "templates", "map")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "daemon.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(STUB_DAEMON)
    return path


@pytest.fixture
def engine_host():
    """The engine-host module, with every managed child stopped after each test."""
    from fused_render.server import engine_host as module

    module.stop_all()
    yield module
    module.stop_all()


def _child_get(child, path: str, timeout: float = 5.0):
    """One direct request to a managed child, bypassing the server."""
    separator = "&" if "?" in path else "?"
    url = f"http://127.0.0.1:{child.port}{path}{separator}t={child.token}"
    return urllib.request.urlopen(url, timeout=timeout)


def _child_post(child, path: str, payload=None, timeout: float = 5.0):
    separator = "&" if "?" in path else "?"
    url = f"http://127.0.0.1:{child.port}{path}{separator}t={child.token}"
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=timeout))


# Fixtures rather than `from conftest import …`: a bare `import conftest` from
# a test module resolves to whichever conftest the suite imported first (the
# repo-root tests/ has one too), so the helpers are handed out by pytest.
@pytest.fixture
def child_get():
    return _child_get


@pytest.fixture
def child_post():
    return _child_post
