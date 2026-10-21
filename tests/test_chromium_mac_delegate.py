"""`fused_render.chromium.mac._ChromiumDelegate` answers Chromium's questions
with the same `window_policy` verdicts `_WebDelegate` gives WebKit. The
module needs `objc`/`AppKit`; here they are stubbed so the mapping is
checked on every OS (the real framework cannot be loaded off macOS)."""
import sys
import types

import pytest

from fused_render import window_policy

PORT = 1777


@pytest.fixture
def mac(monkeypatch):
    """Import `fused_render.chromium.mac` against stub `objc` / `AppKit`."""
    objc = types.ModuleType("objc")
    objc.selector = lambda fn, selector=None, signature=None: fn
    objc.super = super
    objc.loadBundle = lambda *a, **k: None
    objc.lookUpClass = lambda name: None

    class NSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    appkit = types.ModuleType("AppKit")
    appkit.NSObject = NSObject
    appkit.NSEventModifierFlagCommand = 1 << 20
    for name in ("NSAlert", "NSAlertFirstButtonReturn", "NSMakeRect", "NSModalResponseOK",
                 "NSOpenPanel", "NSTextField", "NSDistributedNotificationCenter"):
        setattr(appkit, name, object())
    for name, mod in (("objc", objc), ("AppKit", appkit)):
        monkeypatch.setitem(sys.modules, name, mod)
    # mac_window imports WebKit; the delegate only needs these names from it
    mw = types.ModuleType("fused_render.mac_window")
    mw.THEME_HANDLER_NAME = "fusedTheme"
    mw.THEME_PREFS = ("light", "dark", "system")
    mw.USER_AGENT_MARKER = "FusedRender/0"
    mw.opened_external = []
    mw._open_external = mw.opened_external.append
    mw._downloads_dir = lambda: "/Users/x/Downloads"
    mw.follow_page = lambda manager, window, key, view: window.followed.append(key)
    monkeypatch.setitem(sys.modules, "fused_render.mac_window", mw)
    monkeypatch.delitem(sys.modules, "fused_render.chromium.mac", raising=False)
    # `from fused_render.chromium import mac` elsewhere must not get this
    # stub-backed module after the test: the package attribute is restored too.
    import fused_render.chromium as pkg

    monkeypatch.delattr(pkg, "mac", raising=False)
    import importlib

    import fused_render.chromium.mac as m

    importlib.reload(m)
    # Registered through monkeypatch so teardown removes the stub-backed module
    # (delitem/delattr above record nothing when the key was absent).
    monkeypatch.setitem(sys.modules, "fused_render.chromium.mac", m)
    monkeypatch.setattr(pkg, "mac", m, raising=False)
    m._loaded.clear()
    m._mw = mw
    return m


def _url(s):
    return None if s is None else types.SimpleNamespace(
        absoluteString=lambda: s, host=lambda: s.split("//")[1].split(":")[0].split("/")[0],
        port=lambda: (int(s.split(":")[2].split("/")[0]) if s.count(":") >= 2 else None),
        path=lambda: s)


def _action(url, main=True, target=True, download=False, flags=0, button=0):
    return types.SimpleNamespace(
        URL=lambda: _url(url), isMainFrame=lambda: main, hasTargetFrame=lambda: target,
        isDownload=lambda: download, modifierFlags=lambda: flags, buttonNumber=lambda: button)


def _delegate(mac):
    opened = []
    popups = []
    manager = types.SimpleNamespace(port=PORT, open=opened.append,
                                    open_chromium_popup=lambda u: popups.append(u) or
                                    types.SimpleNamespace(webview="popup-view"),
                                    _forget=lambda w: None, _touch=lambda w: None)
    window = types.SimpleNamespace(key="/a", view="embed", ns=None, followed=[],
                                   loads=[], themes=[])
    window.current_url = lambda: "http://127.0.0.1:1777/x"
    window.load = window.loads.append
    window.set_theme_pref = window.themes.append
    d = mac._ChromiumDelegate.alloc().initWithManager_window_(manager, window)
    return d, manager, window, opened, popups


# ---- navigation ----------------------------------------------------------------

def test_own_origin_navigation_is_allowed(mac):
    d, *_ = _delegate(mac)
    assert d.chromiumView_decidePolicyForNavigationAction_(
        None, _action(f"http://127.0.0.1:{PORT}/view/x")) == mac.POLICY_ALLOW


def test_external_link_goes_to_the_browser_and_is_cancelled(mac):
    d, *_ = _delegate(mac)
    assert d.chromiumView_decidePolicyForNavigationAction_(
        None, _action("https://example.com/")) == mac.POLICY_CANCEL
    assert mac._mw.opened_external == ["https://example.com/"]


def test_target_blank_opens_a_new_window(mac):
    d, manager, window, opened, _ = _delegate(mac)
    url = f"http://127.0.0.1:{PORT}/app"
    assert d.chromiumView_decidePolicyForNavigationAction_(
        None, _action(url, target=False)) == mac.POLICY_CANCEL
    assert opened == [url]


def test_command_click_opens_a_new_window(mac):
    d, manager, window, opened, _ = _delegate(mac)
    url = f"http://127.0.0.1:{PORT}/app"
    flags = sys.modules["AppKit"].NSEventModifierFlagCommand
    assert d.chromiumView_decidePolicyForNavigationAction_(
        None, _action(url, flags=flags)) == mac.POLICY_CANCEL
    assert opened == [url]


def test_download_disposition_becomes_a_download(mac):
    d, *_ = _delegate(mac)
    assert d.chromiumView_decidePolicyForNavigationAction_(
        None, _action(f"http://127.0.0.1:{PORT}/f.zip", download=True)) == mac.POLICY_DOWNLOAD


def test_response_policy_mirrors_window_policy(mac):
    d, *_ = _delegate(mac)
    assert d.chromiumView_decidePolicyForResponseWithMIMEType_isMainFrame_canShow_contentDisposition_(
        None, "application/zip", True, False, None) == mac.POLICY_DOWNLOAD
    assert d.chromiumView_decidePolicyForResponseWithMIMEType_isMainFrame_canShow_contentDisposition_(
        None, "text/html", True, True, "attachment; filename=x") == mac.POLICY_DOWNLOAD
    assert d.chromiumView_decidePolicyForResponseWithMIMEType_isMainFrame_canShow_contentDisposition_(
        None, "text/html", True, True, None) == mac.POLICY_ALLOW


# ---- popups ---------------------------------------------------------------------

def test_window_open_of_our_page_gets_a_popup_view(mac):
    d, manager, window, opened, popups = _delegate(mac)
    url = f"http://127.0.0.1:{PORT}/view/p"
    assert d.chromiumView_createViewForPopupWithURL_(None, _url(url)) == "popup-view"
    assert popups == [url]


def test_window_open_of_external_url_goes_to_the_browser_no_popup(mac):
    d, *_ = _delegate(mac)
    assert d.chromiumView_createViewForPopupWithURL_(None, _url("https://x.test/")) is None
    assert mac._mw.opened_external == ["https://x.test/"]


def test_window_open_of_blob_url_gets_nothing(mac):
    d, *_ = _delegate(mac)
    assert d.chromiumView_createViewForPopupWithURL_(None, _url("blob:abc")) is None
    assert mac._mw.opened_external == []


# ---- downloads, permissions, menu -------------------------------------------------

def test_download_destination_is_in_downloads_and_unique(mac, tmp_path):
    d, *_ = _delegate(mac)
    mac._mw._downloads_dir = lambda: str(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    dest = d.chromiumView_destinationForDownloadWithSuggestedFilename_URL_(None, "a.txt", None)
    assert dest == window_policy.download_destination(str(tmp_path), "a.txt")
    assert dest != str(tmp_path / "a.txt")


def test_permissions_granted_for_our_origin_only(mac):
    d, *_ = _delegate(mac)
    assert d.chromiumView_allowPermission_forOrigin_(None, "camera", _url(f"http://127.0.0.1:{PORT}/"))
    assert not d.chromiumView_allowPermission_forOrigin_(None, "camera", _url("https://evil.test/"))
    assert not d.chromiumView_allowPermission_forOrigin_(None, "geolocation", None)


def test_context_menu_items_follow_window_policy(mac):
    d, *_ = _delegate(mac)
    f = d.chromiumView_titleForContextMenuItem_linkURL_imageURL_mediaURL_proposedTitle_
    own = _url(f"http://127.0.0.1:{PORT}/x")
    # our page: kept with its title; external: retitled; blob: dropped; others untouched
    assert f(None, "WKMenuItemIdentifierOpenLinkInNewWindow", own, None, None, "Open Link in New Window") == "Open Link in New Window"
    assert f(None, "WKMenuItemIdentifierOpenLinkInNewWindow", _url("https://e.test/"), None, None, "Open Link in New Window") == "Open Link in Browser"
    assert f(None, "WKMenuItemIdentifierOpenImageInNewWindow", None, _url("blob:x"), None, "Open Image in New Window") is None
    assert f(None, "WKMenuItemIdentifierCopy", None, None, None, "Copy") == "Copy"


# ---- bridge, crash, close, KVO -------------------------------------------------------

def test_theme_message_reaches_the_window(mac):
    d, manager, window, *_ = _delegate(mac)
    d.chromiumView_didReceiveScriptMessage_body_(None, "fusedTheme", "dark")
    d.chromiumView_didReceiveScriptMessage_body_(None, "fusedTheme", "purple")
    d.chromiumView_didReceiveScriptMessage_body_(None, "other", "dark")
    assert window.themes == ["dark"]


def test_renderer_crash_reloads_once(mac):
    d, manager, window, *_ = _delegate(mac)
    d.chromiumViewRenderProcessDidTerminate_(None)
    d.chromiumViewRenderProcessDidTerminate_(None)
    assert window.loads == ["http://127.0.0.1:1777/x"]


def test_kvo_goes_through_the_shared_follow_page(mac):
    d, manager, window, *_ = _delegate(mac)
    d.observeValueForKeyPath_ofObject_change_context_("URL", None, None, None)
    assert window.followed == ["URL"]


def test_delegate_methods_carry_explicit_objc_signatures():
    """PyObjC infers `id` returns for Python methods; Chromium expects
    NSInteger / BOOL from the policy and permission answers. Every delegate
    method must be declared through `_sig`."""
    import ast
    import inspect
    import pathlib

    src = pathlib.Path(inspect.getsourcefile(window_policy)).parent / "chromium" / "mac.py"
    tree = ast.parse(src.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_ChromiumDelegate")
    missing = [n.name for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name.startswith("chromiumView") and not n.decorator_list]
    assert missing == []


# ---- availability -------------------------------------------------------------------

def test_available_is_false_and_sticky_when_the_engine_is_not_on_disk(mac, monkeypatch):
    from fused_render import chromium

    monkeypatch.setattr(chromium, "artifact", lambda *a, **k: None)
    assert mac.available() is False
    assert "not on disk" in mac.load_error()
    # sticky: a later call does not retry (CEF init is once per process)
    monkeypatch.setattr(chromium, "artifact", lambda *a, **k: (_ for _ in ()).throw(AssertionError("retried")))
    assert mac.available() is False
