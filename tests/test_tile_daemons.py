"""fused_render/tile_daemons.py: best-effort /quit to the tile-server daemons."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render import tile_daemons


@pytest.fixture()
def tile_daemon(tmp_path, monkeypatch):
    """A stub tile-server daemon (records /quit) plus a state file pointing at
    it, wired in as one of DAEMON_STATE_FILES."""
    quits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            quits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"bye")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = tmp_path / "daemon.json"
    state.write_text(json.dumps(
        {"port": server.server_address[1], "pid": 1, "token": "tok-test"}))
    missing = tmp_path / "absent" / "daemon.json"  # the parallel file, absent
    monkeypatch.setattr(tile_daemons, "DAEMON_STATE_FILES",
                        (str(state), str(missing)))
    yield quits
    server.shutdown()


def test_quit_forwards_the_state_file_token(tile_daemon):
    tile_daemons.quit_tile_daemons()
    assert tile_daemon == ["/quit?t=tok-test"]  # D122 gate


def test_quit_tokenless_state_sends_plain_quit(tmp_path, monkeypatch, tile_daemon):
    state = tmp_path / "old.json"
    port = json.loads((tmp_path / "daemon.json").read_text())["port"]
    state.write_text(json.dumps({"port": port}))
    monkeypatch.setattr(tile_daemons, "DAEMON_STATE_FILES", (str(state),))
    tile_daemons.quit_tile_daemons()
    assert tile_daemon == ["/quit"]


def test_quit_skips_missing_and_portless_state(tmp_path, monkeypatch, tile_daemon):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"pid": 1}))
    monkeypatch.setattr(tile_daemons, "DAEMON_STATE_FILES",
                        (str(bad), str(tmp_path / "nope.json")))
    tile_daemons.quit_tile_daemons()
    assert tile_daemon == []


def test_quit_swallows_unreachable_daemon(tmp_path, monkeypatch):
    state = tmp_path / "d.json"
    state.write_text(json.dumps({"port": 1, "token": "t"}))
    monkeypatch.setattr(tile_daemons, "DAEMON_STATE_FILES", (str(state),))
    tile_daemons.quit_tile_daemons()  # must not raise


def test_bounded_returns_within_budget_when_quit_hangs(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(tile_daemons, "quit_tile_daemons",
                        lambda: release.wait(10))
    t0 = time.monotonic()
    tile_daemons.quit_tile_daemons_bounded(0.2)
    assert time.monotonic() - t0 < 2.0
    release.set()


def test_bounded_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(tile_daemons, "quit_tile_daemons", boom)
    tile_daemons.quit_tile_daemons_bounded(1.0)


def test_budgets_ordered():
    assert (0 < tile_daemons.QUIT_FAST_TILE_DAEMONS_BUDGET_S
            <= tile_daemons.QUIT_TILE_DAEMONS_BUDGET_S)
