"""The launcher: a Spotlight-like search panel on a global shortcut (macOS).

``static/launcher.html`` (served by the /static mount) inside a transparent
``WKWebView`` on a borderless, non-activating ``NSPanel`` with native glass
behind it (Liquid Glass on macOS 26, a flat surface before). Fixed width;
the page reports its height after every render and the panel resizes
around it, top edge pinned about a quarter down the screen — where
Spotlight sits — so the field never moves as results come and go.

Non-activating: the panel takes key focus (typing lands in the field) but
the app that was frontmost stays active, so closing the launcher returns
the user exactly where they were — and opening an app from it activates
this app the way the Dock does (``WindowManager.focus_or_open``).

Dismissal: ⎋ in the page (a ``close`` message), the panel resigning key
(a click in any of our windows), a click anywhere else (global mouse
monitor: a non-activating panel gets no resignKey for clicks in the app
that IS active), or the shortcut again (toggle).

Row shortcuts: ``<rowModifier>+1`` … ``+9`` (⌥ by default) are global
shortcuts too, opening the Nth app of the launcher's empty-query list
(recently opened first, then the desk — `launcher.nth_pinned`) from
anywhere; ``+0`` opens the shell home. Which app is resolved at press
time, so an open or a desk change needs no rebind. They are suspended
while the panel is up, so the same digits mean "the Nth row" there — the
same list until the user types.

The shortcut is Carbon's ``RegisterEventHotKey`` (``hotkey.py``), bound
after the server is up and rebound from ``PUT /api/prefs`` through
``launcher.native_hooks["rebind"]`` (installed by ``app.py``). A failed
bind (another app owns the combination) is remembered so the pages can say
so; the launcher stays reachable from the View menu and the popover.

Ported from Render App (fused-render-lite `launcher_panel.py`).
"""
from __future__ import annotations

import json
import logging
import math

import AppKit
import objc
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskRightMouseDown,
    NSMakeRect,
    NSObject,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectMaterialPopover,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorTransient,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSURL, NSURLRequest
from WebKit import (
    WKNavigationActionPolicyAllow,
    WKNavigationActionPolicyCancel,
    WKUserContentController,
    WKWebView,
    WKWebViewConfiguration,
)

from fused_render import hotkey, launcher
from fused_render.mac_window import USER_AGENT_MARKER, _open_external

logger = logging.getLogger(__name__)

WIDTH = 680.0
INITIAL_HEIGHT = 74.0
MAX_HEIGHT = 700.0
MESSAGE_NAME = "launcher"
TOP_FRACTION = 0.22  # top edge this far down the screen's visible frame
RADIUS = 18.0


class _LauncherScriptHandler(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_LauncherScriptHandler, self).init()
        if self is None:
            return None
        self._c = controller
        return self

    def userContentController_didReceiveScriptMessage_(self, _ucc, message):
        try:
            body = message.body()
            data = dict(body) if body is not None else {}
        except Exception:  # noqa: BLE001 — a page can post anything
            return
        kind = data.get("type")
        if kind == "size":
            self._c.resize_to(data.get("height"))
        elif kind == "open":
            self._c.open_path(str(data.get("path") or ""))
        elif kind == "home":
            self._c.open_home()
        elif kind == "close":
            self._c.close()


class _LauncherWebDelegate(NSObject):
    """The launcher page is the only thing this view shows; any other
    main-frame navigation or ``window.open`` goes to the default browser."""

    def initWithController_(self, controller):
        self = objc.super(_LauncherWebDelegate, self).init()
        if self is None:
            return None
        self._c = controller
        return self

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, webview, action, decision):
        request = action.request()
        url = str(request.URL().absoluteString()) if request and request.URL() else ""
        target = action.targetFrame()
        is_main = target is None or bool(target.isMainFrame())
        if not is_main or url == self._c._url() or url.startswith("about:"):
            decision(WKNavigationActionPolicyAllow)
            return
        decision(WKNavigationActionPolicyCancel)
        if url:
            self._c.close()
            _open_external(url)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, webview, config, action, features):
        request = action.request()
        url = str(request.URL().absoluteString()) if request and request.URL() else ""
        if url:
            self._c.close()
            _open_external(url)
        return None


class _LauncherPanelDelegate(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_LauncherPanelDelegate, self).init()
        if self is None:
            return None
        self._c = controller
        return self

    def windowDidResignKey_(self, _n):
        self._c.close()


class _LauncherPanel(NSPanel):
    """Borderless panels refuse key status by default; typing needs it."""

    def canBecomeKeyWindow(self):
        return True


class LauncherController:
    """Owns the panel, its web view and the global shortcuts. Main thread only.

    ``open_app(path)``: what Enter / a click does — the app-level callback
    (focus-or-open in a window); the panel closes first. ``show_home``: the
    last row / <modifier>+0.
    """

    def __init__(self, port: int, open_app, show_home) -> None:
        self._port = port
        self._open_app = open_app
        self._show_home = show_home
        self._handler = _LauncherScriptHandler.alloc().initWithController_(self)
        self._web_delegate = _LauncherWebDelegate.alloc().initWithController_(self)
        self._panel_delegate = _LauncherPanelDelegate.alloc().initWithController_(self)
        self._loaded = False
        self._monitor = None
        self._height = INITIAL_HEIGHT
        self._hotkey: hotkey.HotKey | None = None
        self._bound: bool | None = None
        self._pinned: hotkey.HotKeySet | None = None
        self._pinned_bound: bool | None = None
        self._build_panel()

    # ---- public ------------------------------------------------------------------

    def server_ready(self) -> None:
        self._load()

    def is_shown(self) -> bool:
        return bool(self._panel.isVisible())

    def toggle(self) -> None:
        if self.is_shown():
            self.close()
        else:
            self.show()

    def show(self) -> None:
        if not self._loaded:
            self._load()
        self._place()
        self._panel.setAlphaValue_(1.0)
        self._panel.makeKeyAndOrderFront_(None)
        self._panel.makeFirstResponder_(self._webview)
        if self._monitor is None:
            mask = NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown
            self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, lambda _e: self.close())
        self._webview.evaluateJavaScript_completionHandler_(
            "window.launcherShown && window.launcherShown();", None)
        self._suspend_pinned()

    def close(self) -> None:
        if self._monitor is not None:
            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
        if self._panel.isVisible():
            self._panel.orderOut_(None)
        self._resume_pinned()

    def open_path(self, path: str) -> None:
        self.close()
        if path:
            self._open_app(path)

    def open_home(self) -> None:
        self.close()
        self._show_home()

    # ---- shortcut ----------------------------------------------------------------------

    def bind_hotkey(self, spec: str | None = None) -> bool:
        """Bind ``spec`` (default: the stored one). Returns whether it took;
        the outcome is remembered for ``hotkey_bound`` / the pages."""
        spec = spec or launcher.get_hotkey()
        if self._hotkey is None:
            self._hotkey = hotkey.HotKey(self.toggle)
        previous = self._hotkey.spec
        try:
            self._hotkey.set(spec)
            self._bound = True
        except Exception:  # noqa: BLE001 — the launcher stays reachable from the menu
            logger.exception("launcher shortcut %s could not be bound", spec)
            self._bound = False
            # A rebind the system refused (another app owns the combo): fall
            # back to the shortcut that worked. The stored preference keeps
            # the refused spec — the Preferences page shows it as unbound
            # and the user picks another.
            if previous and previous != spec:
                try:
                    self._hotkey.set(previous)
                except Exception:  # noqa: BLE001
                    logger.exception("could not restore shortcut %s", previous)
        self._push_settings()
        return self._bound

    def hotkey_bound(self) -> bool | None:
        return self._bound

    def suspend_shortcuts(self, on: bool) -> None:
        """Preferences is recording a new shortcut (``on``): unbind the panel
        hotkey and the row shortcuts so the combination being pressed reaches
        the page instead of firing here; ``off`` binds them back (the stored
        spec — a PUT that landed meanwhile is what gets bound)."""
        if on:
            if self._hotkey is not None:
                self._hotkey.clear()
            self._suspend_pinned()
        else:
            self.bind_hotkey()
            self._resume_pinned()

    def push_settings(self) -> None:
        """Settings changed (the row modifier): re-read it, rebind the row
        shortcuts and tell the panel's page."""
        self.bind_pinned()
        self._push_settings()

    # ---- row shortcuts ------------------------------------------------------------

    def bind_pinned(self) -> bool | None:
        """(Re)bind ``<rowModifier>+1…9`` and ``+0``. A digit the system
        refuses is skipped (logged); ``pinned_bound`` is False when any was."""
        if self._pinned is None:
            self._pinned = hotkey.HotKeySet()
        self._pinned.clear()
        modifier = launcher.get_row_modifier()
        if self.is_shown():  # bound on close (_resume_pinned)
            self._pinned_bound = True
            return True
        ok = True
        binds = [(spec, lambda n=n: self._open_pinned(n))
                 for n, spec in enumerate(launcher.pinned_specs(modifier), start=1)]
        home = launcher.home_spec(modifier)
        if home:
            binds.append((home, self._show_home))
        for spec, callback in binds:
            try:
                self._pinned.bind(spec, callback)
            except Exception:  # noqa: BLE001
                logger.exception("row shortcut %s could not be bound", spec)
                ok = False
        self._pinned_bound = ok
        return ok

    def pinned_bound(self) -> bool | None:
        return self._pinned_bound

    def _open_pinned(self, n: int) -> None:
        path = launcher.nth_pinned(n)
        if path:
            self._open_app(path)
        else:
            logger.info("row shortcut %d: no such row", n)

    def _suspend_pinned(self) -> None:
        if self._pinned is not None:
            self._pinned.clear()

    def _resume_pinned(self) -> None:
        if self._pinned is not None and not self._pinned.specs():
            self.bind_pinned()

    def _push_settings(self) -> None:
        js = "window.launcherSettings && window.launcherSettings(%s);" % json.dumps(
            launcher.settings())
        self._webview.evaluateJavaScript_completionHandler_(js, None)

    # ---- geometry ----------------------------------------------------------------------

    def resize_to(self, height) -> None:
        try:
            h = float(height)
        except (TypeError, ValueError):
            return
        if not math.isfinite(h) or h <= 0:
            return
        h = min(max(h, 40.0), MAX_HEIGHT)
        if abs(h - self._height) < 0.5:
            return
        self._height = h
        self._place()

    def _screen(self):
        # The screen under the mouse, like Spotlight; the main screen otherwise.
        p = NSEvent.mouseLocation()
        for s in NSScreen.screens():
            f = s.frame()
            if (f.origin.x <= p.x < f.origin.x + f.size.width
                    and f.origin.y <= p.y < f.origin.y + f.size.height):
                return s
        return NSScreen.mainScreen() or (NSScreen.screens()[0] if NSScreen.screens() else None)

    def _rest_frame(self):
        h = self._height
        screen = self._screen()
        if screen is None:
            f = self._panel.frame()
            return NSMakeRect(f.origin.x, f.origin.y + f.size.height - h, WIDTH, h)
        vis = screen.visibleFrame()
        x = vis.origin.x + (vis.size.width - WIDTH) / 2
        top = vis.origin.y + vis.size.height - vis.size.height * TOP_FRACTION
        y = max(vis.origin.y, top - h)
        return NSMakeRect(round(x), round(y), WIDTH, h)

    def _place(self) -> None:
        rest = self._rest_frame()
        self._panel.setFrame_display_(rest, True)
        h = rest.size.height
        self._panel.contentView().setFrame_(NSMakeRect(0, 0, WIDTH, h))
        self._glass.setFrame_(NSMakeRect(0, 0, WIDTH, h))
        # The web view is as tall as the panel can get and hangs from its top,
        # so a height change never relayouts the page (only the panel moves).
        self._webview.setFrame_(NSMakeRect(0, h - MAX_HEIGHT, WIDTH, MAX_HEIGHT))

    # ---- panel ------------------------------------------------------------------------------

    def _build_panel(self) -> None:
        w, h = WIDTH, INITIAL_HEIGHT
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = _LauncherPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        # No window shadow: on a non-opaque window AppKit traces the opaque
        # pixels with a hairline — a square-ish border around the glass.
        panel.setHasShadow_(False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setHidesOnDeactivate_(False)
        panel.setMovableByWindowBackground_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorTransient)
        panel.setDelegate_(self._panel_delegate)

        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        root.setWantsLayer_(True)
        self._glass = _make_backdrop(w, h)
        root.addSubview_(self._glass)

        config = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(self._handler, MESSAGE_NAME)
        config.setUserContentController_(ucc)
        config.setApplicationNameForUserAgent_(f"{USER_AGENT_MARKER} Launcher")
        try:
            config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except Exception:  # noqa: BLE001
            pass
        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, h - MAX_HEIGHT, w, MAX_HEIGHT), config)
        try:
            self._webview.setValue_forKey_(False, "drawsBackground")
        except Exception:  # noqa: BLE001
            logger.debug("drawsBackground not settable", exc_info=True)
        try:
            self._webview.setUnderPageBackgroundColor_(NSColor.clearColor())
        except Exception:  # noqa: BLE001
            pass
        self._webview.setNavigationDelegate_(self._web_delegate)
        self._webview.setUIDelegate_(self._web_delegate)
        root.addSubview_(self._webview)
        panel.setContentView_(root)
        self._panel = panel

    def _url(self) -> str:
        return f"http://127.0.0.1:{self._port}/static/launcher.html"

    def _load(self) -> None:
        url = self._url()
        self._webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self._loaded = True
        logger.info("launcher panel loading %s", url)


def _make_glass(w: float, h: float):
    """Liquid Glass (``NSGlassEffectView``, macOS 26) with a light tint, or
    the pre-26 ``NSVisualEffectView`` popover material."""
    GlassView = getattr(AppKit, "NSGlassEffectView", None)
    if GlassView is not None:
        glass = GlassView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        if glass.respondsToSelector_(b"setCornerRadius:"):
            glass.setCornerRadius_(RADIUS)
            glass.setTintColor_(NSColor.colorWithWhite_alpha_(0.5, 0.12))
            return glass
    glass = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    glass.setMaterial_(NSVisualEffectMaterialPopover)
    glass.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    glass.setState_(NSVisualEffectStateActive)
    glass.setWantsLayer_(True)
    glass.layer().setCornerRadius_(RADIUS)
    glass.layer().setMasksToBounds_(True)
    if glass.layer().respondsToSelector_(b"setCornerCurve:"):
        glass.layer().setCornerCurve_("continuous")
    return glass


def _make_backdrop(w: float, h: float):
    """Liquid Glass on macOS 26; before that a flat, near-opaque surface in
    the Raycast style — the pre-26 vibrancy materials read as a blurry grey
    box under a search field."""
    if getattr(AppKit, "NSGlassEffectView", None) is not None:
        # A vibrancy material behind the glass (the frost) and a heavier,
        # appearance-following tint on the glass itself, so text stays
        # legible over busy windows.
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        root.setWantsLayer_(True)
        root.layer().setCornerRadius_(RADIUS)
        root.layer().setMasksToBounds_(True)
        if root.layer().respondsToSelector_(b"setCornerCurve:"):
            root.layer().setCornerCurve_("continuous")
        frost = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        frost.setMaterial_(NSVisualEffectMaterialHUDWindow)
        frost.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        frost.setState_(NSVisualEffectStateActive)
        frost.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        glass = _make_glass(w, h)
        if glass.respondsToSelector_(b"setTintColor:"):
            glass.setTintColor_(NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.55))
        glass.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        root.addSubview_(frost)
        root.addSubview_(glass)
        return root
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    view.setWantsLayer_(True)
    layer = view.layer()
    dark = "Dark" in str(NSApp.effectiveAppearance().name()) if NSApp else True
    bg = (0.11, 0.11, 0.12, 0.97) if dark else (0.95, 0.95, 0.96, 0.98)
    line = (1.0, 1.0, 1.0, 0.10) if dark else (0.0, 0.0, 0.0, 0.10)
    layer.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(*bg).CGColor())
    layer.setBorderColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(*line).CGColor())
    layer.setBorderWidth_(1.0)
    layer.setCornerRadius_(RADIUS)
    layer.setMasksToBounds_(True)
    if layer.respondsToSelector_(b"setCornerCurve:"):
        layer.setCornerCurve_("continuous")
    logger.info("launcher backdrop: flat (no NSGlassEffectView)")
    return view
