"""Tests for `fused_render/templates/shared/fused_connect.py` — the stdlib
Python client for `/api/connect` (SPEC-connect-oauth.md §11).

A tiny stdlib HTTP server stands in for the app's routes, so the real
`urllib` round trip (headers, JSON bodies, the 409 mapping) is what runs.
Loaded the way production loads it: the shared dir on `sys.path`, then
`import fused_connect`.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_SHARED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import fused_connect  # noqa: E402 - path seeded above, matching production

_PROVIDERS = [
    {"key": "notion", "name": "Notion", "state": "connected", "account": "Ada", "tools": 2},
    {"key": "slack", "name": "Slack", "state": "disconnected", "account": None, "tools": 0},
]
_TOOLS = [{"name": "echo", "description": "Echo", "input_schema": {"type": "object"},
           "read_only": True, "destructive": False}]


class _Api(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(("GET", self.path, dict(self.headers), None))
        if self.path == "/api/connect":
            return self._send(200, _PROVIDERS)
        if self.path == "/api/connect/notion/tools":
            return self._send(200, _TOOLS)
        return self._send(404, {"error": "unknown provider"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        self.seen.append(("POST", self.path, dict(self.headers), body))
        if self.headers.get("X-Fused") != "1":
            return self._send(403, {"error": "missing X-Fused"})
        if self.path == "/api/connect/notion/tools/call":
            text = json.dumps(body["args"])
            return self._send(200, {"ok": True, "text": text,
                                    "content": [{"type": "text", "text": text}], "is_error": False})
        if self.path == "/api/connect/slack/tools/call":
            return self._send(409, {"error": "slack is not connected"})
        return self._send(404, {"error": "unknown provider"})


@pytest.fixture
def api(monkeypatch):
    _Api.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", f"http://127.0.0.1:{server.server_address[1]}")
    yield _Api
    server.shutdown()
    server.server_close()


def test_providers_lists_every_provider(api):
    assert fused_connect.providers() == _PROVIDERS
    assert api.seen[0][:2] == ("GET", "/api/connect")


def test_tools_reads_the_provider_roster(api):
    assert fused_connect.tools("notion") == _TOOLS
    assert api.seen[0][:2] == ("GET", "/api/connect/notion/tools")


def test_call_posts_name_and_args_with_the_write_marker(api):
    r = fused_connect.call("notion", "echo", {"q": "hi"})
    assert r["ok"] is True and r["is_error"] is False
    assert json.loads(r["text"]) == {"q": "hi"}
    method, path, headers, body = api.seen[0]
    assert (method, path) == ("POST", "/api/connect/notion/tools/call")
    assert headers.get("X-Fused") == "1"
    assert headers.get("Content-Type") == "application/json"
    assert body == {"name": "echo", "args": {"q": "hi"}}


def test_call_on_an_unconnected_provider_says_where_to_connect(api):
    with pytest.raises(RuntimeError) as e:
        fused_connect.call("slack", "post", {})
    msg = str(e.value)
    assert "slack is not connected" in msg
    assert "Preferences" in msg and "Connections" in msg


def test_other_http_errors_carry_the_server_sentence(api):
    with pytest.raises(RuntimeError, match="unknown provider"):
        fused_connect.tools("nope")


def test_no_origin_is_a_readable_error(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    with pytest.raises(RuntimeError, match="FUSED_RENDER_ORIGIN"):
        fused_connect.providers()


def test_loads_its_own_appenv_not_a_shadowing_one(tmp_path, monkeypatch):
    (tmp_path / "appenv.py").write_text("MARKER = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert callable(fused_connect.appenv.origin)
    assert os.path.dirname(fused_connect.appenv.__file__) == _SHARED_DIR
