"""The "in terminal" command hands over the bundled `fused`, not the user's.

`_terminal_command` builds the shell command the page's menu item puts on the
clipboard so the user can continue a session in a real terminal. The sessions
fused-render spawns inherit the `fused` wrapper dir on PATH from the server
process; a terminal the user opens themselves does not.

That is not a version preference. fused-render bakes its own pre-release fused
into the app's interpreter and a shipping user never installs one, so `fused` in
the continued session is `command not found` — an outright breakage. Prepending
the wrapper dir fixes it, and prepending (not appending) also means the app's
CLI wins over any fused a developer happens to have, since only that one carries
the manifest shims the canvas sync depends on.
"""
import importlib.util
import os

import pytest


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_terminal", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return str(d)


def test_the_wrapper_dir_is_prepended_to_path(agent, target, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", "/home/u/.fused-render/fused-bin")
    monkeypatch.setattr(agent, "_plugin_argv", lambda target=None: [])
    monkeypatch.setattr(agent.os, "name", "posix")
    out = agent._terminal_command(target)
    command = out["command"]
    assert "PATH=/home/u/.fused-render/fused-bin:$PATH" in command
    # Prepended, so the bundled CLI beats anything already on PATH.
    assert command.index("fused-bin") < command.index("$PATH")
    # Still the same shape a user would type, and still cd's to the workdir.
    assert command.startswith("cd ")
    assert " claude" in command


def test_no_wrapper_means_the_command_is_unchanged(agent, target, monkeypatch):
    """A machine with no fused CLI must not get a PATH assignment pointing
    nowhere — same condition that gates the Bash(fused:*) pre-allowance."""
    monkeypatch.delenv("FUSED_RENDER_FUSED_CLI_DIR", raising=False)
    monkeypatch.setattr(agent, "_plugin_argv", lambda target=None: [])
    monkeypatch.setattr(agent.os, "name", "posix")
    command = agent._terminal_command(target)["command"]
    assert "PATH=" not in command
    assert command.startswith("cd ")


def test_a_path_with_spaces_is_quoted(agent, target, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", "/home/u/my apps/fused-bin")
    monkeypatch.setattr(agent, "_plugin_argv", lambda target=None: [])
    monkeypatch.setattr(agent.os, "name", "posix")
    command = agent._terminal_command(target)["command"]
    assert "'/home/u/my apps/fused-bin':$PATH" in command


def test_windows_sets_path_with_cmd_syntax(agent, target, monkeypatch):
    """shlex is POSIX-only and its output misleads on cmd.exe, so this branch
    has its own quoting — and `set` scopes to the shell the user pasted into,
    which is the lifetime we want."""
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", r"C:\Users\u\fused-bin")
    monkeypatch.setattr(agent, "_plugin_argv", lambda target=None: [])
    monkeypatch.setattr(agent.os, "name", "nt")
    command = agent._terminal_command(target)["command"]
    assert r'set "PATH=C:\Users\u\fused-bin;%PATH%"' in command
    assert command.startswith("cd /d ")
    assert "&&" in command


def test_the_resume_flag_still_rides_along(agent, target, monkeypatch):
    """The PATH prefix must not displace anything the command already carried."""
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", "/bin/fused-bin")
    monkeypatch.setattr(agent, "_plugin_argv",
                        lambda target=None: ["--plugin-dir", "/p"])
    monkeypatch.setattr(agent.os, "name", "posix")
    command = agent._terminal_command(target, "sess-A")["command"]
    assert "--resume sess-A" in command
    assert "--plugin-dir /p" in command
    assert "PATH=/bin/fused-bin:$PATH" in command
    # Order: cd, then the env prefix, then the binary.
    assert command.index("PATH=") < command.index("claude")
