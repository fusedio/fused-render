"""Tests for fused.ai (SPEC RH-11): the /api/ai relay to a local
OpenAI-compatible proxy, the runtime surface that calls it, and the
`ai_base_url` preference resolution (shell/prefs.py).

The relay is driven through module-level `_ai_relay` with the httpx hop mocked
(the "avoid starlette TestClient" discipline of test_server_fs_write.py) — no
test ever talks to a real proxy. The runtime checks are string-contract checks
over the shipped static/runtime.js, like test_runtime_cancellation.py.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

import fused_render
from fused_render import server
from fused_render.export import plan_export
from fused_render.shell import prefs

_STATIC = Path(fused_render.__file__).parent / "static"
RUNTIME = (_STATIC / "runtime.js").read_text(encoding="utf-8")


def _relay(body):
    return asyncio.run(server._ai_relay(body))


def _data(resp) -> dict:
    return json.loads(bytes(resp.body))


class _FakeClient:
    """Stands in for httpx.AsyncClient: returns a canned response, or raises."""

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.requests = []  # (url, json_payload) of every post

    def __call__(self, *args, **kwargs):  # the AsyncClient(...) constructor call
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        self.requests.append((url, json))
        if self._exc is not None:
            raise self._exc
        return self._response


def _proxy_ok(monkeypatch, payload, status=200):
    fake = _FakeClient(response=httpx.Response(
        status, json=payload) if isinstance(payload, dict) else httpx.Response(
        status, text=payload))
    monkeypatch.setattr(server.httpx, "AsyncClient", fake)
    return fake


_COMPLETION = {
    "choices": [{"message": {"role": "assistant", "content": "hi there"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    "model": "claude-haiku-4-5-20251001",
}


# -- happy path -----------------------------------------------------------------


def test_relay_happy_path(monkeypatch):
    fake = _proxy_ok(monkeypatch, _COMPLETION)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 200
    data = _data(resp)
    assert data["ok"] is True
    assert data["result"]["text"] == "hi there"
    assert data["result"]["model"] == "claude-haiku-4-5-20251001"
    assert data["result"]["usage"]["completion_tokens"] == 2
    # One POST to the proxy's chat/completions with the default model and the
    # medium effort default (4096 tokens), user message only (no system prompt).
    (url, payload), = fake.requests
    assert url.endswith("/v1/chat/completions")
    assert payload["model"] == server._AI_DEFAULT_MODEL
    assert payload["max_tokens"] == 4096
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


def test_relay_options_reach_the_proxy(monkeypatch):
    fake = _proxy_ok(monkeypatch, _COMPLETION)
    _relay({"prompt": "hello", "system_prompt": "be terse",
            "model": "claude-sonnet-5", "effort": "high"})
    (_, payload), = fake.requests
    assert payload["model"] == "claude-sonnet-5"
    assert payload["max_tokens"] == 16384  # effort: high
    assert payload["messages"][0] == {"role": "system", "content": "be terse"}
    assert payload["messages"][1] == {"role": "user", "content": "hello"}


def test_relay_explicit_max_tokens_beats_effort(monkeypatch):
    fake = _proxy_ok(monkeypatch, _COMPLETION)
    _relay({"prompt": "hello", "effort": "low", "max_tokens": 99})
    (_, payload), = fake.requests
    assert payload["max_tokens"] == 99


# -- bad requests ---------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {},                       # missing prompt
    {"prompt": ""},           # empty
    {"prompt": "   "},        # whitespace-only
    {"prompt": 42},           # wrong type
])
def test_relay_rejects_bad_prompt(monkeypatch, body):
    fake = _proxy_ok(monkeypatch, _COMPLETION)
    resp = _relay(body)
    assert resp.status_code == 400
    data = _data(resp)
    assert data["ok"] is False
    assert data["error"]["type"] == "bad_request"
    assert fake.requests == []  # never reached the proxy


def test_relay_rejects_unknown_effort_and_bad_max_tokens(monkeypatch):
    fake = _proxy_ok(monkeypatch, _COMPLETION)
    for body in ({"prompt": "x", "effort": "extreme"},
                 {"prompt": "x", "max_tokens": 0},
                 {"prompt": "x", "max_tokens": True},
                 {"prompt": "x", "max_tokens": "many"}):
        resp = _relay(body)
        assert resp.status_code == 400
        assert _data(resp)["error"]["type"] == "bad_request"
    assert fake.requests == []


# -- proxy failures -------------------------------------------------------------


def test_relay_proxy_down_is_ai_unavailable(monkeypatch):
    fake = _FakeClient(exc=httpx.ConnectError("refused"))
    monkeypatch.setattr(server.httpx, "AsyncClient", fake)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    data = _data(resp)
    assert data["error"]["type"] == "ai_unavailable"
    # The message names the base URL so the user knows what to start.
    assert prefs.ai_base_url() in data["error"]["message"]


def test_relay_proxy_non_200_is_ai_error(monkeypatch):
    _proxy_ok(monkeypatch, {"error": "no such model"}, status=404)
    resp = _relay({"prompt": "hello"})
    assert resp.status_code == 502
    data = _data(resp)
    assert data["error"]["type"] == "ai_error"
    assert "404" in data["error"]["message"]
    assert "no such model" in data["error"]["message"]


def test_relay_unexpected_shape_is_ai_error(monkeypatch):
    _proxy_ok(monkeypatch, {"choices": []})
    resp = _relay({"prompt": "hello"})
    assert _data(resp)["error"]["type"] == "ai_error"


# -- base URL resolution ----------------------------------------------------------


def test_ai_base_url_default_env_and_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_AI_BASE_URL", raising=False)
    assert prefs.ai_base_url() == prefs.DEFAULT_AI_BASE_URL
    # Persisted pref beats the default.
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "prefs.json").write_text(
        json.dumps({"ai_base_url": "http://127.0.0.1:9999"}), encoding="utf-8")
    assert prefs.ai_base_url() == "http://127.0.0.1:9999"
    # The env var beats the pref (same precedence as FUSED_RENDER_ENGINE).
    monkeypatch.setenv("FUSED_RENDER_AI_BASE_URL", "http://127.0.0.1:1234")
    assert prefs.ai_base_url() == "http://127.0.0.1:1234"


def test_relay_uses_configured_base_url(monkeypatch):
    fake = _proxy_ok(monkeypatch, _COMPLETION)
    monkeypatch.setenv("FUSED_RENDER_AI_BASE_URL", "http://127.0.0.1:4242/")
    _relay({"prompt": "hello"})
    (url, _), = fake.requests
    assert url == "http://127.0.0.1:4242/v1/chat/completions"


# -- runtime surface --------------------------------------------------------------


def test_runtime_ships_ai():
    assert "function ai(prompt, opts)" in RUNTIME
    assert '"/api/ai"' in RUNTIME
    assert "ai," in RUNTIME  # registered on window.fused


def test_runtime_ai_rejects_empty_prompt_client_side():
    # The empty-prompt guard runs before any fetch, tagged bad_request like the
    # server's own rejection.
    assert 'err.type = "bad_request"' in RUNTIME


# -- export stance -----------------------------------------------------------------


def test_export_rejects_ai(tmp_path):
    html = "<script>fused.ai('summarize this');</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("fused.ai() is not supported on a hosted page" in e
               for e in plan.errors)
