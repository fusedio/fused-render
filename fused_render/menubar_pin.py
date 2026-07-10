"""Menu-bar pinned view — NSPopover + WKWebView on the status item (SPEC §25, D97).

macOS-only, like rumps. app.py imports this lazily (inside main(), after the
AppKit run loop is up) and degrades to no-pin-feature if the import fails —
e.g. an older env without pyobjc-framework-WebKit.

All methods must be called on the main thread. app.py hops threads with
PyObjCTools.AppHelper.callAfter where needed (server-ready arrives on a
background thread).

Click routing (PV-2): rumps assigns the NSMenu to the status item, which makes
AppKit open it on every click and never fire the button's action. With a pin
set we take the menu OFF the status item and route clicks ourselves: left
click toggles the popover, right/ctrl click pops the same menu manually.
Unpinning restores rumps's arrangement exactly.
"""
import logging
import os
from urllib.parse import quote

import objc
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseUp,
    NSEventModifierFlagControl,
    NSEventTypeRightMouseUp,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSMakeSize,
    NSModalPanelWindowLevel,
    NSModalResponseOK,
    NSObject,
    NSOpenPanel,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSRectEdgeMinY,
    NSViewController,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
)
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

from fused_render import pin_store

logger = logging.getLogger("fused_render")

POPOVER_WIDTH = 420
POPOVER_HEIGHT = 560


class _StatusButtonTarget(NSObject):
    """Action target for the status-item button while a pin is active."""

    def initWithController_(self, controller):
        self = objc.super(_StatusButtonTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def statusItemClicked_(self, _sender):
        self._controller._on_status_click()


class _PopoverDelegate(NSObject):
    """Detach support (PV-5): drag the popover off → floating window."""

    def initWithController_(self, controller):
        self = objc.super(_PopoverDelegate, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def popoverShouldDetach_(self, _popover):
        return True

    def popoverDidDetach_(self, _popover):
        # The content view has just been re-hosted in the detached window.
        # Raise it above other apps' windows one run-loop tick later — at
        # notification time the window swap may not have finished.
        AppHelper.callAfter(self._controller._float_detached_window)


class PinController:
    """Owns pin state, click routing, and the popover (SPEC §25).

    Built after the rumps run loop is up (the status item exists only then).
    The webview is created lazily on first show and kept alive (PV-4).
    """

    def __init__(self, statusitem, nsmenu, port: int, app_support_dir: str, menu_items=None):
        self._statusitem = statusitem
        self._nsmenu = nsmenu
        self._port = port
        self._app_support_dir = app_support_dir
        # {"pin": NSMenuItem, "show": NSMenuItem, "unpin": NSMenuItem} — the
        # three PV-3 items, toggled/renamed to match pin state (PV-3).
        self._menu_items = menu_items or {}
        self._pinned_path = pin_store.load_pin(app_support_dir)
        self._server_ready = False
        self._popover = None
        self._webview = None
        self._button_target = _StatusButtonTarget.alloc().initWithController_(self)
        self._popover_delegate = _PopoverDelegate.alloc().initWithController_(self)
        self._routing_taken_over = False
        if self._pinned_path:
            logger.info("pinned view restored from pin.json: %s", self._pinned_path)
        self._update_routing()
        self._update_menu_items()

    # ---- public API (main thread only) --------------------------------------

    @property
    def pinned_path(self) -> str | None:
        return self._pinned_path

    def server_ready(self) -> None:
        self._server_ready = True
        self._update_routing()

    def choose_and_pin(self) -> None:
        """'Pin File…' menu item: NSOpenPanel, then pin the choice (PV-3).

        Deferred one run-loop tick: the menu-item action fires while the menu's
        tracking session is winding down, and a modal panel started inside it
        gets its clicks eaten (the panel dismisses on first click). callAfter
        runs the panel from a clean run-loop pass.
        """
        AppHelper.callAfter(self._run_open_panel)

    def _run_open_panel(self) -> None:
        # The panel must be able to become KEY or it orders out the moment it
        # is focused. A source-run interpreter (no bundle) has activation
        # policy Prohibited, whose windows can never be key — lift it to
        # Accessory (key-able windows, no Dock icon, no space switch on
        # activate). The packaged .app is already Regular and is left alone.
        if NSApp.activationPolicy() == NSApplicationActivationPolicyProhibited:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        NSApp.activateIgnoringOtherApps_(True)
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)  # directories render too (D81)
        panel.setAllowsMultipleSelection_(False)
        panel.setTitle_("Pin a file to the menu bar")
        panel.setPrompt_("Pin")
        # Menu-bar app: no regular windows to key off, so float the panel
        # above the frontmost app's windows — and let it join fullscreen-app
        # spaces (same FullScreenAuxiliary story as the popover, PV-2 UX).
        panel.setLevel_(NSModalPanelWindowLevel)
        panel.setCollectionBehavior_(
            panel.collectionBehavior()
            | NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        if panel.runModal() != NSModalResponseOK or panel.URL() is None:
            return
        self.set_pin(panel.URL().path())

    def set_pin(self, fs_path: str) -> None:
        fs_path = os.path.abspath(fs_path)
        pin_store.save_pin(self._app_support_dir, fs_path)
        self._pinned_path = fs_path
        logger.info("pinned view set: %s", fs_path)
        if self._webview is not None:
            self._load_pin_url()
        self._update_routing()
        self._update_menu_items()
        self.show_popover()

    def clear_pin(self) -> None:
        if self._pinned_path is None:
            return
        pin_store.clear_pin(self._app_support_dir)
        logger.info("pinned view cleared (was %s)", self._pinned_path)
        self._pinned_path = None
        if self._popover is not None and self._popover.isShown():
            self._popover.close()
        self._update_routing()
        self._update_menu_items()

    def _update_menu_items(self) -> None:
        """PV-3: menu reflects pin state — no dead items.

        Hides "Show Pinned View"/"Unpin" while nothing is pinned and renames
        the pin item. Titles change on the NSMenuItem only; rumps's callback
        registry is keyed by the original construction title and stays intact.
        """
        pinned = self._pinned_path is not None
        pin_item = self._menu_items.get("pin")
        if pin_item is not None:
            pin_item.setTitle_("Change Pinned File…" if pinned else "Pin File…")
        for key in ("show", "unpin"):
            item = self._menu_items.get(key)
            if item is not None:
                item.setHidden_(not pinned)

    def show_popover(self) -> None:
        """'Show Pinned View' menu item — and the left-click path (PV-2/PV-3)."""
        if self._pinned_path is None or not self._server_ready:
            return
        self._ensure_popover()
        button = self._statusitem.button()
        self._popover.showRelativeToRect_ofView_preferredEdge_(
            button.bounds(), button, NSRectEdgeMinY
        )
        # Without FullScreenAuxiliary the popover window is barred from
        # fullscreen-app spaces: a click there "opens" it invisibly on the
        # desktop space. Re-applied every show — the popover can recreate its
        # window.
        window = self._webview.window()
        if window is not None:
            window.setCollectionBehavior_(
                window.collectionBehavior()
                | NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary
            )

    def toggle_popover(self) -> None:
        if self._popover is not None and self._popover.isShown():
            self._popover.close()
        else:
            self.show_popover()

    # ---- click routing (PV-2) ------------------------------------------------

    def _update_routing(self) -> None:
        want_takeover = self._pinned_path is not None and self._server_ready
        if want_takeover == self._routing_taken_over:
            return
        button = self._statusitem.button()
        if want_takeover:
            self._statusitem.setMenu_(None)
            button.setTarget_(self._button_target)
            button.setAction_(b"statusItemClicked:")
            button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        else:
            button.setTarget_(None)
            button.setAction_(None)
            self._statusitem.setMenu_(self._nsmenu)
        self._routing_taken_over = want_takeover
        logger.info("status-item click routing: %s", "popover" if want_takeover else "menu")

    def _on_status_click(self) -> None:
        event = NSApp.currentEvent()
        is_menu_click = event is not None and (
            event.type() == NSEventTypeRightMouseUp
            or (event.modifierFlags() & NSEventModifierFlagControl)
        )
        if is_menu_click:
            # Deprecated since 10.14 but still the one-liner that works; the
            # supported alternative (setMenu_ + performClick_ + clear in
            # menuDidClose) needs another delegate for zero visible benefit.
            self._statusitem.popUpStatusItemMenu_(self._nsmenu)
        else:
            self.toggle_popover()

    # ---- popover / webview ----------------------------------------------------

    def _pin_url(self) -> str:
        # Same URL shape shell panes iframe: chrome-free, registry-dispatched.
        return f"http://127.0.0.1:{self._port}/embed{quote(self._pinned_path)}"

    def _load_pin_url(self) -> None:
        url = self._pin_url()
        self._webview.loadRequest_(
            NSURLRequest.requestWithURL_(NSURL.URLWithString_(url))
        )
        logger.info("pinned webview loading %s", url)

    def _ensure_popover(self) -> None:
        if self._popover is not None:
            return
        config = WKWebViewConfiguration.alloc().init()
        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_HEIGHT), config
        )
        self._load_pin_url()
        vc = NSViewController.alloc().initWithNibName_bundle_(None, None)
        vc.setView_(self._webview)
        popover = NSPopover.alloc().init()
        popover.setContentViewController_(vc)
        popover.setContentSize_(NSMakeSize(POPOVER_WIDTH, POPOVER_HEIGHT))
        popover.setBehavior_(NSPopoverBehaviorTransient)
        popover.setDelegate_(self._popover_delegate)
        self._popover = popover

    def _float_detached_window(self) -> None:
        window = self._webview.window() if self._webview is not None else None
        if window is None:
            logger.warning("popover detached but webview has no window; not floating it")
            return
        window.setLevel_(NSFloatingWindowLevel)
        window.setCollectionBehavior_(
            window.collectionBehavior()
            | NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        logger.info("pinned view detached into floating window")
