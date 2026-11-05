"""agent.py wires the shell-prefix wrapper into the claude spawn env (POSIX)."""
import importlib.util
import os
import re

import pytest

from fused_render import claude_cmd_log

AGENT_PATH = os.path.join("fused_render", "claude_agent", "agent.py")


@pytest.fixture
def agent():
    spec = importlib.util.spec_from_file_location("claude_agent_prefix", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_log_root_matches_agent_runs_sibling(agent):
    assert claude_cmd_log.root() == agent.CMD_LOGS
    assert os.path.dirname(agent.CMD_LOGS) == os.path.dirname(agent.RUNS)


def test_spawn_env_sets_prefix_and_chat_log(agent, monkeypatch):
    monkeypatch.setattr(agent, "CMD_LOGS", os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "fr-cmds-test-%d" % os.getpid()))
    env = agent._spawn_env("chat-123")
    prefix = env["CLAUDE_CODE_SHELL_PREFIX"]
    assert os.path.isabs(prefix) and os.access(prefix, os.X_OK)
    assert re.search(r"claude_shell_prefix-[0-9a-f]{12}\.sh$", prefix)
    log = env["FUSED_CLAUDE_CMD_LOG"]
    assert os.path.basename(log) == "chat-123" and os.path.isdir(log)
    assert (os.stat(log).st_mode & 0o077) == 0


def test_windows_gets_no_prefix(agent, monkeypatch, tmp_path):
    # The wrapper is a POSIX sh script: the CLI runs every MCP stdio server
    # through it, so on Windows it would stop permission_server.py starting.
    monkeypatch.setattr(agent, "CMD_LOGS", str(tmp_path / "cmds"))
    monkeypatch.setattr(agent._platform, "IS_WINDOWS", True)
    env = agent._spawn_env("chat-win")
    assert "CLAUDE_CODE_SHELL_PREFIX" not in env
    assert "FUSED_CLAUDE_CMD_LOG" not in env
    monkeypatch.setattr(agent._platform, "IS_WINDOWS", False)
    env = agent._spawn_env("chat-win")
    assert re.search(r"claude_shell_prefix-[0-9a-f]{12}\.sh$", env["CLAUDE_CODE_SHELL_PREFIX"])
    assert "FUSED_CLAUDE_CMD_LOG" in env


def test_spawn_env_without_chat_id_leaves_prefix_off(agent):
    env = agent._spawn_env()
    assert "FUSED_CLAUDE_CMD_LOG" not in env


def test_unsafe_chat_id_gets_no_prefix(agent):
    env = agent._spawn_env("../x")
    assert "CLAUDE_CODE_SHELL_PREFIX" not in env
    assert "FUSED_CLAUDE_CMD_LOG" not in env


def test_users_own_prefix_is_not_clobbered(agent, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SHELL_PREFIX", "/their/prefix")
    env = agent._spawn_env("chat-9")
    assert env["CLAUDE_CODE_SHELL_PREFIX"] == "/their/prefix"
    assert "FUSED_CLAUDE_CMD_LOG" not in env


def test_second_spawn_for_same_chat_keeps_prefix(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "CMD_LOGS", str(tmp_path / "cmds"))
    first = agent._spawn_env("chat-again")
    second = agent._spawn_env("chat-again")
    for env in (first, second):
        assert re.search(r"claude_shell_prefix-[0-9a-f]{12}\.sh$", env["CLAUDE_CODE_SHELL_PREFIX"])
        assert env["FUSED_CLAUDE_CMD_LOG"] == str(tmp_path / "cmds" / "chat-again")


def test_preexisting_non_private_log_dir_gets_no_prefix(agent, monkeypatch, tmp_path):
    root = tmp_path / "cmds"
    monkeypatch.setattr(agent, "CMD_LOGS", str(root))
    agent._spawn_env("chat-other")  # builds the private parents
    log = root / "chat-other"
    os.chmod(log, 0o755)
    env = agent._spawn_env("chat-other")
    assert "CLAUDE_CODE_SHELL_PREFIX" not in env
    assert "FUSED_CLAUDE_CMD_LOG" not in env


_BUNDLE = "/Applications/FusedRender.app/Contents/Resources"
_PY_VARS = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE",
            "PYTHONINSPECT")


def test_spawn_env_strips_bundle_python_vars(agent, monkeypatch):
    # py2app's launcher exports these for the server; the claude CLI's hooks,
    # MCP servers and shell-prefix wrapper run the SYSTEM python3, which dies
    # on the bundle's PYTHONHOME ("No module named 'encodings'").
    for name in _PY_VARS:
        monkeypatch.setenv(name, _BUNDLE)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for env in (agent._spawn_env(), agent._spawn_env("chat-1")):
        for name in _PY_VARS:
            assert name not in env
        assert env["PATH"].split(os.pathsep)[:2] == ["/usr/bin", "/bin"]
        assert "/opt/homebrew/bin" in env["PATH"].split(os.pathsep)


def test_scripts_handed_to_the_cli_live_outside_the_package_dir(
        agent, tmp_path, monkeypatch):
    """The CLI re-launches these paths for as long as the session lives, which
    can be past an app restart; an AppImage's package dir is a `/tmp/.mount_*`
    that does not survive one. So the CLI gets a copy under the state dir."""
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(agent.__file__)))
    for path in (agent._shell_prefix_path(), agent._permission_server_path()):
        assert os.path.isabs(path) and os.access(path, os.X_OK)
        assert path.startswith(str(home / "bin") + os.sep)
        assert not path.startswith(pkg + os.sep)


def test_each_script_version_keeps_its_own_immutable_copy(
        agent, tmp_path, monkeypatch):
    """Two app versions (or worktrees) share ~/.fused-render/bin: a copy is
    named by its content, so one never overwrites the other's live script."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    a, b = tmp_path / "tool.sh", tmp_path / "other" / "tool.sh"
    b.parent.mkdir()
    a.write_text("echo one\n")
    b.write_text("echo two\n")
    pa, pb = agent._stable_script(str(a)), agent._stable_script(str(b))
    assert pa != pb and os.path.dirname(pa) == os.path.dirname(pb)
    assert open(pa).read() == "echo one\n" and open(pb).read() == "echo two\n"
    assert pa.endswith(".sh") and os.path.basename(pa).startswith("tool-")
    assert agent._stable_script(str(a)) == pa
    assert not [n for n in os.listdir(os.path.dirname(pa)) if n.endswith(".tmp")]


def test_temp_names_are_unique_per_write(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append(src)
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    for i in range(2):
        src = tmp_path / ("s%d.sh" % i)
        src.write_text("echo %d\n" % i)
        agent._stable_script(str(src))
    assert len(set(seen)) == 2 and all(str(os.getpid()) not in os.path.basename(t) for t in seen)


def test_interpreter_for_the_cli_is_not_under_an_appimage_mount(
        agent, tmp_path, monkeypatch):
    """An AppImage's python lives in the same `/tmp/.mount_*` that vanishes on
    restart, so the CLI is handed a system python3 for the stdlib-only
    permission server."""
    appdir = tmp_path / "mnt"
    (appdir / "usr" / "bin").mkdir(parents=True)
    inside = appdir / "usr" / "bin" / "python3"
    inside.write_text("")
    system = tmp_path / "sys" / "python3"
    system.parent.mkdir()
    system.write_text("#!/bin/sh\n")
    system.chmod(0o755)
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setattr(agent.sys, "executable", str(inside))
    monkeypatch.setenv("PATH", "%s%s%s" % (inside.parent, os.pathsep, system.parent))
    assert agent._stable_python() == str(system)
    # Nothing outside the mount to use: the mounted one is all there is.
    monkeypatch.setenv("PATH", str(inside.parent))
    monkeypatch.setattr(agent, "_SYSTEM_PYTHONS", ())
    assert agent._stable_python() == str(inside)
    # Not an AppImage: untouched.
    monkeypatch.delenv("APPDIR")
    monkeypatch.setattr(agent.sys, "executable", str(system))
    assert agent._stable_python() == str(system)
