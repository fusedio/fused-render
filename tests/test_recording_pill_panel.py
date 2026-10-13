"""The recording pill's AppKit half — what can be proved without clicking it.

The window itself (look, drag, buttons, light/dark, Spaces) needs a person; see
the human checklist in D1345. What is checked here is the logic around it: where
the pill goes by default, the restored-frame sanity check, the stack that shows
every recording, and that the page's messages reach the right calls.
"""
from __future__ import annotations

import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="the macOS panel")
if sys.platform == "darwin":
    # WebKit is an app extra (pyproject's `bundled`), not a core dependency.
    pytest.importorskip("WebKit")


def test_the_default_spot_is_the_right_edge_vertically_centred():
    from fused_render import recording_pill_panel as rp

    x, y = rp.default_origin((0, 25, 1440, 875))
    assert x == 1440 - rp.WIDTH - rp.EDGE_INSET
    assert y == pytest.approx(25 + (875 - rp.HEIGHT) / 2)
    # A second display to the left, origin negative: still its own right edge.
    x, y = rp.default_origin((-1920, 0, 1920, 1080))
    assert x == -rp.WIDTH - rp.EDGE_INSET


def test_resized_frame_keeps_the_top_edge_and_clamps():
    from fused_render.recording_pill_panel import resized_frame

    # (x, y, w, h) bottom-left origin: top = y + h. 114 + 28 margin = 142 tall.
    x, y, w, h = resized_frame((100, 200, 72, 142), 114 + 60)
    assert (x, w, h) == (100, 72, 202) and y + h == 200 + 142
    assert resized_frame((100, 200, 72, 142), 114) == (100, 200, 72, 142)
    assert resized_frame((0, 0, 72, 142), 5)[3] == 60            # lo
    assert resized_frame((0, 0, 72, 142), 9999)[3] == 400        # hi
    for bad in (None, "x", float("nan"), True):
        assert resized_frame((1, 2, 72, 142), bad) == (1, 2, 72, 142)


def test_a_restored_frame_on_a_detached_display_is_rejected():
    from fused_render import recording_pill_panel as rp

    screens = [(0, 0, 1440, 900)]
    assert rp.intersects_any((1300, 100, 72, 216), screens)
    assert rp.intersects_any((1430, 100, 72, 216), screens)     # a sliver is enough
    assert not rp.intersects_any((3000, 100, 72, 216), screens)
    assert not rp.intersects_any((-2000, 100, 72, 216), screens)
    assert not rp.intersects_any((100, 100, 72, 216), [])


class FakePanel:
    def __init__(self):
        self.ordered = []
        self.origins = []
        self.saved = []

    def orderFrontRegardless(self):
        self.ordered.append("front")

    def orderOut_(self, sender):
        self.ordered.append("out")

    def frame(self):
        from AppKit import NSMakeRect

        return NSMakeRect(100, 200, 72, 216)

    def setFrameOrigin_(self, point):
        self.origins.append((point.x, point.y))

    def saveFrameUsingName_(self, name):
        self.saved.append(name)


class FakeWeb:
    def __init__(self):
        self.js = []

    def evaluateJavaScript_completionHandler_(self, js, handler):
        self.js.append(js)


def controller(ready=True, page_ready=True):
    from fused_render.recording_pill_panel import RecordingPillController

    ctl = RecordingPillController(1234, lambda app: ctl.opened.append(app))
    ctl.opened = []
    ctl._panel, ctl._webview = FakePanel(), FakeWeb()
    ctl._ready, ctl._page_ready = ready, page_ready
    return ctl


def info(cid):
    return {"id": cid, "name": cid, "app": "/apps/" + cid, "icon": None,
            "flavor": "render", "canPause": True, "mode": "audio"}


def test_one_pill_pushes_every_recording_newest_last():
    ctl = controller()
    ctl.show(info("a"))
    ctl.show(info("b"))
    js = ctl._webview.js[-1]
    assert js.startswith("window.pillSet(") and js.index('"id": "a"') < js.index('"id": "b"')
    ctl.hide("b")
    assert '"id": "a"' in ctl._webview.js[-1] and '"id": "b"' not in ctl._webview.js[-1]
    assert ctl._panel.ordered[-1] == "front"
    ctl.hide("a")
    assert ctl._panel.ordered[-1] == "out"
    assert "pillHidden" in ctl._webview.js[-1]
    n = len(ctl._panel.ordered)
    ctl.hide("a")                                  # unknown: nothing happens
    assert len(ctl._panel.ordered) == n


def test_hide_all_and_a_repeated_show():
    ctl = controller()
    ctl.show(info("a"))
    ctl.show(info("a"))
    assert len(ctl._stack) == 1
    ctl.hide_all()
    assert ctl._stack == [] and ctl._panel.ordered[-1] == "out"


def test_a_recording_that_began_before_the_server_is_shown_once_it_is_ready():
    ctl = controller(ready=False, page_ready=False)
    ctl.show(info("a"))
    assert ctl._panel.ordered == []
    ctl._ready = True
    ctl.handle({"type": "ready"})                  # the page finished loading
    assert any("pillSet" in js for js in ctl._webview.js)


def test_the_pages_buttons_run_capture_calls_off_the_main_thread(monkeypatch):
    from fused_render import capture

    ran = []
    main = threading.current_thread()
    for name in ("pause", "resume", "stop"):
        monkeypatch.setattr(
            capture, name,
            lambda cid, name=name: ran.append((name, cid,
                                               threading.current_thread() is main)))
    ctl = controller()
    for kind in ("pause", "resume", "stop"):
        ctl.handle({"type": kind, "id": "c1"})
    deadline = __import__("time").monotonic() + 3
    while len(ran) < 3 and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert sorted(ran) == [("pause", "c1", False), ("resume", "c1", False),
                           ("stop", "c1", False)]
    ctl.handle({"type": "pause"})                  # no id: ignored
    assert len(ran) == 3


def test_open_goes_to_the_app_and_gone_hides_the_pill():
    ctl = controller()
    ctl.show(info("a"))
    ctl.handle({"type": "open", "id": "a", "app": "/apps/a"})
    ctl.handle({"type": "open", "id": "a", "app": ""})         # no app: nothing
    assert ctl.opened == ["/apps/a"]
    ctl.handle({"type": "gone", "id": "a"})
    assert ctl._stack == [] and ctl._panel.ordered[-1] == "out"


def test_size_resizes_the_window_with_the_top_edge_fixed():
    ctl = controller()
    frames = []
    ctl._panel.setFrame_display_ = lambda r, d: frames.append(
        (r.origin.x, r.origin.y, r.size.width, r.size.height))
    ctl.handle({"type": "size", "height": 114 + 34})   # fake frame: (100, 200, 72, 216)
    assert frames == [(100.0, 200 + 216 - 176, 72.0, 176.0)]
    ctl.handle({"type": "size", "height": "bad"})
    assert len(frames) == 1


def test_dragging_moves_the_window_by_the_pointer_delta_and_saves_it():
    ctl = controller()
    ctl.handle({"type": "drag", "phase": "move", "dx": 5, "dy": 5})   # no start
    assert ctl._panel.origins == []
    ctl.handle({"type": "drag", "phase": "start", "dx": 0, "dy": 0})
    ctl.handle({"type": "drag", "phase": "move", "dx": 10, "dy": 4})
    assert ctl._panel.origins == [(110.0, 196.0)]    # y grows upward in AppKit
    ctl.handle({"type": "drag", "phase": "end", "dx": 10, "dy": 4})
    assert ctl._panel.saved == ["FusedRenderRecordingPill"]
    ctl.handle({"type": "drag", "phase": "move", "dx": 1, "dy": 1})
    assert len(ctl._panel.origins) == 1


def test_a_malformed_page_message_is_ignored():
    ctl = controller()
    ctl.handle({})
    ctl.handle({"type": "drag", "phase": "start"})
    ctl.handle({"type": "drag", "phase": "move", "dx": "x", "dy": None})
    assert ctl._panel.origins == []
