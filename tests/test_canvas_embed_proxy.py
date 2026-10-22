"""The custom-environment embed proxy (canvas_embed_proxy.py): a real
loopback listener in front of a fake upstream, driven with httpx.

What must hold: every forwarded request carries the custom env's bearer and
nothing else that could override it; only the workspace's own frame gets
through (Host, Sec-Fetch-Site, Origin); upstream redirects come back
pointing at the proxy; cookies and framing policies are not relayed; a 401
forgets the token so the next request fetches a fresh one.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from fused_render import canvas_embed_proxy as mod


class _Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []
    status = 200

    def _answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        # header names as the proxy sent them, lowercased: HTTP compares
        # them case-insensitively and the ASGI side relays them lowercase
        type(self).seen.append({"path": self.path, "method": self.command,
                                "headers": {k.lower(): v for k, v in self.headers.items()},
                                "body": body})
        if self.path.startswith("/hop"):
            self.send_response(200)
            self.send_header("Connection", "X-Up-Hop")
            self.send_header("X-Up-Hop", "1")
            self.send_header("X-Up-End", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", f"{self.server.origin}/workbench/x")
            self.end_headers()
            return
        self.send_response(type(self).status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "session=1; Domain=upstream.example; Secure")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Strict-Transport-Security", "max-age=1")
        self.send_header("X-Keep", "yes")
        self.end_headers()
        self.wfile.write(b"hello " + self.path.encode())

    do_GET = do_POST = _answer

    def log_message(self, *args):  # noqa: A002
        return


@pytest.fixture
def upstream():
    server = HTTPServer(("127.0.0.1", 0), _Upstream)
    server.origin = f"http://127.0.0.1:{server.server_address[1]}"
    _Upstream.seen = []
    _Upstream.status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(upstream):
    tokens = iter(["tok-1", "tok-2", "tok-3"])
    calls = []

    def source():
        calls.append(1)
        return next(tokens)

    ctl = mod.Controller()
    ctl.apply(upstream.origin, source)
    assert ctl.wait_ready(), ctl.error
    ctl.calls = calls
    yield ctl
    ctl.apply(None, None)


def _get(ctl, path, **headers):
    base = {"sec-fetch-site": "same-origin"}
    base.update(headers)
    with httpx.Client(base_url=ctl.url(), follow_redirects=False) as c:
        return c.get(path, headers=base)


def test_forwards_with_the_custom_bearer_and_relays_the_answer(proxy):
    r = _get(proxy, "/workbench/me/canvas?fused_embed_auth=1&fused_embed_v=3",
             authorization="Bearer attacker", cookie="a=b")
    assert r.status_code == 200
    assert r.text == "hello /workbench/me/canvas?fused_embed_auth=1&fused_embed_v=3"
    seen = _Upstream.seen[-1]
    assert seen["headers"]["authorization"] == "Bearer tok-1"
    # the inbound Authorization never overrides ours; Host is upstream's own
    assert seen["headers"]["host"] == proxy.upstream.removeprefix("http://")
    # loopback cookies (any port's) are not the deployment's to see
    assert "cookie" not in seen["headers"]
    assert r.headers["x-keep"] == "yes"
    for dropped in ("set-cookie", "x-frame-options", "strict-transport-security"):
        assert dropped not in r.headers


def test_headers_a_connection_header_names_are_dropped_both_ways(proxy):
    r = _get(proxy, "/hop", connection="X-Down-Hop", **{"x-down-hop": "1", "x-down-end": "1"})
    assert r.status_code == 200
    seen = _Upstream.seen[-1]["headers"]
    assert "x-down-hop" not in seen and seen["x-down-end"] == "1"
    assert "x-up-hop" not in r.headers and r.headers["x-up-end"] == "1"


def test_token_is_cached_and_forgotten_on_401(proxy):
    _get(proxy, "/a")
    _get(proxy, "/b")
    assert len(proxy.calls) == 1
    _Upstream.status = 401
    r = _get(proxy, "/c")
    assert r.status_code == 401
    _Upstream.status = 200
    _get(proxy, "/d")
    assert _Upstream.seen[-1]["headers"]["authorization"] == "Bearer tok-2"
    assert len(proxy.calls) == 2


def test_a_credentials_change_drops_the_cached_token(upstream):
    """A sign-out, sign-in or account switch changes the store's stamp: the
    next request fetches anew instead of riding the old token for the TTL."""
    tokens = iter(["tok-1", "tok-2"])
    stamp = ["login-a"]
    ctl = mod.Controller()
    ctl.apply(upstream.origin, lambda: next(tokens), lambda: stamp[0])
    assert ctl.wait_ready(), ctl.error
    try:
        _get(ctl, "/a")
        _get(ctl, "/b")
        assert _Upstream.seen[-1]["headers"]["authorization"] == "Bearer tok-1"
        stamp[0] = "login-b"
        _get(ctl, "/c")
        assert _Upstream.seen[-1]["headers"]["authorization"] == "Bearer tok-2"
    finally:
        ctl.apply(None, None)


def test_posts_forward_the_body(proxy):
    with httpx.Client(base_url=proxy.url()) as c:
        r = c.post("/api/checkpoints", content=b'{"x":1}',
                   headers={"sec-fetch-site": "same-origin", "content-type": "application/json"})
    assert r.status_code == 200
    seen = _Upstream.seen[-1]
    assert seen["method"] == "POST" and seen["body"] == b'{"x":1}'


def test_upstream_redirects_are_rewritten_to_the_proxy(proxy):
    r = _get(proxy, "/redirect")
    assert r.status_code == 302
    assert r.headers["location"] == f"{proxy.url()}/workbench/x"


def test_cross_site_and_foreign_origins_are_refused(proxy):
    assert _get(proxy, "/x", **{"sec-fetch-site": "cross-site"}).status_code == 403
    assert _get(proxy, "/x", origin="https://evil.example").status_code == 403
    assert _get(proxy, "/x", referer="https://evil.example/page").status_code == 403
    # the workspace page framing the proxy: same-site (another loopback port)
    assert _get(proxy, "/x", **{"sec-fetch-site": "same-site"},
                origin="http://127.0.0.1:8765").status_code == 200
    assert _get(proxy, "/x", **{"sec-fetch-site": "none"}).status_code == 200
    assert not any(s["path"] == "/x" and s["headers"].get("origin") == "https://evil.example"
                   for s in _Upstream.seen)


def test_a_foreign_host_header_is_refused(proxy):
    r = _get(proxy, "/x", host=f"evil.example:{proxy.port}")
    assert r.status_code == 403
    r = _get(proxy, "/x", host="127.0.0.1:1")
    assert r.status_code == 403


def test_status_frames_the_proxy_only_for_custom_while_it_runs(upstream, monkeypatch):
    """`embed_url` is what the workspace frames: the proxy's origin for the
    custom env while the proxy is up, the workbench origin otherwise and for
    the hosted envs always; `base_url` stays the real origin (the accounts
    page prints it)."""
    from fused_render import canvases

    ctl = mod.Controller()
    monkeypatch.setattr(mod, "controller", ctl)
    monkeypatch.setattr(canvases, "_apply_embed_proxy", lambda: None)
    assert canvases.embed_base_url("custom") == canvases.workbench_base_url("custom")
    ctl.apply(upstream.origin, lambda: "t")
    assert ctl.wait_ready()
    try:
        assert canvases.embed_base_url("custom") == ctl.url()
        assert canvases.embed_base_url("prod") == canvases.workbench_base_url("prod")
        envs = canvases.api_canvases_status()["envs"]
        assert envs["custom"]["embed_url"] == ctl.url()
        assert envs["custom"]["base_url"] == canvases.workbench_base_url("custom")
        assert envs["prod"]["embed_url"] == envs["prod"]["base_url"]
    finally:
        ctl.apply(None, None)
    assert canvases.embed_base_url("custom") == canvases.workbench_base_url("custom")


def test_apply_restarts_on_a_new_upstream_and_stops_on_none(upstream):
    ctl = mod.Controller()
    ctl.apply(upstream.origin, lambda: "t")
    assert ctl.wait_ready()
    first = ctl.port
    ctl.apply(upstream.origin, lambda: "t")
    assert ctl.port == first  # idempotent
    ctl.apply(upstream.origin + "/", lambda: "t")
    assert ctl.wait_ready() and ctl.port == first  # a trailing slash is the same origin
    ctl.apply("http://127.0.0.1:1", lambda: "t")
    assert ctl.wait_ready() and ctl.port != first
    ctl.apply(None, None)
    assert not ctl.running and ctl.url() is None
