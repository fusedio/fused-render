"""Native titlebar colour follows the in-app theme (fused_render/mac_window.py).

The titlebar is a transparent titlebar over a dynamic window background equal
to the `--bg` token; the shell posts its preference to the `fusedTheme`
script-message handler and the window's NSAppearance follows it.
"""
import re
import sys

import pytest

from _theme_sources import read_repo_file

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit only")


def _mw():
    pytest.importorskip("AppKit")
    pytest.importorskip("WebKit")
    from fused_render import mac_window

    return mac_window


def _rgb(color):
    c = color.colorUsingColorSpace_(
        __import__("AppKit").NSColorSpace.sRGBColorSpace())
    return tuple(round(v * 255) for v in (c.redComponent(), c.greenComponent(), c.blueComponent()))


def test_titlebar_colors_match_the_bg_tokens():
    tokens = read_repo_file("frontend/src/styles/tokens.css")
    mw = _mw()
    for rgb in mw.TITLEBAR_BG.values():
        assert "#%02x%02x%02x" % rgb in tokens.lower()


def test_dynamic_color_resolves_per_appearance():
    mw = _mw()
    from AppKit import NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua

    color = mw._dynamic_titlebar_color()
    for name, expect in ((NSAppearanceNameDarkAqua, (0x13, 0x14, 0x17)),
                         (NSAppearanceNameAqua, (255, 255, 255))):
        NSAppearance.setCurrentAppearance_(NSAppearance.appearanceNamed_(name))
        assert _rgb(color) == expect


def test_appearance_for_pref():
    mw = _mw()
    from AppKit import NSAppearanceNameAqua, NSAppearanceNameDarkAqua

    assert mw.appearance_for_pref("system") is None
    assert mw.appearance_for_pref("dark").name() == NSAppearanceNameDarkAqua
    assert mw.appearance_for_pref("light").name() == NSAppearanceNameAqua


def test_window_has_transparent_titlebar_and_follows_pref():
    mw = _mw()
    from AppKit import NSAppearanceNameDarkAqua, NSWindowStyleMaskFullSizeContentView

    class _Mgr:
        _windows = []
        _menu_target = None

        def _forget(self, *a):
            pass

        def front(self, *a):
            pass

    win = mw._Window(_Mgr(), "about:blank", mw.WKWebViewConfiguration.alloc().init(),
                     load=False)
    try:
        assert win.ns.titlebarAppearsTransparent()
        assert not win.ns.styleMask() & NSWindowStyleMaskFullSizeContentView
        win.set_theme_pref("dark")
        assert win.ns.appearance().name() == NSAppearanceNameDarkAqua
        win.set_theme_pref("system")
        assert win.ns.appearance() is None
    finally:
        win.ns.close()


def test_configuration_registers_the_theme_handler():
    mw = _mw()
    src = read_repo_file("fused_render/mac_window.py")
    assert re.search(r"addScriptMessageHandler_name_\(\s*self\._theme_bridge, THEME_HANDLER_NAME",
                     src)
    assert mw.THEME_HANDLER_NAME == "fusedTheme"


def test_frontend_posts_the_pref_from_the_top_level_only():
    for path in ("frontend/src/platform/lib/theme.ts", "frontend/index.html"):
        src = read_repo_file(path)
        assert "messageHandlers" in src and "fusedTheme" in src, path
        assert "window.parent === window" in src or "window.parent !== window" in src, path
