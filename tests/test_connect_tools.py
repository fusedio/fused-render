"""connect/tools.py: the merged roster (remote MCP + native pack tools) and call_tool,
plus the native Slack tools against a mocked Slack Web API.

Packs are written into a temp folder named by `FUSED_RENDER_CONNECT_PROVIDERS`,
records go to the file store under a temp home, and every HTTP request goes to an
`httpx.MockTransport`: nothing leaves the process.
"""
import json
import os
import shutil
import time

import httpx
import pytest

from fused_render.connect import manager, oauth, store, tools

REMOTE = "https://demo.example/mcp"
SLACK_TOOLS = os.path.join(os.path.dirname(tools.__file__), "providers", "slack", "tools.py")

NATIVE = '''
from fused_render.connect.tools import ToolError
TOOLS = [
    {"name": "local_sum", "description": "Add", "input_schema": {"type": "object"},
     "read_only": True, "destructive": False},
    {"name": "local_fail", "description": "Fails", "input_schema": {"type": "object"},
     "read_only": False, "destructive": True},
]
def call(ctx, name, args):
    if name == "local_fail":
        raise ToolError("no_way")
    return {"sum": args["a"] + args["b"], "token_seen": bool(ctx.token), "kind": ctx.record.get("kind")}
'''


def _pack(root, key, doc, native=None):
    d = root / key
    d.mkdir(parents=True)
    (d / "provider.json").write_text(json.dumps(doc))
    if native:
        (d / "tools.py").write_text(native)
    return d


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "file")
    monkeypatch.setenv("FUSED_RENDER_CONNECT_OPEN_BROWSER", "0")
    root = tmp_path / "packs"
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", str(root))
    _pack(root, "demo", {
        "name": "Demo",
        "auth": [{"kind": "api-key", "label": "Token", "identity_url": "https://demo.example/me"}],
        "tools": {"remote": REMOTE}, "privacy": "test"}, native=NATIVE)
    slack = _pack(root, "slack", {
        "name": "Slack",
        "auth": [{"kind": "api-key", "label": "Token", "identity_url": "https://slack.com/api/auth.test"}],
        "tools": {"remote": None}, "privacy": "test"})
    shutil.copy(SLACK_TOOLS, slack / "tools.py")
    for d in (tools._cache, tools._clients, tools._native, manager._flows):
        d.clear()
    manager._reconnect.clear()
    yield
    for d in (tools._cache, tools._clients, tools._native, manager._flows):
        d.clear()


class Mcp:
    """A fake MCP server: tokens in `good` are accepted, anything else is 401."""

    def __init__(self, good=("tok",), status=None):
        self.good = set(good)
        self.status = status
        self.methods = []

    def __call__(self, request):
        if request.method == "DELETE":
            return httpx.Response(204)
        if self.status:
            return httpx.Response(self.status[0], headers=self.status[1])
        if request.headers.get("authorization", "")[7:] not in self.good:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        body = json.loads(request.content)
        self.methods.append(body.get("method"))
        if "id" not in body:
            return httpx.Response(202)
        res = {}
        if body["method"] == "initialize":
            res = {"serverInfo": {"name": "Demo MCP"}}
        elif body["method"] == "tools/list":
            res = {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"},
                              "annotations": {"readOnlyHint": True}},
                             {"name": "write_note", "inputSchema": {"type": "object"}}]}
        elif body["method"] == "tools/call":
            a = body["params"]["arguments"]
            res = {"content": [{"type": "text", "text": a.get("text", "")},
                               {"type": "image", "data": "AA==", "mimeType": "image/png"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": res})


def _use(monkeypatch, handler):
    monkeypatch.setattr(tools, "_mcp_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_not_connected_lists_nothing_and_refuses_calls(monkeypatch):
    _use(monkeypatch, Mcp())
    assert tools.list_tools("demo") == []
    out = tools.call_tool("demo", "echo", {})
    assert out["ok"] is False and out["error"] == "not_connected"
    assert tools.list_tools("nope") == []


def test_list_merges_remote_and_native_and_caches(monkeypatch):
    srv = Mcp()
    _use(monkeypatch, srv)
    store.put("demo", {"kind": "api-key", "access": "tok"})
    specs = tools.list_tools("demo")
    by = {s.name: s for s in specs}
    assert list(by) == ["echo", "write_note", "local_sum", "local_fail"]
    assert by["echo"].read_only is True and by["echo"].destructive is True
    assert by["write_note"].read_only is False and by["write_note"].destructive is True
    assert by["local_sum"].read_only is True and by["local_sum"].destructive is False
    assert by["echo"].provider == "demo"
    n = len(srv.methods)
    tools.list_tools("demo")
    assert len(srv.methods) == n          # cached
    assert tools.cached_count("demo") == 4
    tools.invalidate("demo")
    assert tools.cached_count("demo") is None
    tools.list_tools("demo")
    assert len(srv.methods) > n


def test_call_remote_joins_text_and_dumps_other_content(monkeypatch):
    _use(monkeypatch, Mcp())
    store.put("demo", {"kind": "api-key", "access": "tok"})
    out = tools.call_tool("demo", "echo", {"text": "hello"})
    assert out["ok"] is True and out["is_error"] is False
    lines = out["text"].split("\n")
    assert lines[0] == "hello"
    assert json.loads(lines[1])["type"] == "image"
    assert "tok" not in out["text"]


def test_native_call_dict_and_tool_error(monkeypatch):
    _use(monkeypatch, Mcp())
    store.put("demo", {"kind": "api-key", "access": "tok"})
    out = tools.call_tool("demo", "local_sum", {"a": 2, "b": 3})
    assert out["ok"] is True
    assert json.loads(out["text"]) == {"sum": 5, "token_seen": True, "kind": "api-key"}
    bad = tools.call_tool("demo", "local_fail", {})
    assert bad == {"ok": False, "text": "demo: no_way", "content": [{"type": "text", "text": "demo: no_way"}],
                   "is_error": True}


def _oauth_record(**over):
    rec = {"kind": "oauth-pkce", "access": "old", "refresh": "r1", "expires_at": time.time() + 3600,
           "token_url": "https://demo.example/token", "client_id": "cid"}
    rec.update(over)
    return rec


def test_401_refreshes_once_and_retries(monkeypatch):
    _use(monkeypatch, Mcp(good=("new",)))
    store.put("demo", _oauth_record())
    seen = {}

    def fake_refresh(**kw):
        seen.update(kw)
        return oauth.TokenSet(access="new", refresh="r2", expires_at=time.time() + 3600,
                              token_type="Bearer", scope=None, raw={})
    monkeypatch.setattr(manager.oauth, "refresh", fake_refresh)
    out = tools.call_tool("demo", "echo", {"text": "after refresh"})
    assert out["ok"] is True and out["text"].startswith("after refresh")
    assert seen["refresh_token"] == "r1" and seen["client_id"] == "cid"
    rec = store.get("demo")
    assert rec["access"] == "new" and rec["refresh"] == "r2"
    assert manager.status("demo")["state"] == "connected"


def test_second_401_marks_reconnect(monkeypatch):
    _use(monkeypatch, Mcp(good=()))
    store.put("demo", _oauth_record())
    monkeypatch.setattr(manager.oauth, "refresh", lambda **kw: oauth.TokenSet(
        access="still-bad", refresh=None, expires_at=None, token_type="Bearer", scope=None, raw={}))
    out = tools.call_tool("demo", "echo", {"text": "x"})
    assert out["ok"] is False and "Reconnect" in out["text"] and out["error"] == "reconnect"
    assert manager.status("demo")["state"] == "reconnect"


def test_failed_refresh_marks_reconnect(monkeypatch):
    _use(monkeypatch, Mcp(good=()))
    store.put("demo", _oauth_record())

    def boom(**kw):
        raise oauth.FlowError("invalid_grant")
    monkeypatch.setattr(manager.oauth, "refresh", boom)
    out = tools.call_tool("demo", "echo", {})
    assert out["error"] == "reconnect"
    assert manager.status("demo")["state"] == "reconnect"


def test_scope_error_names_scope(monkeypatch):
    _use(monkeypatch, Mcp(status=(403, {"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="notes:write"'})))
    store.put("demo", {"kind": "api-key", "access": "tok"})
    out = tools.call_tool("demo", "write_note", {})
    assert out["ok"] is False and "notes:write" in out["text"]


def test_unknown_tool(monkeypatch):
    store.put("slack", {"kind": "api-key", "access": "xoxp"})
    out = tools.call_tool("slack", "nope", {})
    assert out["ok"] is False and out["error"] == "unknown_tool"


# -- native Slack tools ---------------------------------------------------------

class Slack:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        form = dict(httpx.QueryParams(request.content.decode()))
        self.calls.append((method, form, request.headers.get("authorization")))
        if method == "conversations.list":
            return httpx.Response(200, json={"ok": True, "channels": [
                {"id": "C0123456", "name": "general", "is_member": True, "num_members": 9,
                 "topic": {"value": "chat"}, "purpose": {"value": "bulky"}}],
                "response_metadata": {"next_cursor": ""}})
        if method == "conversations.history":
            if form["channel"] != "C0123456":
                return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
            return httpx.Response(200, json={"ok": True, "has_more": False, "messages": [
                {"ts": "1.1", "user": "U1", "text": "hi", "blocks": [{"big": True}],
                 "reactions": [{"name": "tada", "count": 2, "users": ["U2"]}]}]})
        if method == "search.messages":
            return httpx.Response(200, json={"ok": True, "messages": {"total": 1, "matches": [
                {"ts": "2.2", "user": "U1", "text": "budget", "channel": {"id": "C0123456", "name": "general"},
                 "permalink": "https://x.slack.com/p", "blocks": []}]}})
        if method == "users.list":
            return httpx.Response(200, json={"ok": True, "members": [
                {"id": "U1", "name": "ana", "profile": {"real_name": "Ana", "image_512": "..."}}]})
        if method == "chat.postMessage":
            return httpx.Response(200, json={"ok": True, "channel": form["channel"], "ts": "3.3"})
        if method == "reactions.add":
            return httpx.Response(200, json={"ok": False, "error": "already_reacted"})
        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user": "ana", "team": "acme"})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})


@pytest.fixture()
def slack(monkeypatch):
    srv = Slack()
    monkeypatch.setattr(tools, "_http", lambda: httpx.Client(transport=httpx.MockTransport(srv)))
    store.put("slack", {"kind": "api-key", "access": "xoxp-1"})
    return srv


def test_slack_tools_listed_with_flags(slack):
    by = {s.name: s for s in tools.list_tools("slack")}
    assert set(by) == {"slack_search_messages", "slack_list_channels", "slack_channel_history",
                       "slack_list_users", "slack_send_message", "slack_add_reaction"}
    assert by["slack_search_messages"].read_only is True
    assert by["slack_send_message"].read_only is False and by["slack_send_message"].destructive is False


def test_slack_history_resolves_name_and_trims(slack):
    out = tools.call_tool("slack", "slack_channel_history", {"channel": "#general", "limit": 5})
    assert out["ok"] is True
    data = json.loads(out["text"])
    assert data["channel"] == "C0123456"
    assert data["messages"] == [{"ts": "1.1", "user": "U1", "text": "hi", "reactions": {"tada": 2}}]
    assert [c[0] for c in slack.calls] == ["conversations.list", "conversations.history"]
    assert all(c[2] == "Bearer xoxp-1" for c in slack.calls)


def test_slack_search_users_send(slack):
    s = json.loads(tools.call_tool("slack", "slack_search_messages", {"query": "budget"})["text"])
    assert s["messages"][0]["channel"] == "general" and "blocks" not in s["messages"][0]
    u = json.loads(tools.call_tool("slack", "slack_list_users", {})["text"])
    assert u["users"] == [{"id": "U1", "name": "ana", "real_name": "Ana", "is_bot": False, "deleted": False}]
    m = json.loads(tools.call_tool("slack", "slack_send_message",
                                   {"channel": "C0123456", "text": "yo", "bogus": 1})["text"])
    assert m == {"ok": True, "channel": "C0123456", "ts": "3.3"}
    assert slack.calls[-1][1] == {"channel": "C0123456", "text": "yo"}


def test_slack_error_becomes_tool_error(slack):
    out = tools.call_tool("slack", "slack_add_reaction", {"channel": "C0123456", "timestamp": "1.1", "name": ":tada:"})
    assert out["ok"] is False and out["text"] == "slack: already_reacted"
    out = tools.call_tool("slack", "slack_channel_history", {"channel": "nowhere"})
    assert out["text"].startswith("slack: channel_not_found")
    out = tools.call_tool("slack", "slack_send_message", {"channel": "C0123456"})
    assert out["text"] == "slack: missing argument: text"
