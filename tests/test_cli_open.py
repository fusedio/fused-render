"""`fused-render open <path>` (cli.py's `_run_open`): target resolution (a
folder resolves to its app entry, same as the Home grid's "Open app" button)
and the embed URL a ready server gets opened at.

The server itself (uvicorn, `create_app`) is stubbed throughout — this is a
unit test of `open`'s own wiring (target -> URL -> "open once ready"), not an
end-to-end server boot. `tests/test_app_lifespan.py` covers what `lean=True`
does to `create_app`; a real boot is exercised by hand (see the PR/commit
notes), not in the suite, to stay inside the /tmp budget.
"""
import argparse
import socket
import types

import pytest

from fused_render import cli as cli_module
from fused_render._view_url_codec import embed_url


# ---- _open_target: folder/file -> the fs path `open` actually renders


def test_open_target_folder_resolves_to_its_app_entry(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html><head><meta name="fused-app" content=""></head><body></body></html>',
        encoding="utf-8",
    )
    assert cli_module._open_target(str(tmp_path)) == str(tmp_path / "index.html")


def test_open_target_folder_with_no_app_entry_used_as_is(tmp_path):
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    assert cli_module._open_target(str(tmp_path)) == str(tmp_path)


def test_open_target_file_used_as_is(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("1 + 1", encoding="utf-8")
    assert cli_module._open_target(str(f)) == str(f)


def test_open_target_missing_path_exits(tmp_path):
    with pytest.raises(SystemExit):
        cli_module._open_target(str(tmp_path / "does-not-exist"))


# ---- _run_open: target -> embed URL -> browser opened once the (stubbed)
# server answers ready


class _ImmediateThread:
    """Runs its target synchronously instead of spawning a real thread, so
    the test does not have to race `_run_open`'s background poll-and-open."""

    def __init__(self, target=None, **_kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class _FakeUvicornServer:
    def __init__(self, config):
        self.config = config

    def run(self):
        pass  # never actually binds a socket or serves


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_run_open_opens_the_embed_url_once_ready(tmp_path, monkeypatch):
    app_dir = tmp_path / "my-app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text(
        '<html><head><meta name="fused-app" content=""></head><body></body></html>',
        encoding="utf-8",
    )

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace())
    created_with = {}

    def fake_create_app(start_dir, lean):
        created_with["start_dir"] = start_dir
        created_with["lean"] = lean
        return fake_app

    monkeypatch.setattr("fused_render.server.create_app", fake_create_app)
    monkeypatch.setattr("fused_render.server.export_app_env", lambda: None)
    monkeypatch.setattr("fused_render.server.set_server_origin_env", lambda port, host="127.0.0.1": None)
    monkeypatch.setattr("fused_render.windows_process.install_no_window_policy", lambda: None)
    monkeypatch.setattr(cli_module, "setup_logging", lambda: "/dev/null")
    monkeypatch.setattr("uvicorn.Server", _FakeUvicornServer)
    monkeypatch.setattr("uvicorn.Config", lambda app, host, port: types.SimpleNamespace(host=host, port=port))
    monkeypatch.setattr(cli_module, "_wait_ready", lambda port, timeout=10.0: True)
    monkeypatch.setattr(cli_module, "threading", types.SimpleNamespace(Thread=_ImmediateThread))

    opened = []
    monkeypatch.setattr(cli_module.webbrowser, "open", lambda url: opened.append(url))

    port = _free_port()
    args = argparse.Namespace(path=str(app_dir), port=port, no_browser=False)
    cli_module._run_open(args)

    expected_target = str(app_dir / "index.html")
    assert created_with == {"start_dir": str(app_dir), "lean": True}
    assert opened == [embed_url(port, expected_target)]


def test_run_open_no_browser_skips_opening(tmp_path, monkeypatch):
    app_dir = tmp_path / "my-app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace())
    monkeypatch.setattr("fused_render.server.create_app", lambda start_dir, lean: fake_app)
    monkeypatch.setattr("fused_render.server.export_app_env", lambda: None)
    monkeypatch.setattr("fused_render.server.set_server_origin_env", lambda port, host="127.0.0.1": None)
    monkeypatch.setattr("fused_render.windows_process.install_no_window_policy", lambda: None)
    monkeypatch.setattr(cli_module, "setup_logging", lambda: "/dev/null")
    monkeypatch.setattr("uvicorn.Server", _FakeUvicornServer)
    monkeypatch.setattr("uvicorn.Config", lambda app, host, port: types.SimpleNamespace(host=host, port=port))

    opened = []
    monkeypatch.setattr(cli_module.webbrowser, "open", lambda url: opened.append(url))

    port = _free_port()
    args = argparse.Namespace(path=str(app_dir), port=port, no_browser=True)
    cli_module._run_open(args)

    assert opened == []
