"""A relaunch-initiated quit runs the same ordered teardown on tighter budgets.

The server never drains on the packaged app (SSE connections stay open, so the
2 s join always times out). A normal user Quit keeps every budget; only the
relaunch trims them (the server drain).

AppKit-free, like test_app_quit.py; nothing real is mounted or signalled.
"""
import threading
import time

import pytest

import fused_render.app as app_mod


# ------------------------------------------------------------------ plumbing


def test_a_normal_quit_budgets_are_unchanged():
    assert app_mod.QUIT_SERVER_DRAIN_S == 2.0
    assert app_mod.QUIT_CHILDREN_BUDGET_S == 5.0


def test_the_fast_budgets_are_strictly_tighter():
    assert app_mod.QUIT_FAST_SERVER_DRAIN_S < app_mod.QUIT_SERVER_DRAIN_S
    assert app_mod.QUIT_FAST_HARD_DEADLINE_S < app_mod.QUIT_HARD_DEADLINE_S


def test_the_fast_deadline_is_derived_from_the_fast_steps_it_waits_on():
    inner = (app_mod.QUIT_FAST_SERVER_DRAIN_S
             + app_mod.QUIT_CHILDREN_BUDGET_S)
    # Strictly greater: the margin covers the unbudgeted interstitials.
    assert app_mod.QUIT_FAST_HARD_DEADLINE_S > inner


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
            **kw)
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
                            "exit-record"]
