"""The macOS half of the Chromium engine: loading the unpacked framework and
giving `mac_window.py` a view that fits where its WKWebView goes.

`FusedChromium.framework` (source in `native/`) wraps the Chromium Embedded
Framework in one `NSView` subclass, `FRChromiumView`, whose surface mirrors
the WKWebView selectors `mac_window` uses (`URL`/`title` under KVO,
`loadRequest:`, `reload`, `goBack`, `evaluateJavaScript:completionHandler:`,
…) and one delegate protocol, `FRChromiumViewDelegate`, that asks the same
questions WebKit's navigation/UI/download delegates ask. `_ChromiumDelegate`
below answers them with the SAME `window_policy` calls `_WebDelegate` makes,
so a window behaves the same whichever engine draws it: external links go
to the browser, `target=_blank` opens a window, downloads land in
~/Downloads, `alert()` is an NSAlert, camera/mic of our own origin is
granted.

What differs, by design:

* one engine process tree per app (the browser process lives in-process; a
  renderer per window), started on the first Chromium window and shut down
  at quit (`shutdown`) — CEF cannot be initialised twice in a process, so
  switching the preference off does NOT shut it down;
* its own profile (`<base home>/chromium/profile`): Chromium's localStorage
  is not WebKit's, so the shell's state (sidebar, bookmarks cache) is per
  engine; the server-side state is shared as always;
* `evaluateJavaScript:` cannot return a value (CEF has no synchronous eval
  outside DevTools) — every caller in this app passes no completion;
* `window.print()` and the print panel are Chromium's own (it prints the
  calling frame itself), and "Inspect Element" opens Chromium's DevTools.

The delegate methods carry explicit Objective-C type signatures: the
framework declares integer and BOOL returns, and PyObjC would otherwise
infer `id` for a Python method and hand Chromium garbage.

macOS only; never imported at module level by anything that runs elsewhere.
"""
from __future__ import annotations

import logging
import os
import threading

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSMakeRect,
    NSModalResponseOK,
    NSObject,
    NSOpenPanel,
    NSTextField,
)

from fused_render import chromium, window_policy

logger = logging.getLogger(__name__)

# PyObjC type encodings (64-bit): q = NSInteger, Q = NSUInteger, Z = BOOL.
_SIG_POLICY_ACTION = b"q@:@@"
_SIG_POLICY_RESPONSE = b"q@:@@ZZ@"

# FRNavigationPolicy (FRChromiumView.h)
POLICY_ALLOW, POLICY_CANCEL, POLICY_DOWNLOAD = 0, 1, 2

# Chromium's context-menu commands arrive under WebKit's identifiers (the
# framework translates), so `window_policy.context_menu_item` applies as is.

_lock = threading.Lock()
_loaded: dict = {}   # "ok": bool, "error": str, "engine": FRChromiumEngine


def _sig(signature: bytes):
    """Decorator: a delegate method with an explicit ObjC type signature."""
    def wrap(fn):
        return objc.selector(fn, signature=signature)
    return wrap


# ---- loading ------------------------------------------------------------------

def available() -> bool:
    """True once the framework is loaded into this process and the engine
    started; False (and a logged reason) otherwise. Idempotent; the first
    call does the work, on the main thread."""
    with _lock:
        if "ok" in _loaded:
            return _loaded["ok"]
    try:
        _load()
        ok = True
    except Exception as exc:  # noqa: BLE001 — every failure means "use WebKit"
        logger.warning("chromium engine unavailable: %s", exc)
        with _lock:
            _loaded["ok"] = False
            _loaded["error"] = str(exc)
        return False
    with _lock:
        _loaded["ok"] = ok
    return ok


def load_error() -> str | None:
    return _loaded.get("error")


def _load() -> None:
    art = chromium.artifact()
    if art is None or not chromium.is_ready(art):
        raise RuntimeError("engine not on disk")
    engine_dir = chromium.engine_dir(art)
    fw = chromium.framework_path(art)
    objc.loadBundle("FusedChromium", globals(), bundle_path=fw)
    engine_cls = objc.lookUpClass("FRChromiumEngine")
    cache = os.path.join(chromium.engines_root(), "profile")
    os.makedirs(cache, exist_ok=True)
    from fused_render.mac_window import THEME_HANDLER_NAME, USER_AGENT_MARKER

    engine = engine_cls.startWithEngineDirectory_cachePath_userAgentProduct_scriptMessageHandlerNames_(
        engine_dir, cache, USER_AGENT_MARKER, [THEME_HANDLER_NAME])
    if engine is None:
        raise RuntimeError(engine_cls.lastError() or "CEF failed to start")
    with _lock:
        _loaded["engine"] = engine
    logger.info("chromium engine started: CEF %s from %s", engine.cefVersion(), engine_dir)


def engine():
    return _loaded.get("engine")


def shutdown() -> None:
    """At quit, after every window closed: CEF wants an orderly shutdown or
    it leaves helper processes behind."""
    eng = _loaded.pop("engine", None)
    if eng is not None:
        try:
            eng.shutdown()
        except Exception:  # noqa: BLE001 — exiting regardless
            logger.debug("chromium shutdown failed", exc_info=True)


def make_view(frame) -> object:
    """A fresh `FRChromiumView` for a window (no browser yet: `loadRequest:`
    or a popup adoption creates it)."""
    view_cls = objc.lookUpClass("FRChromiumView")
    return view_cls.alloc().initWithFrame_engine_(frame, engine())


def make_delegate(manager, window):
    return _ChromiumDelegate.alloc().initWithManager_window_(manager, window)


# ---- the delegate -----------------------------------------------------------------

class _ChromiumDelegate(NSObject):
    """`FRChromiumViewDelegate` + `NSWindowDelegate` + the KVO observer, one
    per window — the Chromium twin of `mac_window._WebDelegate`. Policy
    decisions are `window_policy`'s; window bookkeeping is the manager's."""

    def initWithManager_window_(self, manager, window):
        self = objc.super(_ChromiumDelegate, self).init()
        if self is None:
            return None
        self._manager = manager
        self._window = window
        self._crash_reloaded = False
        return self

    # ---- navigation -----------------------------------------------------------

    @_sig(_SIG_POLICY_ACTION)
    def chromiumView_decidePolicyForNavigationAction_(self, view, action):
        url = action.URL()
        url = str(url.absoluteString()) if url is not None else None
        from AppKit import NSEventModifierFlagCommand

        verdict = window_policy.navigation_action(
            url, self._manager.port,
            is_main_frame=bool(action.isMainFrame()),
            has_target_frame=bool(action.hasTargetFrame()),
            wants_download=bool(action.isDownload()),
            new_window_modifier=bool(int(action.modifierFlags()) & NSEventModifierFlagCommand)
            or int(action.buttonNumber()) == 2,
        )
        logger.debug("chromium navigation %s -> %s", url, verdict)
        if verdict == "allow":
            return POLICY_ALLOW
        if verdict == "download":
            return POLICY_DOWNLOAD
        if verdict == "new_window":
            self._manager.open(url)
        else:
            from fused_render.mac_window import _open_external

            _open_external(url)
        return POLICY_CANCEL

    @_sig(_SIG_POLICY_RESPONSE)
    def chromiumView_decidePolicyForResponseWithMIMEType_isMainFrame_canShow_contentDisposition_(
            self, view, mime, is_main, can_show, disposition):
        verdict = window_policy.response_action(
            is_main_frame=bool(is_main), can_show_mime=bool(can_show),
            content_disposition=str(disposition) if disposition else None)
        return POLICY_DOWNLOAD if verdict == "download" else POLICY_ALLOW

    @_sig(b"v@:@q@@")
    def chromiumView_didFailNavigationWithCode_URL_description_(self, view, code, url, desc):
        logger.warning("chromium navigation failed (%s): %s — %s", code, desc,
                       url.absoluteString() if url is not None else "?")

    @_sig(b"v@:@")
    def chromiumViewRenderProcessDidTerminate_(self, view):
        # Same contract as WebKit's: one automatic reload per window, ever.
        window = self._window
        label = (window.key or "home") if window is not None else "?"
        if self._crash_reloaded or window is None:
            logger.warning("chromium renderer died under window %s again; not reloading", label)
            return
        url = window.current_url()
        if not url:
            return
        self._crash_reloaded = True
        logger.warning("chromium renderer died under window %s; loading %s once", label, url)
        window.load(url)

    # ---- popups ------------------------------------------------------------------

    @_sig(b"@@:@@")
    def chromiumView_createViewForPopupWithURL_(self, view, url):
        url = str(url.absoluteString()) if url is not None else None
        kind = window_policy.classify(url, self._manager.port)
        if kind == "app":
            return self._manager.open_chromium_popup(url).webview
        if kind == "external":
            from fused_render.mac_window import _open_external

            _open_external(url)
        return None

    @_sig(b"v@:@")
    def chromiumViewDidRequestClose_(self, view):
        window = self._window
        if window is None:
            return
        logger.info("page asked to close its window (%s)", window.key or "home")
        from PyObjCTools import AppHelper

        AppHelper.callAfter(window.close)

    # ---- downloads -----------------------------------------------------------------

    @_sig(b"@@:@@@")
    def chromiumView_destinationForDownloadWithSuggestedFilename_URL_(self, view, suggested, url):
        from fused_render.mac_window import _downloads_dir

        dest = window_policy.download_destination(_downloads_dir(), str(suggested or ""))
        logger.info("download -> %s", dest)
        return dest

    @_sig(b"v@:@@")
    def chromiumView_downloadDidFinish_(self, view, path):
        from AppKit import NSDistributedNotificationCenter

        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_(
            "com.apple.DownloadFileFinished", None)

    @_sig(b"v@:@@@")
    def chromiumView_downloadDidFail_error_(self, view, path, message):
        logger.warning("download failed: %s", message)

    # ---- dialogs, pickers, permissions ------------------------------------------------

    @_sig(b"v@:@@")
    def chromiumView_runJavaScriptAlert_(self, view, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(message))
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    @_sig(b"Z@:@@")
    def chromiumView_runJavaScriptConfirm_(self, view, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(message))
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        return alert.runModal() == NSAlertFirstButtonReturn

    @_sig(b"@@:@@@")
    def chromiumView_runJavaScriptPrompt_defaultText_(self, view, prompt, default_text):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(prompt))
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        field.setStringValue_(str(default_text or ""))
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() == NSAlertFirstButtonReturn:
            return str(field.stringValue())
        return None

    @_sig(b"@@:@ZZ")
    def chromiumView_runOpenPanelAllowingMultiple_directories_(self, view, multiple, directories):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(bool(directories))
        panel.setAllowsMultipleSelection_(bool(multiple))
        if panel.runModal() == NSModalResponseOK:
            return [str(u.path()) for u in panel.URLs()]
        return None

    @_sig(b"Z@:@@@")
    def chromiumView_allowPermission_forOrigin_(self, view, kind, origin):
        # Camera/mic/geolocation from our own origin: granted (TCC still
        # gates the hardware). Anything else: denied — same as WebKit's path
        # for geolocation; for media WebKit showed its own prompt, which
        # Chromium does not offer here.
        own = False
        if origin is not None:
            port = origin.port()
            own = window_policy.is_own_origin(
                str(origin.host() or ""), int(port) if port is not None else None,
                self._manager.port)
        logger.info("%s permission request from %s -> %s", kind,
                    origin.absoluteString() if origin is not None else "?",
                    "grant" if own else "deny")
        return own

    # ---- the right-click menu -----------------------------------------------------------

    @_sig(b"@@:@@@@@@")
    def chromiumView_titleForContextMenuItem_linkURL_imageURL_mediaURL_proposedTitle_(
            self, view, identifier, link, image, media, title):
        def s(u):
            return str(u.absoluteString()) if u is not None else None
        try:
            verdict, new_title = window_policy.context_menu_item(
                str(identifier) if identifier else None, self._manager.port,
                image_url=s(image), link_url=s(link), media_url=s(media))
        except Exception:  # noqa: BLE001 — never lose the whole menu to one item
            logger.exception("context menu curation failed; keeping %s", identifier)
            return title
        if verdict == "drop":
            return None
        if verdict == "retitle":
            return new_title
        return title

    # ---- the page's bridge ---------------------------------------------------------------

    @_sig(b"v@:@@@")
    def chromiumView_didReceiveScriptMessage_body_(self, view, name, body):
        from fused_render.mac_window import THEME_HANDLER_NAME, THEME_PREFS

        try:
            if str(name) != THEME_HANDLER_NAME:
                return
            pref = str(body)
            window = self._window
            if pref in THEME_PREFS and window is not None:
                window.set_theme_pref(pref)
        except Exception:  # noqa: BLE001 — a bad message must not hurt the app
            logger.debug("script message failed", exc_info=True)

    @_sig(b"v@:@Z")
    def chromiumView_fullscreenDidChange_(self, view, on):
        window = self._window
        if window is None or window.ns is None:
            return
        # `requestFullscreen()` → the window goes full screen like Safari's;
        # exiting (Esc) brings it back. 16384 = NSWindowStyleMaskFullScreen.
        is_full = bool(int(window.ns.styleMask()) & (1 << 14))
        if bool(on) != is_full:
            window.ns.toggleFullScreen_(None)

    # ---- KVO: title and URL follow the page (shared with _WebDelegate) ----------------

    def observeValueForKeyPath_ofObject_change_context_(self, key, obj, change, ctx):
        from fused_render.mac_window import follow_page

        follow_page(self._manager, self._window, key, obj)

    # ---- NSWindowDelegate -----------------------------------------------------------------

    def windowWillClose_(self, notification):
        self._manager._forget(self._window)

    def windowDidBecomeKey_(self, notification):
        self._manager._touch(self._window)
