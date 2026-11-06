"""The phone door (lan.py `_DOORS`, D1468): the iOS app renders the desktop's
own bots app and Tasks page, so the LAN listener forwards the DESKTOP'S routes
for a paired device — an allow-list that fails closed. These tests pin the
shape: what a paired phone may reach is device-stamped and forwarded, what it
must not reach (the Mac's screen, terminal, disk, worktrees) is a 404 whatever
the method, and `/api/claude/agent` is gated on its `action`."""
import asyncio
import json

import pytest

from fused_render import lan, lan_doctor, lan_tls


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    # The read scope's workspace: where a phone may start an agent.
    (tmp_path / "Fused" / "app" / "demo").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    lan._roots_cache = None
    lan._task_roots_cache = None
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    lan_tls._cert_by_dir.clear()
    lan_doctor.reset_cache()
    monkeypatch.setattr(lan._controller, "hostname", "studio.local")
    monkeypatch.setattr(lan._controller, "ips", ["192.168.1.20"])
    return tmp_path


class Inner:
    """An inner app that records what reached it: the path, the method, the
    device header the door stamped and the body it was handed."""

    def __init__(self):
        self.hits = []

    async def __call__(self, scope, receive, send):
        body = b""
        while True:
            m = await receive()
            if m["type"] != "http.request":
                break
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        device = next((v.decode() for k, v in scope["headers"] if k == b"x-fused-device"), None)
        self.hits.append((scope["method"], scope["path"], device, body))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})


def call(app, path, method="GET", body=None, headers=(), host=b"studio.local:1889"):
    raw = b"" if body is None else json.dumps(body).encode()
    scope = {"type": "http", "path": path, "method": method, "query_string": b"", "scheme": "https",
             "headers": [(b"host", host), (b"content-type", b"application/json"), *headers]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _paired(app_inner):
    app = lan.LanApp(app_inner)
    secret, record = lan._pair_device("", "phone")
    cookie = (b"cookie", f"{lan.COOKIE}={secret}".encode())
    return app, cookie, record["id"]


#: What the desktop's bots app and Tasks page reach on load and in a chat.
ALLOWED = [
    ("GET", "/api/bots"), ("POST", "/api/bots"), ("GET", "/api/bots/pulse"), ("GET", "/api/bots/presets"),
    ("GET", "/api/bots/setup"), ("DELETE", "/api/bots/b_1"), ("GET", "/api/bots/b_1/shot"),
    ("GET", "/api/bots/b_1/steps/3.jpg"), ("POST", "/api/bots/b_1/send"), ("POST", "/api/bots/b_1/pause"),
    ("POST", "/api/bots/b_1/clear"), ("POST", "/api/bots/b_1/settings"), ("POST", "/api/bots/b_1/routines"),
    ("POST", "/api/bots/b_1/seen"), ("POST", "/api/bots/b_1/react"), ("POST", "/api/bots/b_1/attach"),
    ("POST", "/api/bots/b_1/goto"), ("POST", "/api/claude/app-entry"), ("GET", "/api/claude-sessions/history"),
    ("GET", "/api/tasks"), ("GET", "/api/tasks/changes"), ("GET", "/api/tasks/pulse"),
    ("GET", "/api/tasks/abc/messages"), ("POST", "/api/tasks/read"), ("POST", "/api/tasks/archive"),
    ("POST", "/api/tasks/queue/decide"), ("POST", "/api/phone/tasks/new-app"), ("POST", "/api/schedule"),
    ("GET", "/api/schedule/queue"), ("POST", "/api/schedule/cancel"), ("GET", "/api/drafts"),
    ("POST", "/api/tasks/favourite"), ("POST", "/api/tasks/pr-status"), ("GET", "/api/schedule/events"),
    ("POST", "/api/schedule/events/ack"), ("POST", "/api/schedule/restore"),
    ("PUT", "/api/drafts/chat/some-key"), ("GET", "/api/ui-layout"), ("PUT", "/api/ui-layout"),
    ("GET", "/api/prefs"), ("GET", "/api/recents"), ("GET", "/api/agent-backends"), ("GET", "/api/current-apps"),
    ("POST", "/api/current-apps/ensure"), ("GET", "/api/capture"),
]

#: The Mac's screen, terminal, disk and worktrees — a 404 on the LAN by the
#: owner's call (2026-11-06), whatever the method.
REFUSED = [
    "/api/bots/b_1/takeover", "/api/bots/b_1/giveback", "/api/bots/b_1/popout", "/api/bots/b_1/dock",
    "/api/bots/b_1/wake", "/api/bots/b_1/profile", "/api/bots/b_1/reveal", "/api/bots/b_1/clone",
    "/api/bots/b_1/package", "/api/bots/b_1/export", "/api/bots/browsers", "/api/bots/browsers/x",
    "/api/bots/imessage", "/api/bots/imessage/test", "/api/bots/builds", "/api/bots/usage",
    "/api/bots/roster", "/api/bots/workspace/take", "/api/bots/workspace/yield", "/api/bot-apps",
    "/api/bot-apps/reveal", "/api/bot-apps/starters", "/api/tasks/erase", "/api/tasks/settings/x",
    "/api/capture/shot-region", "/api/current-apps/add", "/api/current-apps/archive", "/api/prefs/chromium/reopen-windows", "/api/phone/tasks", "/api/phone/bots",
    "/api/phone/bots/b_1", "/api/bots/b_1/../../bots/b_1/takeover",
]


def test_the_door_forwards_the_desktop_routes_device_stamped(home):
    inner = Inner()
    app, cookie, device_id = _paired(inner)
    for method, path in ALLOWED:
        assert call(app, path, method, {} if method != "GET" else None, headers=[cookie]) == 200, (method, path)
    assert all(d == device_id for _, _, d, _ in inner.hits)
    assert len(inner.hits) == len(ALLOWED)
    # The door stamps the device from the pairing record: a client-sent header is dropped.
    inner.hits.clear()
    call(app, "/api/bots", headers=[cookie, (b"x-fused-device", b"spoofed")])
    assert inner.hits[0][2] == device_id


def test_the_door_refuses_the_mac_side_routes(home):
    inner = Inner()
    app, cookie, _ = _paired(inner)
    for path in REFUSED:
        for method in ("GET", "POST", "DELETE"):
            assert call(app, path, method, {} if method != "GET" else None, headers=[cookie]) == 404, (method, path)
    # A desktop op that is allowed only as a POST is not reachable by another method.
    assert call(app, "/api/bots/b_1/send", "GET", headers=[cookie]) == 404
    assert call(app, "/api/prefs", "POST", {}, headers=[cookie]) == 404
    assert inner.hits == []


def test_unpaired_gets_nothing(home):
    inner = Inner()
    app = lan.LanApp(inner)
    for method, path in ALLOWED:
        assert call(app, path, method, {} if method != "GET" else None) in (401, 403, 404), (method, path)
    assert inner.hits == []


def test_the_agent_route_is_gated_on_its_action(home):
    inner = Inner()
    app, cookie, _ = _paired(inner)
    folder = str(home / "Fused" / "app" / "demo")
    for action in ("start", "send", "poll", "cancel", "history", "cards", "decide", "shots_dir", "tasks"):
        assert call(app, "/api/claude/agent", "POST", {"action": action, "file": folder}, headers=[cookie]) == 200, action
    # The body reaches the inner app intact (the door read it to look at `action`).
    assert json.loads(inner.hits[-1][3]) == {"action": "tasks", "file": folder}
    inner.hits.clear()
    for action in ("snapshot_plan", "snapshot_revert", "snapshots", "terminal_command", "set_mode", "app_state",
                   "canvas_shot", "", None, [], {}, ["start"], 7):
        body = {"action": action} if action is not None else {}
        assert call(app, "/api/claude/agent", "POST", body, headers=[cookie]) == 404, action
    assert call(app, "/api/claude/worktrees", "POST", {"action": "list"}, headers=[cookie]) == 200
    assert call(app, "/api/claude/worktrees", "POST", {"action": "create"}, headers=[cookie]) == 404
    assert call(app, "/api/claude/worktrees", "POST", {"action": "remove"}, headers=[cookie]) == 404
    assert inner.hits == [h for h in inner.hits if h[1] == "/api/claude/worktrees"]


def test_every_door_row_matches_something_it_names(home):
    """A row nobody can reach is a lie in the security table."""
    for pat, methods, gate in lan._DOORS:
        assert methods, pat.pattern
        assert gate is None or (isinstance(gate[0], str) and gate[1]), pat.pattern


def test_a_phone_starts_agents_only_in_the_read_scope(home):
    """`start`/`send` run an agent in `file`'s folder and grant it `read_dirs`;
    a scheduled task runs in `target`. Each must be the workspace, a linked
    app, SHOTS or a task's own folder — never `~`, `/` or a stranger."""
    inner = Inner()
    app, cookie, _ = _paired(inner)
    agent = "/api/claude/agent"
    folder = str(home / "Fused" / "app" / "demo")
    assert call(app, agent, "POST", {"action": "start", "file": folder, "message": "hi"}, headers=[cookie]) == 200
    assert call(app, agent, "POST", {"action": "start", "file": folder + "/index.html"}, headers=[cookie]) == 200
    assert call(app, agent, "POST", {"action": "start", "file": folder, "read_dirs": json.dumps([folder])},
                headers=[cookie]) == 200
    # A send without a file names its run by id; the inner route validates that.
    assert call(app, agent, "POST", {"action": "send", "run_id": "r1", "message": "more"}, headers=[cookie]) == 200
    assert call(app, "/api/schedule", "POST", {"target": folder, "message": "later"}, headers=[cookie]) == 200
    assert len(inner.hits) == 5
    inner.hits.clear()
    for body in ({"action": "start", "file": "/"}, {"action": "start", "file": "~"},
                 {"action": "start", "file": str(home / "elsewhere")},
                 {"action": "send", "run_id": "r1", "file": "/etc"},
                 {"action": "start", "file": folder, "read_dirs": json.dumps(["/"])},
                 {"action": "start", "file": folder, "read_dirs": "not json"},
                 {"action": "start", "file": folder, "read_dirs": json.dumps({"dir": folder})},
                 {"action": "start", "file": ["x"]}):
        assert call(app, agent, "POST", body, headers=[cookie]) == 404, body
    for body in ({"target": "/"}, {"target": str(home)}, {"target": str(home / "elsewhere")}, {"target": ["x"]}):
        assert call(app, "/api/schedule", "POST", body, headers=[cookie]) == 404, body
    # The actions with no folder in their body are not scoped.
    assert call(app, agent, "POST", {"action": "poll", "file": "/etc"}, headers=[cookie]) == 200
    assert call(app, "/api/schedule", headers=[cookie]) == 200
    assert len(inner.hits) == 2
