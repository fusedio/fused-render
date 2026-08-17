"""Tile responses must be cacheable and reuse the connection (daemon.py).

A rendered tile is expensive to produce and never changes for a given URL, yet
the service used to mark every response ``no-store`` on HTTP/1.0 — so panning
back one tile re-fetched it over a brand new TCP connection. Zooming out and in
again re-read the whole viewport from the network every time.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_tile_transport.py -o addopts=""
"""
import importlib.util
import os
import sys

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


class Recorder:
    """Captures what _headers would put on the wire."""

    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        handler.send_response = lambda status: self.__setattr__("status", status)
        handler.send_header = lambda name, value: self.headers.__setitem__(name, value)
        handler.end_headers = lambda: None


def _handler(daemon):
    handler = daemon.Handler.__new__(daemon.Handler)
    handler.command = "GET"
    return handler


def test_connections_are_reused(daemon):
    # BaseHTTPRequestHandler defaults to HTTP/1.0, which closes the socket after
    # every response; a viewport of tiles then costs a viewport of handshakes.
    assert daemon.Handler.protocol_version == "HTTP/1.1"


def test_idle_keep_alive_connections_are_reaped(daemon):
    # HTTP/1.1 parks a server thread per open connection, so it needs a timeout.
    assert isinstance(daemon.Handler.timeout, (int, float))
    assert daemon.Handler.timeout > 0


def test_tiles_are_cacheable_by_the_browser(daemon):
    handler = _handler(daemon)
    recorder = Recorder(handler)
    handler._headers(200, "image/png", 1024, cache=daemon.TILE_CACHE_CONTROL)
    assert "no-store" not in recorder.headers["Cache-Control"]
    assert "max-age=" in recorder.headers["Cache-Control"]


def test_metadata_responses_stay_uncacheable(daemon):
    # /describe and /jobs report live state; caching them would freeze progress.
    handler = _handler(daemon)
    recorder = Recorder(handler)
    handler._headers(200, "application/json", 12)
    assert recorder.headers["Cache-Control"] == "no-store"
