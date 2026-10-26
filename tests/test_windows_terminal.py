"""The Windows terminal (ConPTY via pywinpty) and its PowerShell integration.

Runs everywhere with a fake `winpty` module injected into `sys.modules`; the
one real-ConPTY test only runs on a Windows lane."""
import os
import queue
import sys
import time
import types

import pytest

from fused_render import _platform, pty_session, shell_integration, terminal_profiles
from fused_render.terminal_profiles import TerminalProfile


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakePty:
    """The slice of `winpty.PtyProcess` the session uses."""

    instances: list = []

    def __init__(self, argv, cwd, env, dimensions):
        self.argv, self.cwd, self.env, self.dimensions = argv, cwd, env, dimensions
        self.pid = 4242
        self.written: list = []
        self.sizes: list = []
        self.exitstatus = None
        self.terminated = False
        self._q: queue.Queue = queue.Queue()
        self._alive = True
        FakePty.instances.append(self)

    @classmethod
    def spawn(cls, argv, cwd=None, env=None, dimensions=(24, 80)):
        return cls(argv, cwd, env, dimensions)

    # test driver
    def feed(self, text):
        self._q.put(text)

    def finish(self, code=0):
        self.exitstatus = code
        self._alive = False
        self._q.put(None)

    # PtyProcess surface
    def read(self, size=1024):
        item = self._q.get()
        if item is None:
            raise EOFError("Pty is closed")
        return item

    def write(self, s):
        self.written.append(s)
        return len(s)

    def setwinsize(self, rows, cols):
        self.sizes.append((rows, cols))

    def isalive(self):
        return self._alive

    def wait(self):
        _wait_until(lambda: not self._alive)
        return self.exitstatus

    def close(self, force=False):
        pass

    def terminate(self, force=False):
        self.terminated = True
        self.finish(1)
        return True


@pytest.fixture
def fake_winpty(monkeypatch):
    FakePty.instances = []
    mod = types.ModuleType("winpty")
    mod.PtyProcess = FakePty
    monkeypatch.setitem(sys.modules, "winpty", mod)
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    return mod


@pytest.fixture
def registry(fake_winpty):
    reg = pty_session.PtySessionRegistry()
    yield reg
    for pty in FakePty.instances:
        if pty.isalive():
            pty.finish(0)
    reg.shutdown_all()


def _profile(tmp_path):
    return TerminalProfile(shell="C:\\pw\\pwsh.exe",
                           argv=["C:\\pw\\pwsh.exe", "-NoLogo"],
                           env={"TERM": "xterm-256color"}, cwd=str(tmp_path))


def _osc(*parts):
    return "".join(f"\x1b]{p}\x07" for p in parts)


# -- session wiring --------------------------------------------------------------


def test_registry_picks_the_conpty_session_on_windows(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path), rows=30, cols=100)
    assert isinstance(session, pty_session.WinPtySession)
    pty = FakePty.instances[0]
    assert pty.argv == ["C:\\pw\\pwsh.exe", "-NoLogo"]
    assert pty.cwd == str(tmp_path)
    assert pty.dimensions == (30, 100)
    assert pty.env == {"TERM": "xterm-256color"}


def test_default_size_when_the_client_sent_none(registry, tmp_path):
    registry.spawn(_profile(tmp_path))
    assert FakePty.instances[0].dimensions == (24, 80)


def test_missing_pywinpty_is_a_clear_runtime_error(monkeypatch, tmp_path):
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "winpty", None)
    with pytest.raises(RuntimeError, match=r"needs pywinpty.*fused-render\[cli\]"):
        pty_session.PtySessionRegistry().spawn(_profile(tmp_path))


def test_the_route_maps_it_to_501(monkeypatch):
    from fused_render.server.routers import terminal

    def refuse(**kw):
        raise RuntimeError("the terminal needs pywinpty on Windows")

    monkeypatch.setattr(pty_session.REGISTRY, "create", refuse)
    resp = terminal.api_terminal_create({}, x_fused="1")
    assert resp.status_code == 501
    assert b"pywinpty" in resp.body


def test_read_encodes_to_utf8_and_feeds_the_ring(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path))
    FakePty.instances[0].feed("h\u00e9llo \u2713")
    assert _wait_until(lambda: "h\u00e9llo \u2713".encode() in session.scrollback())


def test_an_empty_read_is_not_eof(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path))
    pty = FakePty.instances[0]
    pty.feed("")
    pty.feed("after")
    assert _wait_until(lambda: b"after" in session.scrollback())
    assert session.alive


def test_write_and_resize_reach_the_pty(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path))
    pty = FakePty.instances[0]
    session.write(b"echo hi\r")
    session.resize(40, 120)
    assert pty.written == ["echo hi\r"]
    assert pty.sizes == [(40, 120)]
    assert (session.rows, session.cols) == (40, 120)


def test_exit_sets_the_code_and_notifies_subscribers(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path))
    _snap, _alive, _code, q = session.attach()
    FakePty.instances[0].finish(3)
    assert _wait_until(lambda: not session.alive)
    assert session.exit_code == 3
    items = []
    while not q.empty():
        items.append(q.get())
    assert ("exit", 3) in items


def test_kill_runs_the_platform_tree_kill(registry, tmp_path, monkeypatch):
    killed = []
    session = registry.spawn(_profile(tmp_path))
    pty = FakePty.instances[0]
    monkeypatch.setattr(_platform, "kill_tree",
                        lambda pid, sig=None: killed.append(pid) or pty.finish(1))
    session.kill()
    assert killed == [4242]
    assert _wait_until(lambda: not session.alive)
    assert not pty.terminated


def test_kill_falls_back_to_terminate(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pty_session, "_KILL_GRACE_S", 0.1)
    monkeypatch.setattr(_platform, "kill_tree", lambda pid, sig=None: False)
    session = registry.spawn(_profile(tmp_path))
    session.kill()
    assert FakePty.instances[0].terminated
    assert _wait_until(lambda: not session.alive)


def test_foreground_follows_the_shell_integration(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path))
    pty = FakePty.instances[0]
    assert session.shell_is_foreground() is True
    assert session.foreground_name() is None
    pty.feed(_osc("633;E;npm run dev --port 3000", "133;C"))
    assert _wait_until(lambda: session.shell_state.running)
    assert session.shell_is_foreground() is False
    assert session.foreground_name() == "npm"
    pty.feed(_osc("133;D;0"))
    assert _wait_until(lambda: not session.shell_state.running)
    assert session.shell_is_foreground() is True
    assert session.foreground_name() is None


def test_foreground_name_is_unknown_without_a_command_text(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path))
    FakePty.instances[0].feed(_osc("133;C"))
    assert _wait_until(lambda: session.shell_state.running)
    assert session.foreground_name() == "unknown"


def test_a_dead_session_is_never_foreground(registry, tmp_path):
    session = registry.spawn(_profile(tmp_path))
    FakePty.instances[0].finish(0)
    assert _wait_until(lambda: not session.alive)
    assert session.shell_is_foreground() is False


# -- capability ------------------------------------------------------------------


def test_terminal_available_rules(monkeypatch):
    import importlib.util
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert pty_session.terminal_available() is True
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert pty_session.terminal_available() is False
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert pty_session.terminal_available() is True


def test_config_reports_terminal_available(monkeypatch):
    from fused_render.server.routers.config import api_config

    monkeypatch.setattr(pty_session, "terminal_available", lambda: False)
    assert api_config(start_dir="/tmp", token=None)["terminal_available"] is False
    monkeypatch.setattr(pty_session, "terminal_available", lambda: True)
    assert api_config(start_dir="/tmp", token=None)["terminal_available"] is True


# -- profile ---------------------------------------------------------------------


def test_windows_candidate_order(monkeypatch):
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setenv("ComSpec", "C:\\Windows\\System32\\cmd.exe")
    seen = []
    monkeypatch.setattr(terminal_profiles.shutil, "which",
                        lambda n: seen.append(n) or None)
    assert terminal_profiles._default_shells() == (None, None, "C:\\Windows\\System32\\cmd.exe")
    assert seen == ["pwsh", "powershell"]


def test_powershell_gets_nologo_and_no_login_flag(monkeypatch):
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile(
        shells=("C:\\Program Files\\PowerShell\\7\\pwsh.exe",))
    assert profile.argv == ["C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-NoLogo"]
    assert profile.env["TERM"] == "xterm-256color"


def test_cmd_gets_no_flags(monkeypatch):
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(terminal_profiles, "executable", lambda p: True)
    profile = terminal_profiles.resolve_profile(shells=("C:\\Windows\\System32\\cmd.exe",))
    assert profile.argv == ["C:\\Windows\\System32\\cmd.exe"]


def test_posix_candidates_are_unchanged(monkeypatch):
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert terminal_profiles._default_shells() == ("/bin/zsh", "/bin/bash", "/bin/sh")


# -- PowerShell shell integration ------------------------------------------------


@pytest.mark.parametrize("exe", ["pwsh", "pwsh.exe", "powershell.exe", "PowerShell.EXE"])
def test_integrate_powershell(exe, monkeypatch):
    shell = "C:\\Windows\\System32\\" + exe
    profile = TerminalProfile(shell=shell, argv=[shell, "-NoLogo"],
                              env={"A": "1"}, cwd="C:\\")
    out = shell_integration.integrate(profile)
    assert out.argv[:4] == [shell, "-NoLogo", "-NoExit", "-Command"]
    assert "-NoProfile" not in out.argv
    shim = os.path.join(out.env["FUSED_SHIM_DIR"], "fused.ps1")
    assert shim.replace("'", "''") in out.argv[4]
    assert os.path.isfile(shim)
    assert out.env["A"] == "1"
    # A second pass is a no-op.
    assert shell_integration.integrate(out) is out


def test_the_shim_emits_every_sequence():
    text = shell_integration._POWERSHELL
    assert text.isascii()
    for needle in ("]133;D;", "]133;A", "]133;C", "]633;E;", "]7;file://",
                   "PSConsoleHostReadLine", "$function:prompt", "FileSystem",
                   "'\\x3b'", "'\\x1b'", "'\\x07'", "'\\x0a'", "'\\\\'"):
        assert needle in text, needle


def test_the_shim_is_written_into_the_root():
    root = shell_integration._shim_root()
    with open(os.path.join(root, "powershell", "fused.ps1"), encoding="utf-8") as fh:
        assert fh.read() == shell_integration._POWERSHELL


def test_a_windows_file_uri_decodes_to_a_native_path():
    decode = shell_integration._decode_cwd
    assert decode("file://HOST/C:/Users/me/My%20Docs") == "C:\\Users\\me\\My Docs"
    assert decode("file://HOST/c:/") == "c:\\"
    assert decode("file://host/home/me/a%20b") == "/home/me/a b"


def test_the_parser_tracks_a_powershell_command():
    parser = shell_integration.ShellEventParser()
    parser.feed(_osc("133;D;0", "7;file://PC/C:/Users/me", "133;A").encode())
    assert parser.cwd == "C:\\Users\\me"
    parser.feed(_osc("633;E;Get-ChildItem", "133;C").encode())
    assert parser.running and parser.last_command == "Get-ChildItem"
    parser.feed(_osc("133;D;1").encode())
    assert not parser.running and parser.last_exit == 1


# -- the real thing ----------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="ConPTY exists only on Windows")
def test_real_conpty_powershell_round_trip():
    reg = pty_session.PtySessionRegistry()
    try:
        session = reg.create(rows=24, cols=120)
        assert _wait_until(lambda: session.shell_state.cwd is not None, timeout=30), \
            session.text()
        session.write(b"echo fused-ok\r")
        assert _wait_until(lambda: b"fused-ok" in session.scrollback(), timeout=30)
        assert _wait_until(lambda: session.shell_state.last_exit == 0, timeout=30), \
            session.text()
    finally:
        reg.shutdown_all()
