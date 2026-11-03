"""`/api/claude/agent/ws` (server/routers/claude_agent.py): the chat's agent and
artifacts calls over one socket. Transport only, so the bodies must equal the
POST's, and the pool lanes and budgets must not move."""
import json
import threading
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fused_render import calls as shell_calls
from fused_render.claude_agent import pool
from fused_render.server import create_app
from fused_render.server.routers import claude_agent as router

from tests.test_claude_agent_api import stub  # noqa: F401  (fixture)

URL = "ws://127.0.0.1/api/claude/agent/ws"
ORIGIN = {"origin": "http://127.0.0.1"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)), base_url="http://127.0.0.1")


def _call(ws, **msg):
    ws.send_text(json.dumps(msg))
    return json.loads(ws.receive_text())


def test_foreign_origin_refused(client):
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(URL, headers={"origin": "http://evil.example"}):
            pass
    assert ei.value.code == 1008


def test_missing_origin_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(URL):
            pass


@pytest.mark.parametrize("action", ["poll", "send"])
def test_ws_body_equals_post_body(client, stub, action):  # noqa: F811
    def main(action="", file="", text=""):
        return {"action": action, "n": 3, "nested": {"a": [1, 2]}, "é": "ü"}
    stub["install"](main)
    post = client.post("/api/claude/agent", json={"action": action},
                       headers={"X-Fused": "1"})
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        r = _call(ws, id=7, route="agent", headers={}, body={"action": action})
    assert r["id"] == 7 and r["status"] == post.status_code == 200
    assert r["body"] == post.json()


def test_ws_reply_nests_body_with_colliding_keys(client, stub):  # noqa: F811
    def main(action="", file=""):
        return {"id": "inner", "status": "inner-status", "body": 1}
    stub["install"](main)
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        r = _call(ws, id=1, route="agent", headers={}, body={"action": "poll"})
    assert r == {"id": 1, "status": 200,
                 "body": {"id": "inner", "status": "inner-status", "body": 1}}


def test_unknown_action_is_400_reply_not_close(client, stub):  # noqa: F811
    stub["install"](lambda action="", file="": {"ok": True})
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        r = _call(ws, id=1, route="agent", headers={}, body={"action": "nope"})
        assert r["status"] == 400 and "unknown claude agent action" in r["body"]["error"]
        r = _call(ws, id=2, route="bogus", headers={}, body={})
        assert r["status"] == 400 and r["id"] == 2
        ws.send_text("[1]")
        assert json.loads(ws.receive_text())["status"] == 400
        r = _call(ws, id=3, route="agent", headers={}, body={"action": "poll"})
        assert r["status"] == 200 and r["body"] == {"ok": True}


def test_handler_exception_is_a_500_body_like_post(client, stub):  # noqa: F811
    def main(action="", file=""):
        raise ValueError("bad target")
    stub["install"](main)
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        r = _call(ws, id=1, route="agent", headers={}, body={"action": "poll"})
    assert r["status"] == 500 and r["body"]["error"]["type"] == "ValueError"


def test_500_reply_carries_err_id(client, monkeypatch):
    async def boom(body, call):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(router._WS_ROUTES, "agent", ("/api/claude/agent", boom))
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        r = _call(ws, id=1, route="agent", headers={}, body={"action": "poll"})
        assert r["status"] == 500 and "err_id" in r["body"]["error"]
        assert _call(ws, id=2, route="artifacts", headers={}, body={})["id"] == 2


def test_504_budget_shape(client, stub, monkeypatch):  # noqa: F811
    def main(action="", file=""):
        time.sleep(0.4)
        return {}
    stub["install"](main)
    monkeypatch.setattr(pool, "budget", lambda action: 0.05)
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        r = _call(ws, id=1, route="agent", headers={}, body={"action": "poll"})
    assert r["status"] == 504 and r["body"]["error"]["type"] == "Timeout"


def test_call_log_record(client, stub, monkeypatch):  # noqa: F811
    stub["install"](lambda action="", file="": {"ok": True})
    began, finished = [], []
    real_begin, real_finish = shell_calls.begin_from, shell_calls.finish

    def begin(path, method, headers, touched=None):
        rec = real_begin(path, method, headers, touched)
        began.append((path, method, rec))
        return rec

    def finish(call, **kw):
        finished.append((call, kw))
        return real_finish(call, **kw)

    monkeypatch.setattr(shell_calls, "begin_from", begin)
    monkeypatch.setattr(shell_calls, "finish", finish)
    hdrs = {"X-Fused-Page": router.claude_agent.CLAUDE_PAGE_ID}
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        _call(ws, id=1, route="agent", headers=hdrs, body={"action": "poll"})
    assert began[0][:2] == ("/api/claude/agent", "WS")
    call, kw = finished[-1]
    assert kw["status"] == 200 and kw["outcome"] is None
    if call is not None:
        assert call["first_party"]


def test_cancel_uses_control_pool_lane(client, stub, monkeypatch):  # noqa: F811
    lanes = []
    real = pool.pool_for
    monkeypatch.setattr(pool, "pool_for", lambda a: (lanes.append(a), real(a))[1])
    stub["install"](lambda action="", file="", run_id="": {"ok": True})
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        _call(ws, id=1, route="agent", headers={}, body={"action": "cancel", "run_id": "r"})
    assert "cancel" in lanes
    assert pool.pool_for("cancel") is pool.CONTROL_POOL
    assert pool.pool_for("poll") is not pool.CONTROL_POOL


def test_budgets_unchanged():
    assert pool.budget("poll") == 60
    assert pool.budget("cancel") == 15
    assert pool.budget("send") == pool.budget("start") == 60


def test_artifacts_route(client, monkeypatch):
    from fused_render.claude_agent import artifacts
    monkeypatch.setattr(artifacts, "main", lambda action="", file="", session_id="": {"artifacts": []})
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        r = _call(ws, id=9, route="artifacts", headers={},
                  body={"action": "list", "file": "/x", "session_id": "s"})
    assert r == {"id": 9, "status": 200, "body": {"artifacts": []}}


def test_hangup_does_not_cancel_inflight(client, stub):  # noqa: F811
    started, release, done = threading.Event(), threading.Event(), threading.Event()

    def main(action="", file=""):
        started.set()
        release.wait(5)
        done.set()
        return {"ok": True}
    stub["install"](main)
    with client.websocket_connect(URL, headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"id": 1, "route": "agent", "headers": {},
                                 "body": {"action": "send"}}))
        assert started.wait(5)
    release.set()
    assert done.wait(5)
