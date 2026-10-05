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


def test_open_target_fused_file_used_as_is(tmp_path):
    """A `.fused` file is handed to the embed URL unchanged: the `fusedapp`
    preview template extracts it (and lands on its entry page) client-side,
    via the guarded `/api/appfile/open` route — `_open_target` does not need
    to extract it up front."""
    f = tmp_path / "my-app.fused"
    f.write_bytes(b"not a real .fused payload, just a path to resolve")
    assert cli_module._open_target(str(f)) == str(f)


def test_fused_file_end_to_end_extracts_and_renders(tmp_path, monkeypatch):
    """A real `.fused`, exported from a real app folder: the lean server's
    embed URL for the `.fused` file itself answers 200, and the entry page
    the `fusedapp` template's `/api/appfile/open` call resolves to also
    answers 200 with the app's own markup — not an error page or a listing.
    """
    from starlette.testclient import TestClient

    from fused_render import appfile
    from fused_render.server import create_app

    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "appfiles"))

    app_dir = tmp_path / "src-app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text(
        '<html><head><meta name="fused-app" content=""></head>'
        "<body>MARKER-1234</body></html>",
        encoding="utf-8",
    )
    fused_path = str(tmp_path / "src-app.fused")
    appfile.export_app_file(str(app_dir), fused_path)

    target = cli_module._open_target(fused_path)
    assert target == fused_path

    app = create_app(start_dir=str(tmp_path), lean=True)
    with TestClient(app) as client:
        embed_resp = client.get(f"/explorer/embed{fused_path}")
        assert embed_resp.status_code == 200

        opened = client.post(
            "/api/appfile/open", json={"file": fused_path}, headers={"X-Fused": "1"}
        )
        assert opened.status_code == 200
        entry = opened.json()["entry"]

        entry_resp = client.get(f"/explorer/embed{entry}")
        assert entry_resp.status_code == 200

    # The extracted entry is the actual app content, not an error page or a
    # directory listing dressed up as one.
    with open(entry, encoding="utf-8") as fh:
        assert "MARKER-1234" in fh.read()


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


# ---- _open_target: GitHub URL / deep link -> cloned (or pulled), then
# resolved like a folder argument


def _github_spec(subpath=""):
    return {"owner": "acme", "repo": "widgets", "ref": None, "subpath": subpath, "name": "widgets"}


def test_open_target_github_url_clones_and_resolves_entry(tmp_path, monkeypatch):
    dest = tmp_path / "widgets"
    dest.mkdir()
    (dest / "index.html").write_text(
        '<html><head><meta name="fused-app" content=""></head><body></body></html>',
        encoding="utf-8",
    )

    from fused_render import deeplink

    calls = {}

    def fake_parse(src):
        calls["src"] = src
        return _github_spec()

    def fake_destination(spec):
        return str(dest)

    def fake_clone_or_pull(spec):
        calls["cloned_spec"] = spec
        return {"dest": str(dest), "target": str(dest), "view": "irrelevant", "updated": False}

    monkeypatch.setattr(deeplink, "parse_github_url", fake_parse)
    monkeypatch.setattr(deeplink, "destination", fake_destination)
    monkeypatch.setattr(deeplink, "clone_or_pull", fake_clone_or_pull)

    target = cli_module._open_target("https://github.com/acme/widgets")

    assert calls["src"] == "https://github.com/acme/widgets"
    assert calls["cloned_spec"] == _github_spec()
    assert target == str(dest / "index.html")


def test_open_target_github_deeplink_form_is_also_recognized(tmp_path, monkeypatch):
    dest = tmp_path / "widgets"
    dest.mkdir()

    from fused_render import deeplink

    monkeypatch.setattr(deeplink, "parse_github_url", lambda src: _github_spec())
    monkeypatch.setattr(deeplink, "destination", lambda spec: str(dest))
    monkeypatch.setattr(
        deeplink,
        "clone_or_pull",
        lambda spec: {"dest": str(dest), "target": str(dest), "view": "x", "updated": True},
    )

    target = cli_module._open_target("fused-render://open?git=https://github.com/acme/widgets")

    # No app-entry page in `dest` here, so the bare clone dir is used as-is.
    assert target == str(dest)


def test_open_target_github_url_prints_where_it_clones(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "widgets"
    dest.mkdir()

    from fused_render import deeplink

    monkeypatch.setattr(deeplink, "parse_github_url", lambda src: _github_spec())
    monkeypatch.setattr(deeplink, "destination", lambda spec: str(dest))
    monkeypatch.setattr(
        deeplink,
        "clone_or_pull",
        lambda spec: {"dest": str(dest), "target": str(dest), "view": "x", "updated": False},
    )

    cli_module._open_target("https://github.com/acme/widgets")

    out = capsys.readouterr().out
    assert "acme/widgets" in out
    assert str(dest) in out


def test_open_target_github_clone_error_exits_nonzero_with_git_message(monkeypatch):
    from fused_render import deeplink

    monkeypatch.setattr(deeplink, "parse_github_url", lambda src: _github_spec())
    monkeypatch.setattr(deeplink, "destination", lambda spec: "/tmp/wherever")

    def fail(spec):
        raise deeplink.DeeplinkError("fatal: could not read Username for 'https://github.com'")

    monkeypatch.setattr(deeplink, "clone_or_pull", fail)

    with pytest.raises(SystemExit, match="could not read Username"):
        cli_module._open_target("https://github.com/acme/widgets")
