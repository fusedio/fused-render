"""The menu-bar Dock: a click on the status item drops a macOS-Dock-like
tray (``fused_render/dock.py`` decides the tiles: under Fused Render the
user's APPS, under Fused Bot their BOTS — a Home tile, the pinned ones, then
up to three recent), each a tile — an app's own ``icon.svg`` / ``icon.png`` or
a bot's face — with its name beneath, and a dot when a bot is busy. A click
opens the app in its own window (or selects the bot in a Bots window);
right-click pins, reveals, opens in the browser. The default surface of the
status item since 2026-10-08; the pinned-file popover (menubar_pin.py) is the
opt-in alternative (`prefs.menubar_pin_enabled`).

The tray is HTML (``static/shell-dist/dock.html``, built from
``frontend/dock.html`` and served at ``/dock`` by the in-process server)
inside a transparent ``WKWebView``. It floats: the host is a borderless,
non-opaque ``NSPanel`` under the status item, and the glass is an
``NSVisualEffectView`` with rounded corners sized to the tray rect the page
reports — nothing else is drawn, so there is no popover box around the Dock
(an ``NSPopover`` always paints its own chrome; that is why it is not one).

The web view is never resized. It is a fixed ``CANVAS``-sized surface
(``MAX_SIZE``) pinned in screen space — centred under the status item, its
top at the menu bar — and the panel is a viewport onto it: the page lays the
tray out where it wants inside the canvas, reports the rect it occupies, and
the panel's frame plus the web view's offset inside it are set together so
that rect (and only it) is on screen. Resizing a WKWebView is a two-process
affair: AppKit moves the window at once, WebKit paints the new layout a frame
later, and in between the old content shows at the new place — the tray
flashed sideways whenever the panel grew (drag start, first size report).
Moving a window and offsetting a view inside it is AppKit-only and commits
in one transaction, so nothing flashes. The panel tells the page where the
status item and screen edges are in canvas coordinates, and the page centres
the tray under the icon itself.

Status item: rumps attached an ``NSMenu`` to it, which AppKit opens on every
click without ever firing the button's action. The menu is taken off; left
click toggles the panel, right click (or ⌃-click) shows a small utility menu
(Home, Tasks, browser, launcher, logs, diagnostics, quit) so Quit stays one
click away.

macOS-only, like rumps. ``app.py`` builds this lazily after the run loop is
up and keeps rumps' menu when construction fails — the app is never left
without a Quit. Ported from FusedBot (fused-bot ``menubar_dock.py``, itself
Render App's); the tile data model moved to ``fused_render/dock.py``.
"""
from __future__ import annotations

import logging
import math
import os
import time

import AppKit
import objc
from AppKit import (
    NSApp,
    NSColor,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseDown,
    NSEventMaskRightMouseUp,
    NSEventModifierFlagControl,
    NSEventTypeRightMouseUp,
    NSMakePoint,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSPanel,
    NSStatusWindowLevel,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialPopover,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorTransient,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSWorkspace,
    NSBackingStoreBuffered,
)
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer, NSURL, NSURLRequest
from WebKit import (
    WKNavigationActionPolicyAllow,
    WKNavigationActionPolicyCancel,
    WKUserContentController,
    WKWebView,
    WKWebViewConfiguration,
)

from fused_render import _flavor, window_policy
from fused_render import dock as dock_store
from fused_render.mac_window import USER_AGENT_MARKER, _open_external

logger = logging.getLogger(__name__)

# Before the page has reported a size: room for two utility tiles + hint.
INITIAL_SIZE = (360, 100)
MIN_SIZE = (120, 60)
# Height: headroom + the tray at the 128px ⌥-snap + the footroom that holds a
# preview bubble (dock.html --footroom, body.has-previews) ≈ 415px; slack on top.
MAX_SIZE = (1400, 520)
CANVAS = MAX_SIZE  # the web view's fixed size; the panel shows a region of it
MESSAGE_NAME = "dock"
TRAY_RADIUS = 18.0
GAP_BELOW_MENU_BAR = 4.0

# Appear / dismiss: the panel drops from the menu bar (starts tucked up by
# APPEAR_OFFSET, fully transparent) and settles into place; dismissal is the
# reverse, quicker, since it follows a click elsewhere and must not feel
# laggy. The fade is decoupled from the motion and much shorter: the tray is
# fully visible while most of the travel still happens, so the movement
# reads (a fade as long as the slide hides the slide). The slide curve is a
# spring-like "expo out": fast start, long soft settle, no overshoot.
#
# The motion is driven by our own timer (``_Slide``), not by the window's
# ``animator()``: the animator-driven window-frame slide behaved differently
# on macOS 15 (the panel jumped on every open and close; fine on 26) — its
# completion and cancellation semantics are not the same across versions,
# and the old code leaned on both. With our own driver the destination can
# be retargeted while the panel is still moving (the page reports its real
# size right after ``dockShown``), a re-show mid-dismiss reverses from where
# the panel is, and cancellation is explicit. Skipped when Reduce Motion is on.
APPEAR_OFFSET = 18.0
APPEAR_DURATION = 0.42
APPEAR_FADE = 0.14
DISMISS_OFFSET = 8.0
DISMISS_DURATION = 0.2
DISMISS_FADE = 0.16
FRAME_INTERVAL = 1.0 / 120.0  # timer cadence; progress is clock-based, not tick-counted


def ease_out_expo(t: float) -> float:
    return 1.0 if t >= 1.0 else 1.0 - 2.0 ** (-10.0 * t)


def ease_in_cubic(t: float) -> float:
    return t * t * t


def _reduce_motion() -> bool:
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:  # noqa: BLE001
        return False


def _make_glass(w: float, h: float):
    """The tray's glass: Liquid Glass (``NSGlassEffectView``, macOS 26) with a
    light tint, or the pre-26 ``NSVisualEffectView`` popover material. A
    sibling of the web view, not its container: magnified tiles and the name
    bubble hang outside the glass, and a container would clip them."""
    GlassView = getattr(AppKit, "NSGlassEffectView", None)
    if GlassView is not None:
        glass = GlassView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        if glass.respondsToSelector_(b"setCornerRadius:"):
            glass.setCornerRadius_(TRAY_RADIUS)
            glass.setTintColor_(NSColor.colorWithWhite_alpha_(0.5, 0.12))
            logger.info("dock glass: NSGlassEffectView")
            return glass
    glass = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    glass.setMaterial_(NSVisualEffectMaterialPopover)
    glass.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    glass.setState_(NSVisualEffectStateActive)
    glass.setWantsLayer_(True)
    glass.layer().setCornerRadius_(TRAY_RADIUS)
    glass.layer().setMasksToBounds_(True)
    if glass.layer().respondsToSelector_(b"setCornerCurve:"):
        glass.layer().setCornerCurve_("continuous")  # squircle, like the Dock
    logger.info("dock glass: NSVisualEffectView")
    return glass


class _Slide(NSObject):
    """Slides a window's origin and fades its alpha on a main-run-loop timer.

    Progress comes from the monotonic clock, so a late tick never lags the
    motion. ``retarget`` moves the destination while running (the panel keeps
    going from where it is; the remaining travel bends toward the new spot).
    ``cancel`` stops the timer without a completion; ``done`` runs once, when
    both the slide and the fade have ended."""

    def initWithWindow_(self, window):
        self = objc.super(_Slide, self).init()
        if self is None:
            return None
        self._w = window
        self._timer = None
        self._done = None
        self._to = (0.0, 0.0)
        return self

    @objc.python_method
    def start(self, to_origin, duration, ease, to_alpha, fade, done) -> None:
        self.cancel()
        frame = self._w.frame()
        self._from = (float(frame.origin.x), float(frame.origin.y))
        self._to = (float(to_origin[0]), float(to_origin[1]))
        self._alpha0 = float(self._w.alphaValue())
        self._alpha1 = float(to_alpha)
        self._duration = max(float(duration), 1e-3)
        self._fade = max(float(fade), 1e-3)
        self._ease = ease
        self._done = done
        self._t0 = time.monotonic()
        self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            FRAME_INTERVAL, self, b"tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)
        self.tick_(None)

    @objc.python_method
    def running(self) -> bool:
        return self._timer is not None

    @objc.python_method
    def target(self):
        return self._to

    @objc.python_method
    def retarget(self, to_origin) -> None:
        """Move the destination of a running slide; the panel keeps going
        from where it is. No-op when idle."""
        if self._timer is None:
            return
        frame = self._w.frame()
        p = self._progress()
        cur = (float(frame.origin.x), float(frame.origin.y))
        to = (float(to_origin[0]), float(to_origin[1]))
        if p >= 0.999:
            self._from = to
        else:
            # Re-base so from + p·(to − from) == cur at the current fraction:
            # progress so far stays, only the remaining travel changes.
            self._from = tuple((c - p * t) / (1.0 - p) for c, t in zip(cur, to))
        self._to = to

    @objc.python_method
    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        self._done = None

    @objc.python_method
    def _progress(self) -> float:
        t = (time.monotonic() - self._t0) / self._duration
        return self._ease(min(max(t, 0.0), 1.0))

    def tick_(self, _timer) -> None:
        if self._timer is None:
            return
        now = time.monotonic() - self._t0
        p = self._progress()
        x = self._from[0] + (self._to[0] - self._from[0]) * p
        y = self._from[1] + (self._to[1] - self._from[1]) * p
        f = min(max(now / self._fade, 0.0), 1.0)
        alpha = self._alpha0 + (self._alpha1 - self._alpha0) * f
        self._w.setFrameOrigin_(NSMakePoint(x, y))
        self._w.setAlphaValue_(alpha)
        if now >= self._duration and now >= self._fade:
            done = self._done
            self.cancel()
            if done is not None:
                done()


class _Target(NSObject):
    """Action target for the status button and the utility menu."""

    def initWithController_(self, controller):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._c = controller
        return self

    def statusItemClicked_(self, _sender):
        self._c.status_item_clicked()

    @objc.python_method
    def _act(self, name: str) -> None:
        # `.get`: an actions dict without the key (a surface app.py did not
        # build, tests) must not turn a menu click into a pyobjc exception.
        action = self._c.actions.get(name)
        if action is not None:
            action()

    def openHome_(self, _s):
        self._act("show_home")

    def openBrowser_(self, _s):
        self._act("open_browser")

    def copyUrl_(self, _s):
        self._act("copy_url")

    def showTasks_(self, _s):
        self._act("show_tasks")

    def showLauncher_(self, _s):
        self._act("show_launcher")

    def openLogs_(self, _s):
        self._act("open_logs")

    def saveDiagnostics_(self, _s):
        self._act("save_diagnostics")

    def quitApp_(self, _s):
        self._act("quit")

    # ---- per-tile menu (representedObject = "bot:<id>" / "app:<path>" / "appfile:<path>")

    @objc.python_method
    def _tile(self, sender):
        kind, _, key = str(sender.representedObject() or "").partition(":")
        return kind, key

    def itemOpen_(self, sender):
        self._c.item_open(*self._tile(sender))

    def itemPin_(self, sender):
        self._c.item_pin(*self._tile(sender), True)

    def itemUnpin_(self, sender):
        self._c.item_pin(*self._tile(sender), False)

    def itemReveal_(self, sender):
        self._c.item_reveal(*self._tile(sender))

    def itemBrowser_(self, sender):
        self._c.item_browser(*self._tile(sender))


class _ScriptHandler(NSObject):
    """``window.webkit.messageHandlers.dock.postMessage({...})`` lands here.
    Plain NSObject subclass, not declared with the protocol — same PyObjC
    reason as ``mac_window._WebDelegate``."""

    def initWithController_(self, controller):
        self = objc.super(_ScriptHandler, self).init()
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
        tray = data.get("tray")
        tray = dict(tray) if tray is not None else None
        if kind == "size":
            self._c.resize_to(data.get("width"), data.get("height"), tray, data.get("x", 0))
        elif kind == "tray":  # per-frame while the fisheye is live: glass only
            self._c.set_tray(tray)
        elif kind == "resize":  # separator drag started / ended
            self._c.set_resizing(bool(data.get("active")))
        elif kind == "menu":
            self._c.show_item_menu(data)


class _DockWebDelegate(NSObject):
    """Navigation + UI delegate of the tray's web view. The tray page is the
    only thing this view ever shows: any other main-frame navigation, and any
    ``target=_blank`` / ``window.open`` (the context menu's "Open in
    Browser"), goes to the default browser. Without a UI delegate WebKit
    silently drops ``_blank`` clicks — same reason the app windows have one."""

    def initWithController_(self, controller):
        self = objc.super(_DockWebDelegate, self).init()
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


class _DockPanel(NSPanel):
    """Borderless panels refuse key status by default; the tray needs it so
    a click elsewhere resigns it (→ close) and ⎋ reaches the page."""

    def canBecomeKeyWindow(self):
        return True


class _MenuDelegate(NSObject):
    """Tells the page when the tile menu closes, so it can thaw the fisheye
    it froze on that tile while the menu was up."""

    def initWithController_(self, controller):
        self = objc.super(_MenuDelegate, self).init()
        if self is None:
            return None
        self._c = controller
        return self

    def menuDidClose_(self, _menu):
        self._c._webview.evaluateJavaScript_completionHandler_(
            "window.dockMenuClosed && window.dockMenuClosed();", None)


class _PanelDelegate(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_PanelDelegate, self).init()
        if self is None:
            return None
        self._c = controller
        return self

    def windowDidResignKey_(self, _n):
        self._c.close()


class DockController:
    """Owns the status-item click, the floating panel and its web view.

    ``actions``: app-level callbacks for the right-click menu —
    ``show_home`` / ``show_tasks`` / ``open_browser`` / ``copy_url`` /
    ``show_launcher`` / ``open_logs`` / ``save_diagnostics`` / ``quit``; any
    may be absent (its item is then left out). A tile click goes through
    ``window_policy.native_hooks["dock_open"]`` (app.py installs it), the
    same hook the page's ``POST /api/dock/open`` reaches. Main thread only,
    like everything AppKit.
    """

    def __init__(self, statusitem, port: int, actions: dict) -> None:
        self._statusitem = statusitem
        self._port = port
        self.actions = actions
        self._target = _Target.alloc().initWithController_(self)
        self._handler = _ScriptHandler.alloc().initWithController_(self)
        self._panel_delegate = _PanelDelegate.alloc().initWithController_(self)
        self._web_delegate = _DockWebDelegate.alloc().initWithController_(self)
        self._menu_delegate = _MenuDelegate.alloc().initWithController_(self)
        self._loaded = False
        self._monitor = None
        self._size = INITIAL_SIZE
        self._rx = 0.0  # the region's left edge in canvas (page) coordinates
        self._applied = None  # (rx, w, h) the panel frame was last set for
        self._tray = None  # (x, y, w, h) in page coordinates, top-left origin
        self._resizing = False  # separator drag in progress (resize cursor held)
        self._closing = False  # dismiss animation running (panel still ordered in)
        self._animating_in = False  # appear animation running: _place retargets, never snaps
        self._build_panel()
        self._take_over_status_item()

    # ---- public ----------------------------------------------------------------

    def set_port(self, port: int) -> None:
        self._port = port

    def server_ready(self) -> None:
        """Load the tray page once the server answers (the panel may be built
        before the port is bound)."""
        self._load()

    def is_shown(self) -> bool:
        # A panel mid-dismiss counts as hidden: a status-item click while it
        # fades out reopens it instead of doing nothing.
        return bool(self._panel.isVisible()) and not self._closing

    def toggle_popover(self) -> None:
        if self.is_shown():
            self.close()
        else:
            self.show_popover()

    def close_popover(self) -> None:
        self.close()

    def close(self) -> None:
        if self._monitor is not None:
            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
        if not self._panel.isVisible() or self._closing:
            return
        self._slide.cancel()
        self._animating_in = False
        if _reduce_motion():
            self._panel.orderOut_(None)
            return
        self._closing = True
        # Dismiss toward "rest, tucked up": from wherever the panel is (it
        # may still be dropping in).
        rest = self._rest_frame()
        target = (rest.origin.x, rest.origin.y + DISMISS_OFFSET)

        def done():
            self._closing = False
            self._panel.orderOut_(None)
            self._panel.setAlphaValue_(1.0)

        self._slide.start(target, DISMISS_DURATION, ease_in_cubic, 0.0, DISMISS_FADE, done)

    def show_popover(self) -> None:
        if not self._loaded:
            self._load()
        reversing = self._closing and bool(self._panel.isVisible())
        self._slide.cancel()
        self._closing = False
        rest = self._rest_frame()
        if _reduce_motion():
            self._panel.setFrame_display_(rest, False)
            self._panel.setAlphaValue_(1.0)
            self._panel.makeKeyAndOrderFront_(None)
        else:
            if not reversing:
                # Fresh appear: tucked up under the menu bar, transparent.
                start = NSMakeRect(rest.origin.x, rest.origin.y + APPEAR_OFFSET,
                                   rest.size.width, rest.size.height)
                self._panel.setFrame_display_(start, False)
                self._panel.setAlphaValue_(0.0)
            # else: mid-dismiss — slide back from where it is, at its alpha.
            self._panel.makeKeyAndOrderFront_(None)
            self._animating_in = True

            def done():
                self._animating_in = False

            self._slide.start((rest.origin.x, rest.origin.y), APPEAR_DURATION,
                              ease_out_expo, 1.0, APPEAR_FADE, done)
        self._send_anchor()
        # A click anywhere outside the panel — in another app, on the desktop,
        # on the menu bar — dismisses it, like the Dock's own menus. The
        # non-activating panel does not make us the active app, so
        # resignKey alone would not fire for clicks in whatever app is
        # frontmost; the global monitor covers that case.
        if self._monitor is None:
            mask = (NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown)
            self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, lambda _e: self.close())
        self._webview.evaluateJavaScript_completionHandler_(
            "window.dockShown && window.dockShown();", None)

    def set_tray(self, tray: dict | None) -> None:
        """Move the glass to a new tray rect without touching the panel."""
        if not tray:
            return
        try:
            self._tray = tuple(float(tray[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            return
        self._layout_glass()
        if self._resizing:
            # A view frame change makes AppKit re-resolve the cursor (to the
            # arrow): keep the resize cursor up for the whole drag.
            AppKit.NSCursor.resizeUpDownCursor().set()

    def set_resizing(self, active: bool) -> None:
        """The page is dragging the separator (Dock-style resize): hold the
        up/down resize cursor natively, since the web view's CSS cursor does
        not survive the panel and glass frame changes made during the drag."""
        if active == self._resizing:
            return
        self._resizing = active
        if active:
            AppKit.NSCursor.resizeUpDownCursor().push()
        else:
            AppKit.NSCursor.pop()

    def _layout_glass(self) -> None:
        if self._tray is None:
            self._glass.setHidden_(True)
            return
        _w, h = self._size
        tx, ty, tw, th = self._tray
        # Page coordinates are canvas coordinates; the panel shows the canvas
        # from (rx, 0), so shift by rx and flip.
        self._glass.setFrame_(NSMakeRect(tx - self._rx, h - ty - th, tw, th))
        self._glass.setHidden_(False)

    def resize_to(self, width, height, tray: dict | None = None, x=0) -> None:
        """The page reports the canvas region it occupies: ``x`` (its left
        edge in canvas px; top is always 0) and its size."""
        try:
            w = float(width)
            h = float(height)
            rx = float(x or 0)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(w) and math.isfinite(h) and math.isfinite(rx)):
            return
        w = min(max(math.ceil(w), MIN_SIZE[0]), MAX_SIZE[0])
        h = min(max(math.ceil(h), MIN_SIZE[1]), MAX_SIZE[1])
        self._size = (w, h)
        self._rx = float(min(max(math.floor(rx), 0), CANVAS[0] - w))
        if tray:
            try:
                self._tray = tuple(float(tray[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                self._tray = None
        self._layout()
        # The page always has a fresh anchor after a report — including its
        # first, hidden one, so the first show lands right. It re-reports only
        # when the anchor changed, so this does not loop.
        self._send_anchor()
        if self._resizing:  # frame changes reset the cursor: see set_resizing
            AppKit.NSCursor.resizeUpDownCursor().set()

    # ---- status item -------------------------------------------------------------

    def status_item_clicked(self) -> None:
        event = NSApp.currentEvent()
        right = False
        if event is not None:
            right = int(event.type()) == NSEventTypeRightMouseUp or bool(
                int(event.modifierFlags()) & NSEventModifierFlagControl)
        if right:
            self.close()
            self._show_utility_menu()
        else:
            self.toggle_popover()

    def _take_over_status_item(self) -> None:
        self._statusitem.setMenu_(None)
        button = self._statusitem.button()
        button.setTarget_(self._target)
        button.setAction_(b"statusItemClicked:")
        button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        logger.info("status item: menu removed, Dock panel on click")

    def _show_utility_menu(self) -> None:
        name = _flavor.display_name()
        menu = NSMenu.alloc().initWithTitle_(name)

        def item(title, action, key=""):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            it.setTarget_(self._target)
            menu.addItem_(it)

        # The same actions the pinned-file popover's overflow menu carries
        # (menubar_pin._show_overflow_menu), so swapping surfaces loses no door.
        item(f"Open {name}", b"openHome:")
        if self.actions.get("show_tasks"):
            item("Tasks…", b"showTasks:")
        item("Open in Browser", b"openBrowser:")
        if self.actions.get("copy_url"):
            item("Copy URL", b"copyUrl:")
        if self.actions.get("show_launcher"):
            item("Search Apps…", b"showLauncher:")
        menu.addItem_(NSMenuItem.separatorItem())
        item("Open App Logs", b"openLogs:")
        if self.actions.get("save_diagnostics"):
            item("Save Diagnostics…", b"saveDiagnostics:")
        menu.addItem_(NSMenuItem.separatorItem())
        item(f"Quit {name}", b"quitApp:", "q")
        # With a menu set, a click on the button opens it and fires no
        # action; set it just for this synthetic click, then take it off so
        # the next real click reaches statusItemClicked: again.
        self._statusitem.setMenu_(menu)
        self._statusitem.button().performClick_(None)
        self._statusitem.setMenu_(None)

    # ---- per-tile context menu: a real NSMenu, laid out like the Dock's ------------

    def show_item_menu(self, data: dict) -> None:
        """The page's ``{type: "menu", kind: "bot"|"app"|"appfile", id | path,
        name, pinned, running, x, y}``: the tile's menu at (x, y) in page px."""
        kind = str(data.get("kind") or "")
        key = str(data.get("id") or "") if kind == "bot" else str(data.get("path") or "")
        if kind not in ("bot", "app", "appfile") or not key:
            return
        tile = f"{kind}:{key}"
        name = str(data.get("name") or (os.path.basename(key) if kind != "bot" else "Bot"))
        pinned = bool(data.get("pinned"))
        running = bool(data.get("running"))
        menu = NSMenu.alloc().initWithTitle_(name)
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._menu_delegate)

        def add(m, title, action=None, key="", enabled=True, state=False, indent=0):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            it.setTarget_(self._target if action else None)
            it.setRepresentedObject_(tile)
            it.setEnabled_(enabled and action is not None)
            it.setState_(1 if state else 0)
            it.setIndentationLevel_(indent)
            m.addItem_(it)
            return it

        # Header, like the Dock's window list: the tile's name, checked while
        # the bot is busy. "Open" always: a busy bot still needs showing.
        add(menu, name, None, state=running, enabled=False)
        menu.addItem_(NSMenuItem.separatorItem())
        add(menu, "Open", b"itemOpen:")
        menu.addItem_(NSMenuItem.separatorItem())

        options = NSMenu.alloc().initWithTitle_("Options")
        options.setAutoenablesItems_(False)
        if pinned:
            add(options, "Remove from Dock", b"itemUnpin:")
        else:
            add(options, "Keep in Dock", b"itemPin:")
        options.addItem_(NSMenuItem.separatorItem())
        if kind != "bot":
            add(options, "Show in Finder", b"itemReveal:")
        add(options, "Open in Browser", b"itemBrowser:")
        opt_item = add(menu, "Options", None)
        opt_item.setEnabled_(True)
        menu.setSubmenu_forItem_(options, opt_item)

        try:
            x = float(data.get("x", 0))
            y = float(data.get("y", 0))
        except (TypeError, ValueError):
            x = y = 0.0
        # WKWebView is flipped, so page coordinates are view coordinates.
        menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(x, y), self._webview)

    def refresh(self) -> None:
        """Have the page re-read ``GET /api/dock`` (after a pin change)."""
        self._webview.evaluateJavaScript_completionHandler_(
            "window.dockShown && window.dockShown();", None)

    def _origin(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def item_open(self, kind: str, key: str) -> None:
        self.close()
        hook = window_policy.native_hooks.get("dock_open")
        if hook is not None and kind in ("bot", "app", "appfile") and key:
            hook(kind, key)

    def item_pin(self, kind: str, key: str, pinned: bool) -> None:
        try:
            if kind == "bot":
                dock_store.set_bot_pinned(key, pinned)
            else:
                dock_store.set_pinned(key, pinned)
        except Exception:  # noqa: BLE001 — a vanished bot / app: just refresh
            logger.warning("dock pin %s %s -> %s failed", kind, key, pinned, exc_info=True)
        self.refresh()

    def item_reveal(self, kind: str, key: str) -> None:
        if kind == "bot":
            return
        try:
            dock_store.reveal(key)
        except ValueError:
            logger.warning("dock reveal refused: %s", key)

    def item_browser(self, kind: str, key: str) -> None:
        self.close()
        if kind == "bot":
            _open_external(self._origin() + dock_store.bot_view_path(key))
            return
        row = dock_store.row_for_path(key)
        if row is not None:
            _open_external(self._origin() + row["url"])

    # ---- panel -------------------------------------------------------------------

    def _build_panel(self) -> None:
        w, h = INITIAL_SIZE
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = _DockPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        # No window shadow: on a non-opaque window AppKit traces every opaque
        # pixel (glass AND tiles) with a dark hairline. The Dock has none.
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

        self._glass = _make_glass(w, h)
        root.addSubview_(self._glass)

        config = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(self._handler, MESSAGE_NAME)
        config.setUserContentController_(ucc)
        # Same marker as the app windows: the page skips its dev backdrop and
        # its own glass when it sees "FusedRender/" in the UA ("FusedRenderDock"
        # says which host it is in). Under BOTH flavors — mac_window keeps one
        # marker for the same reason.
        config.setApplicationNameForUserAgent_(f"{USER_AGENT_MARKER} FusedRenderDock")
        try:
            config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except Exception:  # noqa: BLE001
            pass

        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, h - CANVAS[1], CANVAS[0], CANVAS[1]), config)
        # Transparent: only the tiles paint; the glass beneath is native.
        try:
            self._webview.setValue_forKey_(False, "drawsBackground")
        except Exception:  # noqa: BLE001 — opaque page then; still works
            logger.debug("drawsBackground not settable", exc_info=True)
        try:
            self._webview.setUnderPageBackgroundColor_(NSColor.clearColor())
        except Exception:  # noqa: BLE001 — older macOS
            pass
        self._webview.setNavigationDelegate_(self._web_delegate)
        self._webview.setUIDelegate_(self._web_delegate)
        root.addSubview_(self._webview)

        panel.setContentView_(root)
        self._panel = panel
        self._slide = _Slide.alloc().initWithWindow_(panel)
        self._layout()

    def _layout(self) -> None:
        """Panel = the reported canvas region, placed so the canvas stays put
        on screen; webview = the whole canvas, offset so the region sits at
        the panel's top-left; glass = the tray rect (flipped into AppKit's
        bottom-left coordinates). One frame write per region change. While
        the panel is sliding, the slide is retargeted to the new rest spot
        instead of snapped (the page reports right after ``dockShown``)."""
        w, h = self._size
        frame = self._panel.frame()
        key = (self._rx, w, h)
        # Views first, window frame second, and no forced display: the web
        # view's offset (canvas (rx, 0) → panel top-left) and the glass must
        # be in place before the panel shows its new region, and all of it
        # commits in the one Core Animation transaction at the end of this
        # run-loop pass. A display:YES here would paint the new viewport over
        # the old canvas slice — exactly the sideways flash this avoids.
        self._webview.setFrame_(NSMakeRect(-self._rx, h - CANVAS[1], CANVAS[0], CANVAS[1]))
        self._layout_glass()
        if not self._panel.isVisible():
            if key != self._applied:
                self._panel.setFrame_display_(self._rest_frame(), False)
        elif key != self._applied:
            rest = self._rest_frame()
            if self._slide.running():
                # Resize in place: keep the panel's current offset from its
                # destination, then bend the slide to the new destination.
                lift = 0.0 if self._animating_in else DISMISS_OFFSET
                old_to = self._slide.target()
                dx, dy = frame.origin.x - old_to[0], frame.origin.y - old_to[1]
                self._panel.setFrame_display_(
                    NSMakeRect(rest.origin.x + dx, rest.origin.y + lift + dy, w, h), False)
                self._slide.retarget((rest.origin.x, rest.origin.y + lift))
            else:
                self._panel.setFrame_display_(rest, False)
            self._send_anchor()
        elif not self._slide.running():
            self._place()  # same region, but the status item may have moved
        self._applied = key
        self._panel.contentView().setFrame_(NSMakeRect(0, 0, w, h))

    def _anchor(self):
        """Status-item centre x, menu-bar bottom y, and the screen's usable
        x-range (screen points, 4px margins in); None before the status item
        is in a window."""
        button = self._statusitem.button()
        bwin = button.window()
        if bwin is None:
            return None
        brect = bwin.convertRectToScreen_(button.convertRect_toView_(button.bounds(), None))
        cx = brect.origin.x + brect.size.width / 2
        top = brect.origin.y
        screen = bwin.screen()
        if screen is None:
            return cx, top, -math.inf, math.inf
        vis = screen.visibleFrame()
        return cx, top, vis.origin.x + 4, vis.origin.x + vis.size.width - 4

    def _canvas_x(self, cx: float, left: float, right: float) -> float:
        """Screen x of the canvas's left edge: centred under the status item,
        then pushed onto the screen (right edge first, then left) so the
        canvas — and with it the room the tray may grow into — covers as much
        of the screen as it can. On a screen narrower than the canvas it
        starts at the left edge and overhangs the right; the anchor's range
        then spans the whole screen."""
        ox = cx - CANVAS[0] / 2
        if math.isfinite(right):
            ox = min(ox, right - CANVAS[0])
        if math.isfinite(left):
            ox = max(ox, left)
        return ox

    def _rest_frame(self):
        """Where the panel rests: the reported region of the canvas, the
        canvas centred under the status item and hanging from the menu bar.
        Keeping the tray on screen is the page's job (see ``_send_anchor``)."""
        w, h = self._size
        a = self._anchor()
        if a is None:
            f = self._panel.frame()
            return NSMakeRect(f.origin.x, f.origin.y + f.size.height - h, w, h)
        cx, top, left, right = a
        x = self._canvas_x(cx, left, right) + self._rx
        y = top - GAP_BELOW_MENU_BAR - h
        return NSMakeRect(x, y, w, h)

    def _place(self) -> None:
        """Centered under the status item, hanging from the menu bar."""
        rest = self._rest_frame()
        if self._slide.running() and self._animating_in:
            self._slide.retarget((rest.origin.x, rest.origin.y))
        else:
            self._panel.setFrameOrigin_((rest.origin.x, rest.origin.y))
        self._send_anchor()

    def _send_anchor(self) -> None:
        """Tell the page, in canvas (page) coordinates, where the status
        item's centre is and the leftmost/rightmost x the tray may occupy
        (screen edges, 4px in, clipped to the canvas). The page centres the
        tray under the icon and clamps it to that range; the panel just
        frames whatever region the page reports. Screen points are CSS px.
        The page re-reports when the anchor changes, so this is idempotent."""
        a = self._anchor()
        if a is None:
            return
        cx, _top, left, right = a
        ox = self._canvas_x(cx, left, right)
        left = max(0.0, left - ox) if math.isfinite(left) else 0.0
        right = min(float(CANVAS[0]), right - ox) if math.isfinite(right) else float(CANVAS[0])
        self._webview.evaluateJavaScript_completionHandler_(
            "window.dockAnchor && window.dockAnchor({icon:%r,left:%r,right:%r});"
            % (float(cx - ox), float(left), float(right)), None)

    def _url(self) -> str:
        return f"http://127.0.0.1:{self._port}/dock"

    def _load(self) -> None:
        url = self._url()
        self._webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self._loaded = True
        logger.info("dock panel loading %s", url)
