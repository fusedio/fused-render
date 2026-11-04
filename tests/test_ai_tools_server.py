"""The stdio MCP bridge of an `/api/ai` tools run (D1460):
`claude_agent/ai_tools_server.py`. Driven through `handle()` with the HTTP
hop injected, the way the CLI drives it over stdin."""
import json
import subprocess
import sys

from fused_render.claude_agent import ai_tools_server as bridge


def _fetch(calls, tools=None, result=None, fail=None):
    def fetch(method, path, body, timeout):
        calls.append((method, path, body, timeout))
        if fail is not None:
            raise fail
        if method == "GET":
            return {"tools": tools or []}
        return result or {"content": "ok", "isError": False}
    return fetch


def test_initialize_announces_tools_and_nothing_else():
    reply = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {}}, fetch=_fetch([]))
    assert reply["id"] == 1
    assert reply["result"]["capabilities"] == {"tools": {}}
    assert reply["result"]["serverInfo"]["name"] == bridge.SERVER_NAME
    # A notification gets no reply; an unknown request a JSON-RPC error.
    assert bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"},
                         fetch=_fetch([])) is None
    err = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
                        fetch=_fetch([]))
    assert err["error"]["code"] == -32601


def test_tools_list_is_the_turns_tools_in_mcp_shape(monkeypatch):
    monkeypatch.setattr(bridge, "TURN", "turn_abc")
    calls = []
    tools = [{"name": "list_udfs", "description": "List UDFs",
              "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}},
             {"name": "bare"}]
    reply = bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                          fetch=_fetch(calls, tools=tools))
    assert calls == [("GET", "/api/ai/turns/turn_abc/tools", None, 30.0)]
    assert reply["result"]["tools"] == [
        {"name": "list_udfs", "description": "List UDFs",
         "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        {"name": "bare", "description": "",
         "inputSchema": {"type": "object", "properties": {}}}]


def test_tools_call_forwards_and_words_the_result(monkeypatch):
    monkeypatch.setattr(bridge, "TURN", "turn_abc")
    monkeypatch.setattr(bridge, "CALL_TIMEOUT", 7.0)
    calls = []
    reply = bridge.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "list_udfs", "arguments": {"q": "x"}}},
        fetch=_fetch(calls, result={"content": "[1,2]", "isError": False}))
    method, path, body, timeout = calls[0]
    assert (method, path) == ("POST", "/api/ai/turns/turn_abc/call")
    assert body == {"name": "list_udfs", "input": {"q": "x"}}
    assert timeout == 7.0 + bridge._HTTP_SLACK_S  # the server answers first
    assert reply["result"] == {"content": [{"type": "text", "text": "[1,2]"}],
                               "isError": False}
    # The caller's failure is the model's failed tool, not a protocol error.
    reply = bridge.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "list_udfs", "arguments": None}},
        fetch=_fetch([], result={"content": "boom", "isError": True}))
    assert reply["result"]["isError"] is True
    # An unreachable server, likewise: a text the model can read, never a hang.
    reply = bridge.handle(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "list_udfs", "arguments": {}}},
        fetch=_fetch([], fail=OSError("connection refused")))
    assert reply["result"]["isError"] is True
    assert "connection refused" in reply["result"]["content"][0]["text"]


def test_the_bridge_runs_as_a_script_over_stdio(tmp_path):
    # Stdlib only, no cwd assumption: the CLI spawns it from anywhere. With
    # no origin, tools/list is an error the model can read, not a crash.
    proc = subprocess.run(
        [sys.executable, "-I", bridge.__file__],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n",
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "FUSED_RENDER_ORIGIN": "", "FUSED_AI_TURN": "t"})
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert [l["id"] for l in lines] == [1, 2]
    assert lines[1]["result"] == {}
