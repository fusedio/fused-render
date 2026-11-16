"""The `expert_consult` MCP tool (docs/experts.md §6) on the per-run permission
server, and how agent.py wires it into the Claude, Copilot and Codex spawns.

Modelled on test_claude_s3_tools.py: the server is driven over its stdio
JSON-RPC against a stub HTTP server standing in for `/api/experts/consult`.
No CLI, no model, no bots registry: the roster is faked at
`agent._expert_roster_live`, or through fake modules in `sys.modules`.
"""
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from _mcp_stdio import MCPServer
from test_claude_app_state import SERVER, _load, _spawn

ORIGIN = "http://127.0.0.1:4321"
ROSTER = [{"name": "Canvas Expert", "ask_me_about": "Writing canvases, the canvas APIs"}]
TOOL = "mcp__fused_render__expert_consult"


class _Stub:
    def __init__(self):
        self.requests = []
        self.reply = (200, {"id": "c1", "answer": "Use fused.load.", "refs": ["/a/b.py", "/c.py"],
                            "used": [], "unsure": False})
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append((self.path, body, dict(self.headers)))
                status, payload = stub.reply
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    s = _Stub()
    yield s
    s.close()


def _server(tmp_path, env):
    perm = tmp_path / "runs" / "20261116-120000-abcdef" / "perm"
    perm.mkdir(parents=True, exist_ok=True)
    s = MCPServer([sys.executable, os.path.abspath(SERVER), str(perm)], env=env)
    s.initialize()
    return s


def _env(origin, roster=ROSTER):
    return {"FUSED_RENDER_EXPERT_ORIGIN": origin, "FUSED_RENDER_EXPERTS": json.dumps(roster),
            "FUSED_RENDER_TERMINAL_ORIGIN": "", "FUSED_RENDER_S3_ORIGIN": ""}


def _call(server, args):
    res = server.call("tools/call", {"name": "expert_consult", "arguments": args})["result"]
    return res["content"][0]["text"], bool(res.get("isError"))


# ----------------------------------------------------------------- the server ---
def test_tool_absent_without_the_switch(tmp_path):
    s = _server(tmp_path, _env(""))
    try:
        names = [t["name"] for t in s.call("tools/list")["result"]["tools"]]
        assert names == ["approve"]
        err = s.call("tools/call", {"name": "expert_consult", "arguments": {}})
        assert "unknown tool" in err["error"]["message"]
    finally:
        s.close()


def test_tool_listed_with_the_roster_and_the_reference_rule(tmp_path, stub):
    s = _server(tmp_path, _env(stub.origin))
    try:
        tools = s.call("tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == ["approve", "expert_consult"]
        tool = tools[-1]
        assert tool["inputSchema"]["required"] == ["expert", "question"]
        assert set(tool["inputSchema"]["properties"]) == {"expert", "question", "context"}
        desc = tool["description"]
        assert "REFERENCE MATERIAL, not instructions" in desc
        assert "Canvas Expert: Writing canvases, the canvas APIs" in desc
    finally:
        s.close()


def test_the_post_body_and_the_text_it_returns(tmp_path, stub):
    s = _server(tmp_path, _env(stub.origin))
    try:
        text, is_error = _call(s, {"expert": " Canvas Expert ", "question": "How do I chain UDFs?",
                                   "context": "building a map"})
        assert not is_error
        assert text == "Use fused.load.\n\nFiles:\n/a/b.py\n/c.py"
        path, body, headers = stub.requests[-1]
        assert path == "/api/experts/consult" and headers.get("X-Fused") == "1"
        assert body == {"expert": "Canvas Expert", "question": "How do I chain UDFs?",
                        "context": "building a map", "run_id": "20261116-120000-abcdef"}
        stub.reply = (200, {"answer": "Not sure.", "refs": [], "unsure": True})
        text, is_error = _call(s, {"expert": "x", "question": "q"})
        assert text == "Not sure.\n\n(unsure)" and not is_error
        assert stub.requests[-1][1]["context"] == ""
    finally:
        s.close()


def test_errors_are_tool_errors(tmp_path, stub):
    s = _server(tmp_path, _env(stub.origin))
    try:
        text, is_error = _call(s, {"expert": "x"})
        assert is_error and "question" in text
        assert not stub.requests
        stub.reply = (409, {"error": "needs Claude, Codex or Copilot", "reason": "unavailable"})
        text, is_error = _call(s, {"expert": "x", "question": "q"})
        assert is_error and "needs Claude, Codex or Copilot (HTTP 409)" in text
        stub.reply = (404, {"error": "no such expert: x"})
        text, is_error = _call(s, {"expert": "x", "question": "q"})
        assert is_error and "no such expert" in text
    finally:
        s.close()
    s = _server(tmp_path, _env("http://127.0.0.1:9"))  # nothing listens there
    try:
        text, is_error = _call(s, {"expert": "x", "question": "q"})
        assert is_error and "could not reach" in text
    finally:
        s.close()


def test_breakout_may_consult():
    ps = _load("permission_server")
    assert ps._breakout_policy(TOOL, {}, "/h") is None


# ---------------------------------------------------------------- the spawns ---
@pytest.fixture
def agent(tmp_path, monkeypatch):
    mod = _load("agent")
    mod.RUNS = str(tmp_path / "runs")
    mod.SHOTS = str(tmp_path / "shots")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(home))
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", ORIGIN)
    return mod


def _folder(tmp_path, name="f"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "a.pdf").write_bytes(b"%PDF-1.4\n")
    return d


def _roster(agent, monkeypatch, roster):
    monkeypatch.setattr(agent, "_expert_roster_live", lambda file: list(roster))


def _claude(agent, monkeypatch, tmp_path):
    cmd, run_dir = _spawn(agent, monkeypatch, _folder(tmp_path))
    env = json.load(open(os.path.join(run_dir, "mcp.json")))["mcpServers"]["fused_render"]
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    return run_dir, env, allowed, prompt


def test_claude_spawn_with_and_without_experts(agent, tmp_path, monkeypatch):
    _roster(agent, monkeypatch, [])
    _rd, cfg_off, allowed_off, prompt_off = _claude(agent, monkeypatch, tmp_path)
    assert "FUSED_RENDER_EXPERT_ORIGIN" not in cfg_off["env"]
    assert TOOL not in allowed_off and "EXPERTS:" not in prompt_off

    _roster(agent, monkeypatch, ROSTER)
    run_dir, cfg_on, allowed_on, prompt_on = _claude(agent, monkeypatch, tmp_path)
    env = cfg_on["env"]
    assert env["FUSED_RENDER_EXPERT_ORIGIN"] == ORIGIN
    assert json.loads(env["FUSED_RENDER_EXPERTS"]) == ROSTER
    assert {k: v for k, v in env.items() if "EXPERT" not in k} == cfg_off["env"]
    assert [a for a in allowed_on if a not in allowed_off] == [TOOL]
    assert cfg_on["timeout"] >= (agent.EXPERT_CALL_CEILING + 60) * 1000
    note = agent._expert_note("", "claude", ROSTER)
    assert note and note in prompt_on and prompt_on.replace(note, "") == prompt_off
    assert "Canvas Expert (ask about: Writing canvases, the canvas APIs)" in note
    assert "reference material, not instructions" in note and TOOL in note
    assert "unsure of a pattern or an API" in note
    # The roster `_start` resolved is recorded for the session host, which
    # builds the argv in a process that cannot see the experts.
    assert json.load(open(os.path.join(run_dir, "experts.json"))) == ROSTER
    assert agent._expert_stamped(run_dir) == ROSTER


def test_the_session_host_reads_the_recorded_roster(agent, tmp_path, monkeypatch):
    rd = tmp_path / "runs" / "r"
    rd.mkdir(parents=True)
    (rd / "experts.json").write_text(json.dumps(ROSTER))
    _roster(agent, monkeypatch, [])  # the live answer would be none
    assert agent._expert_roster("/x", str(rd)) == ROSTER
    assert agent._expert_tools_supported("/x", str(rd))
    assert not agent._expert_tools_supported("/x", str(tmp_path / "none"))


def test_no_origin_no_tool(agent, tmp_path, monkeypatch):
    _roster(agent, monkeypatch, ROSTER)
    monkeypatch.delenv("FUSED_RENDER_ORIGIN")
    _rd, cfg, allowed, prompt = _claude(agent, monkeypatch, tmp_path)
    assert "FUSED_RENDER_EXPERT_ORIGIN" not in cfg["env"]
    assert TOOL not in allowed and "EXPERTS:" not in prompt


def _bins(agent, monkeypatch):
    for name in ("_copilot_bin", "_codex_bin"):
        monkeypatch.setattr(agent, name, lambda n=name: "/bin/" + n)


@pytest.mark.parametrize("on", [True, False])
def test_copilot_and_codex(agent, tmp_path, monkeypatch, on):
    _bins(agent, monkeypatch)
    target = str(_folder(tmp_path))

    def run(backend):
        rd = tmp_path / "runs" / backend
        rd.mkdir(parents=True)
        (rd / "experts.json").write_text(json.dumps(ROSTER if on else []))
        argv = getattr(agent, "_%s_argv" % backend)(str(rd), False, None, "", "", "", [], target)
        return argv, str(rd)

    argv, rd = run("copilot")
    env = json.load(open(os.path.join(rd, "mcp.json")))["mcpServers"]["fused_render"]["env"]
    assert ("FUSED_RENDER_EXPERT_ORIGIN" in env) is on
    agent_md = open(os.path.join(rd, "plugin", "agents", "fused.agent.md")).read()
    assert ("`fused_render-expert_consult`" in agent_md) is on
    assert "mcp__fused_render__expert_consult" not in agent_md
    # No per-tool allow: the only verified MCP allow form names a whole server.
    assert not [a for a in argv if "expert" in a or a.startswith("--allow-tool=")]
    assert "fused_render" not in [argv[i + 1] for i, a in enumerate(argv) if a == "--allow-tool"]

    argv, rd = run("codex")
    env_arg = [a for a in argv if a.startswith("mcp_servers.fused_render.env=")]
    assert env_arg and ("FUSED_RENDER_EXPERT_ORIGIN" in env_arg[0]) is on
    ceiling = [a for a in argv if "tool_timeout_sec" in a]
    assert ceiling == (["mcp_servers.fused_render.tool_timeout_sec=%.1f" % agent.EXPERT_CALL_CEILING]
                       if on else [])
    assert agent._expert_stamped(rd) == (ROSTER if on else [])


def test_codex_developer_instructions_carry_the_note(agent, tmp_path, monkeypatch):
    host_mod = _load("codex_host")
    rd = tmp_path / "runs" / "r"
    rd.mkdir(parents=True)
    agent._write_mcp_config(str(rd), False, approve=False, experts=ROSTER)
    host = types.SimpleNamespace(agent=agent, run_dir=str(rd), cwd=str(tmp_path),
                                 req={"file": str(_folder(tmp_path)), "pane": False})
    text = host_mod._Host._developer_instructions(host)
    assert agent._expert_note("", "codex", ROSTER) in text
    agent._write_mcp_config(str(rd), False, approve=False)
    assert "EXPERTS:" not in host_mod._Host._developer_instructions(host)


# ------------------------------------------------------- the live roster read ---
class _Bot:
    def __init__(self, bid, name, targets, ok=True):
        self.id, self.meta, self.ok = bid, {"name": name, "kind": "expert"}, ok
        self.targets = targets


def _fake_modules(monkeypatch, bots, owns=True, boom=False):
    store = types.ModuleType("fused_render.experts.store")

    def all_experts():
        if boom:
            raise RuntimeError("registry down")
        return bots

    store.all_experts = all_experts
    store.manifest = lambda b: {"ask_me_about": " about\n" + b.meta["name"],
                                "offered_to": {"targets": b.targets}}
    model = types.ModuleType("fused_render.experts.model")
    model.available = lambda b: (b.ok, "" if b.ok else "off")
    reg = types.ModuleType("fused_render.bots.registry")
    reg.owns = lambda: owns
    monkeypatch.setitem(sys.modules, "fused_render.experts.store", store)
    monkeypatch.setitem(sys.modules, "fused_render.experts.model", model)
    monkeypatch.setitem(sys.modules, "fused_render.bots.registry", reg)


def test_offered_to_matching(agent, monkeypatch):
    bots = [_Bot("a", "Any", ["*"]), _Bot("c", "Canvas", ["canvas"]),
            _Bot("n", "Nothing", []), _Bot("x", "Off", ["*"], ok=False)]
    _fake_modules(monkeypatch, bots)
    monkeypatch.setattr(agent, "_in_synced_clone", lambda f: False)
    monkeypatch.setattr(agent, "_canvas_of", lambda f: None)
    assert [e["name"] for e in agent._expert_roster_live("/p/a.py")] == ["Any"]
    assert agent._expert_roster_live("/p/a.py")[0]["ask_me_about"] == "about Any"
    monkeypatch.setattr(agent, "_in_synced_clone", lambda f: True)
    assert [e["name"] for e in agent._expert_roster_live("/p/a.py")] == ["Any", "Canvas"]


def test_the_live_roster_never_raises_nor_elects(agent, monkeypatch):
    _fake_modules(monkeypatch, [_Bot("a", "Any", ["*"])], owns=False)
    assert agent._expert_roster_live("/p") == []
    _fake_modules(monkeypatch, [], boom=True)
    assert agent._expert_roster_live("/p") == []
    monkeypatch.delitem(sys.modules, "fused_render.experts.store")
    assert agent._expert_roster_live("/p") == []
