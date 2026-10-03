"""The app's windows on macOS — native NSWindows hosting the shell, not browser tabs.

Before this module the packaged app was a menu-bar process that pushed every
surface into the default browser: the home page, a Finder-opened file, a Dock
click, a deep link. Now each opens (or focuses) a window of this app: an
`NSWindow` whose content view is a `WKWebView` pointed at the one in-process
server. Any number of windows, all on that one server, sharing one
`WKWebsiteDataStore` (the shell's localStorage is one set across windows, and
`storage` events keep them in step the way browser tabs were) and one
`WKProcessPool`.

ONE SHELL WINDOW, MORE ON DEMAND. The home window runs the shell SPA; its
sidebar and in-page links navigate inside it, exactly as in a browser tab. A
second window comes from what would have been a second tab: `target=_blank`,
`window.open`, ⌘-click / middle-click on an app link, a Finder open, a deep
link, the launcher.

APPS RUN IN WINDOWS OF THEIR OWN. Inside a native window the shell knows it
is one (the ``FusedRender/`` user-agent marker, router.ts
``IS_NATIVE_WINDOW``): an app click — a card, the app page's Open — asks the
server (POST /api/windows/open) to focus-or-open that app's window, which
runs its entry page as a chrome-free embed (`window_policy.app_window_path`)
under its own saved size and place; the launcher's pick lands the same way.
The title bar's Edit button (⌘⇧E) switches the SAME window to the explorer
view of what it runs (`window_policy.edit_target`).

What the browser used to do for a page, the delegates here do instead
(`window_policy.py` holds the decisions; this module enacts them):

- `target=_blank`, `window.open`, ⌘-click / middle-click on an app link →
  a NEW WINDOW.
- an external http(s) link → the DEFAULT BROWSER, never a window of ours.
- `<a download>`, `Content-Disposition: attachment`, un-showable MIME types →
  saved into ~/Downloads under a Finder-style unique name.
- `alert()` / `confirm()` / `prompt()` → NSAlert; `<input type=file>` →
  NSOpenPanel; camera/mic and geolocation requests from the app's own
  origin → granted (the system TCC prompt still gates the hardware);
  `requestFullscreen` enabled; `requestPointerLock` granted;
  `window.close()` closes the window.
- the right-click menu is WebKit's, curated (`window_policy.context_menu_item`):
  "Open … in New Window" says "in Browser" when that is where the URL goes
  and vanishes for a `blob:`/`data:` URL that goes nowhere; "Download Image"
  and friends save like any other download instead of silently dropping.

A window's IDENTITY — the app folder or file it shows, what focus-or-open
and the launcher's running dot key on — is read LIVE from the web view's
URL (KVO on ``URL``), never cached from a navigation: the shell is an SPA,
and its `pushState` navigations never pass through the policy delegate.

A main menu is installed too (rumps never builds one): without an Edit menu
a WKWebView has no ⌘C/⌘V/⌘X/⌘Z/⌘A, and there would be no ⌘W/⌘N/⌘R/⌘P/⌘M.

macOS-only. `app.py` imports this lazily inside `main()` and falls back to
`webbrowser.open` if construction fails — the app is never left without a
surface. Every method must run on the main thread; `app.py` hops with
`PyObjCTools.AppHelper.callAfter`.

Ported from Render App (fused-render-lite `mainwindow.py`); web
notifications and the `.fused`-file identity did not come along, and Edit
opens this app's own explorer instead of handing off to another app.
"""
from __future__ import annotations

import logging
import os
import subprocess
import urllib.parse
import webbrowser

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApp,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSBezelStyleRecessed,
    NSButton,
    NSControlSizeLarge,
    NSImage,
    NSLayoutAttributeTrailing,
    NSTitlebarAccessoryViewController,
    NSView,
    NSDistributedNotificationCenter,
    NSDownloadsDirectory,
    NSEventModifierFlagCommand,
    NSMakePoint,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSModalResponseOK,
    NSObject,
    NSOpenPanel,
    NSPrintInfo,
    NSSearchPathForDirectoriesInDomains,
    NSTextField,
    NSURL,
    NSUserDomainMask,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import NSKeyValueObservingOptionNew, NSThread, NSURLRequest
from WebKit import (
    WKNavigationActionPolicyAllow,
    WKNavigationActionPolicyCancel,
    WKNavigationActionPolicyDownload,
    WKNavigationResponsePolicyAllow,
    WKNavigationResponsePolicyDownload,
    WKPermissionDecisionDeny,
    WKPermissionDecisionGrant,
    WKPermissionDecisionPrompt,
    WKProcessPool,
    WKWebsiteDataStore,
    WKWebView,
    WKWebViewConfiguration,
)

from fused_render import __version__, window_policy
from fused_render._view_url_codec import app_page_path, view_url_path
from fused_render.logs import log_path

logger = logging.getLogger(__name__)

APP_NAME = "Fused Render"
DEFAULT_SIZE = (1280, 840)
MIN_SIZE = (560, 360)
# Rides on WebKit's own UA so a page can tell "inside the app" from "a browser".
USER_AGENT_MARKER = f"FusedRender/{__version__}"

_SHIFT = 1 << 17
_CTRL = 1 << 18
_ALT = 1 << 19


def _nsurl(url: str):
    return NSURL.URLWithString_(url)


def _open_external(url: str) -> None:
    """Hand a URL to the default browser via LaunchServices."""
    if not NSWorkspace.sharedWorkspace().openURL_(_nsurl(url)):
        logger.warning("NSWorkspace refused %s; falling back to webbrowser", url)
        webbrowser.open(url)


def _downloads_dir() -> str:
    found = NSSearchPathForDirectoriesInDomains(NSDownloadsDirectory, NSUserDomainMask, True)
    return str(found[0]) if found else os.path.expanduser("~/Downloads")


def _hit_test_urls(element) -> dict | None:
    """The image / link / media URLs under a right-click, from the private
    `_WKContextMenuElementInfo.hitTestResult` (`_WKHitTestResult`). None when
    WebKit offers no hit test — the caller then leaves the menu alone."""
    if element is None or not element.respondsToSelector_(b"hitTestResult"):
        return None
    hit = element.hitTestResult()
    if hit is None:
        return None
    out = {}
    for key, sel in (("image_url", b"absoluteImageURL"),
                     ("link_url", b"absoluteLinkURL"),
                     ("media_url", b"absoluteMediaURL")):
        if not hit.respondsToSelector_(sel):
            return None
        url = getattr(hit, sel.decode())()
        out[key] = str(url.absoluteString()) if url is not None else None
    return out


# ---- private WKUIDelegate selectors ------------------------------------------
#
# Pointer lock and geolocation have no public WKUIDelegate method on macOS:
# WebKit asks through `WKUIDelegatePrivate` selectors (leading underscore),
# and a host that lacks them gets a silent deny. PyObjC ships no block
# metadata for private selectors, so without the registrations below the
# completion blocks would arrive as ``cannot call block without a signature``
# — the same failure the class docstring describes for `protocols=`. Shapes
# copied from WebKit/_metadata.py (requestMediaCapturePermissionForOrigin…);
# argument indices count self and _cmd, so the web view is 2. Block param
# types from WKUIDelegatePrivate.h: ``void (^)(BOOL)`` for pointer lock
# (PyObjC spells BOOL ``Z``), ``void (^)(WKPermissionDecision)`` (NSInteger,
# ``q``) for geolocation. Registered on NSObject like PyObjC's own entries,
# which is where the plain-NSObject-subclass delegate picks them up.

_SEL_POINTER_LOCK_REQUEST = b"_webViewDidRequestPointerLock:completionHandler:"
_SEL_POINTER_LOCK_LOST = b"_webViewDidLosePointerLock:"
_SEL_GEOLOCATION = (b"_webView:requestGeolocationPermissionForOrigin:"
                    b"initiatedByFrame:decisionHandler:")
_SEL_DID_CLOSE = b"webViewDidClose:"
# The right-click menu (WKUIDelegatePrivate): WebKit proposes its default
# NSMenu and takes back the one to show. Context-menu downloads ("Download
# Image", "Download Linked File") never pass the public didBecomeDownload
# hooks; WKNavigationDelegatePrivate hands them over through the second
# selector, and a WKDownload nobody adopts has no delegate to pick a
# destination, so it silently goes nowhere.
_SEL_CONTEXT_MENU = (b"_webView:getContextMenuFromProposedMenu:forElement:"
                     b"userInfo:completionHandler:")
_SEL_CONTEXT_MENU_DOWNLOAD = b"_webView:contextMenuDidCreateDownload:"

objc.registerMetaDataForSelector(
    b"NSObject",
    _SEL_POINTER_LOCK_REQUEST,
    {
        "required": False,
        "retval": {"type": b"v"},
        "arguments": {
            2: {"type": b"@"},
            3: {
                "callable": {
                    "retval": {"type": b"v"},
                    "arguments": {0: {"type": b"^v"}, 1: {"type": b"Z"}},
                },
                "type": b"@?",
            },
        },
    },
)
objc.registerMetaDataForSelector(
    b"NSObject",
    _SEL_GEOLOCATION,
    {
        "required": False,
        "retval": {"type": b"v"},
        "arguments": {
            2: {"type": b"@"},
            3: {"type": b"@"},
            4: {"type": b"@"},
            5: {
                "callable": {
                    "retval": {"type": b"v"},
                    "arguments": {0: {"type": b"^v"}, 1: {"type": b"q"}},
                },
                "type": b"@?",
            },
        },
    },
)
objc.registerMetaDataForSelector(
    b"NSObject",
    _SEL_CONTEXT_MENU,
    {
        "required": False,
        "retval": {"type": b"v"},
        "arguments": {
            2: {"type": b"@"},
            3: {"type": b"@"},
            4: {"type": b"@"},
            5: {"type": b"@"},
            6: {
                "callable": {
                    "retval": {"type": b"v"},
                    "arguments": {0: {"type": b"^v"}, 1: {"type": b"@"}},
                },
                "type": b"@?",
            },
        },
    },
)


def _private(selector: bytes, signature: bytes):
    """Decorator: bind a method to an underscore-prefixed Objective-C
    selector verbatim. PyObjC's name mangling (``_`` ↔ ``:``) would turn
    ``_webViewDidLosePointerLock_`` into ``:webViewDidLosePointerLock:``."""
    def wrap(fn):
        return objc.selector(fn, selector=selector, signature=signature)
    return wrap


class _WebDelegate(NSObject):
    """One per window: navigation + UI + download delegate of its web view,
    and the window's own delegate (close → forget the window). WebKit keeps
    only a WEAK reference to delegates, so `_Window` holds this strongly.

    Deliberately NOT declared with ``protocols=[WKNavigationDelegate, …]``:
    on PyObjC 12 that declaration makes the completion-handler blocks arrive
    WITHOUT a signature (``cannot call block without a signature`` on the
    first navigation), while a plain NSObject subclass picks up the block
    metadata PyObjC registers for these selectors on NSObject. Measured in
    Render App on its first launch, not guessed."""

    def initWithManager_window_(self, manager, window):
        self = objc.super(_WebDelegate, self).init()
        if self is None:
            return None
        self._manager = manager
        self._window = window  # the _Window record, not the NSWindow
        self._downloads = []   # strong refs: a WKDownload's delegate is weak too
        return self

    # ---- navigation policy -------------------------------------------------

    def webView_decidePolicyForNavigationAction_decisionHandler_(
            self, webview, action, decision):
        request = action.request()
        url = str(request.URL().absoluteString()) if request and request.URL() else None
        target = action.targetFrame()
        source = action.sourceFrame()
        is_main = bool(target.isMainFrame()) if target is not None else \
            (source is None or bool(source.isMainFrame()))
        flags = int(action.modifierFlags())
        verdict = window_policy.navigation_action(
            url, self._manager.port,
            is_main_frame=is_main,
            has_target_frame=target is not None,
            wants_download=bool(action.shouldPerformDownload()),
            new_window_modifier=bool(flags & NSEventModifierFlagCommand)
            or int(action.buttonNumber()) == 2,
        )
        logger.debug("navigation %s -> %s", url, verdict)
        if verdict == "allow":
            decision(WKNavigationActionPolicyAllow)
        elif verdict == "download":
            decision(WKNavigationActionPolicyDownload)
        elif verdict == "new_window":
            self._manager.open(url)
            decision(WKNavigationActionPolicyCancel)
        else:  # open_external
            _open_external(url)
            decision(WKNavigationActionPolicyCancel)

    def webView_decidePolicyForNavigationResponse_decisionHandler_(
            self, webview, nav_response, decision):
        response = nav_response.response()
        disposition = None
        if response.respondsToSelector_(b"allHeaderFields"):
            for key in response.allHeaderFields():
                if str(key).lower() == "content-disposition":
                    disposition = str(response.allHeaderFields()[key])
                    break
        verdict = window_policy.response_action(
            is_main_frame=bool(nav_response.isForMainFrame()),
            can_show_mime=bool(nav_response.canShowMIMEType()),
            content_disposition=disposition,
        )
        decision(WKNavigationResponsePolicyDownload if verdict == "download"
                 else WKNavigationResponsePolicyAllow)

    def webView_didFailProvisionalNavigation_withError_(self, webview, nav, error):
        # -999 is "cancelled": every navigation turned into a new window, a
        # browser hand-off or a download reports as one. Not a failure.
        if error is not None and int(error.code()) != -999:
            logger.warning("navigation failed: %s", error.localizedDescription())

    # ---- downloads (WKDownloadDelegate) ------------------------------------

    def webView_navigationAction_didBecomeDownload_(self, webview, action, download):
        self._adopt_download(download)

    def webView_navigationResponse_didBecomeDownload_(self, webview, response, download):
        self._adopt_download(download)

    @_private(_SEL_CONTEXT_MENU_DOWNLOAD, b"v@:@@")
    def webView_contextMenuDidCreateDownload_(self, webview, download):
        # Right-click → Download Image / Download Linked File / Download
        # Media. Same WKDownload as a link's; same ~/Downloads destination.
        self._adopt_download(download)

    def _adopt_download(self, download) -> None:
        self._downloads.append(download)
        download.setDelegate_(self)

    def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(
            self, download, response, suggested, completion):
        dest = window_policy.download_destination(_downloads_dir(), str(suggested or ""))
        logger.info("download -> %s", dest)
        completion(NSURL.fileURLWithPath_(dest))

    def downloadDidFinish_(self, download):
        self._downloads = [d for d in self._downloads if d is not download]
        # Bounce the Downloads stack in the Dock, the way Safari does.
        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_(
            "com.apple.DownloadFileFinished", None)

    def download_didFailWithError_resumeData_(self, download, error, resume_data):
        self._downloads = [d for d in self._downloads if d is not download]
        logger.warning("download failed: %s", error.localizedDescription() if error else "?")

    # ---- popups, dialogs, pickers, permissions (WKUIDelegate) --------------

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, webview, configuration, action, features):
        # `window.open(url)`: the policy delegate has not seen this URL yet.
        request = action.request()
        url = str(request.URL().absoluteString()) if request and request.URL() else None
        kind = window_policy.classify(url, self._manager.port)
        if kind == "app":
            # A real popup, like a browser's: built on the configuration
            # WebKit handed us (same process as the opener), so the opener
            # gets a live handle back — `popup.postMessage`, `popup.close()`,
            # `window.opener` all work, instead of `window.open` → null.
            return self._manager.open_popup(url, configuration).webview
        if kind == "external":
            _open_external(url)
        # None: we made no view for it; the opener's `window.open` gets null.
        return None

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(
            self, webview, message, frame, completion):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(message))
        alert.addButtonWithTitle_("OK")
        alert.runModal()
        completion()

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
            self, webview, message, frame, completion):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(message))
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        completion(alert.runModal() == NSAlertFirstButtonReturn)

    def webView_runJavaScriptTextInputPanelWithPrompt_defaultText_initiatedByFrame_completionHandler_(
            self, webview, prompt, default_text, frame, completion):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(prompt))
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        field.setStringValue_(str(default_text or ""))
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() == NSAlertFirstButtonReturn:
            completion(field.stringValue())
        else:
            completion(None)

    def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(
            self, webview, parameters, frame, completion):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(bool(parameters.allowsDirectories()))
        panel.setAllowsMultipleSelection_(bool(parameters.allowsMultipleSelection()))
        # NSModalResponseOK (1), not NSAlertFirstButtonReturn (1000): panels
        # and alerts answer on different scales.
        if panel.runModal() == NSModalResponseOK:
            completion(list(panel.URLs()))
        else:
            completion(None)

    def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(
            self, webview, origin, frame, capture_type, decision):
        # Our own page asked (an app using the webcam or mic): grant — the
        # system TCC prompt still gates the hardware (the bundle's
        # NSCamera/NSMicrophoneUsageDescription strings). A third-party
        # iframe inside an app gets WebKit's own prompt.
        own = self._own_origin(origin)
        decision(WKPermissionDecisionGrant if own else WKPermissionDecisionPrompt)

    def _own_origin(self, origin) -> bool:
        manager = self._manager
        if manager is None or origin is None:
            return False
        return window_policy.is_own_origin(
            str(origin.host() or ""), origin.port(), manager.port)

    # ---- private WKUIDelegate: pointer lock, geolocation ---------------------

    @_private(_SEL_GEOLOCATION, b"v@:@@@@?")
    def webView_requestGeolocationPermissionForOrigin_initiatedByFrame_decisionHandler_(
            self, webview, origin, frame, decision):
        # First of two gates: WebKit asks us, then CoreLocation asks the OS
        # (TCC; the bundle's NSLocationWhenInUseUsageDescription). A page of
        # ours is granted here like camera/mic; anything else is denied — a
        # browser would have shown its own prompt, which WebKit does not
        # offer for geolocation.
        own = self._own_origin(origin)
        logger.info("geolocation request from %s:%s -> %s",
                    origin.host() if origin else "?", origin.port() if origin else "?",
                    "grant" if own else "deny")
        decision(WKPermissionDecisionGrant if own else WKPermissionDecisionDeny)

    @_private(_SEL_POINTER_LOCK_REQUEST, b"v@:@@?")
    def webViewDidRequestPointerLock_completionHandler_(self, webview, completion):
        # `canvas.requestPointerLock()`. Needs a user gesture on the page
        # already; the browser granted it silently too.
        logger.debug("pointer lock requested: granted")
        completion(True)

    @_private(_SEL_POINTER_LOCK_LOST, b"v@:@")
    def webViewDidLosePointerLock_(self, webview):
        # Esc / focus loss. WebKit restores the cursor itself; nothing to do
        # but keep the selector present so the callback has a home.
        logger.debug("pointer lock lost")

    # ---- private WKUIDelegate: the right-click menu --------------------------

    @_private(_SEL_CONTEXT_MENU, b"v@:@@@@@?")
    def webView_getContextMenuFromProposedMenu_forElement_userInfo_completionHandler_(
            self, webview, menu, element, user_info, completion):
        # `element` is a _WKContextMenuElementInfo; its hit test carries the
        # image / link / media URL the item would act on. Without one (an
        # older WebKit) the menu is left exactly as proposed — never curate
        # blind, a wrongly dropped item is worse than a mislabelled one.
        # Whatever happens, the completion runs: a raise here would leave
        # the right-click with NO menu at all, worse than an uncurated one.
        try:
            self._curate_context_menu(menu, element)
        except Exception:  # noqa: BLE001 — private API, shapes may shift
            logger.exception("context menu curation failed; showing WebKit's")
        completion(menu)

    def _curate_context_menu(self, menu, element) -> None:
        urls = _hit_test_urls(element)
        if urls is None or menu is None:
            return
        items = list(menu.itemArray())
        # The identifiers are WebKit-source knowledge (the SDK ships no
        # WKMenuItemIdentifiersPrivate.h): log what actually arrived so a
        # miss is a one-line fix read off Show Logs, not a guess.
        logger.debug("context menu %s: %s", urls,
                     [(str(i.identifier() or ""), str(i.title())) for i in items])
        for item in items:
            ident = item.identifier()
            verdict, title = window_policy.context_menu_item(
                str(ident) if ident else None, self._manager.port, **urls)
            if verdict == "drop":
                menu.removeItem_(item)
            elif verdict == "retitle":
                item.setTitle_(title)

    # ---- window.close() (WKUIDelegate) -------------------------------------

    def webViewDidClose_(self, webview):
        # The page closed itself (a popup we opened for its `window.open`,
        # done with its job). Same path as ⌘W, one run-loop turn later: not
        # tearing the web view down from inside its own delegate callback.
        window = self._window
        if window is None:
            return
        logger.info("page asked to close its window (%s)", window.key or "home")
        from PyObjCTools import AppHelper

        AppHelper.callAfter(window.close)

    # ---- window title and identity follow the page (KVO) -------------------

    def observeValueForKeyPath_ofObject_change_context_(self, key, obj, change, ctx):
        window = self._window
        if window is None:
            return
        if key == "title":
            window.set_title(str(obj.title() or ""))
        elif key == "URL":
            u = obj.URL()
            current = str(u.absoluteString()) if u is not None else None
            window.key = window_policy.window_key_of(current)
            window.view = window_policy.window_view_of(current)
            window.sync_edit_button()

    # ---- NSWindowDelegate --------------------------------------------------

    def windowWillClose_(self, notification):
        self._manager._forget(self._window)

    def windowDidBecomeKey_(self, notification):
        self._manager._touch(self._window)


for _sel in (_SEL_POINTER_LOCK_REQUEST, _SEL_POINTER_LOCK_LOST, _SEL_GEOLOCATION,
             _SEL_DID_CLOSE):
    if not _WebDelegate.instancesRespondToSelector_(_sel):
        # A mangled selector name would leave the feature silently denied
        # again; say so where the log will show it.
        logger.error("_WebDelegate does not respond to %s", _sel.decode())


class _Window:
    """One open window: the NSWindow, its WKWebView, and the strong delegate."""

    def __init__(self, manager: "WindowManager", url: str, configuration,
                 load: bool = True):
        self.manager = manager
        # What the window shows NOW (an app folder or a file path, or None):
        # kept current by the URL observer. Plain Python attribute, so the
        # server thread may read it without touching WebKit.
        self.key: str | None = window_policy.window_key_of(url)
        # HOW it shows ``key`` (app page / explorer view / embed): an app's
        # run window and an explorer window on it share a key and differ only here.
        self.view: str | None = window_policy.window_view_of(url)
        # The saved frame's owner is fixed at creation: `key` follows in-window
        # navigation, but a window must never jump or resize because the page
        # inside it navigated. The frame belongs to the window as opened.
        self.frame_name: str | None = None
        self._popup = not load
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        w, h = DEFAULT_SIZE
        self.ns = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        # AppKit must not release the window on close while Python still
        # holds it (⌘W on a second window would crash). We own the lifetime:
        # `teardown` drops every reference so it deallocs right after close.
        self.ns.setReleasedWhenClosed_(False)
        self.ns.setMinSize_(NSMakeSize(*MIN_SIZE))
        self.ns.setTitle_(APP_NAME)
        self.ns.setTabbingMode_(2)  # NSWindowTabbingModeDisallowed: windows, not tabs

        self.webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, w, h), configuration)
        self.webview.setAllowsBackForwardNavigationGestures_(True)
        self.ns.setContentView_(self.webview)

        self.delegate = _WebDelegate.alloc().initWithManager_window_(manager, self)
        self.webview.setNavigationDelegate_(self.delegate)
        self.webview.setUIDelegate_(self.delegate)
        for path in ("title", "URL"):
            self.webview.addObserver_forKeyPath_options_context_(
                self.delegate, path, NSKeyValueObservingOptionNew, None)
        self.ns.setDelegate_(self.delegate)
        self._add_titlebar_buttons()

        self._place(self.key, self.view)
        # A popup WebKit asked us to create (`window.open`) loads itself once
        # we hand the view back; loading here too would race it.
        if load:
            self.webview.loadRequest_(NSURLRequest.requestWithURL_(_nsurl(url)))

    def _add_titlebar_buttons(self) -> None:
        """"Edit", "Open in Browser" and "Home" at the right end of the title
        bar — Home rightmost, Edit leftmost. A titlebar accessory keeps the
        standard titled window (title stays centred, traffic lights
        untouched) — no toolbar row, no full-size-content-view mask. Same
        actions as the ⌘⇧E / ⌘⇧L / ⌘⇧H menu items. Edit means something only
        while the window runs an app (`sync_edit_button`)."""
        specs = (  # left to right
            ("square.and.pencil", "Edit", "Edit (⌘⇧E)", b"editApp:"),
            ("safari", "Open in Browser", "Open in Browser (⌘⇧L)", b"openInBrowser:"),
            ("house", "Home", "Home (⌘⇧H)", b"goHome:"),
        )
        buttons = []
        for symbol, desc, tip, action in specs:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, desc)
            button = NSButton.buttonWithImage_target_action_(
                image, self.manager._menu_target, action)
            button.setBezelStyle_(NSBezelStyleRecessed)
            button.setBordered_(False)
            button.setToolTip_(tip)
            button.setControlSize_(NSControlSizeLarge)
            button.sizeToFit()
            buttons.append(button)
        gap = 6   # between buttons
        pad = 10  # breathing room from the window's right edge
        bh = max(b.frame().size.height for b in buttons)
        # Title-bar height; the accessory is bottom-aligned, so a holder this
        # tall with the buttons centred lines them up with the title text.
        bar = self.ns.frame().size.height - self.ns.contentLayoutRect().size.height
        hh = max(bh, bar)
        total = sum(b.frame().size.width for b in buttons) + gap * (len(buttons) - 1)
        holder = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, total + pad, hh))
        x = 0.0
        for b in buttons:
            b.setFrameOrigin_(NSMakePoint(x, round((hh - b.frame().size.height) / 2)))
            holder.addSubview_(b)
            x += b.frame().size.width + gap

        vc = NSTitlebarAccessoryViewController.alloc().init()
        vc.setView_(holder)
        vc.setLayoutAttribute_(NSLayoutAttributeTrailing)
        self.ns.addTitlebarAccessoryViewController_(vc)
        self.edit_button = buttons[0]
        self.sync_edit_button()

    def edit_path(self) -> str | None:
        """The explorer URL path Edit opens for this window, or None."""
        return window_policy.edit_target(self.view, self.key)

    def sync_edit_button(self) -> None:
        """Edit follows the page (URL KVO): enabled while the window runs an
        app (an embed or an app page), off on Home and in the explorer."""
        button = getattr(self, "edit_button", None)
        if button is not None:
            button.setEnabled_(self.view in ("embed", "app") and bool(self.key))

    def _place(self, key: str | None, view: str | None = None) -> None:
        """Size and position the new window.

        Every app (and Home) has its own saved frame — the size and place
        the user last left a window of it — so reopening an app puts it
        back exactly there. A second window of the same app cascades from
        the one already open instead of stacking on it, and only the first
        owns the saved frame (AppKit gives an autosave name to one window
        at a time). Nothing saved yet: centre if it is the only window,
        else cascade from the front one. Popups (`window.open`) cascade and
        are never saved — they would otherwise overwrite Home's frame.
        """
        name = None if self._popup else window_policy.frame_autosave_name(key, view)
        owner = self.manager.frame_owner(name) if name else None
        if owner is not None:
            self._cascade_from(owner)
            return
        if name and self.ns.setFrameUsingName_(name):
            pass  # AppKit keeps a restored frame on a visible screen
        else:
            front = self.manager.front()
            if front is None:
                self.ns.center()
            else:
                self._cascade_from(front)
        if name:
            if self.ns.setFrameAutosaveName_(name):
                self.frame_name = name
            else:
                logger.warning("frame autosave name in use: %s", name)

    def _cascade_from(self, other: "_Window") -> None:
        # Same size as ``other``, top-left stepped down-right from it.
        # `cascadeTopLeftFromPoint:` PLACES the window at the point and
        # returns the point for the NEXT window: place ourselves on
        # ``other``, then step from there. Both calls are on ``self`` —
        # ``other`` is never moved, not even to constrain it on screen.
        frame = other.ns.frame()
        self.ns.setFrame_display_(frame, False)
        top_left = NSMakePoint(frame.origin.x, frame.origin.y + frame.size.height)
        self.ns.cascadeTopLeftFromPoint_(self.ns.cascadeTopLeftFromPoint_(top_left))

    def save_frame(self) -> None:
        """Persist the frame now. Autosave writes on move/resize; a window
        the user never touched (cascaded, then closed) needs this so it too
        reopens where it was."""
        if self.frame_name and self.ns is not None:
            try:
                self.ns.saveFrameUsingName_(self.frame_name)
            except Exception:  # noqa: BLE001 — a lost frame is not worth a crash
                logger.debug("saveFrameUsingName failed", exc_info=True)

    def set_title(self, title: str) -> None:
        self.ns.setTitle_(title or APP_NAME)

    def show(self) -> None:
        if self.ns.isMiniaturized():  # a Dock click restores a minimized window
            self.ns.deminiaturize_(None)
        self.ns.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def current_url(self) -> str | None:
        if self.webview is None:
            return None
        u = self.webview.URL()
        return str(u.absoluteString()) if u is not None else None

    def load(self, url: str) -> None:
        if self.webview is not None:
            self.webview.loadRequest_(NSURLRequest.requestWithURL_(_nsurl(url)))

    def teardown(self) -> None:
        """Destroy the page, the web view and the window — for real.

        Closing an NSWindow only orders it out: with ``releasedWhenClosed``
        off it keeps retaining its content view, and a WKWebView that is
        merely hidden keeps running its page (a playing `<audio>` carried on
        after ⌘W). A browser tab close unloads the document; this does the
        same, in two steps:

        Now (synchronous, safe inside ``windowWillClose_``): drop the
        delegates and the KVO observers, so WebKit never calls back into a
        half-dead delegate, then stop any load and navigate to
        ``about:blank`` so the document unloads (``pagehide``/``unload``
        fire, media and timers stop).

        Next runloop turn (``_destroy``, via ``AppHelper.callAfter``): pull
        the web view and titlebar accessories out of the window, break the
        Python cycle ``_Window → _WebDelegate → _Window`` and drop every
        reference, so refcounting deallocs the WKWebView — which closes its
        WebKit page — and the NSWindow. Deferred because we are called from
        inside ``-[NSWindow close]``; releasing the window under AppKit's
        feet is the crash the ``releasedWhenClosed`` comment records.

        Idempotent: a second call is a no-op."""
        webview, ns, delegate = self.webview, self.ns, self.delegate
        if webview is None or getattr(self, "_torn", False):
            return
        self._torn = True
        self.save_frame()
        for path in ("title", "URL"):
            try:
                webview.removeObserver_forKeyPath_(delegate, path)
            except Exception:  # noqa: BLE001 — already removed; nothing to undo
                pass
        webview.setNavigationDelegate_(None)
        webview.setUIDelegate_(None)
        ns.setDelegate_(None)
        try:
            webview.stopLoading()
            webview.loadRequest_(NSURLRequest.requestWithURL_(_nsurl("about:blank")))
        except Exception:  # noqa: BLE001 — the page is going away regardless
            logger.debug("about:blank unload failed", exc_info=True)
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._destroy)

    def _destroy(self) -> None:
        webview, ns, delegate = self.webview, self.ns, self.delegate
        if webview is None:
            return
        self.webview = self.ns = self.delegate = None
        try:
            for vc in list(ns.titlebarAccessoryViewControllers() or ()):
                vc.removeFromParentViewController()
            ns.setContentView_(NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0)))
        except Exception:  # noqa: BLE001 — still break the cycle below
            logger.debug("detaching web view failed", exc_info=True)
        delegate._window = None
        delegate._manager = None
        delegate._downloads = []
        logger.debug("window destroyed (%s)", self.key or "home")

    def close(self) -> None:
        """Close the window the way ⌘W does — through AppKit, so
        ``windowWillClose_`` runs ``_forget`` → ``teardown``."""
        if self.ns is not None:
            self.ns.close()


class _MenuTarget(NSObject):
    """Receiver of the main menu's app-specific items. The Edit menu's items
    target nil and reach the web view through the responder chain."""

    def initWithManager_(self, manager):
        self = objc.super(_MenuTarget, self).init()
        if self is None:
            return None
        self._m = manager
        return self

    def newWindow_(self, _s):
        self._m.open(self._m.home_url)

    def openDocument_(self, _s):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(True)
        panel.setTitle_(f"Open in {APP_NAME}")
        if panel.runModal() == NSModalResponseOK:
            for u in panel.URLs():
                self._m.open_path(str(u.path()))

    def reload_(self, _s):
        if (w := self._m.key()) is not None:
            w.webview.reload()

    def goBack_(self, _s):
        if (w := self._m.key()) is not None:
            w.webview.goBack()

    def goForward_(self, _s):
        if (w := self._m.key()) is not None:
            w.webview.goForward()

    def goHome_(self, _s):
        if (w := self._m.key()) is not None:
            w.load(self._m.home_url)
        else:
            self._m.open(self._m.home_url)

    def editApp_(self, _s):
        # The title-bar button targets this too, and a click on an inactive
        # window's button makes that window key first — so `front()` is the
        # window whose button was pressed.
        if (w := self._m.front()) is not None:
            self._m.edit(w)

    def showTasks_(self, _s):
        self._m.show_tasks()

    def showLauncher_(self, _s):
        launcher = self._m.show_launcher
        if launcher is not None:
            launcher()

    def openInBrowser_(self, _s):
        w = self._m.key()
        _open_external((w and w.current_url()) or self._m.home_url)

    def copyUrl_(self, _s):
        w = self._m.key()
        url = (w and w.current_url()) or self._m.home_url
        subprocess.run(["pbcopy"], input=url.encode(), check=False)

    def printDocument_(self, _s):
        if (w := self._m.key()) is None:
            return
        op = w.webview.printOperationWithPrintInfo_(NSPrintInfo.sharedPrintInfo())
        op.setShowsPrintPanel_(True)
        op.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(
            w.ns, None, None, None)

    def showLogs_(self, _s):
        subprocess.run(["open", "-R", log_path()], check=False)

    def quitApp_(self, _s):
        self._m.quit()


class WindowManager:
    """All open windows of this app, one server behind them.

    ``quit`` is `app.py`'s quit action, so ⌘Q from our menu funnels through
    the same ordered teardown as the popover's Quit (SPEC DM-9).
    """

    def __init__(self, port: int, quit, show_launcher=None) -> None:
        self.port = port
        self.home_url = f"http://127.0.0.1:{port}/"
        self.quit = quit
        # Set once the launcher exists (PR2); the View menu's item is a no-op
        # until then.
        self.show_launcher = show_launcher
        self._windows: list[_Window] = []
        # The `native_windows_enabled` preference, applied live by app.py
        # (`set_enabled`). The manager itself is never torn down once built:
        # the main menu and the activation policy it installed stay, and
        # their items keep targeting it — so OFF is a mode of this object,
        # not its absence. Off, `open` (which every menu item, the Dock
        # reopen, the launcher and the popover funnel through) sends the URL
        # to the default browser instead of making a window.
        self.enabled = True

        # One data store and one process pool for every window: the shell's
        # localStorage is a single set, and `storage` events reach the other
        # windows exactly as they reached other tabs of one browser.
        self._pool = WKProcessPool.alloc().init()
        self._configuration = self._make_configuration()

        # A `.venv/bin/python -m fused_render.app` dev run is not a bundle,
        # and AppKit then defaults to a policy under which windows never take
        # focus. A regular app either way (the bundle already is, D34).
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self._menu_target = _MenuTarget.alloc().initWithManager_(self)
        NSApp.setMainMenu_(_build_main_menu(self._menu_target))

    def _make_configuration(self):
        config = WKWebViewConfiguration.alloc().init()
        config.setProcessPool_(self._pool)
        config.setWebsiteDataStore_(WKWebsiteDataStore.defaultDataStore())
        config.setApplicationNameForUserAgent_(USER_AGENT_MARKER)
        prefs = config.preferences()
        if prefs.respondsToSelector_(b"setElementFullscreenEnabled:"):
            prefs.setElementFullscreenEnabled_(True)
        # Right-click → Inspect Element: an app is somebody's HTML, and the
        # inspector is the debugging surface a browser tab used to give.
        try:
            prefs.setValue_forKey_(True, "developerExtrasEnabled")
        except Exception:  # noqa: BLE001 — a WebKit without the private key
            logger.debug("developerExtrasEnabled not settable", exc_info=True)
        # Autoplaying media did not need a click in a browser tab either.
        config.setMediaTypesRequiringUserActionForPlayback_(0)
        return config

    # ---- what app.py calls --------------------------------------------------

    def open(self, url: str) -> _Window | None:
        """Open ``url`` in a NEW window and bring it to the front — or, with
        native windows switched off, in the default browser (None)."""
        if not self.enabled:
            _open_external(url)
            return None
        win = _Window(self, url, self._configuration)
        self._windows.append(win)
        win.show()
        return win

    def set_enabled(self, on: bool) -> None:
        """The preference flipped. Off closes every open window first, so
        nothing of ours stays on screen for a menu item to act on. Main
        thread only (`close_all` drives AppKit)."""
        self.enabled = bool(on)
        if not on:
            self.close_all()

    def open_popup(self, url: str, configuration) -> _Window:
        """A window for a page's `window.open`: WebKit supplies the
        configuration and performs the load itself (see `_Window`)."""
        win = _Window(self, url, configuration, load=False)
        self._windows.append(win)
        win.show()
        return win

    def url_for_path(self, fs_path: str) -> str:
        """The shell URL a filesystem path opens at (`window_policy.shell_path_for`)."""
        from fused_render.window_policy import shell_path_for

        return f"http://127.0.0.1:{self.port}" + shell_path_for(fs_path)

    def open_path(self, fs_path: str) -> _Window:
        return self.open(self.url_for_path(fs_path))

    def reopen(self) -> None:
        """A macOS Dock-icon click on the running app: the front window if
        there is one (whatever it shows — the user put it there), else a
        fresh Home window."""
        front = self.front()
        if front is not None:
            front.show()
        else:
            self.open(self.home_url)

    def show_tasks(self) -> None:
        """Window → Tasks (⌘⇧T): a window already on the Tasks page comes to
        the front, else one opens. Main thread."""
        url = f"http://127.0.0.1:{self.port}/tasks"
        for w in reversed(self._windows):
            current = w.current_url() or ""
            if urllib.parse.urlsplit(current).path == "/tasks":
                w.show()
                return
        self.open(url)

    def _is_home(self, w: _Window) -> bool:
        # The shell's home is `/`, which the SPA rewrites in place to `/home`
        # (shell/App.tsx); `/apps` is the Apps hub, and a window on /tasks or
        # /preferences has no key either — none of those is Home.
        path = urllib.parse.urlsplit(w.current_url() or "").path.rstrip("/")
        return path in ("", "/home")

    def show_home(self) -> None:
        """A window already showing the shell home comes to the front — the
        key/front one if several — otherwise a fresh Home window opens, even
        if app windows are open."""
        homes = [w for w in self._windows if w.ns is not None and self._is_home(w)]
        if homes:
            win = homes[-1]
            for w in reversed(homes):  # prefer the key/front one
                if w is self.key() or w is self.front():
                    win = w
                    break
            win.show()
        else:
            self.open(self.home_url)

    def has_windows(self) -> bool:
        return bool(self._windows)

    # ---- what the launcher asks (main thread unless noted) -----------------

    def open_keys(self) -> set[str]:
        """The app folders / files currently showing in a window. Safe from
        ANY thread: reads Python attributes only, never WebKit."""
        keys = set()
        for w in list(self._windows):
            if w.key:
                keys.add(w.key)
                # An app's run window is keyed on its entry FILE (an embed);
                # the launcher's running dot keys on the app FOLDER.
                if w.view == "embed" and w.key.lower().endswith(".html"):
                    keys.add(os.path.dirname(w.key))
        return keys

    def window_for(self, fs_path: str, view: str | None = None) -> _Window | None:
        """The most recently used window showing ``fs_path`` (in ``view``,
        when given), or None. ``_windows`` is kept in MRU order (see
        ``_touch``), newest last."""
        fs_path = os.path.abspath(fs_path)
        for w in reversed(self._windows):
            if w.key == fs_path and (view is None or w.view == view):
                return w
        return None

    def focus_or_open_url(self, url: str) -> _Window | None:
        """Dock semantics for a shell URL: the window already showing the same
        thing the same way (key AND view) comes to the front, else one opens.
        A keyless URL always opens fresh."""
        key = window_policy.window_key_of(url)
        if key:
            win = self.window_for(key, window_policy.window_view_of(url))
            if win is not None:
                win.show()
                return win
        return self.open(url)

    def focus_or_open_app(self, fs_path: str) -> _Window | None:
        """An app clicked in the shell or picked in the launcher: its OWN
        window, running its entry page as an embed
        (`window_policy.app_window_path`). Already open → to the front."""
        return self.focus_or_open_url(
            f"http://127.0.0.1:{self.port}" + window_policy.app_window_path(fs_path))

    def edit(self, win: _Window) -> _Window | None:
        """The Edit button: ``win`` itself switches to the explorer view of
        what it runs (owner's call — no second window). The URL observer
        then re-keys it as a ``view`` window, which disables Edit.

        The window also changes HANDS for its frame: the app's size and place
        are saved where they are, and the window takes the explorer's own
        saved frame (where the user last left the explorer on this file) —
        kept as it is if there is none yet — and autosaves under that name
        from here on, so resizing the explorer never moves the app."""
        path = win.edit_path()
        if path is None:
            return None
        url = f"http://127.0.0.1:{self.port}" + path
        name = window_policy.frame_autosave_name(window_policy.window_key_of(url), "view")
        if win.ns is not None and name != win.frame_name and self.frame_owner(name) is None:
            win.save_frame()
            win.ns.setFrameAutosaveName_("")
            win.ns.setFrameUsingName_(name)
            win.frame_name = name if win.ns.setFrameAutosaveName_(name) else None
        win.load(url)
        return win

    def focus_or_open(self, fs_path: str) -> _Window:
        """Dock semantics: an app already open comes to the front (its most
        recently used window if several), otherwise it opens fresh."""
        win = self.window_for(fs_path)
        if win is not None:
            win.show()
            return win
        return self.open_path(fs_path)

    def front(self) -> _Window | None:
        return self.key() or (self._windows[-1] if self._windows else None)

    def frame_owner(self, name: str) -> _Window | None:
        """The open window that owns frame-autosave ``name`` (one per name),
        so a sibling window can cascade from it instead of stacking."""
        for w in self._windows:
            if w.frame_name == name and w.ns is not None:
                return w
        return None

    def key(self) -> _Window | None:
        kw = NSApp.keyWindow()
        if kw is None:
            return None
        for w in self._windows:
            if w.ns is not None and w.ns.isEqual_(kw):
                return w
        return None

    def _touch(self, win: _Window) -> None:
        """A window became key: move it to the MRU end so ``window_for`` /
        ``front`` prefer the one the user last used, not the first opened."""
        if win in self._windows and self._windows[-1] is not win:
            self._windows.remove(win)
            self._windows.append(win)

    def _forget(self, win: _Window) -> None:
        if win in self._windows:
            self._windows.remove(win)
        win.teardown()

    def close_all(self) -> None:
        """Close and destroy every window (quit path). Main thread only —
        it drives AppKit. Each close runs the full ``teardown`` via the
        window delegate; anything already gone is torn down directly."""
        if not NSThread.isMainThread():
            logger.warning("close_all called off the main thread; skipped")
            return
        for win in list(self._windows):
            try:
                win.close()
            except Exception:  # noqa: BLE001 — still tear it down
                logger.debug("close failed; tearing down directly", exc_info=True)
            self._forget(win)
        self._windows.clear()


def _build_main_menu(target) -> NSMenu:
    def item(title, action, key="", mods=None, tgt=target):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if mods is not None:
            it.setKeyEquivalentModifierMask_(mods)
        if tgt is not None:
            it.setTarget_(tgt)
        return it

    def submenu(title, items, main):
        menu = NSMenu.alloc().initWithTitle_(title)
        for it in items:
            menu.addItem_(it)
        holder = NSMenuItem.alloc().init()
        holder.setSubmenu_(menu)
        main.addItem_(holder)
        return menu

    CMD = NSEventModifierFlagCommand
    sep = NSMenuItem.separatorItem
    main = NSMenu.alloc().init()

    submenu(APP_NAME, [
        item(f"About {APP_NAME}", b"orderFrontStandardAboutPanel:", tgt=None),
        sep(),
        item(f"Hide {APP_NAME}", b"hide:", "h", tgt=None),
        item("Hide Others", b"hideOtherApplications:", "h", CMD | _ALT, tgt=None),
        item("Show All", b"unhideAllApplications:", tgt=None),
        sep(),
        item(f"Quit {APP_NAME}", b"quitApp:", "q"),
    ], main)

    submenu("File", [
        item("New Window", b"newWindow:", "n"),
        item("Open…", b"openDocument:", "o"),
        sep(),
        item("Close Window", b"performClose:", "w", tgt=None),
        sep(),
        item("Print…", b"printDocument:", "p"),
    ], main)

    # Standard selectors, nil target: the responder chain delivers them to
    # the web view, which is what gives a WKWebView its ⌘C/⌘V/⌘X/⌘Z/⌘A.
    submenu("Edit", [
        item("Undo", b"undo:", "z", tgt=None),
        item("Redo", b"redo:", "z", CMD | _SHIFT, tgt=None),
        sep(),
        item("Cut", b"cut:", "x", tgt=None),
        item("Copy", b"copy:", "c", tgt=None),
        item("Paste", b"paste:", "v", tgt=None),
        item("Delete", b"delete:", tgt=None),
        item("Select All", b"selectAll:", "a", tgt=None),
    ], main)

    submenu("View", [
        item("Reload Page", b"reload:", "r"),
        item("Back", b"goBack:", "["),
        item("Forward", b"goForward:", "]"),
        item("Home", b"goHome:", "H", CMD | _SHIFT),
        item("Edit App", b"editApp:", "E", CMD | _SHIFT),
        sep(),
        item("Search Apps…", b"showLauncher:"),
        sep(),
        item("Open in Browser", b"openInBrowser:", "L", CMD | _SHIFT),
        item("Copy URL", b"copyUrl:", "C", CMD | _SHIFT),
        sep(),
        item("Enter Full Screen", b"toggleFullScreen:", "f", CMD | _CTRL, tgt=None),
    ], main)

    window_menu = submenu("Window", [
        item("Minimize", b"performMiniaturize:", "m", tgt=None),
        item("Zoom", b"performZoom:", tgt=None),
        sep(),
        item("Tasks", b"showTasks:", "T", CMD | _SHIFT),
        sep(),
        item("Bring All to Front", b"arrangeInFront:", tgt=None),
    ], main)
    NSApp.setWindowsMenu_(window_menu)

    help_menu = submenu("Help", [
        item("Show App Logs in Finder", b"showLogs:"),
    ], main)
    NSApp.setHelpMenu_(help_menu)

    return main
