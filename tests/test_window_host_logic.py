"""The decision logic of the Linux native-window host, with no GTK.

`window_host.py` splits into pure logic (this file: dispatch, focus-or-open,
navigation mapping, frame persistence, GTK/display detection) and a thin
GTK/WebKitGTK adapter that only runs on a Linux desktop. The fake backend here
exposes exactly the methods the adapter implements; nothing GTK is imported.
"""
import json
import sys

import pytest

from fused_render.supervisor._linux import window_host as wh

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"


class FakeBackend:
    """Mirrors `window_host.Backend`: handles are opaque ints."""

    def __init__(self):
        self.urls = {}
        self.presented = []
        self.closed = []
        self.external = []
        self.created = 0
        self.quit_called = False

    def run_on_main(self, fn):
        return fn()

    def new_window(self, url, frame_name):
        self.created += 1
        handle = self.created
        self.urls[handle] = url
        return handle

    def present(self, handle):
        self.presented.append(handle)

    def close(self, handle):
        self.closed.append(handle)
        self.urls.pop(handle, None)

    def current_url(self, handle):
        return self.urls.get(handle)

    def open_external(self, url):
        self.external.append(url)

    def quit(self):
        self.quit_called = True


@pytest.fixture
def host():
    backend = FakeBackend()
    return wh.Host(PORT, backend, enabled=True), backend


def test_ping(host):
    h, _ = host
    assert h.dispatch({"cmd": "ping"}) == {"ok": True}


def test_open_creates_a_window_for_an_app_url(host):
    h, b = host
    assert h.dispatch({"cmd": "open", "url": BASE + "/"}) == {"ok": True}
    assert b.created == 1 and b.presented == [1]


def test_open_same_file_focuses_instead_of_duplicating(host):
    h, b = host
    url = BASE + "/explorer/view/home/me/a.csv"
    h.dispatch({"cmd": "open", "url": url})
    h.dispatch({"cmd": "open", "url": url})
    assert b.created == 1 and b.presented == [1, 1]


def test_open_same_key_different_view_is_a_separate_window(host):
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/apps/home/me/app"})
    h.dispatch({"cmd": "open", "url": BASE + "/explorer/view/home/me/app"})
    assert b.created == 2


def test_identity_is_read_live_from_the_window(host):
    # The shell is an SPA: a window opened on Home may have navigated to an app.
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    b.urls[1] = BASE + "/apps/home/me/app"
    h.dispatch({"cmd": "open", "url": BASE + "/apps/home/me/app"})
    assert b.created == 1


def test_home_is_focus_or_open(host):
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    assert b.created == 1


def test_home_matches_the_spa_home_rewrite(host):
    # The shell runs history.replaceState(null, "", "/home") right after first paint.
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    b.urls[1] = BASE + "/home"
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    h.dispatch({"cmd": "open", "url": BASE + "/home"})
    assert b.created == 1
    assert b.presented == [1, 1, 1]


def test_blank_popups_neither_open_nor_focus_a_window(host):
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    b.urls[1] = BASE + "/home"
    for blank in ("about:blank", "about:srcdoc", "", None):
        h.popup(blank, "new_window")
    assert b.created == 1
    assert b.presented == [1]
    assert b.external == []


def test_keyless_non_home_url_does_not_reuse_the_home_window(host):
    # /tasks and /preferences have no window key, same as Home, but they are
    # not Home: ctrl-click / target=_blank on one must show that page, not
    # just raise whatever Home window is already open.
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    b.urls[1] = BASE + "/home"
    h.dispatch({"cmd": "open", "url": BASE + "/tasks"})
    assert b.created == 2
    assert b.urls[2] == BASE + "/tasks"
    assert b.presented == [1, 2]


def test_closed_windows_are_forgotten(host):
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    h.window_closed(1)
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    assert b.created == 2


def test_external_url_goes_to_the_browser_not_a_window(host):
    h, b = host
    r = h.dispatch({"cmd": "open", "url": "https://example.com/x"})
    assert r == {"ok": True}
    assert b.created == 0 and b.external == ["https://example.com/x"]


def test_other_scheme_is_refused(host):
    h, b = host
    r = h.dispatch({"cmd": "open", "url": "javascript:alert(1)"})
    assert r["ok"] is False and b.created == 0 and b.external == []


def test_open_requires_a_string_url(host):
    h, _ = host
    assert h.dispatch({"cmd": "open"})["ok"] is False
    assert h.dispatch({"cmd": "open", "url": 5})["ok"] is False


def test_disabled_host_declines_so_the_caller_falls_back():
    b = FakeBackend()
    h = wh.Host(PORT, b, enabled=False)
    r = h.dispatch({"cmd": "open", "url": BASE + "/"})
    assert r["ok"] is False and r["reason"] == "disabled" and b.created == 0


def test_set_enabled_off_closes_every_window_and_declines_after(host):
    h, b = host
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    h.dispatch({"cmd": "open", "url": BASE + "/apps/home/me/app"})
    assert h.dispatch({"cmd": "set_enabled", "on": False}) == {"ok": True}
    assert sorted(b.closed) == [1, 2]
    assert h.dispatch({"cmd": "open", "url": BASE + "/"})["ok"] is False
    h.dispatch({"cmd": "set_enabled", "on": True})
    assert h.dispatch({"cmd": "open", "url": BASE + "/"})["ok"] is True


def test_set_enabled_requires_a_bool(host):
    h, _ = host
    assert h.dispatch({"cmd": "set_enabled", "on": "yes"})["ok"] is False


def test_quit_asks_the_backend_to_quit(host):
    h, b = host
    assert h.dispatch({"cmd": "quit"}) == {"ok": True}
    assert b.quit_called


def test_unknown_command(host):
    h, _ = host
    assert h.dispatch({"cmd": "frobnicate"})["ok"] is False


def test_dispatch_runs_window_work_on_the_main_thread():
    calls = []

    class B(FakeBackend):
        def run_on_main(self, fn):
            calls.append("main")
            return fn()

    h = wh.Host(PORT, B(), enabled=True)
    h.dispatch({"cmd": "open", "url": BASE + "/"})
    assert calls == ["main"]


# ---- navigation mapping ---------------------------------------------------

@pytest.mark.parametrize("url,nav,button,ctrl,expected", [
    (BASE + "/x", "LINK_CLICKED", 1, False, "allow"),
    (BASE + "/x", "LINK_CLICKED", 2, False, "new_window"),   # middle click
    (BASE + "/x", "LINK_CLICKED", 1, True, "new_window"),    # ctrl-click
    ("https://example.com/", "LINK_CLICKED", 1, False, "open_external"),
    ("https://example.com/", "OTHER", 0, False, "allow"),     # sub-frame / script: left to the page
    ("about:blank", "OTHER", 0, False, "allow"),
])
def test_map_navigation(url, nav, button, ctrl, expected):
    assert wh.map_navigation(url, PORT, nav, button=button, ctrl=ctrl) == expected


def test_new_window_request_for_app_url_opens_a_window_external_goes_out():
    assert wh.map_new_window(BASE + "/x", PORT) == "new_window"
    assert wh.map_new_window("https://example.com", PORT) == "open_external"
    assert wh.map_new_window("about:blank", PORT) == "ignore"
    assert wh.map_new_window("", PORT) == "ignore"
    assert wh.map_new_window(None, PORT) == "ignore"


def test_response_mapping_downloads_what_cannot_be_shown():
    assert wh.map_response(can_show=False, content_disposition=None) == "download"
    assert wh.map_response(can_show=True, content_disposition="attachment; filename=a") == "download"
    assert wh.map_response(can_show=True, content_disposition=None) == "allow"


# ---- frame store -----------------------------------------------------------

def test_frame_store_round_trip(tmp_path):
    store = wh.FrameStore(tmp_path / "frames.json")
    assert store.get("a") is None
    store.put("a", 800, 600, 10, 20)
    again = wh.FrameStore(tmp_path / "frames.json")
    assert again.get("a") == {"w": 800, "h": 600, "x": 10, "y": 20}


def test_frame_store_survives_corrupt_file(tmp_path):
    p = tmp_path / "frames.json"
    p.write_text("{not json")
    store = wh.FrameStore(p)
    assert store.get("a") is None
    store.put("a", 400, 300, None, None)
    assert json.loads(p.read_text())["a"]["w"] == 400


def test_frame_store_rejects_nonsense_sizes(tmp_path):
    store = wh.FrameStore(tmp_path / "f.json")
    store.put("a", 0, 0, 0, 0)
    store.put("b", 99999, 10, 0, 0)
    assert store.get("a") is None and store.get("b") is None


def test_frame_store_unwritable_dir_is_not_fatal(tmp_path):
    store = wh.FrameStore(tmp_path / "missing" / "deep" / "f.json")
    store.put("a", 400, 300, 0, 0)  # creates the dirs
    assert store.get("a")["w"] == 400
    blocked = tmp_path / "file"
    blocked.write_text("x")
    wh.FrameStore(blocked / "f.json").put("a", 400, 300, 0, 0)  # must not raise


# ---- toolkit / display detection -------------------------------------------

def test_load_toolkit_reports_missing_pygobject(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)  # makes `import gi` raise ImportError
    with pytest.raises(wh.ToolkitUnavailable) as e:
        wh.load_toolkit()
    assert "PyGObject" in str(e.value)


def test_load_toolkit_reports_missing_typelib(monkeypatch):
    class Gi:
        @staticmethod
        def require_version(name, version):
            raise ValueError(f"Namespace {name} not available")

    monkeypatch.setitem(sys.modules, "gi", Gi)
    with pytest.raises(wh.ToolkitUnavailable) as e:
        wh.load_toolkit()
    assert "WebKit2" in str(e.value) or "typelib" in str(e.value)


def test_main_exits_with_the_toolkit_code_when_unavailable(monkeypatch, capsys, tmp_path):
    def boom():
        raise wh.ToolkitUnavailable("no display")

    monkeypatch.setattr(wh, "load_toolkit", boom)
    code = wh.main(["--port", "1", "--socket", str(tmp_path / "s"), "--state", str(tmp_path)])
    assert code == wh.EXIT_UNAVAILABLE
    assert "no display" in capsys.readouterr().err


def test_timed_out_main_thread_request_never_runs_later(monkeypatch):
    """A request the host gave up on must not open a window afterwards: the
    caller already fell back to a browser tab."""
    from types import SimpleNamespace

    queued = []
    tk = SimpleNamespace(GLib=SimpleNamespace(idle_add=lambda fn: queued.append(fn)))
    backend = wh.GtkBackend.__new__(wh.GtkBackend)
    backend.tk = tk
    monkeypatch.setattr(wh, "_MAIN_DEADLINE_S", 0.05)
    ran, errors = [], []

    def ipc_thread():  # run_on_main only queues when not on the main thread
        try:
            backend.run_on_main(lambda: ran.append(1))
        except TimeoutError as error:
            errors.append(error)

    import threading
    t = threading.Thread(target=ipc_thread)
    t.start()
    t.join(5)
    assert len(errors) == 1
    for fn in queued:  # the busy loop finally gets to it
        fn()
    assert ran == []


def test_host_deadline_is_shorter_than_every_client_timeout():
    from fused_render import window_host_ipc as ipc
    from fused_render.supervisor._linux import windows

    assert wh._MAIN_DEADLINE_S < windows._OPEN_TIMEOUT_S
    assert wh._MAIN_DEADLINE_S < ipc.CALLER_TIMEOUT_S


def test_open_external_never_raises_and_runs_off_the_gtk_thread(monkeypatch):
    """`open_external` is called straight from a GTK signal handler
    (`decide-policy`) and from `run_on_main`-marshalled IPC `open`s: a slow or
    failing `xdg-open` must neither block that thread nor unwind an
    exception into it."""
    import threading
    import time

    logged = []
    seen_from_main_thread = []

    def fake_open_url(url):
        seen_from_main_thread.append(threading.current_thread() is threading.main_thread())
        raise OSError(f"xdg-open exited with status 1 for {url}")

    monkeypatch.setattr("fused_render.supervisor._linux.ui.open_url", fake_open_url)

    backend = wh.GtkBackend.__new__(wh.GtkBackend)
    backend._log = logged.append

    backend.open_external("https://example.com/x")  # must return immediately

    deadline = time.monotonic() + 3
    while not logged and time.monotonic() < deadline:
        time.sleep(0.01)
    assert logged and "example.com/x" in logged[0]
    assert seen_from_main_thread == [False]


def test_xdg_open_runs_detached_so_host_teardown_cannot_kill_it(monkeypatch):
    """`windows.WindowHost.stop`/`_give_up` `killpg` the host's whole process
    group; an opener spawned without its own session would die with it,
    taking a still-running (non-daemonizing) browser down too."""
    from fused_render.supervisor._linux import ui

    captured = {}

    class FakeProcess:
        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(ui.subprocess, "Popen", fake_popen)
    ui.open_url("https://example.com")
    assert captured.get("start_new_session") is True
