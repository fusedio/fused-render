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


# ----------------------------------------------------------------- argv safety

def _start_capturing(agent, tmp_path, monkeypatch, bin_path, message="hello",
                     **kwargs):
    """Run _start with Popen stubbed; return (cmd, popen_kwargs, run_dir)."""
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "_claude_bin", lambda: bin_path)
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kwargs"] = kw
        # stdin is an open file object at this point; read it before it closes
        captured["stdin_text"] = kw["stdin"].read()
        return FakeProc()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    result = agent._start(str(target), message, kwargs.get("session_id", ""),
                          kwargs.get("model", ""), kwargs.get("effort", ""),
                          kwargs.get("permission_mode", ""))
    assert "run_id" in result, result
    return captured, os.path.join(str(tmp_path / "runs"), result["run_id"])


def test_the_message_travels_over_stdin_not_argv(tmp_path, monkeypatch):
    """A message is arbitrary user text. It must never reach the command line:
    behind a .cmd shim cmd.exe re-parses that line, and cmd-escaping arbitrary
    text is not reliably possible. `-p` with no positional prompt reads stdin."""
    agent = _load_agent()
    message = 'summarize & "quote" | this ^ %PATH%'
    captured, _ = _start_capturing(
        agent, tmp_path, monkeypatch, "/usr/local/bin/claude", message=message)
    assert captured["stdin_text"] == message
    assert message not in captured["cmd"]
    assert captured["cmd"][-1] != message
    # -p is present but carries no positional prompt after it
    assert "-p" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-p") + 1].startswith("--")


def test_the_system_prompt_rides_a_file_in_the_run_dir(tmp_path, monkeypatch):
    """The system prompt embeds the user's file path, so it is not argv-safe
    either. It goes to a file that is cleaned up with the run."""
    agent = _load_agent()
    captured, run_dir = _start_capturing(
        agent, tmp_path, monkeypatch, "/usr/local/bin/claude")
    cmd = captured["cmd"]
    sp_path = cmd[cmd.index("--append-system-prompt-file") + 1]
    assert "--append-system-prompt" not in cmd  # the inline flag is gone
    assert os.path.dirname(sp_path) == run_dir
    with open(sp_path, encoding="utf-8") as f:
        assert "sample.html" in f.read()


@pytest.mark.parametrize("field,value", [
    ("session_id", "abc$(whoami)"),
    ("model", "haiku & del *"),
    ("effort", 'high"'),
])
def test_start_rejects_tokens_outside_the_argv_charset(tmp_path, monkeypatch,
                                                       field, value):
    """These three DO end up in argv, so they are held to a closed charset —
    the security boundary that lets the rest of the line stay static."""
    agent = _load_agent()
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))

    def no_spawn(*args, **kwargs):
        raise AssertionError("nothing may be spawned for a rejected token")

    monkeypatch.setattr(agent.subprocess, "Popen", no_spawn)
    kwargs = {"session_id": "", "model": "", "effort": "", field: value}
    result = agent._start(str(target), "hi", kwargs["session_id"],
                          kwargs["model"], kwargs["effort"])
    assert field in result["error"]


def test_start_accepts_the_tokens_we_actually_send(tmp_path, monkeypatch):
    agent = _load_agent()
    captured, _ = _start_capturing(
        agent, tmp_path, monkeypatch, "/usr/local/bin/claude",
        session_id="3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        model="claude-haiku-4-5-20251001", effort="high")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5-20251001"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_an_exe_is_execed_as_a_plain_argv_list(monkeypatch):
    """Only a .cmd/.bat shim needs cmd.exe. A real .exe — and anything at all
    off win32 — is spawned directly, where argv needs no quoting rules."""
    agent = _load_agent()
    monkeypatch.setattr(agent.sys, "platform", "win32")
    assert agent._popen_cmd(r"C:\u\claude.exe", ["-p"]) == [
        r"C:\u\claude.exe", "-p"]
    monkeypatch.setattr(agent.sys, "platform", "darwin")
    # a .cmd off win32 is not a shim, it is just a file with a funny name
    assert agent._popen_cmd("/usr/local/bin/claude.cmd", ["-p"]) == [
        "/usr/local/bin/claude.cmd", "-p"]


def test_a_shim_is_a_single_cmd_string_with_one_outer_quote_pair(monkeypatch):
    """The spaces bug: handing Popen the list ["cmd.exe", "/c", shim, ...]
    lets list2cmdline quote each element that needs it. cmd.exe only preserves
    inner quoting when the rest of its line holds exactly two quotes — a shim
    path with spaces plus any quoted argument makes four, cmd strips the
    outermost pair instead and re-splits at the spaces.

    So we build the line: /s strips exactly the first and last quote and takes
    everything between verbatim, whatever the payload contains."""
    agent = _load_agent()
    monkeypatch.setattr(agent.sys, "platform", "win32")
    shim = r"C:\Users\John Doe\AppData\Roaming\npm\claude.cmd"
    sp = r"C:\Users\John Doe\runs\r1\system_prompt.txt"
    cmd = agent._popen_cmd(shim, ["-p", "--append-system-prompt-file", sp])

    assert isinstance(cmd, str), (
        "a shim invocation must be one command string — a list would be "
        "re-joined by list2cmdline and mis-parsed by cmd.exe")
    # /s: strip exactly the outer pair, rest verbatim. /d: no AutoRun injection.
    assert cmd.startswith('cmd.exe /d /s /c "')
    assert cmd.endswith('"')
    # both space-bearing paths survive intact, each in its own quoted run
    assert f'"{shim}"' in cmd
    assert f'"{sp}"' in cmd
    # the payload between the outer quotes is what cmd will execute verbatim
    payload = cmd[len('cmd.exe /d /s /c "'):-1]
    assert payload == f'"{shim}" "-p" "--append-system-prompt-file" "{sp}"'


def test_every_shim_argument_is_quoted_so_metacharacters_stay_literal(monkeypatch):
    """/s stops cmd re-parsing QUOTES, not metacharacters — & | > < ^ are
    still live outside a quoted run. Quoting every element, not only the ones
    with spaces, is what keeps them inert."""
    agent = _load_agent()
    monkeypatch.setattr(agent.sys, "platform", "win32")
    cmd = agent._popen_cmd(r"C:\npm\claude.cmd", ["-p", "--verbose"])
    payload = cmd[len('cmd.exe /d /s /c "'):-1]
    for token in (r"C:\npm\claude.cmd", "-p", "--verbose"):
        assert f'"{token}"' in payload
    assert payload.replace('"', " ").split() == [
        r"C:\npm\claude.cmd", "-p", "--verbose"]


def test_a_double_quote_in_an_argument_is_refused_not_smuggled(monkeypatch):
    """Nothing we send can contain a `"` (Windows paths cannot hold one, and
    the rest is static or charset-validated). If that ever changes, fail loudly
    rather than emit a line that means something else."""
    agent = _load_agent()
    monkeypatch.setattr(agent.sys, "platform", "win32")
    with pytest.raises(ValueError):
        agent._popen_cmd(r"C:\npm\claude.cmd", ['--model', 'a"b'])


def test_start_behind_a_shim_with_spaces_produces_a_runnable_line(
        tmp_path, monkeypatch):
    """End to end through _start: a shim path with spaces AND a run_dir with
    spaces (the reported C:\\Users\\John Doe shape) must still yield a single
    correctly-quoted command string."""
    _as_windows(monkeypatch)
    agent = _load_agent()
    monkeypatch.setattr(agent.sys, "platform", "win32")
    spacey = tmp_path / "John Doe"
    spacey.mkdir()
    shim = r"C:\Users\John Doe\npm\claude.cmd"
    target = spacey / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "_claude_bin", lambda: shim)
    monkeypatch.setattr(agent, "RUNS", str(spacey / "my runs"))
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    assert "run_id" in agent._start(str(target), "hello", "", "", "")
    cmd = captured["cmd"]
    assert isinstance(cmd, str)
    assert cmd.startswith('cmd.exe /d /s /c "') and cmd.endswith('"')
    assert f'"{shim}"' in cmd
    # the system-prompt file lives under a directory with a space in it
    sp = [t for t in cmd.split('"') if t.endswith("system_prompt.txt")]
    assert sp and " " in sp[0]
    assert f'"{sp[0]}"' in cmd


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
