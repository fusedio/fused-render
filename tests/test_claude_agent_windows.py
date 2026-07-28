"""Windows coverage for the claude chat template's backend
(fused_render/templates/claude/agent.py): finding the CLI when it isn't on
PATH, plus the win32 route for detach / liveness / cancel and the id guards
that only a Windows path separator can slip past.

Each test simulates win32 from any host (`os.name` and the module's own
candidate tuple are patched), so the Linux matrix exercises the Windows
branches; the windows-desktop CI job runs this same file for real.
"""
import importlib.util
import os
import signal
import subprocess

import pytest

AGENT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "agent.py")


def _load_agent():
    spec = importlib.util.spec_from_file_location("claude_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _as_windows(monkeypatch):
    """Make the module take its win32 branches from any host. The creationflags
    constants are win32-only, so they must be faked in too (with their real
    values, so a Windows host asserts against the same numbers)."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)


# ------------------------------------------------------------ finding `claude`

def test_explicit_override_wins_over_path(tmp_path, monkeypatch):
    agent = _load_agent()
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", str(exe))
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/claude")
    assert agent._claude_bin() == str(exe)


def test_stale_override_does_not_shadow_a_real_install(tmp_path, monkeypatch):
    agent = _load_agent()
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", str(tmp_path / "gone.exe"))
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/claude")
    assert agent._claude_bin() == "/usr/bin/claude"


def test_windows_install_dir_used_when_claude_is_not_on_path(tmp_path, monkeypatch):
    """The reported failure: Claude Code is installed, but the PATH this
    process inherited (a GUI launch, or the supervisor's) doesn't have it."""
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    _as_windows(monkeypatch)
    agent = _load_agent()
    installed = tmp_path / "claude.exe"
    installed.write_text("")
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(agent, "_WINDOWS_CANDIDATES",
                        (str(tmp_path / "missing.exe"), str(installed)))
    monkeypatch.setattr(agent, "_POSIX_CANDIDATES", ())
    assert agent._claude_bin() == str(installed)


def test_windows_candidates_cover_the_documented_install_locations():
    agent = _load_agent()
    joined = "\n".join(agent._WINDOWS_CANDIDATES).lower()
    # the native installer's location — the one Anthropic documents as default
    assert r"%userprofile%\.local\bin\claude.exe" in joined
    assert "winget" in joined            # winget install Anthropic.ClaudeCode
    assert r"%appdata%\npm" in joined    # npm install -g @anthropic-ai/claude-code
    # every entry is rooted in an environment variable, never a bare relative
    # path that would resolve against the executor's cwd
    assert all(c.startswith("%") for c in agent._WINDOWS_CANDIDATES)
    # .exe ahead of any .cmd shim: a shim hands our argv back to cmd.exe, and
    # that argv carries arbitrary user text (-p) and the target path
    exts = [os.path.splitext(c)[1].lower() for c in agent._WINDOWS_CANDIDATES]
    assert exts.index(".exe") < exts.index(".cmd")


def test_missing_claude_error_names_the_override_and_the_locations(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_BIN", raising=False)
    agent = _load_agent()
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(agent, "_WINDOWS_CANDIDATES", (r"%USERPROFILE%\nope.exe",))
    monkeypatch.setattr(agent, "_POSIX_CANDIDATES", ("/nope/claude",))
    with pytest.raises(FileNotFoundError) as excinfo:
        agent._claude_bin()
    message = str(excinfo.value)
    assert "FUSED_RENDER_CLAUDE_BIN" in message
    assert (r"%USERPROFILE%\nope.exe" if os.name == "nt" else "/nope/claude") in message


# --------------------------------------------------------------------- detach

def test_detach_kwargs_are_win32_flags_on_windows(monkeypatch):
    _as_windows(monkeypatch)
    agent = _load_agent()
    assert agent._DETACH == {"creationflags": 0x8 | 0x200}


def test_detach_kwargs_are_setsid_on_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    agent = _load_agent()
    assert agent._DETACH == {"start_new_session": True}


def test_start_detaches_with_the_platform_kwargs(tmp_path, monkeypatch):
    """start_new_session is silently ignored on Windows, so the run must be
    detached with creationflags there instead."""
    _as_windows(monkeypatch)
    agent = _load_agent()
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "_claude_bin", lambda: r"C:\claude.exe")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    assert "run_id" in agent._start(str(target), "hello", "", "", "")
    assert captured["cmd"][0] == r"C:\claude.exe"
    assert "start_new_session" not in captured["kwargs"]
    assert captured["kwargs"]["creationflags"] == 0x8 | 0x200


# ------------------------------------------------------------------- liveness

def test_alive_never_signals_the_process(tmp_path, monkeypatch):
    """os.kill(pid, 0) is a POSIX no-op, but on Windows signal 0 is
    CTRL_C_EVENT — a real Ctrl+C send that either kills the run or raises,
    which _alive would read as "gone". Liveness goes through
    procutil.pid_alive instead, which only ever queries."""
    agent = _load_agent()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pid").write_text("4242\n")

    def boom(*args, **kwargs):
        raise AssertionError("os.kill must not be used as a liveness check")

    monkeypatch.setattr(agent.os, "kill", boom)
    seen = []
    monkeypatch.setattr(agent, "_pid_alive", lambda pid: seen.append(pid) or True)
    assert agent._alive(str(run_dir)) is True
    assert seen == ["4242"]


def test_alive_is_false_without_a_pid_file(tmp_path):
    agent = _load_agent()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert agent._alive(str(run_dir)) is False


# --------------------------------------------------------------------- cancel

def test_cancel_kills_the_tree_with_taskkill_on_windows(tmp_path, monkeypatch):
    _as_windows(monkeypatch)
    agent = _load_agent()
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "pid").write_text("4242")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    calls = []
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setattr(
        agent.os, "killpg",
        lambda *a: pytest.fail("os.killpg does not exist on Windows"),
        raising=False)
    assert agent._cancel("r1") == {"cancelled": "r1"}
    assert [cmd for cmd, _ in calls] == [["taskkill", "/PID", "4242", "/T", "/F"]]
    # taskkill is a console program and this worker has no console to lend it,
    # so without the flag a cancel flashes the window _DETACH just removed
    assert calls[0][1]["creationflags"] == 0x08000000


def test_cancel_signals_the_process_group_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    agent = _load_agent()
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "pid").write_text("4242")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    killed = []
    # raising=False: os.killpg is absent on a Windows host, and this test drives
    # the POSIX branch there too
    monkeypatch.setattr(agent.os, "killpg", lambda pid, sig: killed.append((pid, sig)),
                        raising=False)
    monkeypatch.setattr(
        agent.subprocess, "run",
        lambda *a, **kw: pytest.fail("taskkill is the Windows-only path"))
    assert agent._cancel("r1") == {"cancelled": "r1"}
    assert killed == [(4242, signal.SIGTERM)]


def test_cancel_of_an_unknown_run_kills_nothing(tmp_path, monkeypatch):
    agent = _load_agent()
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent.os, "killpg",
                        lambda *a: pytest.fail("nothing to kill"), raising=False)
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **kw: pytest.fail("nothing to kill"))
    assert agent._cancel("no-such-run") == {"cancelled": "no-such-run"}


# ------------------------------------------------------------------ id guards

@pytest.mark.parametrize("value", [
    "",                     # no id at all
    ".", "..", ".hidden",   # dot-prefixed, incl. traversal
    "a/b", "../../etc",     # POSIX separator
    r"a\b", r"..\..\Windows",  # backslash: a separator on Windows too
    "C:evil", r"C:\Windows",   # a drive prefix makes os.path.join drop our dir
])
def test_bad_id_rejects_path_steering(value):
    agent = _load_agent()
    assert agent._bad_id(value) is True


def test_bad_id_accepts_the_ids_we_actually_mint():
    agent = _load_agent()
    assert agent._bad_id("20260728-101112-a1b2c3") is False          # run id
    assert agent._bad_id("3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d") is False  # session


def test_poll_and_history_refuse_a_windows_escaping_id(tmp_path, monkeypatch):
    agent = _load_agent()
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    assert agent._poll(r"..\..\elsewhere")["error"] == "unknown run_id"
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    assert agent._history(str(target), r"..\..\elsewhere") == {"turns": []}


# ------------------------------------------------------------- transcript dir

def test_projects_follows_claude_config_dir(tmp_path, monkeypatch):
    """The supervisor points CLAUDE_CONFIG_DIR at the app's own state dir in
    every packaged build, so that — not ~/.claude — is where the transcripts
    for our runs live."""
    configured = tmp_path / "state" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    agent = _load_agent()
    assert agent.PROJECTS == os.path.join(str(configured), "projects")


def test_projects_defaults_to_the_home_claude_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    agent = _load_agent()
    assert agent.PROJECTS == os.path.join(os.path.expanduser("~/.claude"), "projects")
