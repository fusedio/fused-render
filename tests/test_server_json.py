"""`server.json` (SPEC PY-19, D463): the discovery file a process the server
did NOT spawn reads to find this server's origin — `fused_ai.py`'s
`resolve_origin()` fallback below `FUSED_RENDER_ORIGIN`.

Driven directly against `write_server_json`/`remove_server_json`
(`fused_render/server/app.py`), not through a running server — this repo's
rule is that the dev server starts only via `scripts/dev.sh`, by a human.
"""
import json
import os
import time

import pytest

import fused_render
from fused_render.server import app as server_app


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)


def test_write_server_json_has_the_right_keys(tmp_path):
    before = time.time()
    server_app.write_server_json(1777, host="127.0.0.1")
    path = server_app._server_json_path()
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["origin"] == "http://127.0.0.1:1777"
    assert data["pid"] == os.getpid()
    assert data["version"] == fused_render.__version__
    assert data["shared"] == os.path.join(
        os.path.dirname(os.path.abspath(fused_render.__file__)),
        "templates", "shared")
    assert os.path.isdir(data["shared"])
    assert before <= data["started"] <= time.time()


def test_write_server_json_lands_under_the_shell_home_dir():
    from fused_render.shell import storage as shell_storage

    server_app.write_server_json(1777)
    assert server_app._server_json_path() == os.path.join(
        shell_storage.home_dir(), "server.json")


def test_remove_server_json_deletes_the_file():
    server_app.write_server_json(1777)
    path = server_app._server_json_path()
    assert os.path.isfile(path)
    server_app.remove_server_json()
    assert not os.path.exists(path)


def test_remove_server_json_is_a_noop_when_nothing_is_there():
    server_app.remove_server_json()  # must not raise


def test_remove_server_json_never_deletes_another_processs_file():
    """Two servers on the same branch (different ports — desktop app +
    `fused-render serve --port 8001`) both write server.json into the same
    branch-resolved home dir; last-writer-wins with no ownership check means
    the first to shut down deletes the SURVIVOR's file, and every external
    resolve_origin() then raises ServerNotRunning while a server IS running.
    remove_server_json() must only ever remove a file this process wrote."""
    path = server_app._server_json_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"origin": "http://127.0.0.1:9999", "pid": os.getpid() + 12345},
                  f)
    server_app.remove_server_json()
    assert os.path.isfile(path), "deleted a file this process did not write"


def test_remove_server_json_deletes_its_own_file_even_with_others_present():
    server_app.write_server_json(1777)  # writes with THIS process's pid
    path = server_app._server_json_path()
    assert os.path.isfile(path)
    server_app.remove_server_json()
    assert not os.path.exists(path)


def test_a_write_failure_is_swallowed_and_never_raises(monkeypatch, tmp_path):
    """A home dir that cannot hold the file (a plain file sitting where a
    directory should be) must not block or crash startup."""
    blocked = tmp_path / "blocked-home"
    blocked.write_text("not a directory")
    monkeypatch.setenv("FUSED_RENDER_HOME", str(blocked))
    server_app.write_server_json(1777)  # must not raise
    assert not os.path.exists(os.path.join(str(blocked), "server.json"))
