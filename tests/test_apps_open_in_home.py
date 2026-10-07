"""`apps_open_in_home`: apps load into the Home window instead of a window of
their own (mac_window.py `open_app_in_home`, the /api/windows/open route,
window_policy.app_home_path)."""
import sys
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import window_policy

FUSED = {"X-Fused": "1"}


def _app(tmp_path):
    d = tmp_path / "myapp"
    d.mkdir(exist_ok=True)
    return d


def test_app_home_path_is_the_explorer_view_not_the_embed(tmp_path):
    d = _app(tmp_path)
    path = window_policy.app_home_path(str(d))
    assert path == window_policy.explorer_view_path(str(d))
    assert path != window_policy.app_window_path(str(d)) or not (d / "index.html").exists()


class _Win:
    def __init__(self, url, home=False):
        self.url, self.home = url, home
        self.ns = None
        self.frame_name = None
        self.loaded, self.shown = [], 0

    def load(self, url):
        self.loaded.append(url)

    def show(self):
        self.shown += 1


def _manager(windows):
    pytest.importorskip("AppKit")
    pytest.importorskip("WebKit")
    from fused_render import mac_window

    m = types.SimpleNamespace(port=1234, _windows=windows, opened=[])
    m._is_home = lambda w: w.home
    m.key = lambda: None
    m.front = lambda: windows[-1] if windows else None
    m.frame_owner = lambda name: None
    m.open = lambda url, html=None: m.opened.append(url) or _Win(url)
    m.run = lambda p: mac_window.WindowManager.open_app_in_home(m, p)
    return m


@pytest.mark.skipif(sys.platform != "darwin", reason="AppKit only")
def test_mac_loads_into_the_home_window(tmp_path):
    home, other = _Win("h", home=True), _Win("o")
    home.ns = other.ns = object()  # truthy marker; frame handoff skipped via name check below
    m = _manager([home, other])
    m.frame_owner = lambda name: object()  # owned elsewhere: no ns calls
    win = m.run(str(_app(tmp_path)))
    assert win is home and m.opened == []
    assert home.loaded == [f"http://127.0.0.1:1234{window_policy.app_home_path(str(tmp_path / 'myapp'))}"]
    assert home.shown == 1 and other.loaded == []


@pytest.mark.skipif(sys.platform != "darwin", reason="AppKit only")
def test_mac_falls_back_to_mru_then_new_window(tmp_path):
    only = _Win("x")
    m = _manager([only])
    assert m.run(str(_app(tmp_path))) is only and len(only.loaded) == 1
    m = _manager([])
    m.run(str(_app(tmp_path)))
    assert len(m.opened) == 1


def test_route_skips_the_window_hook_when_the_pref_is_on(tmp_path, monkeypatch):
    from fused_render.server import app as server_app  # noqa: F401
    from fused_render.shell import prefs

    d = _app(tmp_path)
    calls = []
    window_policy.native_hooks["open_app"] = calls.append
    monkeypatch.setattr(prefs, "apps_open_in_home", lambda: True)
    try:
        from fused_render.server.routers import windows

        out = windows.api_windows_open({"path": str(d)}, "1")
        assert out == {"ok": True, "in_home": True} and calls == []
        monkeypatch.setattr(prefs, "apps_open_in_home", lambda: False)
        assert windows.api_windows_open({"path": str(d)}, "1") == {"ok": True}
        assert calls == [str(d)]
    finally:
        window_policy.native_hooks.pop("open_app", None)
