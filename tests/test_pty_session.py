"""Coverage for fused_render/pty_session.py: the pty session registry and its
fork-safe exec helper.

Unix-only (Python's `pty`/`termios`/`fcntl` modules don't exist on Windows —
see the Decisions section of PLAN-status-bar-terminal.md for why Windows is
out of scope this round).

Sessions are built from a fake `resolve_profile` in most tests, so each one
runs a bare `/bin/sh` or the venv's own interpreter rather than the user's
real login shell — deterministic output, no dependence on the host's dotfiles.
"""
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="pty is unix-only")

if os.name != "nt":
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
