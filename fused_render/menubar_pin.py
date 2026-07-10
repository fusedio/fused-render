"""Menu-bar pinned view — the status item's single surface (SPEC §25, D97/D98).

Any click on the status item toggles an NSPopover: a native header row with
every app action (the status-item NSMenu is gone, D98) above a WKWebView of
the pinned file's `/embed/<path>` page. Dragging the popover off the menu bar
detaches it into a floating always-on-top window (PV-5).

macOS-only, like rumps. app.py imports this lazily (inside main(), after the
AppKit run loop is up) and falls back to attaching the plain rumps menu if the
import or construction fails (PV-8) — the app is never left unquittable.

All methods must be called on the main thread. app.py hops threads with
PyObjCTools.AppHelper.callAfter where needed (server-ready arrives on a
background thread).
"""
import logging
import os
from urllib.parse import quote

import objc
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSButton,
    NSControlSizeSmall,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseUp,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSMakeSize,
    NSModalPanelWindowLevel,
    NSModalResponseOK,
    NSObject,
    NSOpenPanel,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSRectEdgeMinY,
    NSStackView,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSView,
    NSViewController,
    NSViewHeightSizable,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
)
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

from fused_render import pin_store

logger = logging.getLogger("fused_render")

POPOVER_WIDTH = 420
HEADER_HEIGHT = 32
BODY_HEIGHT = 560

_PLACEHOLDER_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  body {{ font: 13px -apple-system, sans-serif; color: #808080;
         display: flex; align-items: center; justify-content: center;
         height: 96vh; margin: 0; background: #ffffff; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #1e1e1e; }} }}
</style></head><body>{message}</body></html>"""


class _ActionsTarget(NSObject):
    """Objective-C action target for the status button and header buttons."""

    def initWithController_(self, controller):
        self = objc.super(_ActionsTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def statusItemClicked_(self, _sender):
        self._controller.toggle_popover()

    def openBrowser_(self, _sender):
        self._controller._actions["open_browser"]()

    def copyUrl_(self, _sender):
        self._controller._actions["copy_url"]()

    def pinFile_(self, _sender):
        self._controller.choose_and_pin()

    def unpin_(self, _sender):
        self._controller.clear_pin()

    def openLogs_(self, _sender):
        self._controller._actions["open_logs"]()

    def quitApp_(self, _sender):
        self._controller._actions["quit"]()


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
    """Owns pin state, the status-item click, and the popover (SPEC §25).

    Built after the rumps run loop is up (the status item exists only then).
    `actions` supplies the app-level callbacks for the header row (PV-3):
    open_browser / copy_url / open_logs / quit.
    """

    def __init__(self, statusitem, port: int, app_support_dir: str, actions: dict):
        self._statusitem = statusitem
        self._port = port
        self._app_support_dir = app_support_dir
        self._actions = actions
        self._pinned_path = pin_store.load_pin(app_support_dir)
        self._server_ready = False
        self._target = _ActionsTarget.alloc().initWithController_(self)
        self._popover_delegate = _PopoverDelegate.alloc().initWithController_(self)
        if self._pinned_path:
            logger.info("pinned view restored from pin.json: %s", self._pinned_path)
        self._build_popover()
        self._take_over_status_item()

    # ---- public API (main thread only) --------------------------------------

    @property
    def pinned_path(self) -> str | None:
        return self._pinned_path

    def server_ready(self) -> None:
        self._server_ready = True
        self._load_body()

    def choose_and_pin(self) -> None:
        """Header "Pin…"/"Change…" button: NSOpenPanel, then pin (PV-3).

        Deferred one run-loop tick: started synchronously from the button's
        action (inside the popover's event handling), the modal panel gets its
        clicks eaten. callAfter runs it from a clean run-loop pass — the
        transient popover closes on its own and reopens from set_pin.
        """
        AppHelper.callAfter(self._run_open_panel)

    def set_pin(self, fs_path: str) -> None:
        fs_path = os.path.abspath(fs_path)
        pin_store.save_pin(self._app_support_dir, fs_path)
        self._pinned_path = fs_path
        logger.info("pinned view set: %s", fs_path)
        self._load_body()
        self._update_header()
        self.show_popover()

    def clear_pin(self) -> None:
        if self._pinned_path is None:
            return
        pin_store.clear_pin(self._app_support_dir)
        logger.info("pinned view cleared (was %s)", self._pinned_path)
        self._pinned_path = None
        self._load_body()
        self._update_header()

    def show_popover(self) -> None:
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
        if self._popover.isShown():
            self._popover.close()
        else:
            self.show_popover()

    # ---- status item (PV-2) ---------------------------------------------------

    def _take_over_status_item(self) -> None:
        # rumps attached its NSMenu in initializeStatusBar; with a menu set,
        # AppKit opens it on every click and never fires the button's action.
        # Remove it for good — every click, any button, toggles the popover.
        self._statusitem.setMenu_(None)
        button = self._statusitem.button()
        button.setTarget_(self._target)
        button.setAction_(b"statusItemClicked:")
        button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        logger.info("status item: menu removed, popover on click")

    # ---- popover construction (PV-3/PV-4) -------------------------------------

    def _build_popover(self) -> None:
        total_height = HEADER_HEIGHT + BODY_HEIGHT
        container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, POPOVER_WIDTH, total_height)
        )

        config = WKWebViewConfiguration.alloc().init()
        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, POPOVER_WIDTH, BODY_HEIGHT), config
        )
        self._webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(self._webview)

        def button(title, action):
            b = NSButton.buttonWithTitle_target_action_(title, self._target, action)
            b.setControlSize_(NSControlSizeSmall)
            b.setFont_(NSFont.systemFontOfSize_(11))
            return b

        self._pin_button = button("Pin…", b"pinFile:")
        self._unpin_button = button("Unpin", b"unpin:")
        header = NSStackView.stackViewWithViews_([
            button("Open in Browser", b"openBrowser:"),
            button("Copy URL", b"copyUrl:"),
            self._pin_button,
            self._unpin_button,
            button("Logs", b"openLogs:"),
            button("Quit", b"quitApp:"),
        ])
        header.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
        header.setSpacing_(4)
        header.setFrame_(
            NSMakeRect(8, BODY_HEIGHT + 5, POPOVER_WIDTH - 16, HEADER_HEIGHT - 10)
        )
        header.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        container.addSubview_(header)

        vc = NSViewController.alloc().initWithNibName_bundle_(None, None)
        vc.setView_(container)
        popover = NSPopover.alloc().init()
        popover.setContentViewController_(vc)
        popover.setContentSize_(NSMakeSize(POPOVER_WIDTH, total_height))
        popover.setBehavior_(NSPopoverBehaviorTransient)
        popover.setDelegate_(self._popover_delegate)
        self._popover = popover

        self._load_body()
        self._update_header()

    def _update_header(self) -> None:
        pinned = self._pinned_path is not None
        self._pin_button.setTitle_("Change…" if pinned else "Pin…")
        self._unpin_button.setHidden_(not pinned)

    # ---- webview body ----------------------------------------------------------

    def _pin_url(self) -> str:
        # Same URL shape shell panes iframe: chrome-free, registry-dispatched.
        return f"http://127.0.0.1:{self._port}/embed{quote(self._pinned_path)}"

    def _load_body(self) -> None:
        """Point the webview at the right content for the current state.

        Called only on state transitions (built / pin set / pin cleared /
        server ready) — never on show, so view state survives close/reopen
        (PV-4).
        """
        if not self._server_ready:
            message = "Starting…"
        elif self._pinned_path is None:
            message = "No file pinned — use Pin… above"
        else:
            url = self._pin_url()
            self._webview.loadRequest_(
                NSURLRequest.requestWithURL_(NSURL.URLWithString_(url))
            )
            logger.info("pinned webview loading %s", url)
            return
        self._webview.loadHTMLString_baseURL_(
            _PLACEHOLDER_HTML.format(message=message), None
        )

    # ---- open panel (PV-3) -----------------------------------------------------

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
        # spaces (same FullScreenAuxiliary story as the popover, PV-5).
        panel.setLevel_(NSModalPanelWindowLevel)
        panel.setCollectionBehavior_(
            panel.collectionBehavior()
            | NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        if panel.runModal() != NSModalResponseOK or panel.URL() is None:
            return
        self.set_pin(panel.URL().path())

    # ---- detach (PV-5) ----------------------------------------------------------

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
