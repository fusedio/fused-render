"""Lean (`--strict-mcp-config`) sessions keep the ENABLED plugins' MCP servers.

Strict mode drops plugin servers along with the user's own, but a plugin like
context-mode has to be there in every session, so `_plugin_mcp_servers` reads
the plugin install records and the run's mcp.json carries them explicitly.
"""
import importlib.util
import json
import os

import pytest


@pytest.fixture()
def agent():
    path = os.path.join("fused_render", "claude_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_pm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _w(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "claude"
    inst = {}

    def install(key, root, scope="user"):
        inst.setdefault(key, []).append({"scope": scope, "installPath": str(root)})

    # 1. enabled, manifest mcpServers dict with ${CLAUDE_PLUGIN_ROOT}
    a = tmp_path / "pa"
    _w(str(a / ".claude-plugin" / "plugin.json"), {"mcpServers": {"srv": {
        "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/start.mjs", "-x"],
        "env": {"DATA": "${CLAUDE_PLUGIN_ROOT}/data"}}}})
    install("alpha@mk", a)
    # 2. disabled
    b = tmp_path / "pb"
    _w(str(b / ".mcp.json"), {"mcpServers": {"nope": {"command": "x"}}})
    install("beta@mk", b)
    # 3. enabled, .mcp.json wrapped; user scope picked over project scope
    c = tmp_path / "pc"
    other = tmp_path / "pc-other"
    _w(str(c / ".mcp.json"), {"mcpServers": {"one": {"command": "${CLAUDE_PLUGIN_ROOT}/bin"}}})
    install("gamma@mk", other, scope="project")
    install("gamma@mk", c, scope="user")
    # 4. enabled, bare .mcp.json dict
    d = tmp_path / "pd"
    _w(str(d / ".mcp.json"), {"bare": {"command": "bare"}})
    install("delta@mk", d)
    # 5. enabled, manifest mcpServers is a path string
    e = tmp_path / "pe"
    _w(str(e / ".claude-plugin" / "plugin.json"), {"mcpServers": "./servers.json"})
    _w(str(e / "servers.json"), {"viafile": {"command": "f"}})
    install("eps@mk", e)
    # 6. enabled but broken json
    f = tmp_path / "pf"
    _w(str(f / ".mcp.json"), "{broken")
    install("zeta@mk", f)
    # 7. enabled but not installed
    _w(str(h / "settings.json"), {"enabledPlugins": {
        "alpha@mk": True, "beta@mk": False, "gamma@mk": True, "delta@mk": True,
        "eps@mk": True, "zeta@mk": True, "ghost@mk": True}})
    _w(str(h / "plugins" / "installed_plugins.json"), {"plugins": inst})
    return h


def test_enabled_plugins_servers_are_keyed_and_expanded(agent, home, tmp_path):
    got = agent._plugin_mcp_servers(str(home))
    assert set(got) == {"plugin_alpha_srv", "plugin_gamma_one", "plugin_delta_bare",
                        "plugin_eps_viafile"}
    root = str(tmp_path / "pa")
    s = got["plugin_alpha_srv"]
    assert s["command"] == "node"
    assert s["args"] == [root + "/start.mjs", "-x"]
    assert s["env"] == {"DATA": root + "/data", "CLAUDE_PLUGIN_ROOT": root}
    assert got["plugin_gamma_one"]["command"] == str(tmp_path / "pc") + "/bin"


def test_missing_or_garbled_home_is_empty(agent, tmp_path):
    assert agent._plugin_mcp_servers(str(tmp_path / "nowhere")) == {}
    (tmp_path / "settings.json").write_text("[]")
    assert agent._plugin_mcp_servers(str(tmp_path)) == {}


def test_strict_mcp_json_has_fused_plus_plugins_and_nonstrict_unchanged(agent, home, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "CLAUDE_DIR", str(home))
    run = tmp_path / "run"
    (run / "perm").mkdir(parents=True)
    lean = json.load(open(agent._write_mcp_config(str(run), False, strict=True)))["mcpServers"]
    assert agent.PERMISSION_SERVER in lean and "plugin_alpha_srv" in lean
    plain = json.load(open(agent._write_mcp_config(str(run), False)))["mcpServers"]
    assert list(plain) == [agent.PERMISSION_SERVER]


def test_our_server_wins_a_key_collision(agent, home, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "CLAUDE_DIR", str(home))
    monkeypatch.setattr(agent, "_plugin_mcp_servers",
                        lambda h: {agent.PERMISSION_SERVER: {"command": "evil"}})
    run = tmp_path / "run"
    (run / "perm").mkdir(parents=True)
    cfg = json.load(open(agent._write_mcp_config(str(run), False, strict=True)))["mcpServers"]
    assert cfg[agent.PERMISSION_SERVER]["command"] != "evil"


def _one_plugin(tmp_path, manifest=None, mcp_json=None, files=None):
    """A single enabled plugin `p@mk`; returns (claude home, install root)."""
    h = tmp_path / "claude1"
    root = tmp_path / "proot"
    if manifest is not None:
        _w(str(root / ".claude-plugin" / "plugin.json"), manifest)
    if mcp_json is not None:
        _w(str(root / ".mcp.json"), mcp_json)
    for rel, body in (files or {}).items():
        _w(str(root / rel), body)
    _w(str(h / "settings.json"), {"enabledPlugins": {"p@mk": True}})
    _w(str(h / "plugins" / "installed_plugins.json"), {"plugins": {
        "p@mk": [{"scope": "user", "installPath": str(root)}]}})
    return h, str(root)


def test_a_string_manifest_path_expands_the_plugin_root(agent, tmp_path):
    h, root = _one_plugin(
        tmp_path, manifest={"mcpServers": "${CLAUDE_PLUGIN_ROOT}/cfg/servers.json"},
        files={"cfg/servers.json": {"viaroot": {"command": "c"}}})
    assert set(agent._plugin_mcp_servers(str(h))) == {"plugin_p_viaroot"}


def test_url_cwd_and_headers_expand_the_plugin_root(agent, tmp_path):
    h, root = _one_plugin(tmp_path, mcp_json={"mcpServers": {
        "web": {"type": "http", "url": "${CLAUDE_PLUGIN_ROOT}/sock",
                "headers": {"X-Root": "${CLAUDE_PLUGIN_ROOT}", "X-N": 5}},
        "proc": {"command": "run", "cwd": "${CLAUDE_PLUGIN_ROOT}/work"}}})
    got = agent._plugin_mcp_servers(str(h))
    assert got["plugin_p_web"]["url"] == root + "/sock"
    assert got["plugin_p_web"]["headers"] == {"X-Root": root, "X-N": 5}
    assert got["plugin_p_proc"]["cwd"] == root + "/work"


def test_http_and_sse_specs_get_no_env_but_stdio_does(agent, tmp_path):
    h, root = _one_plugin(tmp_path, mcp_json={"mcpServers": {
        "h": {"type": "http", "url": "https://x.test/mcp"},
        "u": {"url": "https://y.test/sse"},
        "s": {"type": "sse", "url": "https://z.test/sse"},
        "c": {"command": "node"}}})
    got = agent._plugin_mcp_servers(str(h))
    for name in ("h", "u", "s"):
        assert "env" not in got[f"plugin_p_{name}"], name
    assert got["plugin_p_c"]["env"] == {"CLAUDE_PLUGIN_ROOT": root}
