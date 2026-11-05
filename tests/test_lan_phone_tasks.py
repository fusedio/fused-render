"""The LAN listener's phone tasks door (fused_render/lan.py, docs/phone-tasks.md):
`/tasks` and `/tasks/<key>` serve the lan page for a paired device, the
`/api/phone/tasks` prefix is forwarded with `X-Fused-Device` stamped from the
pairing record (a client-sent one dropped), and the desktop's `/api/tasks` and
`/api/claude/agent` stay a 404. Driven through `LanApp.__call__` with a
recording inner app, the test_lan_phone_bots.py pattern."""
import json

import pytest

from fused_render import lan
from test_lan_phone_bots import Inner, call, home, paired_cookie  # noqa: F401 — the fixture and helpers


def test_unpaired_is_refused(home):
    inner = Inner()
    app = lan.LanApp(inner)
    for path in ("/api/phone/tasks", "/api/phone/tasks/abc/send"):
        status, body, _ = call(app, path)
        assert status == 401 and json.loads(body) == {"error": "not paired"}
    for path in ("/tasks", "/tasks/pending:abc"):
        status, body, _ = call(app, path)  # a page: the how-to-pair page, as at / and /bots
        assert status == 403 and b"Scan to pair" in body, path
    assert inner.scopes == []


def test_paired_tasks_pages_serve_the_lan_page(home):
    cookie, _ = paired_cookie()
    app = lan.LanApp(Inner())
    for path in ("/tasks", "/tasks/6a96b827-1111-2222-3333-444444444444", "/tasks/pending:2026-11-05T10-00-00-abc"):
        status, body, _ = call(app, path, headers=[cookie])
        assert status == 200 and b"lan page" in body, path
    assert call(app, "/tasks/a/b", headers=[cookie])[0] == 404  # one segment only
    assert call(app, "/tasks", method="POST", headers=[cookie])[0] == 404
    assert call(app, "/tasksx", headers=[cookie])[0] == 404


def test_phone_tasks_forwarded_with_the_device_from_the_record(home):
    cookie, record = paired_cookie()
    inner = Inner()
    app = lan.LanApp(inner)
    spoof = (b"x-fused-device", b"someone-else")
    for method, path in (("GET", "/api/phone/tasks"), ("GET", "/api/phone/tasks/s1?since=3"),
                         ("POST", "/api/phone/tasks/s1/send"), ("POST", "/api/phone/tasks"),
                         ("POST", "/api/phone/tasks/pending:e1/cancel")):
        status, body, scope = call(app, path, method=method, body=b'{"text":"hi"}' if method == "POST" else b"",
                                   headers=[cookie, spoof, (b"x-fused", b"1")])
        assert status == 200, (method, path)
        seen = json.loads(body)
        devices = [v for k, v in seen["headers"] if k.lower() == "x-fused-device"]
        assert devices == [record["id"]], devices
        assert ["x-fused", "1"] in seen["headers"]
        if method == "POST":
            assert seen["body"] == '{"text":"hi"}'
        assert spoof in scope["headers"]  # the caller's scope is left as it arrived
    assert len(inner.scopes) == 5
    # DELETE and PUT are not part of this door (every verb is a POST).
    assert call(app, "/api/phone/tasks/s1", method="DELETE", headers=[cookie])[0] == 404
    assert call(app, "/api/phone/tasks/s1", method="PUT", headers=[cookie])[0] == 404


def test_desktop_tasks_api_stays_closed(home):
    cookie, _ = paired_cookie()
    inner = Inner()
    app = lan.LanApp(inner)
    for method, path in (("GET", "/api/tasks"), ("POST", "/api/tasks/erase"), ("POST", "/api/tasks/s1/send"),
                         ("POST", "/api/claude/agent"), ("GET", "/api/claude-sessions/history"),
                         ("GET", "/api/tasks/pulse"), ("GET", "/api/phone/tasksx"), ("GET", "/api/phone")):
        assert call(app, path, method=method, headers=[cookie, (b"x-fused", b"1")])[0] == 404, (method, path)
    assert inner.scopes == []


def test_dot_segments_never_forward(home):
    cookie, _ = paired_cookie()
    inner = Inner()
    app = lan.LanApp(inner)
    for method, path in (("POST", "/api/phone/tasks/../../tasks/erase"), ("GET", "/api/phone/tasks/.."),
                         ("POST", "/api/phone/tasks/s1/./send"), ("GET", "/api/phone/tasks/./s1")):
        assert call(app, path, method=method, headers=[cookie, (b"x-fused", b"1")])[0] == 404, (method, path)
    assert inner.scopes == []
    status, body, _ = call(app, "/tasks/..", headers=[cookie])  # only ever the lan page itself
    assert status == 200 and b"lan page" in body
