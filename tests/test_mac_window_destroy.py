"""Tearing a window down (mac_window.py `_Window._destroy`) works for both
engines' delegates and keeps the names of downloads a WebKit window still
had running."""
import sys
import types

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit only")


def _mw():
    pytest.importorskip("AppKit")
    pytest.importorskip("WebKit")
    from fused_render import mac_window

    return mac_window


def _window(engine, delegate):
    ns = types.SimpleNamespace(titlebarAccessoryViewControllers=list,
                               setContentView_=lambda v: None)
    webview = types.SimpleNamespace(closeBrowser=lambda: None)
    return types.SimpleNamespace(webview=webview, ns=ns, delegate=delegate,
                                 engine=engine, key=None)


def test_chromium_window_closes_without_a_download_map():
    # The Chromium delegate has no WKDownloads and no `_download_dests`.
    mw = _mw()
    delegate = types.SimpleNamespace(_window=object(), _manager=object(), _downloads=[])
    win = _window("chromium", delegate)
    mw._Window._destroy(win)
    assert win.webview is None and delegate._window is None


def test_webkit_window_keeps_names_of_unfinished_downloads(monkeypatch):
    # The WKDownload is not cancelled and may still be writing: its name
    # must not be handed to another download.
    mw = _mw()
    monkeypatch.setattr(mw.window_policy, "_reserved_destinations", set())
    never = {"/dl/a.zip"}.__contains__
    dest = mw.window_policy.download_destination("/dl", "b.zip", exists=never)
    delegate = types.SimpleNamespace(_window=object(), _manager=object(), _downloads=[object()],
                                     _download_dests={1: dest})
    mw._Window._destroy(_window("webkit", delegate))
    assert delegate._download_dests == {}
    assert mw.window_policy.download_destination("/dl", "b.zip", exists=never) != dest
