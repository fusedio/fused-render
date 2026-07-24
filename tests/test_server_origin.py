"""The server must publish its ACTUAL bound origin to the environment.

runPython children (notably the zarr_aoi tile daemon) read store bytes back
through the server's ``/api/fs/raw`` and learn the server's origin from
``FUSED_RENDER_ORIGIN``. Before this wiring existed the daemon fell back to
``branch_port()`` (baseline ``1777``), which is wrong whenever the server was
started on any other port (e.g. ``--port 32953`` from the desktop launcher):
every chunk read hit a dead port and zarr reported "No group found in store".
"""
import os

from fused_render import server


def test_set_server_origin_env_uses_actual_port(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    origin = server.set_server_origin_env(32953)
    assert origin == "http://127.0.0.1:32953"
    assert os.environ["FUSED_RENDER_ORIGIN"] == "http://127.0.0.1:32953"


def test_set_server_origin_env_overrides_stale_value(monkeypatch):
    # A stale origin from a previous bind (or a wrong branch default) must be
    # replaced by the port this process is really serving on.
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:1777")
    server.set_server_origin_env(9000)
    assert os.environ["FUSED_RENDER_ORIGIN"] == "http://127.0.0.1:9000"
