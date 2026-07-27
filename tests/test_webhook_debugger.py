"""Tests for the webhook_debugger capture daemon (templates/webhook_debugger/
server.py). Every test redirects the daemon's state/cache dir into tmp_path via
FUSED_WEBHOOK_HOME and tears the daemon down with /quit, so no daemon leaks past
a test (mirrors tests/test_daemon_reaper.py hygiene).
"""
import http.client
import importlib.util
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "fused_render", "templates", "webhook_debugger", "server.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("webhook_server", os.path.abspath(SERVER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_descriptor(dest, **fields):
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(fields, f)
    return str(dest)


def _control(info, path):
    sep = "&" if "?" in path else "?"
    url = f"http://127.0.0.1:{info['control_port']}{path}{sep}t={info['token']}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.load(r)


def _control_post(info, path, payload):
    url = f"http://127.0.0.1:{info['control_port']}{path}?t={info['token']}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.load(r)


def _fire(capture_port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", capture_port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    out = resp.read()
    conn.close()
    return resp.status, out


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_WEBHOOK_HOME", str(tmp_path / "wh"))
    return tmp_path


@pytest.fixture()
def daemon(home, tmp_path):
    mod = _load_module()
    port = _free_port()
    descriptor = _write_descriptor(tmp_path / "d.webhook", name="Test", path="/hook",
                                   capture_port=port)
    started = []

    def ensure(file=descriptor):
        info = mod.main(action="ensure", file=file)
        assert "error" not in info, info
        if info not in started:
            started.append(info)
        return info

    info = ensure()
    yield {"mod": mod, "info": info, "capture_port": port, "descriptor": descriptor,
           "ensure": ensure}
    seen = set()
    for entry in started:
        if entry["control_port"] in seen:
            continue
        seen.add(entry["control_port"])
        try:
            _control(entry, "/quit")
        except Exception:
            pass
    time.sleep(0.2)


def _wait_count(info, target, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, ping = _control(info, "/ping")
        if ping["count"] >= target:
            return ping
        time.sleep(0.05)
    return ping


def test_ensure_spawns_then_reused(daemon):
    info = daemon["info"]
    assert info["reused"] is False
    assert info["capture_port"] == daemon["capture_port"]
    again = daemon["ensure"]()
    assert again["reused"] is True
    assert again["control_port"] == info["control_port"]
    assert again["token"] == info["token"]


def test_malformed_descriptor(daemon, tmp_path):
    bad = tmp_path / "bad.webhook"
    bad.write_text("{not json", encoding="utf-8")
    out = daemon["mod"].main(action="ensure", file=str(bad))
    assert "error" in out


def test_ping_no_token_required_but_others_403(daemon):
    info = daemon["info"]
    with urllib.request.urlopen(f"http://127.0.0.1:{info['control_port']}/ping", timeout=5) as r:
        assert r.status == 200
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{info['control_port']}/api/requests", timeout=5)
        assert False, "expected 403 without token"
    except urllib.error.HTTPError as e:
        assert e.code == 403
    # capture works with no token at all
    status, _ = _fire(daemon["capture_port"], "POST", "/hook", b"{}")
    assert status == 200


def test_captures_methods_paths_and_bodies(daemon):
    port = daemon["capture_port"]
    info = daemon["info"]
    _fire(port, "GET", "/hook?x=1")
    _fire(port, "POST", "/hook", b'{"a":1}', {"Content-Type": "application/json"})
    _fire(port, "PUT", "/things/42", b"payload")
    _fire(port, "DELETE", "/things/42")
    _fire(port, "FROBNICATE", "/weird/path", b"exotic")
    _fire(port, "POST", "/bin", b"\xff\xfe\x00\x01", {"Content-Type": "application/octet-stream"})
    big = b"x" * (1024 * 1024 + 500)
    _fire(port, "POST", "/big", big, {"Content-Type": "text/plain"})

    _wait_count(info, 7)
    _, data = _control(info, "/api/requests?limit=1000")
    rows = {(r["method"], r["path"]): r for r in data["rows"]}
    assert ("GET", "/hook") in rows
    assert ("POST", "/hook") in rows
    assert ("PUT", "/things/42") in rows
    assert ("DELETE", "/things/42") in rows
    assert ("FROBNICATE", "/weird/path") in rows
    assert rows[("GET", "/hook")]["query"] == "x=1"

    big_row = rows[("POST", "/big")]
    assert big_row["body_truncated"] is True
    assert big_row["body_len"] == len(big)

    bin_row = rows[("POST", "/bin")]
    _, full = _control(info, f"/api/request?id={bin_row['id']}")
    assert full["binary"] is True
    assert full["body_base64"]


def test_filters_and_histogram(daemon):
    port = daemon["capture_port"]
    info = daemon["info"]
    _fire(port, "GET", "/alpha", b"hello world")
    _fire(port, "POST", "/beta", b"goodbye moon")
    _fire(port, "POST", "/alpha/deep", b"error: boom")
    _wait_count(info, 3)

    _, q1 = _control(info, "/api/requests?q=goodbye")
    assert q1["matching_count"] == 1 and q1["rows"][0]["path"] == "/beta"

    _, q2 = _control(info, "/api/requests?q=re:err.r")
    assert q2["matching_count"] == 1 and q2["rows"][0]["path"] == "/alpha/deep"

    _, q3 = _control(info, "/api/requests?q=/hello/")
    assert q3["matching_count"] == 1 and q3["rows"][0]["path"] == "/alpha"

    _, m = _control(info, "/api/requests?methods=POST")
    assert m["matching_count"] == 2

    _, p = _control(info, "/api/requests?path_prefix=/alpha")
    assert p["matching_count"] == 2

    now = time.time()
    _, r = _control(info, f"/api/requests?from={now + 3600}")
    assert r["matching_count"] == 0

    _, h = _control(info, "/api/histogram?bins=40")
    assert h["matching_count"] == 3
    assert h["bins"] and h["bucket_seconds"] >= 1.0
    total = sum(b["count"] for b in h["bins"])
    assert total == 3


def test_persistence_across_restart(daemon):
    port = daemon["capture_port"]
    info = daemon["info"]
    _fire(port, "POST", "/persist", b"keep me")
    _wait_count(info, 1)
    _, before = _control(info, "/api/requests")
    seq_before = before["rows"][0]["seq"]

    _control(info, "/quit")
    time.sleep(0.4)

    info2 = daemon["ensure"]()
    assert info2["reused"] is False
    _, after = _control(info2, "/api/requests")
    paths = [r["path"] for r in after["rows"]]
    assert "/persist" in paths
    assert after["next_seq"] >= seq_before


def test_clear(daemon):
    port = daemon["capture_port"]
    info = daemon["info"]
    _fire(port, "POST", "/x", b"a")
    _wait_count(info, 1)
    _control_post(info, "/api/clear", {})
    _, data = _control(info, "/api/requests")
    assert data["count"] == 0


def test_replay_reconstructs_request(daemon):
    port = daemon["capture_port"]
    info = daemon["info"]
    received = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            received.append({"method": self.command, "path": self.path,
                             "body": body, "ctype": self.headers.get("Content-Type")})
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            payload = b'{"ok":true}'
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_port = target.server_address[1]
    threading.Thread(target=target.serve_forever, daemon=True).start()
    try:
        _fire(port, "POST", "/hook", b'{"replay":1}', {"Content-Type": "application/json"})
        _wait_count(info, 1)
        _, data = _control(info, "/api/requests")
        rid = data["rows"][0]["id"]

        status, result = _control_post(info, "/api/replay",
            {"id": rid, "url": f"http://127.0.0.1:{target_port}/replayed"})
        assert status == 200, result
        assert result["status"] == 201
        assert result["duration_ms"] >= 0
        assert received and received[0]["path"] == "/replayed"
        assert received[0]["body"] == b'{"replay":1}'
        assert received[0]["ctype"] == "application/json"

        _, full = _control(info, f"/api/request?id={rid}")
        assert len(full["replays"]) == 1
        assert full["replays"][0]["status"] == 201
    finally:
        target.shutdown()


def test_response_override(home, tmp_path):
    mod = _load_module()
    port = _free_port()
    descriptor = _write_descriptor(tmp_path / "resp.webhook", capture_port=port,
        response={"status": 202, "content_type": "application/json",
                  "body": "{\"received\": true}"})
    info = mod.main(action="ensure", file=descriptor)
    assert "error" not in info
    try:
        status, body = _fire(port, "POST", "/hook", b"{}")
        assert status == 202
        assert json.loads(body) == {"received": True}
    finally:
        try:
            _control(info, "/quit")
        except Exception:
            pass
        time.sleep(0.2)
