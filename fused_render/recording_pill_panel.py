"""The recording pill: a small floating panel shown while audio is recorded (macOS).

The ``/recording-pill`` page (a Vite entry, frontend/recording-pill.html →
shell-dist/recording-pill.html, served by routers/capture.py) inside a
transparent ``WKWebView`` on a borderless, non-activating ``NSPanel`` — the
launcher's recipe (`launcher_panel.py`) with three differences: it floats until
the recording ends instead of dismissing on a click away, it can be dragged and
remembers where it was left, and it never takes key focus, so clicking its
buttons does not pull the frontmost app out from under the user.

What shows is the server half's business (`recording_pill.py` calls the hooks
`app.py` installs); this file is only the window. The page polls
``GET /api/capture/{id}/state`` for time, paused and level, and posts messages
back: ``pause`` / ``resume`` / ``stop`` (run on a worker thread — they may wait
on the native backend and the main thread must not), ``open`` (bring the
recording app's window forward), ``gone`` (the recording ended under it) and
``drag`` (window moves; `setMovableByWindowBackground_` alone cannot do it
because the page's whole surface is a web view).

Everything here runs on macOS 13: no ``NSGlassEffectView``, no
``setInspectable_`` and nothing else newer than 13. Light/dark is the page's CSS.

Several recordings at once share the one pill: it shows the newest, and when that
one ends the previous is shown again.
"""
from __future__ import annotations

import json
import logging
import threading

import AppKit
import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSMakePoint,
    NSMakeRect,
    NSObject,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
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

from fused_render.mac_window import USER_AGENT_MARKER, _open_external

logger = logging.getLogger(__name__)

MESSAGE_NAME = "recpill"
#: The window: the 44x152 pill plus a 14pt margin all round for the page's CSS
#: shadow (the window has none of its own — see `_build_panel`).
WIDTH = 72.0
HEIGHT = 180.0
EDGE_INSET = 16.0
FRAME_NAME = "FusedRenderRecordingPill"


# ----------------------------------------------------------------- pure helpers


def default_origin(visible: tuple, size: tuple = (WIDTH, HEIGHT),
                   inset: float = EDGE_INSET) -> tuple:
    """Bottom-left origin for the default spot: the right edge of `visible`
    (x, y, w, h), `inset` in, vertically centred."""
    x, y, w, h = visible
    return (x + w - size[0] - inset, y + (h - size[1]) / 2.0)


def intersects_any(frame: tuple, screens: list) -> bool:
    """Does the (x, y, w, h) `frame` overlap any of the `screens` rects? A frame
    restored from a display that is no longer attached must not leave the pill
    somewhere nothing can reach it."""
    fx, fy, fw, fh = frame
    for sx, sy, sw, sh in screens:
        if fx < sx + sw and fx + fw > sx and fy < sy + sh and fy + fh > sy:
            return True
    return False


# --------------------------------------------------------------------- AppKit


class _PillScriptHandler(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_PillScriptHandler, self).init()
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
        self._c.handle(data)


class _PillWebDelegate(NSObject):
    """The pill page is the only thing this view shows; anything else goes to
    the default browser."""

    def initWithController_(self, controller):
        self = objc.super(_PillWebDelegate, self).init()
        if self is None:
            return None
        self._c = controller
        return self

    def webView_decidePolicyForNavigationAction_decisionHandler_(
            self, webview, action, decision):
        request = action.request()
        url = str(request.URL().absoluteString()) if request and request.URL() else ""
        target = action.targetFrame()
        is_main = target is None or bool(target.isMainFrame())
        if not is_main or url.startswith(self._c._base()) or url.startswith("about:"):
            decision(WKNavigationActionPolicyAllow)
            return
        decision(WKNavigationActionPolicyCancel)
        if url:
            _open_external(url)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, webview, config, action, features):
        request = action.request()
        url = str(request.URL().absoluteString()) if request and request.URL() else ""
        if url:
            _open_external(url)
        return None


class _PillPanel(NSPanel):
    """Never key, never main: a click on a button must not steal focus."""

    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class _PillWebView(WKWebView):
    """A click on the pill acts at once instead of first activating the window
    (this app may not be the frontmost one — that is the point of the pill)."""

    def acceptsFirstMouse_(self, _event):
        return True


class RecordingPillController:
    """Owns the panel and its web view. Main thread only (`app.py` hops here).

    ``open_app(path)``: bring the recording app's window forward — the same
    callback the launcher uses (`_open_app_native`).
    """

    def __init__(self, port: int, open_app) -> None:
        self._port = port
        self._open_app = open_app
        self._handler = _PillScriptHandler.alloc().initWithController_(self)
        self._web_delegate = _PillWebDelegate.alloc().initWithController_(self)
        self._stack: list[dict] = []     # shown infos, newest last
        self._ready = False              # the server is up
        self._loaded = False             # the page has been asked for
        self._page_ready = False         # ... and has finished loading
        self._drag_origin = None
        self._panel = None
        self._webview = None

    # ---- lifecycle ------------------------------------------------------------------------

    def server_ready(self) -> None:
        """The server answers now: load the page (once). A recording that began
        before this is shown as soon as the page can take it."""
        self._ready = True
        if self._panel is None:
            self._build_panel()
        if not self._loaded:
            self._load()
        if self._stack:
            self._present()

    def show(self, info: dict) -> None:
        self._stack = [i for i in self._stack if i.get("id") != info.get("id")]
        self._stack.append(info)
        if not self._ready:
            return                       # `server_ready` presents it
        self._present()

    def hide(self, cid: str) -> None:
        before = len(self._stack)
        self._stack = [i for i in self._stack if i.get("id") != cid]
        if len(self._stack) == before:
            return
        if self._stack:
            self._present()
        else:
            self._order_out()

    def hide_all(self) -> None:
        self._stack = []
        self._order_out()

    # ---- messages from the page -----------------------------------------------------------

    def handle(self, data: dict) -> None:
        kind = data.get("type")
        cid = str(data.get("id") or "")
        if kind in ("pause", "resume", "stop") and cid:
            self._run_capture(kind, cid)
        elif kind == "open":
            app = str(data.get("app") or "")
            if app:
                try:
                    self._open_app(app)
                except Exception:  # noqa: BLE001
                    logger.warning("recording pill: opening %s failed", app,
                                   exc_info=True)
        elif kind == "gone" and cid:
            self.hide(cid)
        elif kind == "ready":
            self._page_ready = True
            self._push_current()
        elif kind == "drag":
            self._drag(str(data.get("phase") or ""), data.get("dx"), data.get("dy"))

    def _run_capture(self, kind: str, cid: str) -> None:
        """pause / resume / stop on a worker thread: they take the capture
        module's native lock and `stop` waits for the file to finalise, neither
        of which the main thread may wait on."""
        def run():
            from fused_render import capture

            try:
                getattr(capture, kind)(cid)
            except Exception:  # noqa: BLE001 - the page polls the truth
                logger.warning("recording pill: %s %s failed", kind, cid,
                               exc_info=True)

        threading.Thread(target=run, daemon=True,
                         name=f"recpill-{kind}").start()

    def _drag(self, phase: str, dx, dy) -> None:
        if self._panel is None:
            return
        if phase == "start":
            frame = self._panel.frame()
            self._drag_origin = (frame.origin.x, frame.origin.y)
        elif phase == "move" and self._drag_origin is not None:
            try:
                x0, y0 = self._drag_origin
                # Screen coordinates grow downward in the page, upward in AppKit.
                self._panel.setFrameOrigin_(
                    NSMakePoint(x0 + float(dx), y0 - float(dy)))
            except (TypeError, ValueError):
                return
        elif phase == "end":
            self._drag_origin = None
            self._panel.saveFrameUsingName_(FRAME_NAME)

    # ---- showing --------------------------------------------------------------------------

    def _present(self) -> None:
        if self._panel is None:
            return
        self._push_current()
        self._panel.orderFrontRegardless()

    def _order_out(self) -> None:
        if self._panel is None:
            return
        self._eval("window.pillHidden && window.pillHidden()")
        self._panel.orderOut_(None)

    def _push_current(self) -> None:
        if not self._stack or not self._page_ready:
            return
        self._eval("window.pillShow(%s)" % json.dumps(self._stack[-1]))

    def _eval(self, js: str) -> None:
        if self._webview is None or not self._page_ready:
            return
        try:
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:  # noqa: BLE001
            logger.debug("recording pill: evaluateJavaScript failed", exc_info=True)

    # ---- panel ----------------------------------------------------------------------------

    def _build_panel(self) -> None:
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = _PillPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        # No window shadow (AppKit would trace a hairline around the opaque
        # pixels of a non-opaque window); the page draws one in its 14pt margin.
        panel.setHasShadow_(False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setHidesOnDeactivate_(False)
        panel.setMovableByWindowBackground_(True)
        # Visible on every Space and over full-screen apps; NOT Transient — that
        # is what makes the launcher vanish with its Space, and the pill must stay.
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary)

        config = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(self._handler, MESSAGE_NAME)
        config.setUserContentController_(ucc)
        config.setApplicationNameForUserAgent_(f"{USER_AGENT_MARKER} RecordingPill")
        try:
            config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except Exception:  # noqa: BLE001
            pass
        webview = _PillWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), config)
        try:
            webview.setValue_forKey_(False, "drawsBackground")
        except Exception:  # noqa: BLE001
            logger.debug("drawsBackground not settable", exc_info=True)
        try:
            webview.setUnderPageBackgroundColor_(NSColor.clearColor())
        except Exception:  # noqa: BLE001
            pass
        webview.setNavigationDelegate_(self._web_delegate)
        webview.setUIDelegate_(self._web_delegate)
        panel.setContentView_(webview)
        self._panel = panel
        self._webview = webview
        self._place()

    def _place(self) -> None:
        """Restore the remembered spot, or the right edge of the main screen."""
        panel = self._panel
        panel.setFrameAutosaveName_(FRAME_NAME)
        restored = panel.setFrameUsingName_(FRAME_NAME)
        frame = panel.frame()
        rect = (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height)
        screens = [(s.frame().origin.x, s.frame().origin.y, s.frame().size.width,
                    s.frame().size.height) for s in NSScreen.screens()]
        if restored and intersects_any(rect, screens):
            return
        main = NSScreen.mainScreen()
        if main is None:
            return
        v = main.visibleFrame()
        x, y = default_origin((v.origin.x, v.origin.y, v.size.width, v.size.height))
        panel.setFrameOrigin_(NSMakePoint(x, y))

    def _base(self) -> str:
        return f"http://127.0.0.1:{self._port}/recording-pill"

    def _load(self) -> None:
        url = self._base()
        self._webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self._loaded = True
        logger.info("recording pill loading %s", url)
