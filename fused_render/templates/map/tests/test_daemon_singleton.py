"""Only one service per build may survive startup (daemon.py).

The spawn lock in map_render serializes callers, but a render killed by its own
timeout while waiting for a cold start releases that lock without recording the
daemon it began — so the next render starts another. Each survivor holds a full
geospatial runtime, so the losers have to notice and exit.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_daemon_singleton.py -o addopts=""
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")


def _load(name, filename):
    for path in (_SHARED, _MAP):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_MAP, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def daemon():
    return _load("map_daemon", "daemon.py")


@pytest.fixture
def serving():
    """A stand-in service that answers /ping with a version."""
    version = {"value": "v1"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_GET(self):
            body = json.dumps({"ok": True, "version": version["value"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, version
    server.shutdown()


def _state(tmp_path, port, version="v1", pid=999999):
    path = tmp_path / "daemon-v1.json"
    path.write_text(
        json.dumps({"version": version, "port": port, "token": "tok", "pid": pid}),
        encoding="utf-8",
    )
    return path


def test_a_second_service_for_the_same_build_stands_down(daemon, serving, tmp_path):
    server, _ = serving
    path = _state(tmp_path, server.server_address[1])
    assert daemon._already_serving(path, "v1") is True


def test_a_different_build_starts_anyway(daemon, serving, tmp_path):
    server, _ = serving
    path = _state(tmp_path, server.server_address[1], version="v2")
    # The registered service is a different build, so it is not ours to defer to.
    assert daemon._already_serving(path, "v1") is False


def test_a_registered_service_that_does_not_answer_is_replaced(daemon, tmp_path):
    # Port 9 (discard) refuses connections locally.
    path = _state(tmp_path, 9)
    assert daemon._already_serving(path, "v1") is False


def test_no_state_file_means_start(daemon, tmp_path):
    assert daemon._already_serving(tmp_path / "missing.json", "v1") is False


def test_our_own_record_is_not_mistaken_for_a_rival(daemon, serving, tmp_path):
    server, _ = serving
    path = _state(tmp_path, server.server_address[1], pid=os.getpid())
    assert daemon._already_serving(path, "v1") is False
