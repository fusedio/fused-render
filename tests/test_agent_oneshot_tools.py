"""`agent_oneshot.prepare(tools_mcp=...)` (D1460): the turn's MCP bridge as
each agent CLI's one tool surface — and the recap argv untouched without it."""
import json
import os

import pytest

from fused_render import agent_oneshot


class _Agent:
    def _spawn_env(self, backend=""):
        return {"PATH": "/bin"}


_MCP = {"command": "/py", "args": ["/bridge.py"],
        "env": {"FUSED_RENDER_ORIGIN": "http://127.0.0.1:1", "FUSED_AI_TURN": "t1"}}


@pytest.fixture(autouse=True)
def _bins(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_oneshot, "_bin", lambda agent, backend: f"/usr/bin/{backend}")
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(agent_oneshot, "_markers", lambda backend: ((), ()))


def test_copilot_tools_replace_the_no_tools_switch_with_the_bridge():
    plain = agent_oneshot.prepare("copilot", "auto", "sys", "p", agent=_Agent())
    spec = agent_oneshot.prepare("copilot", "auto", "sys", "p", agent=_Agent(), tools_mcp=_MCP)
    try:
        assert f"--available-tools={agent_oneshot.COPILOT_NO_TOOLS}" in plain.argv
        assert "--additional-mcp-config" not in plain.argv
        name = agent_oneshot._mcp_server_name()
        assert f"--available-tools={name}" in spec.argv
        assert f"--allow-tool={name}" in spec.argv
        assert f"--available-tools={agent_oneshot.COPILOT_NO_TOOLS}" not in spec.argv
        cfg = json.loads(spec.argv[spec.argv.index("--additional-mcp-config") + 1])
        assert cfg["mcpServers"][name] == {
            "type": "local", "command": "/py", "args": ["/bridge.py"],
            "env": _MCP["env"], "tools": ["*"]}
        # Everything else is the recap run: no instructions, no built-in
        # MCPs, nothing to ask, the same temp cwd.
        for flag in ("--no-custom-instructions", "--disable-builtin-mcps", "--no-ask-user"):
            assert flag in spec.argv and flag in plain.argv
    finally:
        plain.cleanup()
        spec.cleanup()


def test_codex_tools_are_config_overrides():
    spec = agent_oneshot.prepare("codex", "gpt-6-luna", "sys", "p", agent=_Agent(), tools_mcp=_MCP)
    try:
        name = agent_oneshot._mcp_server_name()
        overrides = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-c"]
        assert f'mcp_servers.{name}.command="/py"' in overrides
        assert f'mcp_servers.{name}.args=["/bridge.py"]' in overrides
        assert any(o.startswith(f"mcp_servers.{name}.env=") for o in overrides)
        assert "project_root_markers=[]" in overrides  # the recap's own override stays
        assert "read-only" in spec.argv
    finally:
        spec.cleanup()


def test_gemini_tools_ride_a_system_settings_file_and_an_allowing_policy():
    spec = agent_oneshot.prepare("gemini", "auto", "sys", "p", agent=_Agent(), tools_mcp=_MCP)
    try:
        name = agent_oneshot._mcp_server_name()
        # No cwd (the spawn rule: posix_spawn, never fork), nothing under the
        # user's own Gemini home: the server rides a SYSTEM settings file.
        assert not hasattr(spec, "cwd")
        settings = spec.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
        with open(settings, encoding="utf-8") as fh:
            assert json.load(fh)["mcpServers"][name]["command"] == "/py"
        policy = spec.argv[spec.argv.index("--policy") + 1]
        assert os.path.dirname(policy) == os.path.dirname(settings)
        text = open(policy, encoding="utf-8").read()
        assert f'toolName = "{name}__*"' in text and 'decision = "allow"' in text
        assert 'toolName = "*"' in text and 'decision = "deny"' in text
        # Gemini refuses priority 1000 ("tier overflow"); the allow outranks
        # the deny below the ceiling.
        assert "priority = 999" in text and "priority = 998" in text
        assert "1000" not in text
        assert spec.argv[spec.argv.index("--allowed-mcp-server-names") + 1] == name
        run_dir = os.path.dirname(settings)
    finally:
        spec.cleanup()
    assert not os.path.exists(run_dir)  # cleanup removes the run's files
    # Without tools: the shared deny-all policy, the user's own settings.
    plain = agent_oneshot.prepare("gemini", "auto", "sys", "p", agent=_Agent())
    try:
        assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in plain.env
        assert plain.argv[plain.argv.index("--policy") + 1] == agent_oneshot.gemini_policy_path()
        assert "--allowed-mcp-server-names" not in plain.argv
    finally:
        plain.cleanup()


def test_cursor_carries_the_whole_prompt_as_the_positional_and_nothing_on_stdin():
    """`cursor-agent -p` documents a positional prompt and no stdin read, so
    the full text (system and prompt) is the last argument, after `--`."""
    spec = agent_oneshot.prepare("cursor", "auto", "sys", "-p question", agent=_Agent())
    try:
        assert spec.argv[-2:] == ["--", "sys\n\n-p question"]
        assert spec.stdin == ""
    finally:
        spec.cleanup()


def test_cursor_hands_an_oversize_prompt_over_as_a_file_in_its_workspace():
    big = "x" * (agent_oneshot.CURSOR_ARGV_MAX + 1)
    spec = agent_oneshot.prepare("cursor", "auto", "", big, agent=_Agent())
    try:
        workspace = spec.argv[spec.argv.index("--workspace") + 1]
        with open(os.path.join(workspace, agent_oneshot.CURSOR_PROMPT_FILE)) as fh:
            assert fh.read() == big
        assert spec.argv[-2] == "--"
        assert agent_oneshot.CURSOR_PROMPT_FILE in spec.argv[-1]
        assert big not in spec.argv
        assert spec.stdin == ""
    finally:
        spec.cleanup()
    assert not os.path.exists(workspace)


def test_cursor_leaves_no_temp_workspace_when_the_binary_is_missing(monkeypatch, tmp_path):
    def missing(agent, backend):
        raise agent_oneshot.OneShotError("ai_unavailable", "cursor CLI not found")
    monkeypatch.setattr(agent_oneshot, "_bin", missing)
    monkeypatch.setattr(agent_oneshot.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(agent_oneshot.OneShotError):
        agent_oneshot.prepare("cursor", "auto", "sys", "p", agent=_Agent())
    assert [n for n in os.listdir(tmp_path) if n.startswith("fused-cursor-oneshot-")] == []
