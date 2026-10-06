"""relaunch_windows: the open-window snapshot a relaunch hands its successor,
and the quit-ordering that makes sure it is taken before the windows close."""
import json

import fused_render.app as app_mod
import fused_render.relaunch_windows as rw


def test_targets_keep_path_query_and_order_and_drop_the_origin():
    urls = ["http://127.0.0.1:8123/", "http://127.0.0.1:8123/view/a%20b.csv?x=1",
            "http://localhost:8123/apps/m#frag"]
    assert rw.targets_from_urls(urls, 8123) == ["/", "/view/a%20b.csv?x=1", "/apps/m#frag"]


def test_targets_drop_pages_that_are_not_ours():
    urls = ["about:blank", "https://example.com/x", "http://127.0.0.1:9999/other",
            None, "http://127.0.0.1:8123/ok"]
    assert rw.targets_from_urls(urls, 8123) == ["/ok"]


def test_a_snapshot_round_trips_once(tmp_path):
    p = str(tmp_path / "w.json")
    assert rw.write_snapshot(p, ["/a", "/b?q=1"], pid=7, now=1000.0) is True
    assert rw.take_snapshot(p, max_age_s=100, now=1010.0) == ["/a", "/b?q=1"]
    # one-shot: consumed
    assert not (tmp_path / "w.json").exists()
    assert rw.take_snapshot(p, max_age_s=100, now=1010.0) == []


def test_an_empty_snapshot_writes_nothing_and_clears_an_old_file(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("{}")
    assert rw.write_snapshot(str(p), [], now=1.0) is False
    assert not p.exists()


def test_a_stale_snapshot_is_deleted_and_ignored(tmp_path):
    p = str(tmp_path / "w.json")
    rw.write_snapshot(p, ["/a"], now=1000.0)
    assert rw.take_snapshot(p, max_age_s=100, now=1200.0) == []
    assert not (tmp_path / "w.json").exists()


def test_a_future_dated_snapshot_is_ignored(tmp_path):
    p = str(tmp_path / "w.json")
    rw.write_snapshot(p, ["/a"], now=5000.0)
    assert rw.take_snapshot(p, max_age_s=100, now=1000.0) == []


def test_an_unreadable_snapshot_is_deleted_and_ignored(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("not json{")
    assert rw.take_snapshot(str(p), max_age_s=100, now=1.0) == []
    assert not p.exists()
    p.write_text(json.dumps({"at": 1.0, "windows": "nope"}))
    assert rw.take_snapshot(str(p), max_age_s=100, now=2.0) == []
    assert not p.exists()


def test_a_missing_file_is_just_empty(tmp_path):
    assert rw.take_snapshot(str(tmp_path / "none.json"), max_age_s=100) == []


def test_entries_that_could_repoint_the_origin_are_dropped(tmp_path):
    p = str(tmp_path / "w.json")
    rw.write_snapshot(p, ["/ok", "//evil.com/x", "http://evil.com/", 5], now=1.0)
    assert rw.take_snapshot(p, max_age_s=100, now=2.0) == ["/ok"]


def test_an_unwritable_location_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert rw.write_snapshot(str(blocker / "sub" / "w.json"), ["/a"]) is False


# ------------------------------------------------------- quit ordering (app.py)


def _quit(state, claimed=True, log=None):
    log = [] if log is None else log

    def begin(on_claim=None):
        log.append(("begin", on_claim is not None))
        return claimed

    action = app_mod.make_windows_quit(
        begin, state=state,
        close_windows=lambda: log.append("close"),
        snapshot=lambda: log.append("snapshot") or True,
        discard=lambda: log.append("discard"))
    return action, log


def test_a_relaunch_snapshots_the_windows_and_leaves_them_open():
    # The windows stay up (the RestartOverlay is what the user sees during the
    # teardown); the process exit takes them. Nothing closes them.
    action, log = _quit({})
    assert action(on_claim=lambda: None) is True
    assert log == ["snapshot", ("begin", True)]


def test_a_plain_quit_takes_no_snapshot():
    action, log = _quit({})
    assert action() is True
    assert log == ["close", ("begin", False)]


def test_a_relaunch_that_did_not_claim_the_quit_discards_its_snapshot():
    action, log = _quit({}, claimed=False)
    assert action(on_claim=lambda: None) is False
    assert log == ["snapshot", ("begin", True), "discard"]


def test_a_relaunch_joining_a_quit_in_flight_never_overwrites_the_snapshot():
    # The windows are already closed by the quit in flight: a snapshot taken
    # now would be empty and would erase the good one.
    action, log = _quit({"quitting": True}, claimed=False)
    assert action(on_claim=lambda: None) is False
    assert "snapshot" not in log and "discard" not in log


def test_the_snapshot_age_bound_is_tied_to_the_relauncher_deadline():
    assert app_mod.RELAUNCH_SNAPSHOT_MAX_AGE_S >= app_mod.RELAUNCH_DEADLINE_S
    assert app_mod.RELAUNCH_WINDOWS_FILE.startswith(app_mod.APP_SUPPORT_DIR)


def test_a_relaunch_never_closes_windows_even_joining_a_quit_in_flight():
    action, log = _quit({"quitting": True}, claimed=False)
    action(on_claim=lambda: None)
    assert "close" not in log


# ------------------------------------------- successor: windows before server


class _FakeWindow:
    def __init__(self, url):
        self.url = url
        self.loaded = []

    def load(self, url):
        self.loaded.append(url)


class _FakeManager:
    """Only what the real WindowManager has: `enabled`, `open(url, html=None)`."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.opened = []

    def open(self, url, html=None):
        win = _FakeWindow(url)
        self.opened.append((url, html, win))
        return win


def test_the_fake_matches_the_real_window_manager_signatures():
    # mac_window.py is AppKit and never imports in CI: check the source.
    from _theme_sources import read_repo_file

    src = read_repo_file("fused_render/mac_window.py")
    assert "def open(self, url: str, html: str | None = None)" in src
    assert "def save_frames(self)" in src
    assert "def load(self, url: str)" in src


def _snap(tmp_path, targets, now=1000.0):
    p = str(tmp_path / "w.json")
    rw.write_snapshot(p, targets, now=now)
    return p


def test_windows_reopen_as_placeholders_before_any_server(tmp_path, monkeypatch):
    monkeypatch.setattr(rw.time, "time", lambda: 1002.0)
    p = _snap(tmp_path, ["/a", "/b?x=1"])
    m = _FakeManager()
    restored = app_mod.reopen_windows_early(
        m, 8123, snapshot_file=p, max_age_s=100, html=rw.RESTARTING_HTML)
    assert [(u, h) for u, h, _ in m.opened] == [
        ("http://127.0.0.1:8123/a", rw.RESTARTING_HTML),
        ("http://127.0.0.1:8123/b?x=1", rw.RESTARTING_HTML)]
    assert [u for u, _ in restored] == [u for u, _, _ in m.opened]
    # Nothing has navigated yet: that waits for the server.
    assert all(w.loaded == [] for _, w in restored)


def test_restored_windows_navigate_to_their_snapshot_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(rw.time, "time", lambda: 1001.0)
    p = _snap(tmp_path, ["/a", "/b"])
    m = _FakeManager()
    restored = app_mod.reopen_windows_early(
        m, 9, snapshot_file=p, max_age_s=100, html="x")
    app_mod.navigate_restored(restored)
    assert [w.loaded for _, w in restored] == [
        ["http://127.0.0.1:9/a"], ["http://127.0.0.1:9/b"]]


def test_the_early_reopen_is_one_shot_and_honours_staleness(tmp_path, monkeypatch):
    monkeypatch.setattr(rw.time, "time", lambda: 5000.0)
    p = _snap(tmp_path, ["/a"], now=1000.0)  # 4000 s old
    m = _FakeManager()
    assert app_mod.reopen_windows_early(
        m, 1, snapshot_file=p, max_age_s=100, html="x") == []
    assert m.opened == []
    import os
    assert not os.path.exists(p)  # consumed even though stale


def test_a_manager_that_is_off_or_missing_leaves_the_snapshot_unread(tmp_path):
    import os
    p = _snap(tmp_path, ["/a"])
    for m in (None, _FakeManager(enabled=False)):
        assert app_mod.reopen_windows_early(
            m, 1, snapshot_file=p, max_age_s=10**9, html="x") == []
        # still there for the browser-tab fallback at server-ready
        assert os.path.exists(p)


def test_the_placeholder_is_static_and_needs_no_server():
    h = rw.RESTARTING_HTML
    assert "Restarting" in h and "<script" not in h.lower()
    assert "http://" not in h and "https://" not in h


def test_take_snapshot_reports_its_age(tmp_path):
    p = _snap(tmp_path, ["/a"], now=10.0)
    ages = []
    rw.take_snapshot(p, max_age_s=100, now=13.5, on_age=ages.append)
    assert ages == [3.5]


def test_importing_app_does_not_pull_in_the_server():
    # The server module is ~0.7 s warm / seconds cold, before the first log
    # line and before any window; it loads on the bootstrap thread instead.
    import subprocess, sys
    code = ("import sys, fused_render.app; "
            "print(any(m in sys.modules for m in "
            "('fastapi', 'uvicorn', 'fused_render.server')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "False"
