"""Coverage for fused_render/terminal_profiles.py: resolving the user's shell
into a spawn profile for the status-bar terminal.

Follows tests/test_claude_agent_windows.py's style of monkeypatching the
platform facts (`os.environ["SHELL"]`, `sys.platform`, `os.name`) rather than
relying on the host's actual shell, so the whole matrix runs on any CI box.
"""
import os
import sys

import pytest

from fused_render import terminal_profiles


def test_default_shell_when_unset(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile()
    assert profile is not None
    assert profile.shell == "/bin/bash"


def test_darwin_argv_carries_login_flag(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile()
    assert profile is not None
    assert profile.argv == ["/bin/zsh", "-l"]


def test_linux_argv_has_no_login_flag(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile()
    assert profile is not None
    assert profile.argv == ["/bin/zsh"]


def test_pythonhome_and_pythonpath_are_kept(monkeypatch):
    # Regression guard for finding 1 (code review, PR #1290): the immediate
    # Popen target for this profile is `sys.executable` running
    # `_pty_exec_helper.py`, not the shell itself. In a packaged build that
    # IS the bundled interpreter, which needs PYTHONHOME to find its own
    # runtime (see engine.py's documented failure mode) — scrubbing it here,
    # one process too early, would kill the helper before it ever reaches
    # execv. The scrub belongs in the helper, right before execv; see
    # test_pty_session.py's coverage of `_pty_exec_helper.main`.
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("PYTHONHOME", "/some/bundled/interpreter")
    monkeypatch.setenv("PYTHONPATH", "/some/bundled/site-packages")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile()
    assert profile is not None
    assert profile.env["PYTHONHOME"] == "/some/bundled/interpreter"
    assert profile.env["PYTHONPATH"] == "/some/bundled/site-packages"


def test_term_and_colorterm_are_set(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile()
    assert profile is not None
    assert profile.env["TERM"] == "xterm-256color"
    assert profile.env["COLORTERM"] == "truecolor"


def test_none_on_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert terminal_profiles.resolve_profile() is None


@pytest.mark.parametrize("cwd_in, expect_fallback", [
    (None, True),
    ("/nonexistent/definitely/not/a/dir", True),
])
def test_cwd_falls_back_to_home_when_unusable(monkeypatch, cwd_in, expect_fallback):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile(cwd=cwd_in)
    assert profile is not None
    if expect_fallback:
        assert profile.cwd == os.path.expanduser("~")


def test_cwd_used_when_it_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile(cwd=str(tmp_path))
    assert profile is not None
    assert profile.cwd == str(tmp_path)
