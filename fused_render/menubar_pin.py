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
    NSBox,
    NSBoxSeparator,
    NSButton,
    NSColor,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseUp,
    NSFloatingWindowLevel,
    NSFont,
    NSImage,
    NSLineBreakByTruncatingMiddle,
    NSMakePoint,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSModalPanelWindowLevel,
    NSModalResponseOK,
    NSObject,
    NSOpenPanel,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSRectEdgeMinY,
    NSTextField,
    NSView,
    NSViewController,
    NSViewHeightSizable,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewWidthSizable,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskResizable,
)
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

from fused_render import pin_store

logger = logging.getLogger("fused_render")

POPOVER_WIDTH = 420
BAR_HEIGHT = 30
# Square webview by default (owner call 2026-07-10); the popover is
# user-resizable (PV-4) and the chosen size is remembered in pin.json.
BODY_HEIGHT = POPOVER_WIDTH

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

    def pinOrUnpin_(self, _sender):
        # Bottom-bar pin icon is a toggle: pin when empty, unpin when pinned.
        if self._controller.pinned_path is None:
            self._controller.choose_and_pin()
        else:
            self._controller.clear_pin()

    def openLogs_(self, _sender):
        self._controller._actions["open_logs"]()

    def quitApp_(self, _sender):
        self._controller._actions["quit"]()

    def showMore_(self, sender):
        self._controller._show_overflow_menu(sender)


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

    def popoverDidClose_(self, _notification):
        self._controller._save_current_size()

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
            # NSPopover has no official user-resize; adding Resizable to its
            # window's style mask enables edge-drag resizing (PV-4). The
            # chosen size is saved on close and becomes the new default.
            window.setStyleMask_(window.styleMask() | NSWindowStyleMaskResizable)

    def _save_current_size(self) -> None:
        view = self._popover.contentViewController().view()
        size = view.frame().size
        width, height = int(size.width), int(size.height)
        if (width, height) == pin_store.load_size(self._app_support_dir):
            return
        if width < 200 or height < 150:
            return  # degenerate mid-detach frames; never remember those
        pin_store.save_size(self._app_support_dir, width, height)
        # Keep the popover's own notion in sync so the next show uses it even
        # when AppKit rebuilds the popover window.
        self._popover.setContentSize_(NSMakeSize(width, height))
        logger.info("popover size remembered: %dx%d", width, height)

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
        """Content-first layout (PV-3/PV-4): the webview is the whole popover
        except a slim bottom bar — truncating filename on the left, three
        borderless SF-Symbol buttons on the right (pin, open-in-browser, and
        an overflow "…" carrying Copy URL / Unpin / Logs / Quit). Menu-bar
        popover convention: chrome whispers, content is the hero.

        Default size gives a square webview; a remembered user resize
        (pin.json "size") overrides it.
        """
        width, total_height = (
            pin_store.load_size(self._app_support_dir)
            or (POPOVER_WIDTH, BODY_HEIGHT + BAR_HEIGHT)
        )
        container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width, total_height)
        )

        config = WKWebViewConfiguration.alloc().init()
        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, BAR_HEIGHT, width, total_height - BAR_HEIGHT), config
        )
        self._webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(self._webview)

        separator = NSBox.alloc().initWithFrame_(
            NSMakeRect(0, BAR_HEIGHT - 1, width, 1)
        )
        separator.setBoxType_(NSBoxSeparator)
        separator.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        container.addSubview_(separator)

        def icon_button(symbol, action, tooltip, x):
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                symbol, tooltip
            )
            b = NSButton.buttonWithImage_target_action_(image, self._target, action)
            b.setBordered_(False)
            b.setToolTip_(tooltip)
            b.setContentTintColor_(NSColor.secondaryLabelColor())
            b.setFrame_(NSMakeRect(x, 4, 26, 22))
            b.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
            container.addSubview_(b)
            return b

        right = width
        self._more_button = icon_button(
            "ellipsis.circle", b"showMore:", "More", right - 34
        )
        self._browser_button = icon_button(
            "safari", b"openBrowser:", "Open in Browser", right - 64
        )
        self._pin_button = icon_button("pin", b"pinOrUnpin:", "Pin a file…", right - 94)

        label = NSTextField.labelWithString_("")
        label.setFont_(NSFont.systemFontOfSize_(11))
        label.setTextColor_(NSColor.secondaryLabelColor())
        label.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        label.setFrame_(NSMakeRect(12, 7, width - 12 - 100, 16))
        label.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        container.addSubview_(label)
        self._file_label = label

        vc = NSViewController.alloc().initWithNibName_bundle_(None, None)
        vc.setView_(container)
        popover = NSPopover.alloc().init()
        popover.setContentViewController_(vc)
        popover.setContentSize_(NSMakeSize(width, total_height))
        popover.setBehavior_(NSPopoverBehaviorTransient)
        popover.setDelegate_(self._popover_delegate)
        self._popover = popover

        self._load_body()
        self._update_header()

    def _update_header(self) -> None:
        pinned = self._pinned_path is not None
        tooltip = "Unpin" if pinned else "Pin a file…"
        self._pin_button.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "pin.slash" if pinned else "pin", tooltip
            )
        )
        self._pin_button.setToolTip_(tooltip)
        self._file_label.setStringValue_(
            os.path.basename(self._pinned_path) if pinned else "Nothing pinned"
        )

    def _show_overflow_menu(self, sender) -> None:
        menu = NSMenu.alloc().init()

        def add(title, action):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, action, ""
            )
            item.setTarget_(self._target)
            menu.addItem_(item)

        add("Copy URL", b"copyUrl:")
        if self._pinned_path is not None:
            add("Change Pinned File…", b"pinFile:")
        menu.addItem_(NSMenuItem.separatorItem())
        add("Open Logs", b"openLogs:")
        add("Quit fused-render", b"quitApp:")
        menu.popUpMenuPositioningItem_atLocation_inView_(
            None, NSMakePoint(0, sender.bounds().size.height + 4), sender
        )

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
            message = "Nothing pinned yet — click the pin below to choose a file"
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
