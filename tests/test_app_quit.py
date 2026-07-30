"""The macOS quit teardown (app.py `_do_quit` -> `quit_teardown`/`start_quit`).

Three defects were funnelling through the old one-liner quit (INCIDENT
2026-07-29, crash report FusedRender-2026-07-29-135823.ips):

  A. rcd was SIGTERM/SIGKILLed with live kernel NFS mounts still attached, so
     the NFS server vanished under its own client and macOS raised "server
     connection interrupted / disks not ejected properly". The quit path was
     the one mount teardown in the tree that skipped the rc-unmount ->
     force-unmount ladder every other path goes through.
  B. the whole reap ran synchronously inside a menu-item action, i.e. with the
     AppKit run loop blocked — up to ~13s of beachball by construction.
  C. `rumps.quit_application()` -> `NSApplication.terminate:` -> C `exit()`
     runs C++ static destructors with the GIL RELEASED (pyobjc drops it for the
     duration of the ObjC call), and the duckdb reader's cached
     `DuckDBPyConnection` destructs there, touching the Python C-API ->
     Py_FatalError -> abort().

These tests pin the fix: the ordering (server drain -> duckdb stash close ->
unmount -> reap rcd), the non-blocking entry point with its hard deadline, and
per-mount isolation. Nothing macOS-only is exercised — rumps/AppKit are never
imported (app.py imports them lazily inside `main()`), and the mount ladder is
faked at the rc/subprocess boundary exactly like tests/test_shell_mounts.py and
tests/test_mounts_rcd_owner.py do, so no real rclone, mount or `umount` runs.
"""
import importlib.util
import os
import sys
import threading
import time
import types

import pytest

import fused_render.app as app_mod
import fused_render.shell.mounts as mounts_mod


# --------------------------------------------------------------- duckdb stash
# Defect C. The stash lives on the *duckdb module* (see reader._http_connection),
# which is why quit can reach it at all: the reader module object itself is
# transient (executor._run_inprocess never puts it in sys.modules), so the
# connection outlives every reader run and nothing ever closed it.


def _load_reader():
    """The duckdb template's reader.py, loaded by path (it isn't importable —
    templates/ is deliberately not a package). Same loader as
    tests/test_duckdb_reader.py."""
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", "duckdb", "reader.py")
    spec = importlib.util.spec_from_file_location("duckdb_reader_quit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCon:
    def __init__(self, raises=False):
        self.closed = 0
        self._raises = raises

    def close(self):
        self.closed += 1
        if self._raises:
            raise RuntimeError("connection already invalidated")


def _clear_duckdb_module_state(reader_mod) -> None:
    """Remove BOTH pieces of process-global state the reader keeps on the duckdb
    module — the connection stash and the one-way quit latch.

    `monkeypatch.delattr(..., raising=False)` is NOT this, which is the trap the
    first version of the fixture fell into: it records an undo entry only when the
    attribute already EXISTS, so on a clean run there is nothing to restore and a
    test that trips the latch leaves `duckdb._fused_render_http_con_closed = True`
    on the real, sys.modules-resident duckdb module for the rest of the worker.
    After that `_http_connection` raises for every later test in the process, and
    tests/test_duckdb_reader.py's `source_url` cases don't fail loudly — the reader
    swallows the raise and reads the local path instead, so they assert on a
    silently different code path. (Invisible in this checkout only because httpfs
    isn't installed and those tests skip.)

    The shared lock goes too. It carries no state, but a test that somehow left it
    HELD would deadlock every later test in the worker; dropping the attribute means
    the next test gets a fresh one. Safe only because these tests always join their
    builder threads — never delete a lock another live thread may hold."""
    for attr in (reader_mod._HTTP_CON_KEY, reader_mod._HTTP_CON_LATCH,
                 reader_mod._HTTP_CON_LOCK):
        try:
            delattr(reader_mod.duckdb, attr)
        except AttributeError:
            pass


@pytest.fixture()
def reader():
    """reader.py with a guaranteed-clean stash slot AND latch on the real duckdb
    module — cleaned both before and after, explicitly, since the latch is one-way
    by design and must not outlive the test that tripped it."""
    pytest.importorskip("duckdb")
    mod = _load_reader()
    _clear_duckdb_module_state(mod)
    try:
        yield mod
    finally:
        _clear_duckdb_module_state(mod)


def test_close_http_connection_closes_and_clears_the_stash(reader):
    con = _FakeCon()
    setattr(reader.duckdb, reader._HTTP_CON_KEY, con)

    assert reader.close_http_connection() is True

    assert con.closed == 1
    # Cleared, not merely closed: a closed-but-reachable DuckDBPyConnection is
    # still a live object for exit()'s static destructors to trip over, and
    # _http_connection would hand out cursors from it.
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)


def test_close_http_connection_is_a_no_op_without_a_stash(reader):
    # Never previewed a mounted parquet file this run — the common case.
    assert reader.close_http_connection() is False


def test_close_http_connection_survives_a_raising_close(reader):
    con = _FakeCon(raises=True)
    setattr(reader.duckdb, reader._HTTP_CON_KEY, con)

    assert reader.close_http_connection() is False

    # The stash is dropped either way: quit must not leave the connection
    # reachable just because close() complained.
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)


def test_http_connection_stashes_under_the_shared_key(reader, monkeypatch):
    """_http_connection and close_http_connection must agree on the key by
    construction (one constant), not by two matching string literals."""
    made = _FakeCon()
    made.cursor = lambda: "cursor"
    monkeypatch.setattr(reader.duckdb, "connect", lambda *a, **k: made,
                        raising=False)
    made.execute = lambda sql: None

    assert reader._http_connection() == "cursor"
    assert getattr(reader.duckdb, reader._HTTP_CON_KEY) is made

    assert reader.close_http_connection() is True


# ---- the app-side call: never blocks, never raises, never loads duckdb itself


def test_quit_close_of_the_stash_reaches_the_reader(monkeypatch):
    called = []
    stub = types.ModuleType("__fused_duckdb_reader_stub__")
    stub.close_http_connection = lambda: called.append(True) or True
    monkeypatch.setitem(sys.modules, "duckdb", types.ModuleType("duckdb"))
    monkeypatch.setattr(app_mod, "_load_duckdb_reader", lambda: stub)

    app_mod._close_duckdb_stash()

    assert called == [True]


def test_quit_close_skips_everything_when_duckdb_was_never_imported(monkeypatch):
    # No duckdb in sys.modules == no connection can exist, so quit must not pay
    # a (multi-hundred-ms) duckdb import just to find nothing.
    loaded = []
    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr(app_mod, "_load_duckdb_reader",
                        lambda: loaded.append(True))

    app_mod._close_duckdb_stash()

    assert loaded == []


def test_quit_close_swallows_a_reader_that_cannot_be_loaded(monkeypatch, tmp_path):
    # duckdb not installed / a mangled reader.py: an ImportError here must not
    # take the quit path down with it.
    monkeypatch.setitem(sys.modules, "duckdb", types.ModuleType("duckdb"))
    monkeypatch.setattr(app_mod, "_DUCKDB_READER_PATH",
                        str(tmp_path / "does-not-exist.py"))

    app_mod._close_duckdb_stash()  # must not raise


def test_quit_close_swallows_a_raising_reader_hook(monkeypatch):
    stub = types.ModuleType("__fused_duckdb_reader_stub__")

    def _boom():
        raise RuntimeError("duckdb is wedged")

    stub.close_http_connection = _boom
    monkeypatch.setitem(sys.modules, "duckdb", types.ModuleType("duckdb"))
    monkeypatch.setattr(app_mod, "_load_duckdb_reader", lambda: stub)

    app_mod._close_duckdb_stash()  # must not raise


# ----------------------------------------------------- the mount ladder (A)
# Faked at the same boundary tests/test_shell_mounts.py fakes: `_rc` (rcd's HTTP
# API), `_force_unmount` (the umount -f / diskutil shell-out) and `_is_mounted`
# (the kernel mount table). Every one of them is resolved through the package at
# CALL time by the code under test, so patching `mounts_mod` reaches it.

_RCD_PID = 4321


@pytest.fixture(autouse=True)
def _clear_quit_interlock():
    """The teardown latch is module-global and deliberately sticky within a
    process, so every test here clears it on both sides — otherwise the attach
    tests in tests/test_shell_mounts.py would find every mount refused."""
    mounts_mod._QUIT_TEARDOWN_LATCH.clear()
    try:
        yield
    finally:
        mounts_mod._QUIT_TEARDOWN_LATCH.clear()


@pytest.fixture()
def ladder(tmp_path, monkeypatch):
    """Records the whole quit teardown ladder without touching a real mount."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_RCLONE_PERSIST", raising=False)
    monkeypatch.setattr(mounts_mod, "_live_port_cache", None)  # no cross-test leak

    ctx = {
        "calls": [],                 # ordered ladder trace
        "mounts": [{"id": "a", "name": "alpha", "remote": "s3a:bucket"},
                   {"id": "b", "name": "beta", "remote": "s3b:bucket"}],
        "rc_fail": set(),            # mount names whose rc mount/unmount errors
        "force_fail": set(),         # mount names whose force unmount raises
        "hang": set(),               # mount names whose force unmount never returns
        "lingers": set(),            # rcd reports success, kernel mount survives
        "entry": {"port": 5572, "pid": _RCD_PID, "spawner_pid": os.getpid()},
        "signalled": [],             # pids os.kill was called with
        "kernel": {"alpha", "beta"}, # what the kernel still holds
        "on_force": None,            # side effect to run inside a force unmount
        "release": threading.Event(),
    }

    monkeypatch.setattr(mounts_mod, "list_mounts", lambda: list(ctx["mounts"]))
    monkeypatch.setattr(mounts_mod.storage, "read_json", lambda path: ctx["entry"])

    def _rc(port, method, params=None, **kw):
        name = os.path.basename((params or {}).get("mountPoint", ""))
        ctx["calls"].append(("rc", method, name))
        if name in ctx["rc_fail"]:
            raise RuntimeError("failed to unmount: device or resource busy")
        # A successful rcd unmount drops the kernel entry too (StubRcd models it
        # the same way) — unless the mount is in "lingers", the split-brain where
        # rcd reports success over a kernel mount that stays behind.
        if name not in ctx["lingers"]:
            ctx["kernel"].discard(name)
        return {}

    monkeypatch.setattr(mounts_mod, "_rc", _rc)

    # A live daemon until it is signalled — which is also what makes
    # _kill_current_rcd's "is it gone yet" poll terminate promptly here.
    monkeypatch.setattr(mounts_mod, "_live_rcd_port",
                        lambda *a, **k: None if ctx["signalled"] else 5572)
    # The simulated kernel mount table, faked where tests/test_shell_mounts.py's
    # `rcd` fixture fakes it: os.path.ismount, which is what detach_mount's
    # _ismount reads (lifecycle imports the name at module scope, so patching the
    # package attribute would not reach it).
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: os.path.basename(p) in ctx["kernel"])
    # Quiescing the tile daemons is detach_mount's answer to a BUSY unmount (they
    # hold files open under the mount); recorded, not performed — no daemon exists.
    # Patched on the defining submodule: detach_mount calls this one by bare name
    # (a module global), not through the package re-export.
    monkeypatch.setattr(mounts_mod.lifecycle, "_quit_tile_daemons",
                        lambda: ctx["calls"].append(("quiesce", None)))

    def _force_unmount(mp):
        name = os.path.basename(mp)
        ctx["calls"].append(("force", name))
        if ctx["on_force"] is not None:
            # Lets a test land a racing attach mid-teardown.
            ctx["on_force"](mp)
        if name in ctx["hang"]:
            ctx["release"].wait(30)  # a wedged umount -f, as seen in the field
            return f"force unmount of {mp} failed: still mounted"
        if name in ctx["force_fail"]:
            raise OSError("mountpoint is wedged")
        ctx["kernel"].discard(name)
        return None

    monkeypatch.setattr(mounts_mod, "_force_unmount", _force_unmount)

    # rcd reap gates: proven-ours, and alive until signalled.
    monkeypatch.setattr(mounts_mod, "_confirmed_our_rcd", lambda entry: True)
    monkeypatch.setattr(mounts_mod, "_pid_alive",
                        lambda pid: pid not in ctx["signalled"])

    def _kill(pid, sig):
        ctx["calls"].append(("kill", pid))
        ctx["signalled"].append(pid)

    # rcd.py does `import os`, so this is the same module object the code under
    # test signals through (the documented monkeypatch route for this package).
    monkeypatch.setattr(mounts_mod.os, "kill", _kill)

    yield ctx
    ctx["release"].set()  # never leave a hung fake thread waiting 30s


def _names(calls, kind):
    return [c[-1] for c in calls if c[0] == kind]


def test_unmount_all_detaches_every_mount_through_the_ladder(ladder):
    mounts_mod.unmount_all_for_quit()

    # Sorted: mounts are unmounted in PARALLEL (one wedge must not delay the
    # rest), so the order ACROSS mounts is not deterministic.
    assert sorted(_names(ladder["calls"], "rc")) == ["alpha", "beta"]
    # rcd's own unmount took, so the kernel force rung has nothing to do — it is
    # for what rcd cannot or will not detach.
    assert _names(ladder["calls"], "force") == []
    assert ladder["kernel"] == set()


def test_unmount_all_forces_a_kernel_mount_rcd_claims_to_have_dropped(ladder):
    # The INCIDENT 2026-07-16 split-brain, and the reason the force rung is gated
    # on the KERNEL's view and not on rcd's answer: mount/unmount reports success
    # while the kernel entry stays behind. Left attached, that entry outlives its
    # NFS server by exactly the amount of time it takes to signal rcd.
    ladder["lingers"].add("beta")

    mounts_mod.unmount_all_for_quit()

    assert _names(ladder["calls"], "force") == ["beta"]
    assert ladder["kernel"] == set()


def test_unmount_all_still_forces_when_no_daemon_answers(ladder):
    # The "disconnected" state: rcd already died, the kernel mount survives it.
    ladder["signalled"].append(_RCD_PID)  # makes _live_rcd_port() answer None

    mounts_mod.unmount_all_for_quit()

    assert _names(ladder["calls"], "rc") == []
    assert sorted(_names(ladder["calls"], "force")) == ["alpha", "beta"]


def test_a_busy_mount_quiesces_the_tile_daemons_and_then_forces(ladder):
    # detach_mount's own EBUSY ladder, reused rather than re-implemented: the tile
    # daemons hold files open under the mount (the measured EBUSY cause), so they
    # are asked to quit and the unmount is retried before the force.
    ladder["rc_fail"].add("alpha")

    mounts_mod.unmount_all_for_quit()

    assert _names(ladder["calls"], "rc").count("alpha") == 2  # asked twice
    assert ("quiesce", None) in ladder["calls"]
    assert "alpha" in _names(ladder["calls"], "force")
    assert ladder["kernel"] == set()


def test_a_wedged_mount_does_not_stop_the_others(ladder):
    ladder["mounts"].insert(0, {"id": "w", "name": "wedged", "remote": "s3w:b"})
    ladder["kernel"].add("wedged")
    ladder["rc_fail"].add("wedged")
    ladder["force_fail"].add("wedged")

    mounts_mod.unmount_all_for_quit()

    assert set(_names(ladder["calls"], "force")) == {"wedged"}
    assert sorted(_names(ladder["calls"], "rc")).count("alpha") == 1
    assert ladder["kernel"] == {"wedged"}  # only the unfixable one is left


def test_a_hanging_unmount_is_bounded_and_does_not_hold_the_others(ladder):
    ladder["mounts"].insert(0, {"id": "h", "name": "hangs", "remote": "s3h:b"})
    ladder["kernel"].add("hangs")
    ladder["lingers"].add("hangs")  # reaches the force rung...
    ladder["hang"].add("hangs")     # ...which blocks in the kernel forever

    t0 = time.monotonic()
    mounts_mod.unmount_all_for_quit(budget_s=0.3)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0, "a wedged umount -f must not consume the quit"
    assert ladder["kernel"] == {"hangs"}  # alpha/beta went, despite the wedge


def test_persist_leaves_every_mount_attached(ladder, monkeypatch):
    # Dev (dev.sh): rcd is meant to outlive the process, so its mounts must too —
    # unmounting them would tear the mounts out of a daemon we deliberately keep.
    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "1")

    mounts_mod.unmount_all_for_quit()

    assert ladder["calls"] == []


def test_another_live_spawner_keeps_its_mounts(ladder):
    # rcd is shared per-home: a CLI `fused-render` server may still be serving
    # these very mounts. Same gate stop_local_rcd applies to the reap.
    ladder["entry"] = {"port": 5572, "pid": _RCD_PID,
                       "spawner_pid": os.getpid() + 1}

    mounts_mod.unmount_all_for_quit()

    assert ladder["calls"] == []


def test_ours_to_reap_agrees_with_the_reap_gate(ladder, monkeypatch):
    assert mounts_mod._rcd_is_ours_to_reap() is True

    ladder["entry"] = {"port": 5572, "pid": _RCD_PID,
                       "spawner_pid": os.getpid() + 1}
    assert mounts_mod._rcd_is_ours_to_reap() is False

    ladder["signalled"].append(os.getpid() + 1)  # spawner exited: orphaned rcd
    assert mounts_mod._rcd_is_ours_to_reap() is True

    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "1")
    assert mounts_mod._rcd_is_ours_to_reap() is False


# ------------------------------------------------- ordering + the quit entry
# The order is load-bearing, not incidental: the server must stop accepting
# requests before we pull the mounts out from under any in-flight read; the
# duckdb stash must be closed while Python is healthy and long before exit();
# and every mount must be detached before its NFS server (rcd) is signalled.


class _FakeServer:
    def __init__(self):
        self.should_exit = False


@pytest.fixture()
def quit_ctx(ladder, monkeypatch):
    """`ladder` plus a recorded duckdb close, so one trace covers all four steps."""
    monkeypatch.setattr(
        app_mod, "_close_duckdb_stash",
        lambda: ladder["calls"].append(("duckdb", None)))
    return ladder


def test_teardown_order_duckdb_then_unmounts_then_the_rcd_reap(quit_ctx):
    server = _FakeServer()

    steps = app_mod.quit_teardown(server)

    calls = quit_ctx["calls"]
    kinds = [c[0] for c in calls]
    assert server.should_exit is True          # step 1: stop serving requests
    assert kinds[0] == "duckdb"                # step 2: while the GIL is held
    first_kill = kinds.index("kill")
    unmounts = [i for i, c in enumerate(calls) if c[0] in ("rc", "force")]
    assert unmounts, "the mounts must actually be torn down"
    # step 3 entirely before step 4: no mount may still be attached when its own
    # NFS server gets a signal.
    assert max(unmounts) < first_kill
    assert calls[first_kill] == ("kill", _RCD_PID)
    assert steps == ["server", "duckdb", "unmount", "rcd"]


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
    # ...and the rest of the ladder still ran.
    assert ("kill", _RCD_PID) in quit_ctx["calls"]


def test_teardown_closes_the_duckdb_stash_even_when_rcd_persists(quit_ctx,
                                                                 monkeypatch):
    # The stash is this process's, not the daemon's: persistence keeps rcd and
    # its mounts alive but says nothing about a connection that must not survive
    # into exit().
    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "1")

    app_mod.quit_teardown(_FakeServer())

    assert [c[0] for c in quit_ctx["calls"]] == ["duckdb"]


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


def test_the_hard_deadline_exceeds_the_sum_of_the_bounded_steps():
    inner = (app_mod.QUIT_SERVER_DRAIN_S
             + mounts_mod._QUIT_UNMOUNT_BUDGET_S
             + mounts_mod.RCD_REAP_WORST_CASE_S)

    # Strictly greater: terminating DURING the rcd SIGTERM wait would skip the
    # SIGKILL escalation, and on macOS a surviving rcd reparents to launchd — a
    # live daemon under mounts we may not have finished detaching, which is
    # exactly the alert this branch exists to stop.
    assert app_mod.QUIT_HARD_DEADLINE_S > inner


def test_the_rcd_reap_worst_case_counts_every_blocking_call_it_makes():
    # Derived from rcd's own constants, not restated in app.py: a change to any of
    # them has to move the deadline with it. Each kill phase can overrun its poll
    # budget by one _live_rcd_port() probe — the loops test the clock BEFORE an
    # iteration, so one entered just under the deadline still blocks for a full
    # probe timeout — and that probe was missing from the first version of this
    # sum, which is how the deadline came to be short again.
    assert mounts_mod.RCD_REAP_WORST_CASE_S == pytest.approx(
        mounts_mod._CONFIRM_RC_TIMEOUT_S + mounts_mod._PS_TIMEOUT_S
        + 2 * (mounts_mod._KILL_TIMEOUT_S + mounts_mod._LIVE_PORT_PROBE_TIMEOUT_S))


def test_the_kill_poll_does_not_probe_the_rc_port_while_the_pid_is_alive(
        monkeypatch, tmp_path):
    """The expensive half of the conjunction must be the SHORT-CIRCUITED one.

    `_live_rcd_port()` does a 3s-timeout rc probe; polled first, it ran on every
    iteration for the whole wait (dead-cached, but re-probing every 5s and able to
    overrun the phase). The pid check is a free syscall and is the authoritative
    "our daemon is gone", so it goes first: no probe at all while the process
    lives, and at most one after it dies."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    probes = []
    alive = {"pid": True}

    def _live_rcd_port(*a, **k):
        probes.append(time.monotonic())
        time.sleep(0.4)  # stands in for the rc probe's timeout
        return None

    monkeypatch.setattr(mounts_mod.storage, "read_json",
                        lambda path: {"port": 5572, "pid": _RCD_PID,
                                      "spawner_pid": os.getpid()})
    monkeypatch.setattr(mounts_mod, "_confirmed_our_rcd", lambda entry: True)
    monkeypatch.setattr(mounts_mod, "_pid_alive", lambda pid: alive["pid"])
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", _live_rcd_port)

    def _kill(pid, sig):
        # A daemon that takes a beat to shut down: the loop has to POLL, which is
        # what makes the ordering observable (probe-first burned one probe per
        # iteration for the whole wait).
        threading.Timer(1.2, lambda: alive.__setitem__("pid", False)).start()

    monkeypatch.setattr(mounts_mod.os, "kill", _kill)

    t0 = time.monotonic()
    mounts_mod._kill_current_rcd()

    assert len(probes) <= 1, "one probe after the pid died, never during the wait"
    assert time.monotonic() - t0 < 3.0


# ------------------------------------------------- the stash cannot come back
# Closing the stash mid-teardown is only safe if nothing can re-create it: the
# drain is a BOUNDED join that can time out, so uvicorn may still be serving
# while unmount + reap run for seconds, and one in-process parquet read in that
# window would restash a fresh DuckDBPyConnection and bring the SIGABRT back.


def _working_connect(monkeypatch, reader):
    """Make _http_connection's build path SUCCEED, so the only thing that can
    make it fail is the latch (a real duckdb.connect would raise on `LOAD httpfs`
    in an env without the extension, which would pass these tests vacuously)."""
    made = _FakeCon()
    made.execute = lambda sql: None
    made.cursor = lambda: "cursor"
    monkeypatch.setattr(reader.duckdb, "connect", lambda *a, **k: made,
                        raising=False)
    return made


def test_the_stash_cannot_be_recreated_after_the_close(reader, monkeypatch):
    con = _FakeCon()
    setattr(reader.duckdb, reader._HTTP_CON_KEY, con)
    reader.close_http_connection()
    _working_connect(monkeypatch, reader)

    with pytest.raises(RuntimeError):
        reader._http_connection()
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)


def test_the_latch_holds_on_a_process_that_never_read_a_remote_file(reader,
                                                                   monkeypatch):
    # Closing with nothing stashed still latches, so a read arriving after quit
    # began cannot start one.
    assert reader.close_http_connection() is False
    _working_connect(monkeypatch, reader)

    with pytest.raises(RuntimeError):
        reader._http_connection()
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)


def test_the_fixture_leaves_no_latch_behind_for_the_next_test(reader):
    """The cross-test leak IS the defect here, not the latch itself: this pins the
    cleanup both halves of the fixture use, so a duckdb module that some other test
    file then imports is as it was found."""
    setattr(reader.duckdb, reader._HTTP_CON_KEY, _FakeCon())
    reader.close_http_connection()  # closes, and latches
    assert getattr(reader.duckdb, reader._HTTP_CON_LATCH, False) is True

    _clear_duckdb_module_state(reader)

    assert not hasattr(reader.duckdb, reader._HTTP_CON_LATCH)
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)


def test_the_readers_own_call_site_falls_back_when_the_latch_is_closed(reader,
                                                                      monkeypatch):
    """The raise must be one the existing caller already handles — reader.main's
    remote fast path does `try: cur = _http_connection() except Exception: cur =
    None` and then reads the plain file path."""
    reader.close_http_connection()
    _working_connect(monkeypatch, reader)
    try:
        cur = reader._http_connection()
    except Exception:
        cur = None
    assert cur is None


def test_the_tile_daemons_are_quiesced_once_before_the_mounts_fan_out(ladder):
    # Every per-mount detach_mount would otherwise quit ALL tile daemons on its
    # own (they hold files open under the mounts — the measured EBUSY cause), so
    # N busy mounts meant N rounds of /quit requests. Asked once, up front, before
    # any unmount: the daemons are going away with the app regardless, and a
    # released file cannot cause an EBUSY in the first place.
    ladder["rc_fail"].update({"alpha", "beta"})

    mounts_mod.unmount_all_for_quit()

    calls = ladder["calls"]
    # Before ANY unmount attempt, so no mount has to fail busy first to get it.
    assert calls[0] == ("quiesce", None)
    assert calls.index(("quiesce", None)) < min(
        i for i, c in enumerate(calls) if c[0] == "rc")
    # detach_mount's own busy-retry may still repeat it per mount — that stays its
    # contract for its other callers, and after the hoisted call above those
    # requests hit already-dead ports and fail immediately.


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


def test_begin_quit_joins_a_teardown_already_in_flight(quit_state):
    assert app_mod.begin_quit(quit_state, start=_FakeStart(),
                              remove_pidfile=lambda: None) is True
    second = _FakeStart()

    assert app_mod.begin_quit(quit_state, start=second,
                              remove_pidfile=lambda: None) is False
    assert second.calls == []


def test_appkit_quit_starts_the_same_teardown_and_replies_when_it_is_done(
        quit_state):
    start = _FakeStart()
    replies = []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=start,
        remove_pidfile=lambda: None)

    assert hook() == app_mod.NS_TERMINATE_LATER  # AppKit waits for our reply
    assert start.calls == [("srv", "thr")]       # ...on the ONE teardown
    time.sleep(0.05)
    assert replies == [], "must not resume termination before teardown finishes"

    start.finish()

    deadline = time.monotonic() + 3.0
    while not replies and time.monotonic() < deadline:
        time.sleep(0.01)
    assert replies == [True]


def test_appkit_quit_during_a_tray_teardown_does_not_start_a_second_one(
        quit_state):
    start = _FakeStart()
    tray_terminated = []
    app_mod.make_quit_action(quit_state, terminate=lambda: tray_terminated.append(True),
                             start=start, remove_pidfile=lambda: None)()
    second = _FakeStart()
    replies = []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=second,
        remove_pidfile=lambda: None)

    assert hook() == app_mod.NS_TERMINATE_LATER
    assert second.calls == []  # converged on the tray's teardown

    start.finish()

    deadline = time.monotonic() + 3.0
    while not replies and time.monotonic() < deadline:
        time.sleep(0.01)
    assert replies == [True]
    assert tray_terminated == [True]


def test_appkit_terminate_after_our_own_teardown_finished_is_immediate(quit_state):
    # The tray path ends by calling terminate: itself, which re-enters this hook.
    # Nothing is left to wait for, so answering LATER would hang the quit.
    start = _FakeStart()
    app_mod.make_quit_action(quit_state, terminate=lambda: None, start=start,
                             remove_pidfile=lambda: None)()
    start.finish()

    second = _FakeStart()
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=lambda ok: pytest.fail("no reply is owed"),
        start=second, remove_pidfile=lambda: None)

    assert hook() == app_mod.NS_TERMINATE_NOW
    assert second.calls == []


def test_appkit_reply_is_not_left_pending_if_ready_is_never_set(quit_state,
                                                               monkeypatch):
    # Defence in depth: an app AppKit is waiting on a reply for is unquittable, so
    # the waiter gives up on the event rather than waiting forever.
    monkeypatch.setattr(app_mod, "QUIT_APPKIT_REPLY_WAIT_S", 0.2)
    replies = []
    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=replies.append, start=lambda *a, **k: None,
        remove_pidfile=lambda: None)

    assert hook() == app_mod.NS_TERMINATE_LATER

    deadline = time.monotonic() + 3.0
    while not replies and time.monotonic() < deadline:
        time.sleep(0.01)
    assert replies == [True]


def test_appkit_reply_failure_does_not_raise_into_the_thread(quit_state,
                                                            monkeypatch):
    monkeypatch.setattr(app_mod, "QUIT_APPKIT_REPLY_WAIT_S", 0.1)

    def _boom(_ok):
        raise RuntimeError("callAfter unavailable")

    hook = app_mod.make_appkit_terminate_hook(
        quit_state, reply=_boom, start=lambda *a, **k: None,
        remove_pidfile=lambda: None)
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


def test_quit_application_is_only_ever_called_from_the_terminate_hop():
    """Structural, because the bypass is invisible in behavior: any code path that
    calls rumps.quit_application() directly gets AppKit's exit() with NO teardown
    — no drain, no duckdb close, no unmount, no rcd reap. The readiness-failure
    abort in _bootstrap_server did exactly that (the server has been up for as
    long as 15s by then, so run_automount has had ample time to attach mounts)."""
    import ast

    def is_quit_call(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "quit_application")

    assert set(_enclosing_functions(_app_source_tree(), is_quit_call)) == {
        "_terminate"}


def test_the_readiness_failure_abort_goes_through_the_quit_action():
    import ast

    def is_do_quit_call(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_do_quit")

    assert "_bootstrap_server" in _enclosing_functions(
        _app_source_tree(), is_do_quit_call)


def test_the_unmount_budget_app_py_is_promised_covers_the_quiesce_too(ladder,
                                                                     monkeypatch):
    """`_QUIT_UNMOUNT_BUDGET_S` is what app.py's deadline is built from, so it has
    to bound the WHOLE step. The hoisted quiesce is sequential over
    DAEMON_STATE_FILES with a per-daemon timeout, and two wedged tile daemons —
    the state that motivates quiescing at all — spent that outside the join
    budget."""
    assert mounts_mod._QUIT_UNMOUNT_BUDGET_S == pytest.approx(
        mounts_mod._QUIT_QUIESCE_BUDGET_S + mounts_mod._QUIT_UNMOUNT_JOIN_BUDGET_S)

    # And the join budget is still spent on unmounts alone — a slow quiesce must
    # not eat the time the mounts need, which is the correctness-critical half.
    slow = 0.4
    monkeypatch.setattr(mounts_mod.lifecycle, "_quit_tile_daemons",
                        lambda: time.sleep(slow))
    ladder["lingers"].add("alpha")
    ladder["hang"].add("alpha")

    t0 = time.monotonic()
    mounts_mod.unmount_all_for_quit(budget_s=0.3)
    elapsed = time.monotonic() - t0

    assert elapsed >= slow          # the quiesce ran...
    assert elapsed < slow + 2.0     # ...and the join still got its own budget


def test_a_hanging_quiesce_is_bounded_by_its_own_budget(ladder, monkeypatch):
    # Bounded by construction rather than by counting per-daemon timeouts: a tile
    # daemon that never answers /quit is exactly the wedge this step exists for,
    # and DAEMON_STATE_FILES growing must not silently grow the quit deadline.
    monkeypatch.setattr(mounts_mod.lifecycle, "_QUIT_QUIESCE_BUDGET_S", 0.2)
    stuck = threading.Event()
    monkeypatch.setattr(mounts_mod.lifecycle, "_quit_tile_daemons",
                        lambda: stuck.wait(30))
    try:
        t0 = time.monotonic()
        mounts_mod.unmount_all_for_quit()
        elapsed = time.monotonic() - t0
    finally:
        stuck.set()

    assert elapsed < 3.0
    assert ladder["kernel"] == set()  # and the mounts still came down


# ------------------------------------------ the automount / quit interlock (A)
# `health.startup()` runs run_automount on a daemon thread, calling attach_mount
# per mount, seconds each against S3. Quitting while that is in flight — very much
# including the readiness-failure abort, whose whole premise is that automount has
# had time to run — let an attach COMPLETE after that mount's unmount thread had
# finished, so stop_local_rcd killed rcd with a live kernel nfsmount attached:
# defect (A) again, on the path most likely to hit it.


def test_an_attach_after_quit_began_declines_instead_of_mounting(ladder):
    mounts_mod.unmount_all_for_quit()

    before = list(ladder["calls"])
    err = mounts_mod.attach_mount({"id": "n", "name": "newbie", "remote": "s3n:b"})

    assert err and "quit" in err.lower()
    # Declined, not half-done: no mount/mount, nothing added to the kernel table.
    assert ladder["calls"] == before
    assert "newbie" not in ladder["kernel"]


def test_a_mount_that_lands_mid_teardown_is_still_detached(ladder):
    # The narrow window the latch cannot close: an attach already PAST the check
    # and inside rcd's mount/mount. It must not be left attached for the reap, so
    # the fan-out is followed by a sweep of what the kernel actually still holds.
    late = {"id": "l", "name": "late", "remote": "s3l:b"}
    ladder["mounts"].append(late)

    original = ladder["kernel"]

    def _attach_mid_flight(mp):
        # Runs while alpha is being force-unmounted: the racing attach completes.
        original.add("late")

    ladder["on_force"] = _attach_mid_flight
    ladder["lingers"].add("alpha")

    mounts_mod.unmount_all_for_quit()

    assert ladder["kernel"] == set(), "a mount that landed mid-teardown survived"


def test_the_interlock_is_not_armed_when_the_daemon_is_left_running(ladder,
                                                                   monkeypatch):
    # Persisted dev daemon: its mounts are meant to stay up, so an attach racing
    # the quit is harmless and must not be refused.
    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "1")

    mounts_mod.unmount_all_for_quit()

    assert not mounts_mod._QUIT_TEARDOWN_LATCH.is_set()


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


# --------------------------------- the stash cannot be installed after the latch
# The latch check and the setattr that installs the stash are far apart in wall
# time: duckdb.connect(":memory:") + LOAD httpfs + four PRAGMAs sit between them.
# A read that passed the check could therefore install its connection AFTER quit
# had latched and cleared — and because the latch is one-way, nothing would ever
# close that one, so the GIL-less exit() abort comes straight back.


def _handshake_connect(reader_mod, monkeypatch, built, release):
    """A duckdb.connect that parks the builder INSIDE the window: it announces it
    has entered (`built`) and waits for the test to let it out (`release`).

    An Event handshake rather than "start two threads and hope": the same lesson as
    the begin_quit race tests — a timing-dependent version of this test passes
    against the broken code, because the window it needs is microseconds wide unless
    something holds it open."""
    con = _FakeCon()
    con.execute = lambda sql: None
    con.cursor = lambda: "cursor"

    def _connect(*a, **k):
        built.set()
        assert release.wait(5), "test never released the builder"
        return con

    monkeypatch.setattr(reader_mod.duckdb, "connect", _connect, raising=False)
    return con


def test_a_connection_built_before_the_latch_is_never_installed_after_it(
        reader, monkeypatch):
    built, release = threading.Event(), threading.Event()
    con = _handshake_connect(reader, monkeypatch, built, release)
    outcome = {}

    def _builder():
        try:
            outcome["cursor"] = reader._http_connection()
        except Exception as e:  # noqa: BLE001 — the outcome IS what we assert
            outcome["error"] = e

    t = threading.Thread(target=_builder, daemon=True)
    t.start()
    assert built.wait(5), "the builder never reached the build step"

    # Quit lands squarely in the window: the builder is past the latch check with
    # a connection in hand and has not stashed it yet.
    reader.close_http_connection()
    release.set()
    t.join(5)

    assert "cursor" not in outcome, "the read must not proceed on a quitting process"
    assert isinstance(outcome.get("error"), RuntimeError)
    # The invariant, stated as an assertion: after close_http_connection returns,
    # NOTHING can put a connection back on the duckdb module.
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)
    # And the loser closed what it built. Dropping it for GC is the same crash by a
    # slower route — CPython may run that destructor at any later point, including
    # inside exit() with no GIL.
    assert con.closed == 1


def test_the_losing_builder_of_two_closes_its_own_connection(reader, monkeypatch):
    """Same invariant on the non-quit path, which the old comment waved away as
    "last-stashed wins and the loser is GC'd — harmless": an unclosed
    DuckDBPyConnection left for the collector is exactly the object that aborts in
    exit()'s static destructors."""
    built, release = threading.Event(), threading.Event()
    mine = _handshake_connect(reader, monkeypatch, built, release)
    outcome = {}

    def _builder():
        outcome["cursor"] = reader._http_connection()

    t = threading.Thread(target=_builder, daemon=True)
    t.start()
    assert built.wait(5)

    # Another call finished first and installed ITS connection while we were in
    # duckdb.connect.
    winner = _FakeCon()
    winner.cursor = lambda: "winner-cursor"
    setattr(reader.duckdb, reader._HTTP_CON_KEY, winner)
    release.set()
    t.join(5)

    assert getattr(reader.duckdb, reader._HTTP_CON_KEY) is winner  # one stash
    assert outcome["cursor"] == "winner-cursor"  # and the caller uses it
    assert mine.closed == 1                     # the loser is closed, not leaked
    assert winner.closed == 0
