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


def test_a_relaunch_snapshots_the_windows_before_closing_them():
    action, log = _quit({})
    assert action(on_claim=lambda: None) is True
    assert log == ["snapshot", "close", ("begin", True)]


def test_a_plain_quit_takes_no_snapshot():
    action, log = _quit({})
    assert action() is True
    assert log == ["close", ("begin", False)]


def test_a_relaunch_that_did_not_claim_the_quit_discards_its_snapshot():
    action, log = _quit({}, claimed=False)
    assert action(on_claim=lambda: None) is False
    assert log == ["snapshot", "close", ("begin", True), "discard"]


def test_a_relaunch_joining_a_quit_in_flight_never_overwrites_the_snapshot():
    # The windows are already closed by the quit in flight: a snapshot taken
    # now would be empty and would erase the good one.
    action, log = _quit({"quitting": True}, claimed=False)
    assert action(on_claim=lambda: None) is False
    assert "snapshot" not in log and "discard" not in log


def test_the_snapshot_age_bound_is_tied_to_the_relauncher_deadline():
    assert app_mod.RELAUNCH_SNAPSHOT_MAX_AGE_S >= app_mod.RELAUNCH_DEADLINE_S
    assert app_mod.RELAUNCH_WINDOWS_FILE.startswith(app_mod.APP_SUPPORT_DIR)
