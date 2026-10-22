"""connect/mcp_client.py: the Streamable HTTP MCP client, against httpx.MockTransport.

No server runs here: each test is a transport function that answers the way a
vendor's hosted MCP server does (a JSON body, an SSE stream, GitHub's 400 for a
bad token), so what is under test is the client's framing and error typing.
"""
import json

import httpx
import pytest

from fused_render.connect.mcp_client import (
    McpAuthError,
    McpClient,
    McpError,
    McpScopeError,
    auth_params,
)

URL = "https://mcp.example.com/mcp"
TOOLS = [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"},
          "annotations": {"readOnlyHint": True}}]


class Server:
    """A minimal MCP server as a MockTransport handler; records every request."""

    def __init__(self, sse=False, session="sess-1"):
        self.sse = sse
        self.session = session
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    def reply(self, msg_id, result, extra_events=()):
        if self.sse:
            events = [f"data: {json.dumps(e)}\n\n" for e in extra_events]
            events.append(f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': result})}\n\n")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=(": ping\n\n" + "".join(events)).encode())
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg_id, "result": result})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        self.bodies.append(body)
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            r = self.reply(body["id"], {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                        "serverInfo": {"name": "Example", "version": "1"}})
            if self.session:
                r.headers["Mcp-Session-Id"] = self.session
            return r
        if method == "tools/list":
            return self.reply(body["id"], {"tools": TOOLS},
                              extra_events=[{"jsonrpc": "2.0", "method": "notifications/message",
                                             "params": {"level": "info"}}])
        if method == "tools/call":
            args = body["params"]["arguments"]
            return self.reply(body["id"], {"content": [{"type": "text", "text": args.get("text", "")}],
                                           "structuredContent": {"echo": args.get("text")}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "error": {"code": -32601, "message": "no such method"}})


def _client(handler, token="tok-1"):
    return McpClient(URL, token, http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("sse", [False, True])
def test_initialize_list_call(sse):
    srv = Server(sse=sse)
    c = _client(srv)
    info = c.initialize()
    assert info["serverInfo"]["name"] == "Example"
    assert c.session_id == "sess-1"
    tools = c.list_tools()
    assert [t["name"] for t in tools] == ["echo"]
    out = c.call("echo", {"text": "hi"})
    assert out == {"content": [{"type": "text", "text": "hi"}], "isError": False,
                   "structuredContent": {"echo": "hi"}}
    methods = [b.get("method") for b in srv.bodies]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    first = srv.requests[0]
    assert first.headers["accept"] == "application/json, text/event-stream"
    assert first.headers["content-type"] == "application/json"
    assert first.headers["mcp-protocol-version"] == "2025-06-18"
    assert first.headers["authorization"] == "Bearer tok-1"
    assert "mcp-session-id" not in first.headers
    # The session id is echoed on every request after initialize.
    assert all(r.headers.get("mcp-session-id") == "sess-1" for r in srv.requests[1:])


def test_lazy_initialize_and_callable_token():
    srv = Server()
    calls = []

    def tok():
        calls.append(1)
        return f"tok-{len(calls)}"
    c = _client(srv, token=tok)
    c.call("echo", {"text": "x"})   # initializes itself first
    assert [b.get("method") for b in srv.bodies][:2] == ["initialize", "notifications/initialized"]
    assert [r.headers["authorization"] for r in srv.requests] == ["Bearer tok-1", "Bearer tok-2", "Bearer tok-3"]


def test_sse_list_changed_notification_is_noted():
    srv = Server(sse=True)
    orig = srv.reply

    def reply(msg_id, result, extra_events=()):
        return orig(msg_id, result, extra_events=[{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}])
    srv.reply = reply
    c = _client(srv)
    c.initialize()
    c.call("echo", {"text": "a"})
    assert c.tools_changed is True
    # A fresh tools/list is the answer to the notification, so it clears the flag.
    assert [t["name"] for t in c.list_tools()] == ["echo"]
    assert c.tools_changed is False


def test_401_is_auth_error_with_header():
    def h(request):
        return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://x/.well-known/oauth-protected-resource"'})
    with pytest.raises(McpAuthError) as ei:
        _client(h).initialize()
    assert "resource_metadata" in ei.value.www_authenticate


def test_github_400_invalid_token_is_auth_error():
    def h(request):
        return httpx.Response(400, text="bad request: Authorization header is badly formatted",
                              headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
    with pytest.raises(McpAuthError):
        _client(h).initialize()


def test_plain_400_is_generic_error():
    def h(request):
        return httpx.Response(400, text="nope")
    with pytest.raises(McpError) as ei:
        _client(h).initialize()
    assert not isinstance(ei.value, McpAuthError)


def test_403_scope_error():
    def h(request):
        return httpx.Response(403, headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="notes:write"'})
    with pytest.raises(McpScopeError) as ei:
        _client(h).initialize()
    assert ei.value.scope == "notes:write"


def test_jsonrpc_error_raises():
    srv = Server()
    c = _client(srv)
    c.initialize()
    with pytest.raises(McpError, match="no such method"):
        c._request("bogus/method")


def test_is_error_defaults_false_and_true_passthrough():
    def h(request):
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"serverInfo": {"name": "x"}}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"content": [{"type": "text", "text": "boom"}], "isError": True}})
    out = _client(h).call("t", {})
    assert out == {"content": [{"type": "text", "text": "boom"}], "isError": True}


def test_session_404_reinitializes_once():
    srv = Server()
    state = {"expired": False}

    def h(request):
        if state["expired"] and request.headers.get("mcp-session-id") == "sess-1":
            return httpx.Response(404)
        return srv(request)
    c = _client(h)
    c.initialize()
    state["expired"] = True
    srv.session = "sess-2"
    out = c.call("echo", {"text": "again"})
    assert out["content"][0]["text"] == "again"
    assert c.session_id == "sess-2"


def test_list_tools_follows_cursor():
    def h(request):
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        cursor = (body.get("params") or {}).get("cursor")
        if cursor is None:
            res = {"tools": [{"name": "a"}], "nextCursor": "p2"}
        else:
            res = {"tools": [{"name": "b"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": res})
    assert [t["name"] for t in _client(h).list_tools()] == ["a", "b"]


def test_close_deletes_session():
    srv = Server()
    c = _client(srv)
    c.initialize()
    c.close()
    assert srv.requests[-1].method == "DELETE"
    assert srv.requests[-1].headers["mcp-session-id"] == "sess-1"


def test_auth_params():
    assert auth_params('Bearer error="invalid_token", scope="a b", resource_metadata="https://x/y"') == {
        "error": "invalid_token", "scope": "a b", "resource_metadata": "https://x/y"}
    assert auth_params(None) == {}
