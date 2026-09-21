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
    body = "".join(f"{i:06d}" for i in range(60_000))  # 360000 bytes, no '\n'
    code = f"import sys; sys.stdout.write({body!r}); sys.stdout.flush()"
    monkeypatch.setattr(
        pty_session, "resolve_profile",
        lambda cwd=None: _profile(tmp_path, [sys.executable, "-c", code]))
    session = registry.create()
    assert _wait_until(lambda: not session.alive, timeout=10.0)
    tail = bytes(session.scrollback())
    assert 0 < len(tail) <= pty_session.SCROLLBACK_CAP
    full = body.encode()
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
