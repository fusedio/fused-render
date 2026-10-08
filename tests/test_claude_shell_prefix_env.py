"""agent.py wires the shell-prefix wrapper into the claude spawn env (POSIX)."""
import importlib.util
import os

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


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_spawn_env_sets_prefix_and_chat_log(agent, monkeypatch):
    monkeypatch.setattr(agent, "CMD_LOGS", os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "fr-cmds-test-%d" % os.getpid()))
    env = agent._spawn_env("chat-123")
    prefix = env["CLAUDE_CODE_SHELL_PREFIX"]
    assert os.path.isabs(prefix) and os.access(prefix, os.X_OK)
    assert prefix.endswith("claude_shell_prefix.sh")
    log = env["FUSED_CLAUDE_CMD_LOG"]
    assert os.path.basename(log) == "chat-123" and os.path.isdir(log)
    assert (os.stat(log).st_mode & 0o077) == 0


def test_spawn_env_without_chat_id_leaves_prefix_off(agent):
    env = agent._spawn_env()
    assert "FUSED_CLAUDE_CMD_LOG" not in env


def test_unsafe_chat_id_gets_no_prefix(agent):
    env = agent._spawn_env("../x")
    assert "CLAUDE_CODE_SHELL_PREFIX" not in env
    assert "FUSED_CLAUDE_CMD_LOG" not in env


def test_windows_never_sets_prefix(agent, monkeypatch):
    monkeypatch.setattr(agent, "_prefix_supported", lambda: False)
    env = agent._spawn_env("chat-1")
    assert "CLAUDE_CODE_SHELL_PREFIX" not in env


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_users_own_prefix_is_not_clobbered(agent, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SHELL_PREFIX", "/their/prefix")
    env = agent._spawn_env("chat-9")
    assert env["CLAUDE_CODE_SHELL_PREFIX"] == "/their/prefix"
    assert "FUSED_CLAUDE_CMD_LOG" not in env


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_second_spawn_for_same_chat_keeps_prefix(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "CMD_LOGS", str(tmp_path / "cmds"))
    first = agent._spawn_env("chat-again")
    second = agent._spawn_env("chat-again")
    for env in (first, second):
        assert env["CLAUDE_CODE_SHELL_PREFIX"].endswith("claude_shell_prefix.sh")
        assert env["FUSED_CLAUDE_CMD_LOG"] == str(tmp_path / "cmds" / "chat-again")


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
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
        assert env["PATH"] == "/usr/bin:/bin"
