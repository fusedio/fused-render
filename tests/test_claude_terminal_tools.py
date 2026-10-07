"""The terminal MCP tools (`terminal_list`, `terminal_read`, `terminal_send`) on
the per-run permission server, and how agent.py wires them.

The server is driven over its own stdio JSON-RPC like test_claude_app_state.py,
against a stub HTTP server that stands in for fused-render's /api/terminal
routes. The `claude` CLI is never invoked.

Rules pinned here: the tools appear only when the run was given a server origin
(`FUSED_RENDER_TERMINAL_ORIGIN`, which agent.py stamps into mcp.json's env and
only on POSIX); list/read are pre-allowed, send is not (it types into the
user's shell, so it keeps its permission card); a busy terminal is a tool error.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from _mcp_stdio import MCPServer
from test_claude_app_state import SERVER, _load, _spawn

pytestmark = pytest.mark.skipif(os.name == "nt", reason="terminal tools are POSIX-only")

SESSIONS = [
    {"id": "t1", "alive": True, "shell": "zsh", "cwd": "/a", "foreground": None,
     "lastCommand": "ls", "lastExit": 0, "lastActivity": 1.0, "focused": False},
    {"id": "t2", "alive": True, "shell": "zsh", "cwd": "/b", "foreground": "vim",
     "lastCommand": "vim x", "lastExit": None, "lastActivity": 2.0, "focused": True},
]


class _Stub:
    def __init__(self):
        self.requests = []
        self.sessions = [dict(s) for s in SESSIONS]
        self.input_status = 200
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                url = urlparse(self.path)
                stub.requests.append(("GET", self.path, dict(self.headers), None))
                if url.path == "/api/terminal":
                    return self._reply(200, {"sessions": stub.sessions})
                if url.path.endswith("/text"):
                    sid = url.path.split("/")[3]
                    if sid not in {s["id"] for s in stub.sessions}:
                        return self._reply(404, {"error": "no such terminal session"})
                    q = parse_qs(url.query)
                    return self._reply(200, {"id": sid, "text": "screen of " + sid,
                                             "lines": q.get("lines")})
                self._reply(404, {"error": "?"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path, dict(self.headers), body))
                if stub.input_status == 409:
                    return self._reply(409, {"error": "terminal is busy"})
                self._reply(200, {"ok": True})

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


def _server(tmp_path, env=None):
    perm = tmp_path / "perm"
    perm.mkdir(exist_ok=True)
    s = MCPServer([sys.executable, os.path.abspath(SERVER), str(perm)], env=env)
    s.initialize()
    return s


def _call(server, name, args=None):
    res = server.call("tools/call", {"name": name, "arguments": args or {}})["result"]
    payload = json.loads(res["content"][0]["text"])
    return payload, bool(res.get("isError"))


def test_tools_absent_without_a_terminal_origin(tmp_path):
    env = {"FUSED_RENDER_TERMINAL_ORIGIN": ""}
    s = _server(tmp_path, env)
    try:
        names = [t["name"] for t in s.call("tools/list")["result"]["tools"]]
        assert names == ["approve"]
    finally:
        s.close()


def test_tools_listed_when_an_origin_is_given(tmp_path, stub):
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        names = [t["name"] for t in s.call("tools/list")["result"]["tools"]]
        assert names == ["approve", "terminal_list", "terminal_read", "terminal_send"]
    finally:
        s.close()


def test_tools_absent_on_windows(monkeypatch):
    srv = _load("permission_server")
    monkeypatch.setenv("FUSED_RENDER_TERMINAL_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(srv, "_IS_WINDOWS", True)
    names = [t["name"] for t in srv._dispatch("tools/list", {})["tools"]]
    assert not [n for n in names if n.startswith("terminal_")]
    with pytest.raises(LookupError):
        srv._dispatch("tools/call", {"name": "terminal_list", "arguments": {}})


def test_terminal_list_returns_sessions_with_focus(tmp_path, stub):
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        payload, is_error = _call(s, "terminal_list")
        assert not is_error
        assert [(t["id"], t["focused"]) for t in payload["terminals"]] == [
            ("t1", False), ("t2", True)]
        assert payload["terminals"][1]["foreground"] == "vim"
    finally:
        s.close()


def test_terminal_read_defaults_to_the_focused_terminal(tmp_path, stub):
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        payload, is_error = _call(s, "terminal_read", {"lines": 50})
        assert not is_error
        assert payload["id"] == "t2"
        get = [r for r in stub.requests if r[1].startswith("/api/terminal/t2/text")][0]
        assert "lines=50" in get[1]
        payload, _ = _call(s, "terminal_read", {"id": "t1"})
        assert payload["id"] == "t1"
    finally:
        s.close()


def test_terminal_read_falls_back_to_the_only_live_terminal(tmp_path, stub):
    stub.sessions = [dict(SESSIONS[0])]
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        payload, is_error = _call(s, "terminal_read")
        assert not is_error and payload["id"] == "t1"
    finally:
        s.close()


def test_terminal_read_without_a_focused_or_unique_terminal_is_an_error(tmp_path, stub):
    stub.sessions[1]["focused"] = False
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        payload, is_error = _call(s, "terminal_read")
        assert is_error and "terminal_list" in payload["error"]
        stub.sessions = []
        payload, is_error = _call(s, "terminal_read")
        assert is_error and "no terminal" in payload["error"].lower()
    finally:
        s.close()


def test_terminal_read_unknown_id_is_a_tool_error(tmp_path, stub):
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        payload, is_error = _call(s, "terminal_read", {"id": "zzz"})
        assert is_error and "no such terminal" in payload["error"]
    finally:
        s.close()


def test_terminal_send_posts_input_with_the_fused_header(tmp_path, stub):
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        payload, is_error = _call(s, "terminal_send", {"id": "t1", "text": "ls\n"})
        assert not is_error and payload["ok"] is True
        method, path, headers, body = [r for r in stub.requests if r[0] == "POST"][0]
        assert path == "/api/terminal/t1/input"
        assert body == {"data": "ls\n"}
        assert headers.get("X-Fused") == "1"
    finally:
        s.close()


def test_terminal_send_busy_is_surfaced_as_an_error(tmp_path, stub):
    stub.input_status = 409
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        payload, is_error = _call(s, "terminal_send", {"id": "t1", "text": "ls\n"})
        assert is_error and "busy" in payload["error"]
    finally:
        s.close()


def test_terminal_send_validates_arguments(tmp_path, stub):
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        _, is_error = _call(s, "terminal_send", {"text": "ls\n"})
        assert is_error
        _, is_error = _call(s, "terminal_send", {"id": "t1"})
        assert is_error
        assert not [r for r in stub.requests if r[0] == "POST"]
    finally:
        s.close()


def test_unreachable_server_is_a_tool_error_not_a_crash(tmp_path):
    s = _server(tmp_path, {"FUSED_RENDER_TERMINAL_ORIGIN": "http://127.0.0.1:1"})
    try:
        payload, is_error = _call(s, "terminal_list")
        assert is_error and "error" in payload
    finally:
        s.close()


# -- agent.py wiring ----------------------------------------------------------

@pytest.fixture
def agent():
    return _load("agent")


def _folder(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"%PDF-1.4\n")
    return d


def test_agent_stamps_the_origin_and_pre_allows_only_the_reads(agent, tmp_path, monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:4321")
    cmd, run_dir = _spawn(agent, monkeypatch, _folder(tmp_path))
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    env = cfg["mcpServers"][agent.PERMISSION_SERVER]["env"]
    assert env["FUSED_RENDER_TERMINAL_ORIGIN"] == "http://127.0.0.1:4321"
    allowed = cmd[cmd.index("--allowed-tools") + 1]
    prefix = "mcp__%s__" % agent.PERMISSION_SERVER
    assert prefix + "terminal_list" in allowed
    assert prefix + "terminal_read" in allowed
    assert "terminal_send" not in allowed


def test_agent_omits_everything_without_an_origin(agent, tmp_path, monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    cmd, run_dir = _spawn(agent, monkeypatch, _folder(tmp_path))
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    assert "FUSED_RENDER_TERMINAL_ORIGIN" not in cfg["mcpServers"][agent.PERMISSION_SERVER]["env"]
    assert "terminal_" not in cmd[cmd.index("--allowed-tools") + 1]


def test_agent_omits_everything_on_windows(agent, tmp_path, monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:4321")
    monkeypatch.setattr(agent, "_terminal_tools_supported", lambda: False)
    cmd, run_dir = _spawn(agent, monkeypatch, _folder(tmp_path))
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    assert "FUSED_RENDER_TERMINAL_ORIGIN" not in cfg["mcpServers"][agent.PERMISSION_SERVER]["env"]
    assert "terminal_" not in cmd[cmd.index("--allowed-tools") + 1]
