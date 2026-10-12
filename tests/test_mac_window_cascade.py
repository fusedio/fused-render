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
        _step_off_siblings=lambda n: None,
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


class _FakeNs:
    """Frame with a top-left at (x, top); cascade places there and returns
    the next point, offset (+20, -20) like AppKit."""

    def __init__(self, x, top, h=100.0, moves=True):
        self.x, self.y, self.h, self.moves = x, top - h, h, moves

    def frame(self):
        return types.SimpleNamespace(
            origin=types.SimpleNamespace(x=self.x, y=self.y),
            size=types.SimpleNamespace(height=self.h),
        )

    def cascadeTopLeftFromPoint_(self, pt):
        if self.moves:
            self.x, self.y = pt.x, pt.y - self.h
        return types.SimpleNamespace(x=pt.x + 20, y=pt.y - 20)


def _sib(x, top, name="n", ns=True):
    return types.SimpleNamespace(
        place_name=name, ns=_FakeNs(x, top) if ns else None
    )


def _mgr(mw, windows):
    m = types.SimpleNamespace(_windows=windows)
    m.placed_at = lambda *a: mw.WindowManager.placed_at(m, *a)
    return m


def test_placed_at_matches_top_left_within_one_point():
    mw = _mw()
    me = _sib(0, 0)
    s = _sib(100, 500)
    m = types.SimpleNamespace(_windows=[s, me])
    assert mw.WindowManager.placed_at(m, "n", 100, 500, me)
    assert mw.WindowManager.placed_at(m, "n", 100.9, 499.2, me)
    assert not mw.WindowManager.placed_at(m, "n", 102, 500, me)
    assert not mw.WindowManager.placed_at(m, "n", 100, 502, me)


def test_placed_at_ignores_exclude_closed_and_other_names():
    mw = _mw()
    me = _sib(100, 500)
    closed = _sib(100, 500, ns=False)
    other = _sib(100, 500, name="other")
    m = types.SimpleNamespace(_windows=[me, closed, other])
    assert not mw.WindowManager.placed_at(m, "n", 100, 500, me)


def test_step_off_siblings_steps_past_two_stacked_siblings():
    mw = _mw()
    w1, w2 = _sib(100, 500), _sib(120, 480)
    me = _sib(100, 500)
    me.manager = _mgr(mw, [w1, w2, me])
    me.ns = me.ns  # placed exactly on w1
    mw._Window._step_off_siblings(me, "n")
    f = me.ns.frame()
    assert (f.origin.x, f.origin.y + f.size.height) == (140, 460)


def test_step_off_siblings_noop_when_free():
    mw = _mw()
    w1, me = _sib(100, 500), _sib(300, 300)
    me.manager = _mgr(mw, [w1, me])
    mw._Window._step_off_siblings(me, "n")
    f = me.ns.frame()
    assert (f.origin.x, f.origin.y + f.size.height) == (300, 300)


def test_step_off_siblings_stops_when_ns_will_not_move():
    mw = _mw()
    w1 = _sib(100, 500)
    me = _sib(100, 500)
    me.ns.moves = False
    me.manager = _mgr(mw, [w1, me])
    mw._Window._step_off_siblings(me, "n")  # must return
    f = me.ns.frame()
    assert (f.origin.x, f.origin.y + f.size.height) == (100, 500)


def test_new_window_after_mru_touch_does_not_land_on_newest():
    # w2 sits one step off w1; w1 was touched last so it is LAST in _windows.
    mw = _mw()
    w1, w2 = _sib(100, 500), _sib(120, 480)
    me = _sib(0, 0)
    me.manager = _mgr(mw, [w2, w1, me])
    # cascade from w1 (the MRU one) puts me exactly on w2
    me.ns.cascadeTopLeftFromPoint_(types.SimpleNamespace(x=120, y=480))
    mw._Window._step_off_siblings(me, "n")
    f = me.ns.frame()
    assert (f.origin.x, f.origin.y + f.size.height) != (120, 480)
    assert not me.manager.placed_at("n", f.origin.x, f.origin.y + f.size.height, me)


# --- Dock click (`WindowManager.reopen`) -----------------------------------
# The Dock icon means Home: a Home window comes forward (key, else front,
# else MRU among Home windows), else one opens — an app window on top never
# swallows the click.


def _reopen_mgr(mw, windows, key=None, front=None):
    opened = []
    m = types.SimpleNamespace(
        _windows=windows, home_url="http://127.0.0.1:1/", open=opened.append
    )
    m.key = lambda: key
    m.front = lambda: front
    m._pick = lambda c: mw.WindowManager._pick(m, c)
    m._is_home = lambda w: getattr(w, "home", False)
    m.focus_or_open_home = lambda: mw.WindowManager.focus_or_open_home(m)
    return m, opened


def _shown_win(shown, name, home=True):
    return types.SimpleNamespace(ns=object(), home=home, show=lambda: shown.append(name))


def test_reopen_shows_the_only_home_window_and_opens_nothing():
    mw = _mw()
    shown = []
    m, opened = _reopen_mgr(mw, [_shown_win(shown, "a")])
    mw.WindowManager.reopen(m)
    assert shown == ["a"] and opened == []


def test_reopen_picks_the_key_home_window_among_several():
    mw = _mw()
    shown = []
    a, b, c = (_shown_win(shown, n) for n in "abc")
    m, opened = _reopen_mgr(mw, [a, b, c], key=b)
    mw.WindowManager.reopen(m)
    assert shown == ["b"] and opened == []


def test_reopen_raises_home_not_the_app_window_on_top():
    mw = _mw()
    shown = []
    home, app = _shown_win(shown, "home"), _shown_win(shown, "app", home=False)
    m, opened = _reopen_mgr(mw, [home, app], key=app, front=app)
    mw.WindowManager.reopen(m)
    assert shown == ["home"] and opened == []


def test_reopen_opens_home_when_only_app_windows_are_open():
    mw = _mw()
    shown = []
    m, opened = _reopen_mgr(mw, [_shown_win(shown, "app", home=False)])
    mw.WindowManager.reopen(m)
    assert shown == [] and opened == [m.home_url]


def test_reopen_opens_home_when_no_live_window():
    mw = _mw()
    closed = types.SimpleNamespace(ns=None, home=True, show=lambda: pytest.fail("closed window shown"))
    m, opened = _reopen_mgr(mw, [closed])
    mw.WindowManager.reopen(m)
    assert opened == [m.home_url]


# --- launcher Home row (`app.launcher_home_action`) ------------------------


def test_launcher_home_activates_and_focuses_or_opens_home():
    import fused_render.app as app_mod

    focused, activated = [], []
    manager = types.SimpleNamespace(focus_or_open_home=lambda: focused.append(True))
    nsapp = types.SimpleNamespace(activateIgnoringOtherApps_=activated.append)
    app_mod.launcher_home_action({"windows": manager}, "http://127.0.0.1:1/", nsapp)
    assert activated == [True] and focused == [True]


def test_launcher_home_without_a_manager_opens_a_browser_tab(monkeypatch):
    import fused_render.app as app_mod

    opened = []
    monkeypatch.setattr(app_mod.webbrowser, "open", opened.append)
    nsapp = types.SimpleNamespace(
        activateIgnoringOtherApps_=lambda _f: pytest.fail("no manager, no activate"))
    app_mod.launcher_home_action({"windows": None}, "http://127.0.0.1:1/", nsapp)
    assert opened == ["http://127.0.0.1:1/"]


def _dock_manager(mw):
    m = types.SimpleNamespace(home_url="http://h/", opened=[])
    m.open = m.opened.append
    m._menu_target = mw._MenuTarget.alloc().initWithManager_(m)
    return m


def test_dock_menu_is_one_new_window_item_on_the_main_menu_target():
    mw = _mw()
    m = _dock_manager(mw)
    menu = mw.WindowManager.dock_menu(m)
    items = list(menu.itemArray())
    assert [i.title() for i in items] == ["New Window"]
    assert items[0].action() == "newWindow:"
    assert items[0].keyEquivalent() == ""
    assert items[0].target() is m._menu_target


def test_dock_menu_is_built_once():
    mw = _mw()
    m = _dock_manager(mw)
    assert mw.WindowManager.dock_menu(m) is mw.WindowManager.dock_menu(m)


def test_new_window_activates_the_app_and_opens_home(monkeypatch):
    mw = _mw()
    m = _dock_manager(mw)
    calls = []
    monkeypatch.setattr(
        mw, "NSApp", types.SimpleNamespace(
            activateIgnoringOtherApps_=lambda f: calls.append(("activate", f))))
    m.open = lambda url: calls.append(("open", url))
    m._menu_target.newWindow_(None)
    assert calls == [("activate", True), ("open", "http://h/")]
