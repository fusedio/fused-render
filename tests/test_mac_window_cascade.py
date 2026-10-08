"""A new window of an app that is already open cascades from the NEWEST
sibling, not from the window that owns the saved frame (mac_window.py
`_Window._place`, `WindowManager.newest_placed`)."""
import sys
import types

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit only")


def _mw():
    pytest.importorskip("AppKit")
    pytest.importorskip("WebKit")
    from fused_render import mac_window

    return mac_window


def _win(place_name=None, frame_name=None, ns=True):
    return types.SimpleNamespace(
        place_name=place_name, frame_name=frame_name, ns=object() if ns else None
    )


def test_newest_placed_returns_the_last_matching_window():
    mw = _mw()
    a, b, c = _win("n", "n"), _win("n"), _win("other")
    m = types.SimpleNamespace(_windows=[a, b, c])
    assert mw.WindowManager.newest_placed(m, "n") is b


def test_newest_placed_skips_closed_windows_and_misses():
    mw = _mw()
    a, b = _win("n", "n"), _win("n", ns=False)
    m = types.SimpleNamespace(_windows=[a, b])
    assert mw.WindowManager.newest_placed(m, "n") is a
    assert mw.WindowManager.newest_placed(m, "zzz") is None


def _place_self(windows, monkeypatch, owner):
    mw = _mw()
    monkeypatch.setattr(mw.window_policy, "frame_autosave_name", lambda k, v: "n")
    m = types.SimpleNamespace(_windows=windows)
    m.frame_owner = lambda name: owner
    m.newest_placed = lambda name: mw.WindowManager.newest_placed(m, name)
    cascaded = []
    me = types.SimpleNamespace(
        _popup=False, manager=m, place_name=None, frame_name=None,
        _cascade_from=cascaded.append,
        ns=types.SimpleNamespace(
            setFrameUsingName_=lambda n: False,
            setFrameAutosaveName_=lambda n: True,
            center=lambda: None,
        ),
    )
    return mw, me, cascaded


def test_place_cascades_from_the_newest_sibling(monkeypatch):
    owner, sib = _win("n", "n"), _win("n")
    mw, me, cascaded = _place_self([owner, sib], monkeypatch, owner)
    mw._Window._place(me, "k", "view")
    assert cascaded == [sib]
    assert me.place_name == "n"


def test_place_cascades_from_the_owner_when_it_is_alone(monkeypatch):
    owner = _win("n", "n")
    mw, me, cascaded = _place_self([owner], monkeypatch, owner)
    mw._Window._place(me, "k", "view")
    assert cascaded == [owner]
    assert me.place_name == "n"


def test_place_sets_place_name_when_no_owner_exists(monkeypatch):
    mw, me, _ = _place_self([], monkeypatch, None)
    me.manager.front = lambda: None
    mw._Window._place(me, "k", "view")
    assert me.place_name == "n" and me.frame_name == "n"
