"""A relaunch-initiated quit runs the same ordered teardown on tighter budgets.

Measured on the owner's machine, an in-app restart took 8.7 s / 13.2 s: the
server never drains on the packaged app (SSE connections stay open, so the 2 s
join always times out), several unmounts each ran to their 6 s budget, and rcd's
SIGTERM wait cost ~4.5 s. A normal user Quit keeps every one of those budgets;
only the relaunch trims them, and it can afford to because the successor
force-clears whatever mount it leaves behind (health._clear_dead_mount).

AppKit-free, like test_app_quit.py; nothing real is mounted or signalled.
"""
import threading
import time

import pytest

import fused_render.app as app_mod
import fused_render.shell.mounts as mounts_mod
import fused_render.shell.mounts.rcd as rcd_mod


# ------------------------------------------------------------------ plumbing


def test_a_normal_quit_budgets_are_unchanged():
    assert app_mod.QUIT_SERVER_DRAIN_S == 2.0
    assert app_mod.QUIT_CHILDREN_BUDGET_S == 5.0
    assert mounts_mod._QUIT_UNMOUNT_JOIN_BUDGET_S == 6.0
    assert mounts_mod._QUIT_QUIESCE_BUDGET_S == 2.0
    assert mounts_mod._KILL_TIMEOUT_S == 5.0


def test_the_fast_budgets_are_strictly_tighter():
    assert app_mod.QUIT_FAST_SERVER_DRAIN_S < app_mod.QUIT_SERVER_DRAIN_S
    assert mounts_mod._QUIT_FAST_UNMOUNT_JOIN_BUDGET_S < mounts_mod._QUIT_UNMOUNT_JOIN_BUDGET_S
    assert mounts_mod._QUIT_FAST_QUIESCE_BUDGET_S <= mounts_mod._QUIT_QUIESCE_BUDGET_S
    assert mounts_mod._FAST_KILL_TIMEOUT_S < mounts_mod._KILL_TIMEOUT_S
    assert app_mod.QUIT_FAST_HARD_DEADLINE_S < app_mod.QUIT_HARD_DEADLINE_S


def test_the_fast_deadline_is_derived_from_the_fast_steps_it_waits_on():
    inner = (app_mod.QUIT_FAST_SERVER_DRAIN_S
             + app_mod.QUIT_CHILDREN_BUDGET_S
             + mounts_mod._QUIT_FAST_UNMOUNT_BUDGET_S
             + mounts_mod.RCD_FAST_REAP_WORST_CASE_S)
    # Strictly greater, for the reason the normal deadline is: a deadline that
    # fires mid-SIGTERM-wait skips the SIGKILL escalation.
    assert app_mod.QUIT_FAST_HARD_DEADLINE_S > inner
    assert mounts_mod._QUIT_FAST_UNMOUNT_BUDGET_S == pytest.approx(
        mounts_mod._QUIT_FAST_QUIESCE_BUDGET_S + mounts_mod._QUIT_FAST_UNMOUNT_JOIN_BUDGET_S)
    assert mounts_mod.RCD_FAST_REAP_WORST_CASE_S == pytest.approx(
        mounts_mod._CONFIRM_RC_TIMEOUT_S + mounts_mod._PS_TIMEOUT_S
        + 2 * (mounts_mod._FAST_KILL_TIMEOUT_S + mounts_mod._LIVE_PORT_PROBE_TIMEOUT_S))


def test_begin_quit_passes_fast_to_the_teardown_only_when_asked():
    seen = []

    def start(server, *, terminate, server_thread=None, **kw):
        seen.append(kw)

    app_mod.begin_quit({}, start=start, remove_pidfile=lambda: None)
    app_mod.begin_quit({}, start=start, remove_pidfile=lambda: None, fast=True)
    # The plain call is byte-for-byte what it was: no new keyword reaches `start`.
    assert seen == [{}, {"fast": True}]


def test_a_start_that_predates_fast_still_works_for_a_normal_quit():
    def old_start(server, *, terminate, server_thread=None):
        pass

    assert app_mod.begin_quit({}, start=old_start, remove_pidfile=lambda: None) is True


def test_the_relaunch_quit_is_fast_and_the_menu_quit_is_not():
    seen = []

    def start(server, *, terminate, server_thread=None, **kw):
        seen.append(kw)

    action = app_mod.make_quit_action({}, terminate=lambda: None, start=start,
                                      remove_pidfile=lambda: None)
    assert action(on_claim=lambda: None) is True      # begin_relaunch's call
    assert seen == [{"fast": True}]
    seen.clear()
    action2 = app_mod.make_quit_action({}, terminate=lambda: None, start=start,
                                       remove_pidfile=lambda: None)
    assert action2() is True                          # menu / popover
    assert seen == [{}]


def test_start_quit_fast_runs_a_fast_teardown_under_the_fast_deadline(monkeypatch):
    got = {}

    def fake_teardown(server, **kw):
        got.update(kw)

    monkeypatch.setattr(app_mod, "quit_teardown", fake_teardown)
    done = threading.Event()
    watchdog = app_mod.start_quit(None, terminate=done.set, fast=True)
    watchdog.join(5)
    assert done.is_set()
    assert got.get("fast") is True


def test_start_quit_normal_does_not_ask_for_a_fast_teardown(monkeypatch):
    got = {}
    monkeypatch.setattr(app_mod, "quit_teardown",
                        lambda server, **kw: got.update(kw))
    done = threading.Event()
    app_mod.start_quit(None, terminate=done.set).join(5)
    assert "fast" not in got or got["fast"] is False


# ------------------------------------------------------------- the teardown


class _FakeServer:
    should_exit = False


def _never_draining_thread():
    release = threading.Event()
    t = threading.Thread(target=release.wait, args=(30,), daemon=True)
    t.start()
    return t, release


def _teardown(fast, **kw):
    calls = []
    t, release = _never_draining_thread()
    try:
        t0 = time.monotonic()
        steps = app_mod.quit_teardown(
            _FakeServer(), server_thread=t, fast=fast,
            close_duckdb=lambda: None, stop_captures=lambda: None,
            stop_children=lambda: None, record_exit=lambda: None,
            unmount_mounts=lambda: calls.append("unmount"),
            stop_rcd=lambda: calls.append("rcd"), **kw)
        return steps, time.monotonic() - t0, calls
    finally:
        release.set()


def test_a_fast_teardown_does_not_wait_the_normal_drain(monkeypatch):
    monkeypatch.setattr(app_mod, "QUIT_SERVER_DRAIN_S", 1.5)
    monkeypatch.setattr(app_mod, "QUIT_FAST_SERVER_DRAIN_S", 0.05)
    _, fast_elapsed, _ = _teardown(True)
    _, slow_elapsed, _ = _teardown(False, drain_s=1.5)
    assert fast_elapsed < 0.8
    assert slow_elapsed >= 1.4


def test_a_fast_teardown_runs_the_same_steps_in_the_same_order():
    fast, _, _ = _teardown(True, drain_s=0.01)
    slow, _, _ = _teardown(False, drain_s=0.01)
    assert fast == slow == ["server", "children", "capture", "duckdb",
                            "unmount", "rcd", "exit-record"]


def test_the_default_fast_unmount_and_reap_use_the_fast_budgets(monkeypatch):
    seen = {}
    monkeypatch.setattr(mounts_mod, "unmount_all_for_quit",
                        lambda *a, **k: seen.__setitem__("unmount", (a, k)))
    monkeypatch.setattr(mounts_mod, "stop_local_rcd",
                        lambda *a, **k: seen.__setitem__("rcd", (a, k)))
    common = dict(close_duckdb=lambda: None, stop_captures=lambda: None,
                  stop_children=lambda: None, record_exit=lambda: None)
    app_mod.quit_teardown(None, fast=True, **common)
    assert seen["unmount"] == ((), {"budget_s": mounts_mod._QUIT_FAST_UNMOUNT_JOIN_BUDGET_S,
                                    "quiesce_s": mounts_mod._QUIT_FAST_QUIESCE_BUDGET_S})
    assert seen["rcd"] == ((), {"fast": True})
    seen.clear()
    app_mod.quit_teardown(None, **common)
    # A normal quit calls them exactly as before: no arguments.
    assert seen["unmount"] == ((), {})
    assert seen["rcd"] == ((), {})


# ------------------------------------------------------------ mounts / rcd


def test_a_fast_unmount_bounds_the_join_and_the_quiesce_by_its_own_budgets(monkeypatch):
    stuck = threading.Event()
    mounts = [{"id": "a", "name": "a", "remote": "r:a"},
              {"id": "b", "name": "b", "remote": "r:b"}]
    monkeypatch.setattr(mounts_mod, "_rcd_is_ours_to_reap", lambda: True)
    monkeypatch.setattr(mounts_mod, "list_mounts", lambda: mounts)
    monkeypatch.setattr(mounts_mod, "_unmount_for_quit", lambda m: stuck.wait(30))
    monkeypatch.setattr(mounts_mod.lifecycle, "_quit_tile_daemons",
                        lambda: stuck.wait(30))
    try:
        t0 = time.monotonic()
        mounts_mod.unmount_all_for_quit(budget_s=0.2, quiesce_s=0.1)
        elapsed = time.monotonic() - t0
    finally:
        stuck.set()
        mounts_mod._QUIT_TEARDOWN_LATCH.clear()
    # Quiesce 0.1 + the join budget 0.2 (spent once, not per mount) + slack.
    assert elapsed < 1.5


def test_the_default_unmount_budgets_are_the_normal_ones():
    import inspect

    sig = inspect.signature(mounts_mod.unmount_all_for_quit)
    assert sig.parameters["budget_s"].default == mounts_mod._QUIT_UNMOUNT_JOIN_BUDGET_S
    assert sig.parameters["quiesce_s"].default is None


def test_a_fast_reap_escalates_to_sigkill_after_the_short_grace(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rcd_mod.storage, "read_json", lambda p: {"pid": 4242})
    monkeypatch.setattr(mounts_mod, "_confirmed_our_rcd", lambda e: True)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda *a, **k: None)
    state = {"alive": True}
    monkeypatch.setattr(mounts_mod, "_pid_alive", lambda pid: state["alive"])
    sent = []

    def kill(pid, sig):
        sent.append(sig)
        if sig == rcd_mod.signal.SIGKILL:
            state["alive"] = False

    monkeypatch.setattr(rcd_mod.os, "kill", kill)
    t0 = time.monotonic()
    mounts_mod._kill_current_rcd(kill_timeout_s=0.2)
    assert sent == [rcd_mod.signal.SIGTERM, rcd_mod.signal.SIGKILL]
    assert time.monotonic() - t0 < 2.0


def test_stop_local_rcd_fast_uses_the_fast_grace(monkeypatch):
    seen = {}
    monkeypatch.setattr(mounts_mod, "_rcd_is_ours_to_reap", lambda: True)
    monkeypatch.setattr(mounts_mod, "_kill_current_rcd",
                        lambda **k: seen.update(k))
    mounts_mod.stop_local_rcd(fast=True)
    assert seen == {"kill_timeout_s": mounts_mod._FAST_KILL_TIMEOUT_S}
    seen.clear()
    mounts_mod.stop_local_rcd()
    assert seen == {}
