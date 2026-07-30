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


@pytest.fixture()
def reader(monkeypatch):
    """reader.py with a guaranteed-clean stash slot on the real duckdb module.
    monkeypatch.delattr/setattr restores whatever was there, so a stash a
    previous test in this worker created can't leak in (or out)."""
    pytest.importorskip("duckdb")
    mod = _load_reader()
    monkeypatch.delattr(mod.duckdb, mod._HTTP_CON_KEY, raising=False)
    return mod


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
        "entry": {"port": 5572, "pid": _RCD_PID, "spawner_pid": os.getpid()},
        "signalled": [],             # pids os.kill was called with
        "kernel": {"alpha", "beta"}, # what the kernel still holds
        "release": threading.Event(),
    }

    monkeypatch.setattr(mounts_mod, "list_mounts", lambda: list(ctx["mounts"]))
    monkeypatch.setattr(mounts_mod.storage, "read_json", lambda path: ctx["entry"])

    def _rc(port, method, params=None, **kw):
        name = os.path.basename((params or {}).get("mountPoint", ""))
        ctx["calls"].append(("rc", method, name))
        if name in ctx["rc_fail"]:
            raise RuntimeError("failed to unmount: device or resource busy")
        return {}

    monkeypatch.setattr(mounts_mod, "_rc", _rc)

    # A live daemon until it is signalled — which is also what makes
    # _kill_current_rcd's "is it gone yet" poll terminate promptly here.
    monkeypatch.setattr(mounts_mod, "_live_rcd_port",
                        lambda *a, **k: None if ctx["signalled"] else 5572)
    monkeypatch.setattr(mounts_mod, "_is_mounted",
                        lambda mp: os.path.basename(mp) in ctx["kernel"])
    monkeypatch.setattr(mounts_mod, "_ismount",
                        lambda mp: os.path.basename(mp) in ctx["kernel"])

    def _force_unmount(mp):
        name = os.path.basename(mp)
        ctx["calls"].append(("force", name))
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


def test_unmount_all_asks_rcd_then_forces_what_the_kernel_still_holds(ladder):
    mounts_mod.unmount_all_for_quit()

    # Both rungs, in order, for every mount: rcd's own mount/unmount first, then
    # the kernel force-unmount a busy macOS nfsmount needs (a plain umount is
    # what rclone tries on its way out, and what a busy mount rejects).
    # Sorted: mounts are unmounted in PARALLEL (one wedge must not delay the
    # rest), so only the per-mount rung order below is deterministic.
    assert sorted(_names(ladder["calls"], "rc")) == ["alpha", "beta"]
    assert sorted(_names(ladder["calls"], "force")) == ["alpha", "beta"]
    for name in ("alpha", "beta"):
        assert ladder["calls"].index(("rc", "mount/unmount", name)) < \
            ladder["calls"].index(("force", name))
    assert ladder["kernel"] == set()


def test_unmount_all_skips_the_force_rung_for_an_already_gone_mount(ladder):
    ladder["kernel"] = set()  # rcd's own unmount took, or it was never mounted

    mounts_mod.unmount_all_for_quit()

    assert _names(ladder["calls"], "force") == []


def test_unmount_all_still_forces_when_no_daemon_answers(ladder):
    # The "disconnected" state: rcd already died, the kernel mount survives it.
    ladder["signalled"].append(_RCD_PID)  # makes _live_rcd_port() answer None

    mounts_mod.unmount_all_for_quit()

    assert _names(ladder["calls"], "rc") == []
    assert sorted(_names(ladder["calls"], "force")) == ["alpha", "beta"]


def test_a_wedged_mount_does_not_stop_the_others(ladder):
    ladder["mounts"].insert(0, {"id": "w", "name": "wedged", "remote": "s3w:b"})
    ladder["kernel"].add("wedged")
    ladder["rc_fail"].add("wedged")
    ladder["force_fail"].add("wedged")

    mounts_mod.unmount_all_for_quit()

    assert sorted(_names(ladder["calls"], "force")) == ["alpha", "beta", "wedged"]
    assert ladder["kernel"] == {"wedged"}  # only the unfixable one is left


def test_a_hanging_unmount_is_bounded_and_does_not_hold_the_others(ladder):
    ladder["mounts"].insert(0, {"id": "h", "name": "hangs", "remote": "s3h:b"})
    ladder["kernel"].add("hangs")
    ladder["hang"].add("hangs")

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
