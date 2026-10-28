"""detached_argv: run shell probes with no controlling terminal.

An interactive shell opens /dev/tty and tcsetpgrp()s itself foreground; parallel
probes then leave the terminal owned by a dead process group (SIGTTOU for us,
EIO for the user's shell). setsid in the child removes the tty. Done by a
python wrapper, never start_new_session=/preexec_fn= (fork + libproj = SIGSEGV).
"""
import os
import subprocess
import sys

import pytest

from fused_render import claude_health, codex_health, copilot_health, gemini_health, login_path

posix_only = pytest.mark.skipif(os.name == "nt", reason="setsid is POSIX-only")


def _is_wrapped(argv, inner):
    return (argv[:3] == [sys.executable, "-B", "-I"] and argv[3] == "-c"
            and "os.setsid()" in argv[4] and argv[5:] == inner)


@posix_only
def test_wraps_with_setsid_on_posix():
    inner = ["/bin/zsh", "-lic", "command -v x"]
    assert _is_wrapped(claude_health.detached_argv(inner), inner)


def test_unchanged_on_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    inner = ["bash", "-c", "x"]
    assert claude_health.detached_argv(inner) == inner


@pytest.mark.parametrize("exe", ["", "python3", "bin/python"])
def test_unchanged_without_absolute_interpreter(monkeypatch, exe):
    monkeypatch.setattr(sys, "executable", exe)
    inner = ["/bin/sh", "-c", "x"]
    assert claude_health.detached_argv(inner) == inner


@posix_only
def test_child_really_is_in_its_own_session():
    argv = claude_health.detached_argv(
        [sys.executable, "-c", "import os; print(os.getsid(0))"])
    for _ in range(3):
        out = subprocess.run(argv, capture_output=True, text=True,
                             close_fds=False, timeout=30).stdout
        assert int(out.strip()) != os.getsid(0)


@posix_only
def test_wrapped_timeout_still_kills_the_shell_itself(tmp_path):
    """After execvp the shell IS the direct child, so run(timeout=) kills it."""
    sh = tmp_path / "slow"
    sh.write_text("#!/bin/sh\nprintf partial\nsleep 30\n")
    sh.chmod(0o755)
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        subprocess.run(claude_health.detached_argv([str(sh)]), capture_output=True,
                       text=True, close_fds=False, timeout=1)
    assert "partial" in (ei.value.stdout.decode() if isinstance(ei.value.stdout, bytes)
                         else ei.value.stdout or "")


@posix_only
def test_login_path_probe_parses_through_wrapper(tmp_path):
    sh = tmp_path / "fakesh"
    sh.write_text("#!/bin/sh\nprintf %s __FR_PATH_BEGIN__/opt/a:/opt/b__FR_PATH_END__\n")
    sh.chmod(0o755)
    assert login_path.probe(shell=str(sh)) == ["/opt/a", "/opt/b"]


@posix_only
@pytest.mark.parametrize("mod,name", [
    (claude_health, "claude"), (codex_health, "codex"),
    (gemini_health, "gemini"), (copilot_health, "copilot")])
def test_shell_probes_pass_wrapped_argv(monkeypatch, mod, name):
    monkeypatch.setenv("SHELL", "/bin/sh")
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)

        class R:
            stdout = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._shell_probe() is None
    assert seen
    for argv in seen:
        assert _is_wrapped(argv, argv[5:])
        assert argv[5] == "/bin/sh" and argv[-1] == f"command -v {name}"


@posix_only
def test_login_path_probe_passes_wrapped_argv(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)

        class R:
            stdout = ""
        return R()

    monkeypatch.setattr(login_path.subprocess, "run", fake_run)
    assert login_path.probe(shell="/bin/sh") is None
    assert seen and all(_is_wrapped(a, a[5:]) and a[5] == "/bin/sh" for a in seen)
