"""Coverage for fused_render/pty_session.py: the pty session registry and its
fork-safe exec helper.

Sessions are built from a fake `resolve_profile` in most tests, so each one
runs a bare `/bin/sh` or the venv's own interpreter rather than the user's
real login shell — deterministic output, no dependence on the host's dotfiles.
"""
import os
import subprocess
import sys
import time

import pytest

from fused_render import pty_session
from fused_render.terminal_profiles import TerminalProfile


def _profile(tmp_path, argv, env=None):
    return TerminalProfile(
        shell=argv[0], argv=argv,
        env=env if env is not None else dict(os.environ),
        cwd=str(tmp_path),
    )


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def registry():
    reg = pty_session.PtySessionRegistry()
    yield reg
    reg.shutdown_all()


def test_output_reaches_the_ring(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "printf hi"]))
    session = registry.create()
    assert _wait_until(lambda: b"hi" in bytes(session.scrollback()))


def test_write_exit_ends_the_session(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    assert _wait_until(lambda: len(session.scrollback()) > 0)
    session.write(b"exit\n")
    assert _wait_until(lambda: not session.alive)
    assert session.exit_code is not None


def test_scrollback_is_capped_and_keeps_the_tail(registry, tmp_path, monkeypatch):
    # A distinguishable, non-repeating payload so a byte-for-byte tail
    # comparison actually proves something (uniform bytes could pass by luck).
    #
    # The child GENERATES the non-repeating bytes itself from a short loop
    # rather than receiving them as a literal in the `python -c` argument:
    # embedding a 360000-byte string there would put a single argv element
    # past Linux's MAX_ARG_STRLEN (128 KiB, independent of the total
    # ARG_MAX), raising `OSError: [Errno 7] Argument list too long` on every
    # Linux CI lane (macOS has no comparable per-argument cap). Generating
    # the bytes in-process keeps the argv element tiny; `count` is shared
    # with the `full` computation below so the two can't drift apart.
    count = 60_000  # 360000 bytes, no '\n'
    code = f"import sys; [sys.stdout.write('%06d' % i) for i in range({count})]; sys.stdout.flush()"
    monkeypatch.setattr(
        pty_session, "resolve_profile",
        lambda cwd=None: _profile(tmp_path, [sys.executable, "-c", code]))
    session = registry.create()
    assert _wait_until(lambda: not session.alive, timeout=10.0)
    tail = bytes(session.scrollback())
    assert 0 < len(tail) <= pty_session.SCROLLBACK_CAP
    full = "".join(f"{i:06d}" for i in range(count)).encode()
    assert tail == full[-len(tail):]


def test_resize_is_reflected_by_stty_size(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    assert _wait_until(lambda: len(session.scrollback()) > 0)
    session.resize(40, 120)
    session.write(b"stty size\n")
    assert _wait_until(lambda: b"40 120" in bytes(session.scrollback()))


def test_cap_refuses_a_ninth_session(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    for _ in range(pty_session.MAX_SESSIONS):
        registry.create()
    with pytest.raises(pty_session.SessionLimitError):
        registry.create()


def test_shutdown_all_reaps_every_live_session(registry, tmp_path, monkeypatch):
    """The shutdown hook (`fused_render/server/app.py`'s on_shutdown) calls
    `REGISTRY.shutdown_all()` so a server restart never leaves an orphaned
    shell running. Two long-lived sessions here (`/bin/sh`
    with no exit command — nothing to make them die on their own) prove the
    hook does the reaping itself rather than the test getting lucky with
    sessions that were about to exit anyway."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session_a = registry.create()
    session_b = registry.create()
    assert _wait_until(lambda: len(session_a.scrollback()) > 0)
    assert _wait_until(lambda: len(session_b.scrollback()) > 0)
    assert session_a.alive and session_b.alive

    pid_a, pid_b = session_a.proc.pid, session_b.proc.pid
    registry.shutdown_all()

    assert not session_a.alive
    assert not session_b.alive
    assert session_a.exit_code is not None
    assert session_b.exit_code is not None
    # No orphan: `shutdown_all` already `join()`ed each reader thread above,
    # which only returns once the child's exit has been reaped — so by now
    # the pid must be gone from the process table entirely, not merely
    # signalled.
    for pid in (pid_a, pid_b):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_shell_does_not_inherit_pythonhome_or_pythonpath(registry, tmp_path, monkeypatch):
    """The profile's env keeps PYTHONHOME/PYTHONPATH: `terminal_profiles.py`
    does not scrub them, because the immediate Popen target there is
    `sys.executable`, which in a packaged build needs them. The scrub
    happens one process later, in `_pty_exec_helper.py` right before
    `execv`, so the actual SHELL never sees either var. This spawns a real
    shell through the real helper (no mocking of `resolve_profile`'s env)
    and asserts both are empty in the shell's own environment.

    PYTHONHOME is set to `sys.base_prefix` (the running interpreter's own,
    real prefix) rather than a made-up path: an invalid PYTHONHOME crashes
    the interpreter running the helper script before it ever reaches execv —
    this test is about what the SHELL inherits, one process later, so the
    helper's own interpreter needs to actually start."""
    env = dict(os.environ)
    env["PYTHONHOME"] = sys.base_prefix
    env["PYTHONPATH"] = "/some/bundled/site-packages"
    monkeypatch.setattr(
        pty_session, "resolve_profile",
        lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c",
                                              'echo "[$PYTHONHOME|$PYTHONPATH]"'],
                                   env=env))
    session = registry.create()
    assert _wait_until(lambda: b"[|]" in bytes(session.scrollback()))


def test_kill_on_an_already_dead_session_signals_nothing(registry, tmp_path, monkeypatch):
    """Once the reader thread has reaped the child (`alive` False), its pid
    is free for the OS to hand to an unrelated process. `kill()` must not
    call `os.getpgid`/`os.kill` at all in that case — this asserts `_signal`
    (the only thing that would ever touch a pid) is never invoked."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "printf hi"]))
    session = registry.create()
    assert _wait_until(lambda: not session.alive)

    called = []
    monkeypatch.setattr(pty_session.PtySession, "_signal",
                         staticmethod(lambda pid, sig: called.append((pid, sig))))
    session.kill()
    assert called == []


def test_reap_dead_runs_on_create_and_list(registry, tmp_path, monkeypatch):
    """Without a caller, a dead session would linger in the registry
    forever and keep appearing in `list()`. This asserts `create()` and
    `list()` each drop it, via `_reap_dead_locked`."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "printf hi"]))
    dead = registry.create()
    assert _wait_until(lambda: not dead.alive)
    # `list()` itself reaps on every call, so the dead session is already
    # gone by the time this reads it back.
    assert dead.id not in [s.id for s in registry.list()]

    # `live` needs to still be alive by the time the assertions below run,
    # so re-patch `resolve_profile` to a long-running command instead of
    # reusing the `printf hi` profile above, which could itself exit and
    # get reaped before `list()` runs.
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "sleep 30"]))
    live = registry.create()
    try:
        assert live.id in [s.id for s in registry.list()]
        assert dead.id not in [s.id for s in registry.list()]
    finally:
        live.kill()


def test_failed_popen_does_not_leak_the_master_fd(tmp_path, monkeypatch):
    """If Popen raises, `master_fd` (already assigned to `self.master_fd`)
    must still be closed on that path, or each failed create leaks a pty
    master."""
    profile = _profile(tmp_path, ["/bin/sh"])

    def boom(*args, **kwargs):
        raise OSError("simulated ENOENT")

    monkeypatch.setattr(pty_session.subprocess, "Popen", boom)
    closed = []
    real_close = os.close
    monkeypatch.setattr(pty_session.os, "close",
                         lambda fd: (closed.append(fd), real_close(fd)))

    with pytest.raises(OSError):
        pty_session.PtySession("scratch-sid", profile)

    # Two closes: the slave fd (existing `finally`) and the master fd (the
    # fix) — neither leaked.
    assert len(closed) == 2


def test_attach_snapshot_alive_and_subscribe_are_atomic(registry, tmp_path, monkeypatch):
    """`attach()` returns a scrollback snapshot, the alive/exit_code pair,
    and a subscriber queue from a single lock hold, so a client can never
    see a scrollback snapshot that is stale relative to `alive`."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "printf hi"]))
    session = registry.create()
    assert _wait_until(lambda: not session.alive)
    snapshot, alive, exit_code, q = session.attach()
    assert b"hi" in snapshot
    assert alive is False
    assert exit_code is not None
    assert q is not None


def test_shell_is_foreground_true_at_the_prompt(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    assert _wait_until(lambda: len(session.scrollback()) > 0)
    assert session.shell_is_foreground()


def test_shell_is_foreground_false_while_a_child_runs(registry, tmp_path, monkeypatch):
    """`POST /api/terminal/{sid}/input` (routers/terminal.py) refuses to type
    into a pane the shell doesn't currently own — this is the check behind
    that 409. `sleep 30` becomes the terminal's foreground process group the
    moment the shell execs it; Ctrl-C (SIGINT to the whole group) kills it
    and hands the foreground back to the shell itself."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    assert _wait_until(lambda: len(session.scrollback()) > 0)
    session.write(b"sleep 30\n")
    assert _wait_until(lambda: not session.shell_is_foreground())
    session.write(b"\x03")
    assert _wait_until(lambda: session.shell_is_foreground())


def test_shell_is_foreground_false_once_the_session_is_dead(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    session.write(b"exit\n")
    assert _wait_until(lambda: not session.alive)
    assert session.shell_is_foreground() is False


def test_wait_shell_foreground_polls_until_the_shell_lands(registry, tmp_path, monkeypatch):
    """A brand-new session's `tcgetpgrp` can briefly read back 0 while the
    child's own `setsid()` (`_pty_exec_helper.py`) is still landing —
    `wait_shell_foreground` polls instead of forcing every caller to poll."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    assert session.wait_shell_foreground(timeout=1.5) is True


def test_wait_shell_foreground_times_out_while_a_child_holds_it(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    assert _wait_until(lambda: len(session.scrollback()) > 0)
    session.write(b"sleep 30\n")
    assert _wait_until(lambda: not session.shell_is_foreground())
    assert session.wait_shell_foreground(timeout=0.2) is False


def test_wait_shell_foreground_false_once_the_session_is_dead(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    session.write(b"exit\n")
    assert _wait_until(lambda: not session.alive)
    assert session.wait_shell_foreground(timeout=0.2) is False


def test_popen_kwargs_are_fork_safe(registry, tmp_path, monkeypatch):
    """The regression guard for the SIGSEGV: a Popen with cwd=,
    start_new_session=True, or close_fds=True (the default) takes the fork
    path in a process with libproj resident and dies -11. This asserts the
    exact kwargs so a future edit that reintroduces one of them fails here
    rather than crashing the whole server at runtime."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "printf hi"]))
    captured = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(pty_session.subprocess, "Popen", spy)
    session = registry.create()
    assert _wait_until(lambda: not session.alive)

    kwargs = captured["kwargs"]
    assert kwargs.get("close_fds") is False
    assert "cwd" not in kwargs
    assert "start_new_session" not in kwargs
    assert "preexec_fn" not in kwargs

    interpreter = captured["args"][0][0]
    assert os.path.isabs(interpreter)


def test_initial_size_is_set_before_the_child_starts(registry, tmp_path, monkeypatch):
    # The child reads its window size on its very first instruction, so a
    # size applied after spawn (resize()) is too late for zsh's PROMPT_SP.
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "stty size; sleep 1"]))
    session = registry.create(rows=33, cols=117)
    assert _wait_until(lambda: b"33 117" in bytes(session.scrollback()))


def test_no_initial_size_keeps_the_zero_size(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "stty size; sleep 1"]))
    session = registry.create()
    assert _wait_until(lambda: b"0 0" in bytes(session.scrollback()))


@pytest.mark.parametrize("rows,cols", [(0, 80), (24, 0), (70000, 80), (24, -1)])
def test_invalid_initial_size_is_ignored(registry, tmp_path, monkeypatch, rows, cols):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "stty size; sleep 1"]))
    session = registry.create(rows=rows, cols=cols)
    assert _wait_until(lambda: b"0 0" in bytes(session.scrollback()))


def test_asyncio_subscriber_receives_data_then_exit(registry, tmp_path, monkeypatch):
    """`attach(loop=...)` hands back an asyncio.Queue the reader thread feeds
    through `call_soon_threadsafe`: ("data", ...) frames, then one ("exit", code)."""
    import asyncio

    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "sleep 0.3; printf hi"]))

    async def main():
        session = registry.create()
        _snap, _alive, _code, q = session.attach(asyncio.get_running_loop())
        assert isinstance(q, asyncio.Queue)
        seen, exit_code = b"", "unset"
        while True:
            kind, payload = await asyncio.wait_for(q.get(), timeout=5)
            if kind == "data":
                seen += payload
            else:
                exit_code = payload
                break
        return session, q, seen, exit_code

    session, q, seen, exit_code = asyncio.run(main())
    assert b"hi" in seen
    assert exit_code == 0
    session.unsubscribe(q)
    assert q not in session._subscribers and q not in session._loops


def test_closed_loop_subscriber_is_dropped_not_fatal(registry, tmp_path, monkeypatch):
    """A subscriber whose loop has closed is unsubscribed by the reader instead
    of killing the reader thread."""
    import asyncio

    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "sleep 0.3; printf hi"]))
    session = registry.create()
    loop = asyncio.new_event_loop()
    _snap, _alive, _code, q = session.attach(loop)
    loop.close()
    assert _wait_until(lambda: not session.alive)
    assert q not in session._subscribers
    assert b"hi" in session.scrollback()


# -- flow control (pause / resume) ---------------------------------------------

def _flood_session(registry, tmp_path, monkeypatch):
    """A session whose child writes forever, with one subscriber attached."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "yes"]))
    session = registry.create()
    _snap, _alive, _code, q = session.attach()
    assert _wait_until(lambda: len(session.scrollback()) > 1000)
    return session, q


def _grows(session, window=0.4):
    """True if the ring got new bytes over `window` seconds. The ring is full
    (256 KiB of `yes`), so compare the tail of activity via last_activity."""
    before = session.last_activity
    time.sleep(window)
    return session.last_activity > before


def test_pause_stops_reads_and_resume_restarts_them(registry, tmp_path, monkeypatch):
    session, q = _flood_session(registry, tmp_path, monkeypatch)
    assert _grows(session)
    session.pause(q)
    time.sleep(0.3)  # let a read already in flight land
    assert not _grows(session, 0.6)
    session.resume(q)
    assert _wait_until(lambda: _grows(session, 0.1), timeout=3.0)


def test_pause_auto_expires(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "_PAUSE_MAX_S", 0.5)
    monkeypatch.setattr(pty_session, "_FLOW_POLL_S", 0.1)
    session, q = _flood_session(registry, tmp_path, monkeypatch)
    session.pause(q)
    time.sleep(0.3)
    assert not _grows(session, 0.1)
    assert _wait_until(lambda: _grows(session, 0.1), timeout=3.0)
    assert q not in session._paused


def test_unsubscribe_clears_the_pause(registry, tmp_path, monkeypatch):
    session, q = _flood_session(registry, tmp_path, monkeypatch)
    session.pause(q)
    time.sleep(0.3)
    assert not _grows(session, 0.4)
    session.unsubscribe(q)
    assert session._paused == {}
    assert _wait_until(lambda: _grows(session, 0.1), timeout=3.0)


def test_pause_from_a_non_subscriber_is_ignored(registry, tmp_path, monkeypatch):
    session, q = _flood_session(registry, tmp_path, monkeypatch)
    session.unsubscribe(q)
    session.pause(q)
    assert session._paused == {}
    assert session._flowing.is_set()


def test_exit_while_paused_still_ends_the_session(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "sleep 0.4; printf hi"]))
    session = registry.create()
    _snap, _alive, _code, q = session.attach()
    session.pause(q)
    # Well inside the 5 s auto-expiry: only the exit check can free the reader.
    assert _wait_until(lambda: not session.alive, timeout=3.0)
    assert session.exit_code == 0
    assert b"hi" in session.scrollback()


def test_scrollback_ring_holds_exactly_the_cap_and_the_tail(registry, tmp_path, monkeypatch):
    """The ring is a deque of chunks; trimming must land on exactly the cap and
    keep the newest bytes, including when one chunk straddles the boundary."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    assert _wait_until(lambda: len(session.scrollback()) > 0)
    with session._lock:
        session._scrollback.clear()
        session._scrollback_len = 0
        sent = bytearray()
        chunk_sizes = [1000, 65536, 70001, 3, 65536, 100000, 65536]  # > 2 * cap in total
        for n, size in enumerate(chunk_sizes):
            chunk = bytes([n + 1]) * size
            sent += chunk
            session._append_scrollback(chunk)
            assert session._scrollback_len == min(len(sent), pty_session.SCROLLBACK_CAP)
    ring = session.scrollback()
    assert len(ring) == pty_session.SCROLLBACK_CAP
    assert ring == bytes(sent[-pty_session.SCROLLBACK_CAP:])
    assert session.attach()[0] == ring


def test_exited_child_drains_without_a_wait_per_read(monkeypatch):
    """A pause still held when the child has exited must not cost a
    _FLOW_POLL_S wait per remaining read: _wait_flowing returns at once, so
    EOF, the reap and the exit item follow without 0.5 s per buffered read.
    Driven on a bare session object: on macOS a real child cannot exit while
    its output sits undrained in the pty, so the state cannot be staged."""
    import threading
    monkeypatch.setattr(pty_session, "_FLOW_POLL_S", 0.5)
    session = object.__new__(pty_session.PtySession)
    session._lock = threading.Lock()
    session._flowing = threading.Event()
    session._paused = {object(): time.monotonic()}
    session.proc = type("P", (), {"returncode": 0, "poll": lambda self: 0})()
    t0 = time.monotonic()
    for _ in range(10):  # ten buffered reads' worth
        session._wait_flowing()
    assert time.monotonic() - t0 < 0.2


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="needs os.waitid")
def test_child_exited_does_not_reap(registry, tmp_path, monkeypatch):
    """_child_exited sees the exit but leaves the zombie in place: returncode
    stays None until the reader's own proc.wait(), so kill()'s `alive` check
    keeps the pid reserved."""
    import threading
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "exit 0"]))
    # Hold the reader off the pty so it cannot reap while we look — by not
    # starting it. A pause cannot: the reader is usually already blocked in
    # `os.read` (past `_wait_flowing`) by the time `create()` returns, so the
    # child's exit (EIO) reaps at once — and `_wait_flowing` itself never
    # holds back an exited child's reap, by design.
    held = []
    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", lambda self: held.append(self))
        session = registry.create()
    assert held == [session._reader]
    try:
        assert _wait_until(session._child_exited, timeout=3.0)
        assert session.proc.returncode is None  # not reaped
        assert session._child_exited()  # idempotent
    finally:
        session._reader.start()  # the fixture's shutdown joins it
    assert _wait_until(lambda: not session.alive, timeout=3.0)
    assert session.proc.returncode is not None


def test_put_to_an_unsubscribed_asyncio_queue_is_a_silent_noop(registry, tmp_path, monkeypatch):
    """The reader snapshots subscribers, then one unsubscribes before delivery:
    the asyncio queue has no loop entry, and must neither be fed from the
    reader thread nor trigger a never-awaited/thread-unsafe warning."""
    import asyncio
    import warnings
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    session = registry.create()
    q = asyncio.Queue()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        session._put(q, b"late")
    assert q.empty()


def test_kill_after_poll_reaped_the_child_never_signals(registry, tmp_path, monkeypatch):
    """poll() reaps and sets proc.returncode while `alive` can still be True
    (reader not yet at EOF). The pid may be recycled by then, so kill() and
    the foreground probes must not touch it. Portable: no os.waitid needed."""
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "exit 0"]))
    session = registry.create()
    # Park the reader so it cannot finish first, then reap via poll().
    session._flowing.clear()
    session._paused[object()] = time.monotonic() + 3600
    monkeypatch.setattr(session, "_child_exited", lambda: False)
    assert _wait_until(lambda: session.proc.poll() is not None, timeout=3.0)
    assert session.proc.returncode is not None

    calls = []
    monkeypatch.setattr(os, "killpg", lambda *a: calls.append(("killpg", a)))
    monkeypatch.setattr(os, "kill", lambda *a: calls.append(("kill", a)))
    monkeypatch.setattr(os, "getpgid", lambda *a: calls.append(("getpgid", a)) or 0)
    session.kill()
    assert session.shell_is_foreground() is False
    assert session.foreground_name() is None
    assert calls == []
    session._paused.clear()
    session._flowing.set()


# -- owner-tagged sessions, env overrides, last-command output ----------------

def _osc_script(body: str) -> list:
    return ["/bin/sh", "-c", body + "; sleep 30"]


def test_owner_and_env_override(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh", "-c", "printf %s \"$PAGER-$KEEP\""],
                                                   env={**os.environ, "PAGER": "less", "KEEP": "k"}))
    session = registry.create(owner="bot:abc", env={"PAGER": "cat"})
    assert session.owner == "bot:abc"
    assert session.snapshot()["owner"] == "bot:abc"
    assert _wait_until(lambda: b"cat-k" in bytes(session.scrollback()))
    assert registry.create().owner is None


def test_bot_sessions_have_their_own_cap(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, ["/bin/sh"]))
    for i in range(pty_session.MAX_BOT_SESSIONS):
        registry.create(owner=f"bot:{i}")
    with pytest.raises(pty_session.SessionLimitError):
        registry.create(owner="bot:extra")
    # Bot sessions do not eat the user's slots, and vice versa.
    for _ in range(pty_session.MAX_SESSIONS):
        registry.create()
    with pytest.raises(pty_session.SessionLimitError):
        registry.create()


def test_last_output_slices_between_c_and_d(registry, tmp_path, monkeypatch):
    body = ("printf 'prompt$ echo one\\r\\n'; printf '\\033]133;C\\007'; "
            "printf 'one\\r\\ntwo\\r\\n'; printf '\\033]133;D;3\\007'; printf 'next-prompt'")
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, _osc_script(body)))
    session = registry.create()
    assert _wait_until(lambda: session.shell_state.last_exit == 3 and b"next-prompt" in session.scrollback())
    assert session.last_output(200) == "one\ntwo"
    assert session.last_output(1) == "two"


def test_last_output_is_live_while_running(registry, tmp_path, monkeypatch):
    body = "printf '\\033]133;C\\007partial'"
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, _osc_script(body)))
    session = registry.create()
    assert _wait_until(lambda: b"partial" in session.scrollback())
    assert session.shell_state.running
    assert session.last_output() == "partial"


def test_last_output_empty_before_any_command(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, _osc_script("printf hello")))
    session = registry.create()
    assert _wait_until(lambda: b"hello" in session.scrollback())
    assert session.last_output() == ""


def test_last_output_marks_truncation_when_the_ring_dropped_the_start(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "SCROLLBACK_CAP", 2048)
    body = ("printf '\\033]133;C\\007'; head -c 20000 /dev/zero | tr '\\\\0' 'x'; "
            "printf '\\r\\ntail-marker\\r\\n\\033]133;D;0\\007'")
    monkeypatch.setattr(pty_session, "resolve_profile",
                         lambda cwd=None: _profile(tmp_path, _osc_script(body)))
    session = registry.create()
    assert _wait_until(lambda: session.shell_state.last_exit == 0)
    out = session.last_output(200)
    assert out.startswith("[output truncated]")
    assert "tail-marker" in out


def test_create_honours_the_shells_argument(registry, tmp_path, monkeypatch):
    from fused_render import terminal_profiles as tp
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(tp, "executable", lambda p: p == "/bin/bash")
    assert tp.resolve_profile(shells=("/bin/zsh", "/bin/bash")).shell == "/bin/bash"
    assert tp.resolve_profile(shells=("/bin/zsh",)) is None
    assert tp.resolve_profile().shell == "/bin/bash"
