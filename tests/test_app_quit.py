"""The macOS quit teardown (app.py `_do_quit` -> `quit_teardown`/`start_quit`).

Three defects were funnelling through the old one-liner quit (INCIDENT
2026-07-29, crash report FusedRender-2026-07-29-135823.ips):

  A. (retired with the mount feature: the teardown no longer detaches mounts
     or reaps a mount daemon.)
  B. the whole reap ran synchronously inside a menu-item action, i.e. with the
     AppKit run loop blocked — up to ~13s of beachball by construction.
  C. `rumps.quit_application()` -> `NSApplication.terminate:` -> C `exit()`
     runs C++ static destructors with the GIL RELEASED (pyobjc drops it for the
     duration of the ObjC call), and the duckdb reader's cached
     `DuckDBPyConnection` destructs there, touching the Python C-API ->
     Py_FatalError -> abort().

Defect C came back on 2026-08-19 (crash report
FusedRender-2026-08-19-133416.ips) in its general shape: the teardown had fully
succeeded, and the process still aborted in exit()'s `__cxa_finalize` — this
time destructing duckdb's DEFAULT connection, which 1.5.5 creates eagerly at
IMPORT, so no stash and no reader run were involved at all. Two fixes, and only
the second one stops the crash: quit also closes that connection (correct
shutdown of a handle we own — but measured on the shipped interpreter, closing
it averts the abort only while a live Python reference pins the closed wrapper,
which is a leak, not a fix), and the quit never hands control back to C exit()
at all. It ends in `hard_exit` (os._exit), which skips atexit, Python
finalization and `__cxa_finalize` entirely — which is the only fix that also
covers every other native extension we load (GDAL/rasterio, pyarrow, torch).

These tests pin the fix: the ordering (server drain -> children -> duckdb
closes -> exit record), the non-blocking entry point with its hard
deadline, and that every quit surface dies by hard exit. Nothing macOS-only is
exercised — rumps/AppKit are never imported (app.py imports them lazily inside
`main()`).
"""
import os
import sys
import threading
import time
import types

import pytest

import fused_render.app as app_mod


# --------------------------------------------------------------- duckdb default connection
# Defect C: duckdb builds a default connection at import; quit closes it.


class _FakeCon:
    def __init__(self, raises=False):
        self.closed = 0
        self._raises = raises

    def close(self):
        self.closed += 1
        if self._raises:
            raise RuntimeError("connection already invalidated")


# ---- the app-side call: never blocks, never raises, never loads duckdb itself


def test_quit_close_skips_everything_when_duckdb_was_never_imported(monkeypatch):
    # No duckdb in sys.modules == no connection can exist, so quit must not pay
    # a (multi-hundred-ms) duckdb import just to find nothing.
    monkeypatch.delitem(sys.modules, "duckdb", raising=False)

    app_mod._close_duckdb_stash()

    assert "duckdb" not in sys.modules


# ---- the DEFAULT connection: the half of defect C the 2026-07-29 fix missed --
# Closing the reader's stash was necessary and not sufficient. duckdb (1.5.5,
# what the bundle ships) builds its default connection EAGERLY AT IMPORT and
# holds it in a C++ global, so a process that merely `import duckdb`s — which
# the in-process server does for /api/index, parquet, search, git_repos, h3,
# excel, tableau — aborts in __cxa_finalize with no stash ever created.
#
# Closing it is NOT what stops the abort (the hard exit below is). Measured on
# the shipped interpreter, `duckdb.default_connection().close()` still aborts,
# while `dc = duckdb.default_connection(); dc.close()` does not — and neither
# does pinning it WITHOUT closing. The difference is the surviving Python
# reference, not the close: the abort is `~DuckDBPyConnection`, which runs only
# when the last reference dies. See _close_duckdb_default_connection for the
# whole table. What these tests pin is the correct shutdown of a handle we own,
# and that neither half of the close can break the quit.


def _fake_duckdb(default_connection):
    mod = types.ModuleType("duckdb")
    if default_connection is not None:
        mod.default_connection = default_connection
    return mod


@pytest.fixture()
def no_reader():
    """Kept as a no-op: the reader half of the quit close is gone."""
    return None


def test_quit_close_also_closes_duckdbs_default_connection(monkeypatch, no_reader):
    con = _FakeCon()
    monkeypatch.setitem(sys.modules, "duckdb",
                        _fake_duckdb(lambda: con))

    app_mod._close_duckdb_stash()

    assert con.closed == 1


def test_a_default_connection_exposed_as_an_attribute_is_closed_too(monkeypatch,
                                                                    no_reader):
    # 1.5.5 spells it as a builtin function; older/newer duckdb may hand back the
    # connection object itself. Both shapes have to close — guessing wrong is an
    # abort, and the cost of handling both is one `callable()`.
    con = _FakeCon()
    monkeypatch.setitem(sys.modules, "duckdb", _fake_duckdb(con))

    app_mod._close_duckdb_stash()

    assert con.closed == 1


def test_a_duckdb_without_a_default_connection_is_not_an_error(monkeypatch,
                                                               no_reader):
    monkeypatch.setitem(sys.modules, "duckdb", _fake_duckdb(None))

    app_mod._close_duckdb_stash()  # must not raise


def test_a_raising_close_of_the_default_connection_does_not_raise(monkeypatch,
                                                                  no_reader):
    con = _FakeCon(raises=True)
    monkeypatch.setitem(sys.modules, "duckdb", _fake_duckdb(lambda: con))

    app_mod._close_duckdb_stash()  # must not raise

    assert con.closed == 1


def test_the_default_connection_is_left_alone_when_duckdb_was_never_imported(
        monkeypatch, no_reader):
    # Same reason as the stash: no import means no connection, and quit must not
    # pay a duckdb import to learn that.
    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    touched = []
    monkeypatch.setattr(app_mod, "_close_duckdb_default_connection",
                        lambda: touched.append(True))

    app_mod._close_duckdb_stash()

    assert touched == []


# ------------------------------------------------- ordering + the quit entry
# The order is load-bearing, not incidental: the server must stop accepting
# requests first; the duckdb stash must be closed while Python is healthy and
# long before exit(); the exit record is written last.


class _FakeServer:
    def __init__(self):
        self.should_exit = False


@pytest.fixture()
def quit_ctx(monkeypatch):
    """Records every teardown rung, so one trace covers all of them."""
    calls = []
    monkeypatch.setattr(
        app_mod, "_close_duckdb_stash", lambda: calls.append("duckdb"))
    # The children rung would reach the real engine/AI/pty registries and the
    # server discovery file; record it instead.
    monkeypatch.setattr(app_mod, "_stop_children", lambda: calls.append("children"))
    # The "exit-record" rung writes outages.jsonl in the real log home and
    # releases this process's crash file — neither belongs in a unit test.
    monkeypatch.setattr(app_mod, "_record_clean_exit", lambda: None)
    return calls


def test_teardown_order_children_capture_duckdb_then_exit_record(quit_ctx):
    server = _FakeServer()

    steps = app_mod.quit_teardown(server)

    assert server.should_exit is True          # step 1: stop serving requests
    assert quit_ctx[0] == "children"           # nothing outlives the app
    assert "tile-daemons" not in quit_ctx      # shared daemons are never quit
    # "exit-record" (SPEC section 50) is last: a teardown cut off by the hard
    # deadline must leave its crash file behind, because that quit was not clean.
    assert steps == ["server", "children", "capture", "duckdb",
                     "exit-record"]


def test_teardown_drains_the_server_thread_within_a_bounded_wait(quit_ctx):
    # A hung request handler must not become a hung quit.
    never = threading.Event()
    thread = threading.Thread(target=lambda: never.wait(30), daemon=True)
    thread.start()
    try:
        t0 = time.monotonic()
        app_mod.quit_teardown(_FakeServer(), server_thread=thread, drain_s=0.2)
        assert time.monotonic() - t0 < 3.0
    finally:
        never.set()
    # ...and the rest of the teardown still ran.
    assert "duckdb" in quit_ctx


def test_a_failing_rung_does_not_stop_the_later_ones(quit_ctx, monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(app_mod, "_close_duckdb_stash", boom)
    steps = app_mod.quit_teardown(_FakeServer())
    assert "children" in quit_ctx
    assert steps[-1] == "exit-record"


def test_start_quit_returns_promptly_and_terminates_afterwards():
    entered = threading.Event()
    terminated = threading.Event()

    def _slow_teardown():
        entered.set()
        time.sleep(1.0)

    t0 = time.monotonic()
    watchdog = app_mod.start_quit(None, terminate=terminated.set,
                                  teardown=_slow_teardown, deadline_s=5.0)
    elapsed = time.monotonic() - t0

    # The menu action runs on the AppKit main thread: blocking here IS the
    # beachball, whatever the teardown costs.
    assert elapsed < 0.3
    assert entered.wait(2.0)
    assert not terminated.is_set()  # teardown gets its chance to finish first
    watchdog.join(5.0)
    assert terminated.is_set()


def test_start_quit_terminates_anyway_when_teardown_never_finishes():
    # A wedged umount -f must not leave an app that can never be quit.
    release = threading.Event()
    terminated = threading.Event()
    try:
        app_mod.start_quit(None, terminate=terminated.set,
                           teardown=lambda: release.wait(30), deadline_s=0.2)
        assert terminated.wait(3.0)
    finally:
        release.set()


def test_start_quit_terminates_once_even_if_teardown_raises():
    terminated = []

    def _boom():
        raise RuntimeError("mount store unreadable")

    watchdog = app_mod.start_quit(None, terminate=lambda: terminated.append(True),
                                 teardown=_boom, deadline_s=5.0)
    watchdog.join(5.0)

    assert terminated == [True]


def test_the_quit_action_is_idempotent_across_both_surfaces():
    # The app stays alive and clickable while teardown runs, and the menu item
    # and the popover's quitApp_ are the SAME action object — a second click
    # (from either surface) must not start a second reap racing the first.
    state = {"server": "srv", "server_thread": "thr"}
    starts, pidfiles = [], []
    do_quit = app_mod.make_quit_action(
        state, terminate=lambda: None,
        start=lambda server, **kw: starts.append((server, kw)),
        remove_pidfile=lambda: pidfiles.append(True))

    do_quit()
    do_quit()

    assert len(starts) == 1
    assert pidfiles == [True]
    assert starts[0][0] == "srv"
    assert starts[0][1]["server_thread"] == "thr"


def test_the_quit_action_works_before_the_server_has_booted():
    # Quit during startup: the bootstrap thread hasn't published the server yet.
    state = {}
    starts = []
    app_mod.make_quit_action(state, terminate=lambda: None,
                            start=lambda server, **kw: starts.append(server),
                            remove_pidfile=lambda: None)()

    assert starts == [None]


# ------------------------------------------------------------- the arithmetic
# The hard deadline is a BACKSTOP for a step that hangs, so it has to be larger
# than every bounded step it waits on — otherwise it fires mid-teardown and
# becomes the bug it guards against.


def test_children_rung_arms_the_spawn_latches_before_any_killer_runs(monkeypatch):
    # The server is still answering while the rung runs (bounded drain, SSE
    # never closes), so an in-flight route could respawn what stop_all just
    # killed. The latch must therefore be armed FIRST (bugbot, PR #1400). Every
    # real killer is replaced: the latch must not be left set for later tests,
    # and remove_server_json must not touch the real discovery file.
    from fused_render.ai import supervisor
    from fused_render.bots import browser as bots_browser
    from fused_render.bots import registry as bots_registry
    from fused_render.server import engine_host, index_watch
    from fused_render.server import app as server_app
    from fused_render import pty_session

    order: list[str] = []
    monkeypatch.setattr(engine_host, "refuse_new_children",
                        lambda: order.append("latch-engines"))
    monkeypatch.setattr(supervisor, "refuse_new_workers",
                        lambda: order.append("latch-ai"))
    monkeypatch.setattr(bots_browser, "refuse_launches",
                        lambda: order.append("latch-bots"))
    monkeypatch.setattr(bots_registry, "shutdown",
                        lambda budget_s=None: order.append("bots"))
    monkeypatch.setattr(engine_host, "stop_all", lambda: order.append("engines"))
    monkeypatch.setattr(supervisor, "unload_all", lambda: order.append("ai"))
    monkeypatch.setattr(pty_session.REGISTRY, "shutdown_all",
                        lambda: order.append("terminals"))
    monkeypatch.setattr(index_watch, "stop", lambda: order.append("index"))
    monkeypatch.setattr(server_app, "remove_server_json",
                        lambda: order.append("discovery"))
    import fused_render.index.runner as runner
    monkeypatch.setattr(runner, "list_runs", lambda cfg: {"runs": []})

    app_mod._stop_children(budget_s=2.0)

    assert order[:3] == ["latch-engines", "latch-ai", "latch-bots"]
    assert set(order[3:]) == {"engines", "ai", "terminals", "index", "bots", "discovery"}


def test_children_rung_is_bounded_by_its_budget(monkeypatch):
    from fused_render.ai import supervisor
    from fused_render.server import engine_host

    from fused_render.server import index_watch
    from fused_render.server import app as server_app
    from fused_render import pty_session
    import fused_render.index.runner as runner

    from fused_render.bots import browser as bots_browser
    from fused_render.bots import registry as bots_registry

    never = threading.Event()
    monkeypatch.setattr(engine_host, "refuse_new_children", lambda: None)
    monkeypatch.setattr(supervisor, "refuse_new_workers", lambda: None)
    monkeypatch.setattr(bots_browser, "refuse_launches", lambda: None)
    monkeypatch.setattr(bots_registry, "shutdown", lambda budget_s=None: None)
    monkeypatch.setattr(engine_host, "stop_all", lambda: never.wait(30))
    monkeypatch.setattr(supervisor, "unload_all", lambda: None)
    # Every other killer stubbed too: the real index path would cancel live runs
    # on this machine and remove_server_json would touch the real discovery file.
    monkeypatch.setattr(pty_session.REGISTRY, "shutdown_all", lambda: None)
    monkeypatch.setattr(index_watch, "stop", lambda: None)
    monkeypatch.setattr(server_app, "remove_server_json", lambda: None)
    monkeypatch.setattr(runner, "list_runs", lambda cfg: {"runs": []})
    try:
        t0 = time.monotonic()
        app_mod._stop_children(budget_s=0.3)
        assert time.monotonic() - t0 < 3.0
    finally:
        never.set()


def test_the_hard_deadline_exceeds_the_sum_of_the_bounded_steps():
    inner = (app_mod.QUIT_SERVER_DRAIN_S
             + app_mod.QUIT_CHILDREN_BUDGET_S)

    # Strictly greater: the margin covers the unbudgeted interstitials.
    assert app_mod.QUIT_HARD_DEADLINE_S > inner


# --------------------------------------------------- the hard exit (defect C)
# The 2026-08-19 recurrence (crash report FusedRender-2026-08-19-133416.ips):
# a fully successful teardown ("quit teardown finished in 11.5s (steps: server,
# duckdb, unmount, rcd)"), then SIGABRT 20ms later inside
# -[NSApplication terminate:] -> exit() -> __cxa_finalize_ranges ->
# ~DuckDBPyConnection -> PyEval_SaveThread -> Py_FatalError. Closing connections
# one by one is whack-a-mole across every native extension we load (duckdb,
# GDAL/rasterio, pyarrow, torch), so the quit stops handing control back to C
# exit() at all.

# Captured BEFORE the autouse stub below replaces the module global — the tests
# of hard_exit itself need the real function.
_REAL_HARD_EXIT = app_mod.hard_exit


@pytest.fixture(autouse=True)
def no_real_exit(monkeypatch):
    """Safety net for the WHOLE module: nothing here may actually _exit the
    pytest worker. Every quit surface now ends in `hard_exit`, and a test that
    forgot to inject a stub would take the run down with it — silently, since an
    os._exit'd worker is not a test failure. Returns the recorded exit codes."""
    codes = []
    monkeypatch.setattr(app_mod, "hard_exit", lambda code=0: codes.append(code))
    return codes


def test_hard_exit_calls_os_exit_with_the_code():
    exits = []

    _REAL_HARD_EXIT(3, exit_process=exits.append)

    assert exits == [3]


def test_hard_exit_flushes_logging_before_dying(monkeypatch):
    # os._exit runs no atexit handler, and logging's flush IS one: without this
    # the last lines of the quit log — exactly what a crash report is read
    # against — can be lost.
    order = []
    monkeypatch.setattr(app_mod.logging, "shutdown",
                        lambda: order.append("logging"))

    _REAL_HARD_EXIT(0, exit_process=lambda code: order.append(("exit", code)))

    assert order == ["logging", ("exit", 0)]


def test_hard_exit_still_exits_when_the_log_flush_raises(monkeypatch):
    # Dying is not optional: a flush that raises must not leave the app alive.
    def _boom():
        raise RuntimeError("handler already closed")

    monkeypatch.setattr(app_mod.logging, "shutdown", _boom)
    exits = []

    _REAL_HARD_EXIT(0, exit_process=exits.append)

    assert exits == [0]


def test_hard_exit_still_exits_when_the_log_flush_HANGS(monkeypatch):
    """A try/except catches raises, not hangs — and the hang is the case that
    matters. `logging.shutdown()` acquires every handler's lock, and the one
    scenario the AppKit backstop exists for (a teardown thread wedged somewhere
    unbudgeted) is precisely the scenario where that thread may be wedged
    mid-emit holding the RotatingFileHandler lock — a rollover or write against
    a wedged FUSED_RENDER_LOG_DIR. Blocking on acquire() there would make the
    app unquittable, the exact outcome QUIT_HARD_DEADLINE_S and the backstop
    exist to rule out."""
    wedged = threading.Event()
    monkeypatch.setattr(app_mod.logging, "shutdown", wedged.wait)
    exits = []

    started = time.monotonic()
    _REAL_HARD_EXIT(0, exit_process=exits.append, flush_budget_s=0.1)
    elapsed = time.monotonic() - started

    assert exits == [0]
    assert elapsed < 2.0, "the flush must be bounded, not waited out"
    wedged.set()  # let the parked flusher unwind


def test_the_log_flush_budget_is_small_enough_to_be_invisible():
    # It is paid on EVERY quit, so it may not be a second of beachball; and it
    # is a backstop on a wedged filesystem, not a real I/O budget — the flush
    # takes microseconds whenever anything is working.
    assert 0 < app_mod.QUIT_LOG_FLUSH_S <= 1.0


# ------------------------------------------- AppKit's own quit surfaces (D34)
# The app is a REGULAR app (setup_py2app.py sets no LSUIElement: Dock icon AND
# menu bar item, D34), so the Dock icon's right-click Quit, ⌘Q and logout/restart
# all send -[NSApplication terminate:] straight through to exit(). Those surfaces
# never touch make_quit_action, so before applicationShouldTerminate_ existed
# every defect this branch fixes was still fully live on them. The delegate hook
# and the tray action must converge on ONE teardown.


class _FakeStart:
    """Stand-in for start_quit: records the call and lets the test decide when
    teardown 'finishes' by invoking the terminate callback."""

    def __init__(self):
        self.calls = []

    def __call__(self, server, *, terminate, server_thread=None, **kw):
        self.calls.append((server, server_thread))
        self.terminate = terminate
        return None

    def finish(self):
        self.terminate()


@pytest.fixture()
def quit_state():
    return {"server": "srv", "server_thread": "thr"}


def _wait_for(recorded, timeout=3.0):
    """Block until a background quit thread records something (or give up)."""
    deadline = time.monotonic() + timeout
    while not recorded and time.monotonic() < deadline:
        time.sleep(0.01)


class _StubExit:
    """Stands in for hard_exit in the threaded cases.

    A plain recorder is not faithful: the real one never returns, so a recorder
    that does makes the hook take its last-resort `reply` branch. This records
    the code and then blocks forever, which is what "the process died here"
    looks like to the daemon thread that called it."""

    def __init__(self):
        self.codes = []
        self._dead = threading.Event()

    def __call__(self, code):
        self.codes.append(code)
        self._dead.wait()  # never set: this thread is "gone"

    def release(self):
        self._dead.set()


@pytest.fixture()
def stub_exit():
    stub = _StubExit()
    yield stub
    stub.release()  # let the parked daemon thread unwind at teardown


def test_begin_quit_starts_one_teardown_and_flags_ready_before_terminating(
        quit_state):
    start = _FakeStart()
    order = []
    started = app_mod.begin_quit(
        quit_state, terminate=lambda: order.append("terminate"),
        start=start, remove_pidfile=lambda: order.append("pidfile"))

    assert started is True
    assert order == ["pidfile"]
    assert start.calls == [("srv", "thr")]
    assert not app_mod._quit_ready_event(quit_state).is_set()

    start.finish()  # teardown done (or its deadline fired)

    # ready is set BEFORE the surface's own terminate action, because the AppKit
    # hook reads it to answer NSTerminateNow for the terminate: that action causes.
    assert app_mod._quit_ready_event(quit_state).is_set()
    assert order == ["pidfile", "terminate"]


def test_begin_quit_runs_the_claim_hook_before_the_teardown_can_start(
        quit_state):
    """The ordering guarantee `begin_relaunch` needs (D357 took away the one it
    used to get for free).

    The quit now ends in os._exit off a watchdog thread with no main-thread hop,
    so "do this before we die" cannot mean "do it after begin_quit returns": an
    instance with nothing mounted can finish its teardown and exit while the
    caller is still inside a fork+exec. `on_claim` runs inside the claim, before
    anything that could terminate exists."""
    order = []

    def _start(server, *, terminate, server_thread=None, **kw):
        order.append("teardown")
        terminate()  # a teardown with nothing to do, finishing instantly

    started = app_mod.begin_quit(
        quit_state, terminate=lambda: order.append("terminate"),
        start=_start, remove_pidfile=lambda: order.append("pidfile"),
        on_claim=lambda: order.append("claim-hook"))

    assert started is True
    assert order == ["pidfile", "claim-hook", "teardown", "terminate"]


def test_the_claim_hook_does_not_run_for_a_quit_that_joined_another(quit_state):
    # Same rule the claim bool already encodes: only the surface that STARTED
    # the teardown gets to hang work off it.
    assert app_mod.begin_quit(quit_state, start=_FakeStart(),
                              remove_pidfile=lambda: None) is True
    ran = []

    assert app_mod.begin_quit(quit_state, start=_FakeStart(),
                              remove_pidfile=lambda: None,
                              on_claim=lambda: ran.append(True)) is False
    assert ran == []


def test_a_raising_claim_hook_still_tears_down_and_quits(quit_state):
    # A quit that is already CLAIMED but never torn down is the worst outcome
    # available: quit_ready is never set, so every later surface waits out its
    # backstop. So the hook is best-effort, exactly like the teardown steps.
    start = _FakeStart()

    def _boom():
        raise RuntimeError("Popen failed")

    started = app_mod.begin_quit(quit_state, terminate=lambda: None,
                                 start=start, remove_pidfile=lambda: None,
                                 on_claim=_boom)

    assert started is True
    assert start.calls == [("srv", "thr")]


def test_begin_quit_joins_a_teardown_already_in_flight(quit_state):
    assert app_mod.begin_quit(quit_state, start=_FakeStart(),
                              remove_pidfile=lambda: None) is True
    second = _FakeStart()

    assert app_mod.begin_quit(quit_state, start=second,
                              remove_pidfile=lambda: None) is False
    assert second.calls == []


def test_appkit_quit_starts_the_same_teardown_and_hard_exits_when_it_is_done(
        quit_state, stub_exit):
    start = _FakeStart()
    exits, replies = stub_exit.codes, []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=start,
        remove_pidfile=lambda: None, exit_now=stub_exit)

    assert hook() == app_mod.NS_TERMINATE_LATER  # AppKit waits while we tear down
    assert start.calls == [("srv", "thr")]       # ...on the ONE teardown
    time.sleep(0.05)
    assert exits == [], "must not die before the teardown finishes"

    start.finish()

    _wait_for(exits)
    assert exits == [0]
    # NOT replyToApplicationShouldTerminate:, which resumes AppKit's exit() and
    # aborts in __cxa_finalize (the 2026-08-19 crash). The teardown is done, so
    # there is nothing left a graceful termination would still do for us.
    assert replies == []


def test_appkit_quit_during_a_tray_teardown_does_not_start_a_second_one(
        quit_state, stub_exit):
    start = _FakeStart()
    tray_terminated = []
    app_mod.make_quit_action(quit_state, terminate=lambda: tray_terminated.append(True),
                             start=start, remove_pidfile=lambda: None)()
    second = _FakeStart()
    exits, replies = stub_exit.codes, []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=second,
        remove_pidfile=lambda: None, exit_now=stub_exit)

    assert hook() == app_mod.NS_TERMINATE_LATER
    assert second.calls == []  # converged on the tray's teardown

    start.finish()

    _wait_for(exits)
    assert exits == [0]
    assert replies == []
    assert tray_terminated == [True]


def test_appkit_terminate_after_our_own_teardown_finished_is_immediate(quit_state):
    # A second Quit after one completed. Nothing is left to wait for, so this
    # dies on the spot rather than answering LATER (which would hang the quit)
    # or NOW (which hands control to exit() and aborts).
    start = _FakeStart()
    app_mod.make_quit_action(quit_state, terminate=lambda: None, start=start,
                             remove_pidfile=lambda: None)()
    start.finish()

    second = _FakeStart()
    exits = []

    def _exit_and_die(code):
        # Faithful to the real one: it does not come back. SystemExit is a
        # BaseException, so the hook's `except Exception` guards cannot swallow
        # it — which is how this test proves the reply branch is never reached
        # on the normal path.
        exits.append(code)
        raise SystemExit(code)

    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=lambda ok: pytest.fail("no reply is owed"),
        start=second, remove_pidfile=lambda: None, exit_now=_exit_and_die)

    with pytest.raises(SystemExit):
        hook()
    assert exits == [0]
    assert second.calls == []


def test_an_immediate_appkit_terminate_answers_now_if_the_exit_hands_back(
        quit_state):
    # The unreachable-in-the-app return value, pinned anyway: if a stubbed or
    # broken exit ever lets control back, NSTerminateNow is the honest answer —
    # it is what the hook meant before the hard exit existed.
    start = _FakeStart()
    app_mod.make_quit_action(quit_state, terminate=lambda: None, start=start,
                             remove_pidfile=lambda: None)()
    start.finish()

    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=lambda ok: pytest.fail("no reply is owed"),
        start=_FakeStart(), remove_pidfile=lambda: None,
        exit_now=lambda code: None)

    assert hook() == app_mod.NS_TERMINATE_NOW


def test_appkit_quit_is_not_left_pending_if_ready_is_never_set(quit_state,
                                                               monkeypatch,
                                                               stub_exit):
    # Defence in depth, unchanged in intent by the hard exit: an app AppKit is
    # waiting on is unquittable, so the waiter gives up on the event and dies
    # anyway. The teardown had its bounded chance (this backstop sits past the
    # quit deadline), and a mount left attached beats an app that cannot quit.
    monkeypatch.setattr(app_mod, "QUIT_APPKIT_REPLY_WAIT_S", 0.2)
    exits, replies = stub_exit.codes, []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=lambda *a, **k: None,
        remove_pidfile=lambda: None, exit_now=stub_exit)

    assert hook() == app_mod.NS_TERMINATE_LATER

    _wait_for(exits)
    assert exits == [0]
    assert replies == []


def test_a_hard_exit_that_somehow_returns_falls_back_to_replying(quit_state,
                                                                 monkeypatch):
    # `reply` is not a dead parameter: it is the last resort for the one case
    # where the process is still alive after exit_now. os._exit cannot fail, but
    # a future hard_exit that grew a bug would otherwise leave AppKit waiting
    # forever on a reply we owe it.
    monkeypatch.setattr(app_mod, "QUIT_APPKIT_REPLY_WAIT_S", 0.1)
    replies = []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=lambda *a, **k: None,
        remove_pidfile=lambda: None, exit_now=lambda code: None)

    assert hook() == app_mod.NS_TERMINATE_LATER

    _wait_for(replies)
    assert replies == [True]


def test_a_failing_hard_exit_still_replies_and_does_not_raise(quit_state,
                                                              monkeypatch):
    monkeypatch.setattr(app_mod, "QUIT_APPKIT_REPLY_WAIT_S", 0.1)

    def _boom(_code):
        raise RuntimeError("os._exit is gone")

    replies = []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=lambda *a, **k: None,
        remove_pidfile=lambda: None, exit_now=_boom)
    assert hook() == app_mod.NS_TERMINATE_LATER

    _wait_for(replies)
    assert replies == [True]


def test_appkit_reply_failure_does_not_raise_into_the_thread(quit_state,
                                                             monkeypatch):
    monkeypatch.setattr(app_mod, "QUIT_APPKIT_REPLY_WAIT_S", 0.1)

    def _boom(_ok):
        raise RuntimeError("callAfter unavailable")

    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=_boom, start=lambda *a, **k: None,
        remove_pidfile=lambda: None, exit_now=lambda code: None)
    assert hook() == app_mod.NS_TERMINATE_LATER
    time.sleep(0.3)  # the waiter thread must die quietly


def test_installing_the_terminate_hook_never_makes_the_app_unquittable():
    """PV-8 shape: if the delegate patch fails (a rumps that rejects it, an
    upgrade that changes the class), log and keep today's behavior instead of
    raising out of main() — an app that won't launch is worse than one whose
    AppKit quit skips the teardown."""
    class _Locked:
        def __setattr__(self, name, value):
            raise TypeError("cannot set attributes on this class")

    assert app_mod.install_terminate_hook(_Locked(), lambda: 1) is False

    class _Open:
        pass

    target = _Open()
    assert app_mod.install_terminate_hook(target, lambda: 1) is True
    assert callable(target.applicationShouldTerminate_)


# ---- no surface may reach NSApplication.terminate: around the teardown -------


def _app_source_tree():
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render", "app.py")
    with open(path) as f:
        return ast.parse(f.read())


def _enclosing_functions(tree, predicate):
    """Names of the functions containing a node matching `predicate`."""
    import ast
    found = []

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            nxt = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nxt = stack + [child.name]
            if predicate(child):
                found.append(stack[-1] if stack else "<module>")
            walk(child, nxt)

    walk(tree, [])
    return found


def test_no_code_path_reaches_nsapplication_terminate_any_more():
    """Structural, because both bypasses are invisible in behavior.

    rumps.quit_application() -> -[NSApplication terminate:] was BOTH failure
    modes at once: called directly it skips the teardown entirely (no drain, no
    duckdb close, no unmount, no rcd reap — the readiness-failure abort in
    _bootstrap_server did exactly that), and called at the END of the teardown it
    still lands in C exit(), whose __cxa_finalize destructs native globals with
    the GIL released and aborts (the 2026-08-19 crash, after a teardown that had
    fully succeeded). There is no longer any correct caller: the quit dies via
    hard_exit, so no call site may exist at all."""
    import ast

    def is_quit_call(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "quit_application")

    assert _enclosing_functions(_app_source_tree(), is_quit_call) == []


def test_the_terminate_hop_hard_exits():
    """The other half of the property above: the tray/popover/relaunch quit still
    ENDS somewhere, and that end is hard_exit rather than AppKit's termination."""
    import ast

    def is_hard_exit_call(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "hard_exit")

    assert "_terminate" in _enclosing_functions(_app_source_tree(),
                                                is_hard_exit_call)


def test_the_readiness_failure_abort_goes_through_the_quit_action():
    import ast

    def is_do_quit_call(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_do_quit")

    assert "_bootstrap_server" in _enclosing_functions(
        _app_source_tree(), is_do_quit_call)


class _RacingState(dict):
    """A `state` that holds every caller INSIDE the check-then-set window until they
    have all read the flag, so the interleave is a certainty rather than a timing
    accident. A barrier-and-hope version of these tests (start N threads and wait for
    the GIL to switch in the right microsecond) passed against the *unlocked* code —
    worse than no test — and a sleep-in-the-read version flipped depending on how
    fast thread startup happened to be.

    With the window guarded, only one caller ever reaches the read; the barrier then
    times out, which is why its wait is short and its expiry is not an error."""

    def __init__(self, *a, parties=2, slow_key="quitting", **kw):
        super().__init__(*a, **kw)
        self._barrier = threading.Barrier(parties)
        self._slow_key = slow_key

    def get(self, key, default=None):
        value = super().get(key, default)
        if key == self._slow_key and not self._barrier.broken:
            try:
                self._barrier.wait(0.5)
            except threading.BrokenBarrierError:
                pass  # guarded: the other caller never got here
        return value


def _race(target, parties=2):
    threads = [threading.Thread(target=target) for _ in range(parties)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)


def test_only_one_teardown_starts_when_two_threads_quit_at_once():
    """begin_quit's check-then-set used to lean on "both entry points are AppKit
    callbacks, so no lock is needed" — a premise the readiness-failure abort
    falsified: _bootstrap_server calls the quit action from the BOOTSTRAP thread. A
    Dock/⌘Q quit interleaving with it could see `quitting` False in both and run two
    unmount fan-outs and two reaps (the second likely raising "did not exit")."""
    state = _RacingState(server="srv", server_thread="thr")
    start = _FakeStart()
    started = []

    _race(lambda: started.append(app_mod.begin_quit(
        state, start=start, remove_pidfile=lambda: None)))

    assert started.count(True) == 1, "exactly one caller may start the teardown"
    assert len(start.calls) == 1


def test_the_ready_event_is_the_same_object_for_racing_callers():
    # Two lazily-created Events would mean one surface waiting on a signal the other
    # never sets — an app AppKit is waiting on a reply from, forever.
    state = _RacingState(slow_key="quit_ready")
    seen = []

    _race(lambda: seen.append(app_mod._quit_ready_event(state)))

    assert len({id(e) for e in seen}) == 1
