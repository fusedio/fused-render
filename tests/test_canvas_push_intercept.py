"""`fused workbench canvas push` inside a canvas clone goes through the server.

A Claude session working in a clone is told to publish with the standard
command. That command is only safe because `_fused_cli.py` — the in-interpreter
entry point every shipping install runs — recognises a clone push and turns it
into a POST to `/api/canvases/sync/push`, where the real `_push` runs under the
watcher's lock with its probe+merge+abort guard intact.

Two halves are tested: the matcher (which commands are ours, and the much longer
list of ones that are NOT), and the request behaviour (what the session sees,
and every case that must fall through to the real CLI).
"""
import io
import json
import os
import subprocess
import sys

import pytest

from fused_render import _canvas_push, fusedcli


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """A canvases root with one clone in it."""
    canvases = tmp_path / "canvases"
    (canvases / "alpha").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(canvases))
    return canvases


# -- the matcher ---------------------------------------------------------------


def test_the_canonical_push_is_recognised():
    """The exact shape `_SyncManager._push` itself uses, and the shape the
    prompt tells a session to type."""
    assert _canvas_push.parse_push(
        ["workbench", "canvas", "push", "/x/alpha", "--canvas", "alpha"]
    ) == {"source_dir": "/x/alpha", "canvas": "alpha", "unsupported": []}
    assert _canvas_push.parse_push(["workbench", "canvas", "push", "."]) == {
        "source_dir": ".", "canvas": None, "unsupported": []}
    assert _canvas_push.parse_push(
        ["workbench", "canvas", "push", "--canvas=alpha", "."]
    ) == {"source_dir": ".", "canvas": "alpha", "unsupported": []}


@pytest.mark.parametrize("global_prefix", [
    ["--env", "unstable"],
    ["--backend", "local"],
    ["--enable-infra"],
    ["--enable-destructive"],
    ["--disable-reset"],
    ["--env=unstable"],
    ["--backend=local"],
    # Several stacked, in whatever order a session might type them.
    ["--enable-infra", "--env", "unstable"],
    ["--backend", "local", "--enable-infra", "--disable-reset"],
])
def test_a_global_option_before_the_subcommand_is_still_recognised(global_prefix):
    """`fused` mounts `workbench` under a TOP-LEVEL group with its own options
    (fused/agent_core/cli.py) — `fused --env unstable workbench canvas push .`
    is a real shape a session can type, and matching `args[:3]` verbatim
    missed it entirely, falling through to the raw, unguarded push."""
    assert _canvas_push.parse_push([*global_prefix, "workbench", "canvas", "push", "."]) == {
        "source_dir": ".", "canvas": None, "unsupported": []}
    assert _canvas_push.parse_push(
        [*global_prefix, "workbench", "canvas", "push", "/x/alpha", "--canvas", "alpha"]
    ) == {"source_dir": "/x/alpha", "canvas": "alpha", "unsupported": []}


def test_an_unrecognised_option_before_the_subcommand_falls_through():
    """Conservative by construction: a global flag this parser has never heard
    of must fall through rather than guess whether it takes a value."""
    assert _canvas_push.parse_push(
        ["--some-future-flag", "workbench", "canvas", "push", "."]
    ) is None
    # A malformed value-taking global option (nothing after it) is also left
    # to the real CLI to complain about, not guessed at.
    assert _canvas_push.parse_push(["--env"]) is None


@pytest.mark.parametrize("args", [
    # Not a push at all — the overwhelming majority of `fused` traffic.
    ["workbench", "canvas", "pull", "alpha", "-o", "/x/alpha"],
    ["workbench", "canvas", "validate", "."],
    ["workbench", "canvas", "list"],
    ["workbench", "whoami"],
    ["udf", "run", "x"],
    [],
    ["--version"],
    # A push, but not through the `workbench` group.
    ["canvas", "push", "/x/alpha"],
    # --help must reach the real CLI, which owns the help text.
    ["workbench", "canvas", "push", "--help"],
    # --id means the reference is a canvas ID, not a directory.
    ["workbench", "canvas", "push", "--id", "abc123"],
    # A flag this parser has never heard of: the CLI is the authority.
    ["workbench", "canvas", "push", ".", "--brand-new-flag"],
    # SOURCE_DIR is required and singular.
    ["workbench", "canvas", "push"],
    ["workbench", "canvas", "push", "/x/alpha", "/x/beta"],
    # Malformed --canvas: let the CLI produce its own usage error.
    ["workbench", "canvas", "push", ".", "--canvas"],
])
def test_everything_else_falls_through(args):
    """A false positive breaks a command that has nothing to do with canvases,
    so the matcher refuses anything it does not model exactly."""
    assert _canvas_push.parse_push(args) is None


def test_the_clone_root_must_be_exactly_one_segment_under_the_root(root):
    assert _canvas_push._clone_name(str(root / "alpha")) == "alpha"
    # A subdirectory of a clone is a different push (different content), and
    # the canvases root itself is not a canvas.
    (root / "alpha" / "sub").mkdir()
    assert _canvas_push._clone_name(str(root / "alpha" / "sub")) is None
    assert _canvas_push._clone_name(str(root)) is None
    assert _canvas_push._clone_name(str(root.parent / "elsewhere")) is None


# -- the request ---------------------------------------------------------------


class _Server:
    """Records the POST and replays a canned response."""

    def __init__(self, status=200, body=None):
        self.status, self.body = status, body if body is not None else {}
        self.calls = []

    def __call__(self, origin, name):
        self.calls.append((origin, name))
        return self.status, self.body


def _intercept(args, monkeypatch, server=None, origin="http://127.0.0.1:9999"):
    if origin is None:
        monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("FUSED_RENDER_ORIGIN", origin)
    if server is not None:
        monkeypatch.setattr(_canvas_push, "_post_push", server)
    out, err = io.StringIO(), io.StringIO()
    code = _canvas_push.maybe_intercept(args, out, err)
    return code, out.getvalue(), err.getvalue()


def test_a_clone_push_is_sent_to_the_server(root, monkeypatch):
    server = _Server(200, {"ok": True, "push_state": "idle", "push_seq": 1})
    code, out, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code == 0, err
    assert server.calls == [("http://127.0.0.1:9999", "alpha")]
    assert "alpha" in out
    assert err == ""


def test_a_relative_target_resolves(root, monkeypatch):
    """The session's cwd IS the clone, so `fused workbench canvas push .` is
    the form it will actually type."""
    server = _Server(200, {"ok": True})
    monkeypatch.chdir(root / "alpha")
    code, _, err = _intercept(["workbench", "canvas", "push", "."],
                              monkeypatch, server)
    assert code == 0, err
    assert server.calls == [("http://127.0.0.1:9999", "alpha")]


def test_a_validation_failure_reaches_the_session_verbatim(root, monkeypatch):
    """The reason for all of this: the lines that name the broken nodes have to
    land in the agent's own transcript so it can fix them without a human
    relaying the output."""
    lines = ["error: node 'buffer' has no source file (buffer.py missing)",
             "error: edge references unknown node 'join'",
             "Error: Canvas validation failed with 2 error(s)."]
    server = _Server(200, {"ok": False, "push_state": "error",
                           "error": "Canvas validation failed with 2 error(s).",
                           "error_detail": lines})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code == 1
    for line in lines:
        assert line in err
    assert err.splitlines() == lines, "the transcript must be verbatim, not reworded"


def test_a_failure_with_no_detail_still_says_something(root, monkeypatch):
    server = _Server(200, {"ok": False, "push_state": "error",
                           "error": "the fused CLI is not available",
                           "error_detail": []})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code == 1
    assert "the fused CLI is not available" in err


def test_a_refusal_is_reported_not_retried_as_a_raw_push(root, monkeypatch):
    """A paused watcher / a push already running must NOT degrade into the raw
    CLI push — that is the unguarded path this whole mechanism removes."""
    server = _Server(409, {"error": "syncing for 'alpha' is paused; try again"})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code == 1
    assert "paused" in err


def test_unsupported_flags_are_refused_rather_than_passed_through(root, monkeypatch):
    """--no-validate / --no-ignore change what gets published in ways the
    endpoint cannot express. Falling through would run exactly the unguarded
    push this module exists to prevent, so it refuses and says why."""
    for flag in ("--no-validate", "--no-ignore"):
        server = _Server(200, {"ok": True})
        code, _, err = _intercept(
            ["workbench", "canvas", "push", str(root / "alpha"), flag],
            monkeypatch, server)
        assert code == 2, flag
        assert flag in err
        assert server.calls == [], "it must not have pushed"


# -- every fall-through case --------------------------------------------------


def test_the_internal_marker_short_circuits_everything(root, monkeypatch):
    """The reentrancy guard. `_SyncManager._push` runs `[*cli.command, …]`, and
    on the shim path cli.command IS `[sys.executable, _fused_cli.py]` — this very
    interception. Without the marker the server's own push POSTs back to the
    endpoint, is refused because a push is already running (itself), and the
    refusal is recorded as a CLI failure: canvas sync dead at push_seq 0.

    Checked before the argv match, so it costs nothing on unrelated commands."""
    server = _Server(200, {"ok": True})
    monkeypatch.setenv(_canvas_push.INTERNAL_ENV, "1")
    code, out, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code is None, "the manager's own push must fall through to the CLI"
    assert server.calls == []
    assert out == "" and err == ""


def test_the_marker_name_is_shared_with_canvases(root):
    """canvases.py sets it, this module reads it; they must not drift."""
    from fused_render import canvases

    assert canvases._canvas_push_internal_env() == _canvas_push.INTERNAL_ENV


def test_the_cli_env_carries_the_marker(root):
    """Every fused child canvases.py spawns goes through _cli_env — push, pull,
    validate, the shims — and none of them may be intercepted."""
    from fused_render import canvases

    env = canvases._cli_env(fusedcli.FusedCli(command=["fused"], external=False))
    assert env[_canvas_push.INTERNAL_ENV] == "1"


def test_a_busy_refusal_reads_as_retryable_not_as_a_broken_canvas(root, monkeypatch):
    """A genuine double-push is a timing conflict. The message has to say that,
    or the reader goes hunting for a validation problem that does not exist."""
    server = _Server(409, {"error": "a push is already running for this canvas",
                           "code": "busy"})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code == 1, "the push did not happen, so it must not exit 0"
    assert "already running" in err
    assert "Nothing is wrong with the canvas" in err
    assert "again" in err


def test_no_server_origin_falls_through(root, monkeypatch):
    """Nothing published an origin, so there is no fused-render around. Never a
    guessed port: 1777 is wrong under any --port override."""
    server = _Server(200, {"ok": True})
    code, _, _ = _intercept(["workbench", "canvas", "push", str(root / "alpha")],
                            monkeypatch, server, origin=None)
    assert code is None
    assert server.calls == []


def test_a_push_outside_the_canvases_root_falls_through(root, monkeypatch, tmp_path):
    """An ordinary canvas directory the user keeps in their own project is not
    two-way synced, so the real CLI is exactly right for it."""
    other = tmp_path / "myproject" / "mycanvas"
    other.mkdir(parents=True)
    server = _Server(200, {"ok": True})
    code, _, _ = _intercept(["workbench", "canvas", "push", str(other)],
                            monkeypatch, server)
    assert code is None
    assert server.calls == []


def test_no_watcher_on_a_clone_refuses_rather_than_falls_through(root, monkeypatch):
    """A positively-identified clone is never an inert fall-through target: a
    merge base from a prior sync session can still be on disk, and the remote
    can have moved through the hosted workbench with nothing watching to
    notice. "no watcher right now" is not "sync was never engaged here", so
    this refuses instead of running the raw, unguarded push."""
    server = _Server(409, {"error": "not being synced", "code": "no_watcher"})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code == 1, "must not silently run the raw push"
    assert "not being synced" in err
    assert "start sync" in err.lower() or "open this canvas" in err.lower()


def test_an_unreachable_server_falls_through(root, monkeypatch):
    """The origin is set but nothing answers (server shut down mid-session).
    The real CLI is the honest fallback, not an invented failure."""
    server = _Server(None, {})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code is None
    assert err == ""


def test_a_slow_but_alive_server_does_not_fall_through(root, monkeypatch):
    """A `socket.timeout` (bare `except OSError` swallows this — timeout is an
    OSError subclass) must NOT be treated the same as "could not connect".

    _post_push's genuine 200s timeout can be exceeded by a slow-but-successful
    push (lock-wait + probe + pull + push), and the naive fix of catching
    OSError broadly turns that into `status=None`, which `maybe_intercept`
    reads as "no server around" and runs the raw, unguarded CLI push
    CONCURRENTLY with the server's own still-in-flight guarded one. Connect
    failures are a different, non-timeout OSError (e.g. ConnectionRefusedError)
    and must still fall through — this only pins the timeout case."""
    server = _Server(_canvas_push._TIMED_OUT, {})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha")], monkeypatch, server)
    assert code == 1, "a timeout must not exit as if nothing happened"
    assert "timed out" in err


def test_post_push_reports_a_real_socket_timeout_distinctly(monkeypatch):
    """No mocking of _post_push here: a real slow HTTP server that accepts the
    connection but never answers, exercising the actual except clauses."""
    import http.server
    import threading
    import time as _time

    class _SlowHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            _time.sleep(5)  # longer than the shortened timeout below

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _SlowHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(_canvas_push, "_HTTP_TIMEOUT_S", 0.3)
    try:
        status, body = _canvas_push._post_push(
            "http://127.0.0.1:%d" % httpd.server_port, "alpha")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == _canvas_push._TIMED_OUT, (
        "a real socket timeout must be distinguishable from a failed connect",
        status, body)


def test_post_push_falls_through_on_a_genuine_connect_failure():
    """Nothing listens on this port — a real ConnectionRefusedError, which
    must stay in the `status is None` (safe-to-fall-through) bucket."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    status, body = _canvas_push._post_push("http://127.0.0.1:%d" % port, "alpha")
    assert status is None, (status, body)


def test_pushing_a_clone_at_a_different_canvas_falls_through(root, monkeypatch):
    """`--canvas beta` from alpha's folder is a cross-push. The endpoint is
    keyed on the watcher for THIS folder and cannot express it."""
    server = _Server(200, {"ok": True})
    code, _, _ = _intercept(
        ["workbench", "canvas", "push", str(root / "alpha"), "--canvas", "beta"],
        monkeypatch, server)
    assert code is None
    assert server.calls == []


# -- the shim actually wires it up ---------------------------------------------


_SHIM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "fused_render", "_fused_cli.py")


def test_the_shim_intercepts_before_dispatching_to_the_real_cli(root, tmp_path):
    """End-to-end through the real entry point: a clone push must not reach
    `fused._cli.main` at all. Run in a subprocess because that is how the shim
    runs, and with a stub `fused` package on the path so a dispatch would be
    loudly visible instead of hitting the network.
    """
    stub = tmp_path / "stub"
    (stub / "fused").mkdir(parents=True)
    (stub / "fused" / "__init__.py").write_text("")
    (stub / "fused" / "_cli.py").write_text(
        "import sys\n"
        "def main():\n"
        "    sys.stderr.write('REAL CLI RAN\\n')\n"
        "    sys.exit(99)\n")
    # A server that answers the push endpoint, so no real network is involved.
    server = tmp_path / "fake_server.py"
    server.write_text(
        "import http.server, json, sys, threading\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers['Content-Length'])\n"
        "        body = json.loads(self.rfile.read(n))\n"
        "        assert self.headers.get('X-Fused') == '1', 'guard header missing'\n"
        "        out = json.dumps({'ok': True, 'name': body['name'],\n"
        "                          'push_state': 'idle'}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(out)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(out)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "srv = http.server.HTTPServer(('127.0.0.1', 0), H)\n"
        "print(srv.server_port, flush=True)\n"
        "srv.serve_forever()\n")
    proc = subprocess.Popen([sys.executable, str(server)], stdout=subprocess.PIPE,
                            text=True)
    try:
        port = int(proc.stdout.readline().strip())
        env = dict(os.environ)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = os.pathsep.join([str(stub), repo])
        env["FUSED_RENDER_CANVASES_DIR"] = str(root)
        env["FUSED_RENDER_ORIGIN"] = "http://127.0.0.1:%d" % port

        done = subprocess.run(
            [sys.executable, _SHIM, "workbench", "canvas", "push",
             str(root / "alpha")],
            capture_output=True, text=True, env=env, timeout=60)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert "REAL CLI RAN" not in done.stderr, "the raw CLI push was dispatched"
        assert "alpha" in done.stdout

        # And a command that is NOT a clone push still reaches the real CLI.
        other = subprocess.run(
            [sys.executable, _SHIM, "workbench", "canvas", "list"],
            capture_output=True, text=True, env=env, timeout=60)
        assert other.returncode == 99, (other.stdout, other.stderr)
        assert "REAL CLI RAN" in other.stderr
    finally:
        proc.kill()
        proc.wait()


def test_the_shim_falls_through_when_the_interception_cannot_load(root, tmp_path):
    """The try/except around the import is a promise: whatever goes wrong in the
    interception, this file still behaves like the `fused` console script."""
    with open(_SHIM, encoding="utf-8") as fh:
        source = fh.read()
    assert "except Exception" in source
    assert "from fused._cli import main" in source
    # The fallthrough must be reached with the interception unimportable.
    stub = tmp_path / "stub"
    (stub / "fused").mkdir(parents=True)
    (stub / "fused" / "__init__.py").write_text("")
    (stub / "fused" / "_cli.py").write_text(
        "import sys\n"
        "def main():\n"
        "    sys.stderr.write('REAL CLI RAN\\n')\n"
        "    sys.exit(99)\n")
    env = dict(os.environ)
    # No repo on PYTHONPATH → `fused_render` is not importable from the shim's
    # own directory, so the import inside it raises.
    env["PYTHONPATH"] = str(stub)
    env.pop("FUSED_RENDER_ORIGIN", None)
    done = subprocess.run(
        [sys.executable, _SHIM, "workbench", "canvas", "push", str(root / "alpha")],
        capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 99, (done.stdout, done.stderr)
    assert "REAL CLI RAN" in done.stderr


def test_the_guard_header_constant_matches_the_server(root):
    """The endpoint rejects a request without it, so a drift here would turn
    every intercepted push into a 403."""
    from fused_render import canvases

    assert _canvas_push._GUARD_HEADER == "X-Fused"
    assert _canvas_push._GUARD_VALUE == "1"
    assert canvases._require_fused(_canvas_push._GUARD_VALUE) is None
    assert canvases._require_fused(None) is not None


def test_the_canvases_root_rule_matches_canvases_py(root):
    """_canvas_push duplicates the rule to keep FastAPI off the CLI's startup
    path; this is what keeps the copy honest."""
    from fused_render import canvases

    assert _canvas_push.canvases_root() == canvases.canvases_root()
    _ = json
