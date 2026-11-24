"""`fused workbench canvas push`/`pull` inside a canvas clone never run.

The revision spine (2026-11-11, owner Q7): a Claude session in a clone has no
sync verbs — the folder publishes itself (turn end, quiet folder) and server
changes are merged into it. `_fused_cli.py` — the in-interpreter entry point
every shipping install runs — recognises a clone push and answers with the
folder's state line (exit 0, nothing to do), and refuses a pull into a clone
(exit 1).

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

# The environment the `root` fixture's clones belong to, and the other one.
ENV, OTHER_ENV = "unstable", "prod"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """ENV's clone folder (`<base>/<env>`), with one clone in it.
    FUSED_RENDER_CANVASES_DIR is the BASE above every environment's folder."""
    canvases = tmp_path / "canvases"
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(canvases))
    monkeypatch.delenv("FUSED_RENDER_WORKBENCH_ENV", raising=False)
    # A fall-through binds FUSED_ENV in os.environ (`_fall_through`); set then
    # unset so monkeypatch records it and teardown leaves no value behind.
    monkeypatch.setenv("FUSED_ENV", "placeholder")
    monkeypatch.delenv("FUSED_ENV")
    root = canvases / ENV
    (root / "user" / "alpha").mkdir(parents=True)
    return root


# -- the matcher ---------------------------------------------------------------


def test_the_canonical_push_is_recognised():
    """The exact shape `_SyncManager._push` itself uses, and the shape the
    prompt tells a session to type."""
    assert _canvas_push.parse_push(
        ["workbench", "canvas", "push", "/x/alpha", "--canvas", "alpha"]
    ) == {"source_dir": "/x/alpha", "canvas": "alpha", "unsupported": [], "env": None}
    assert _canvas_push.parse_push(["workbench", "canvas", "push", "."]) == {
        "source_dir": ".", "canvas": None, "unsupported": [], "env": None}
    assert _canvas_push.parse_push(
        ["workbench", "canvas", "push", "--canvas=alpha", "."]
    ) == {"source_dir": ".", "canvas": "alpha", "unsupported": [], "env": None}


@pytest.mark.parametrize("global_prefix", [
    ["--env", "unstable"],
    ["--backend", "local"],
    ["--env=unstable"],
    ["--backend=local"],
    # Several stacked, in whatever order a session might type them.
    ["--backend", "local", "--env", "unstable"],
    ["--env=unstable", "--backend=local"],
])
def test_a_global_option_before_the_subcommand_is_still_recognised(global_prefix):
    """`fused` mounts `workbench` under a TOP-LEVEL group with its own options
    (fused/agent_core/cli.py) — `fused --env unstable workbench canvas push .`
    is a real shape a session can type, and matching `args[:3]` verbatim
    missed it entirely, falling through to the raw, unguarded push."""
    assert _canvas_push.parse_push([*global_prefix, "workbench", "canvas", "push", "."]) == {
        "source_dir": ".", "canvas": None, "unsupported": [], "env": None}
    assert _canvas_push.parse_push(
        [*global_prefix, "workbench", "canvas", "push", "/x/alpha", "--canvas", "alpha"]
    ) == {"source_dir": "/x/alpha", "canvas": "alpha", "unsupported": [], "env": None}


def test_the_workbench_groups_own_env_is_recognised_and_reported():
    """`fused workbench --env prod canvas push .` is FUSED_ENV in flag form
    (the workbench group's `--env`), unlike the top-level `--env` (agent_core's
    OPENFUSED_ENV, reported as no target). Unmatched it fell through to the
    raw push unchecked; now it is matched and its value carried."""
    assert _canvas_push.parse_push(
        ["workbench", "--env", "prod", "--format=json", "canvas", "push", "."]
    ) == {"source_dir": ".", "canvas": None, "unsupported": [], "env": "prod"}
    assert _canvas_push.parse_push(
        ["--env", "x", "workbench", "--env=custom", "canvas", "push", "."]
    )["env"] == "custom"
    assert _canvas_push.parse_push(["workbench", "--env"]) is None


def test_an_unrecognised_option_before_the_subcommand_falls_through():
    """Conservative by construction: a global flag this parser has never heard
    of must fall through rather than guess whether it takes a value."""
    assert _canvas_push.parse_push(
        ["--some-future-flag", "workbench", "canvas", "push", "."]
    ) is None
    # A malformed value-taking global option (nothing after it) is also left
    # to the real CLI to complain about, not guessed at.
    assert _canvas_push.parse_push(["--env"]) is None


@pytest.mark.parametrize("removed_flag", [
    "--enable-infra", "--enable-destructive", "--disable-reset",
])
def test_a_removed_root_flag_before_the_subcommand_falls_through(removed_flag):
    """fusedio/fused#371 dropped these root-group flags with the MCP server.
    The real CLI now rejects them, so the matcher must not skip them as if
    they were still valid — the CLI's own usage error is the right outcome."""
    assert _canvas_push.parse_push(
        [removed_flag, "workbench", "canvas", "push", "."]
    ) is None


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
    env = ENV
    assert _canvas_push._clone_name(str(root / "user" / "alpha")) == ("alpha", env)
    # The OTHER environment's clone is a clone too (its push is routed to
    # that environment's manager); an unknown segment in the environment
    # position is not.
    other = OTHER_ENV
    (root.parent / other / "user" / "beta").mkdir(parents=True)
    assert _canvas_push._clone_name(str(root.parent / other / "user" / "beta")) == ("beta", other)
    (root.parent / "staging" / "gamma").mkdir(parents=True)
    assert _canvas_push._clone_name(str(root.parent / "staging" / "gamma")) is None
    # A subdirectory of a clone is a different push (different content), and
    # the canvases root itself is not a canvas.
    (root / "user" / "alpha" / "sub").mkdir()
    assert _canvas_push._clone_name(str(root / "user" / "alpha" / "sub")) is None
    assert _canvas_push._clone_name(str(root)) is None
    assert _canvas_push._clone_name(str(root.parent / "elsewhere")) is None
    assert _canvas_push._clone_name(str(root.parent)) is None


# -- the request ---------------------------------------------------------------


def _watching(**extra):
    """A sync/state answer for a clone this server watches."""
    return {"name": "alpha", "watching": True, "watcher_epoch": "e1",
            "base_revision": 3, "server_revision": 4, "pending_files": 2,
            "legs": {"disk_to_server": {"state": "idle", "reason": None,
                                        "last_push_at": None, "push_seq": 0}},
            "error_detail": [], "other_watchers": [], **extra}


class _Server:
    """Records the state GET and replays a canned response."""

    def __init__(self, status=200, body=None):
        self.status, self.body = status, body if body is not None else {}
        self.calls = []

    def __call__(self, origin, name, env):
        self.calls.append((origin, name, env))
        return self.status, self.body


def _intercept(args, monkeypatch, server=None, origin="http://127.0.0.1:9999"):
    if origin is None:
        monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("FUSED_RENDER_ORIGIN", origin)
    if server is not None:
        monkeypatch.setattr(_canvas_push, "_get_state", server)
    out, err = io.StringIO(), io.StringIO()
    code = _canvas_push.maybe_intercept(args, out, err)
    return code, out.getvalue(), err.getvalue()


def test_a_clone_push_is_sent_to_the_server(root, monkeypatch):
    """The folder publishes itself: a clone push answers with its state line,
    exit 0 — nothing to do, nothing failed."""
    server = _Server(200, _watching())
    code, out, err = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 0, err
    assert server.calls == [("http://127.0.0.1:9999", "alpha", ENV)]
    assert out == ("This folder publishes itself: base rev 3, server rev 4, 2 pending "
                   "files, no push yet. Nothing to do.\n"), out
    assert err == ""


def test_a_relative_target_resolves(root, monkeypatch):
    """The session's cwd IS the clone, so `fused workbench canvas push .` is
    the form it will actually type."""
    server = _Server(200, _watching())
    monkeypatch.chdir(root / "user" / "alpha")
    code, out, err = _intercept(["workbench", "canvas", "push", "."],
                                monkeypatch, server)
    assert code == 0, err
    assert server.calls == [("http://127.0.0.1:9999", "alpha", ENV)]
    assert "publishes itself" in out


def test_a_validation_failure_reaches_the_session_verbatim(root, monkeypatch):
    """The last publish failed: the lines that name the broken nodes still
    land in the session's transcript, verbatim, so it can fix them — the next
    publish (its turn end) ships the fix."""
    lines = ["error: node 'buffer' has no source file (buffer.py missing)",
             "error: edge references unknown node 'join'",
             "Error: Canvas validation failed with 2 error(s)."]
    server = _Server(200, _watching(
        legs={"disk_to_server": {"state": "error", "reason": lines[-1],
                                 "last_push_at": None, "push_seq": 0}},
        error_detail=lines))
    code, out, _ = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 0
    assert out.splitlines()[1:] == ["The last publish failed:", *lines], out


def test_a_failure_with_no_detail_still_says_something(root, monkeypatch):
    server = _Server(200, _watching(
        legs={"disk_to_server": {"state": "error", "reason": "the fused CLI is not available",
                                 "last_push_at": None, "push_seq": 0}}))
    code, out, _ = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 0
    assert "the fused CLI is not available" in out


def test_a_refusal_is_reported_not_retried_as_a_raw_push(root, monkeypatch):
    """A state read the server refuses must NOT degrade into the raw CLI push
    — that is the unguarded path this whole mechanism removes."""
    server = _Server(400, {"error": "'env' is not a known environment"})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 1
    assert "not a known environment" in err


def test_unsupported_flags_are_refused_rather_than_passed_through(root, monkeypatch):
    """--no-validate / --no-ignore: the same answer as any push — the folder
    publishes itself, with validation, honouring .fusedignore."""
    for flag in ("--no-validate", "--no-ignore"):
        server = _Server(200, _watching())
        code, out, _ = _intercept(
            ["workbench", "canvas", "push", str(root / "user" / "alpha"), flag],
            monkeypatch, server)
        assert code == 0, flag
        assert "publishes itself" in out


# -- every fall-through case --------------------------------------------------


def test_the_internal_marker_short_circuits_everything(root, monkeypatch):
    """The reentrancy guard. `_SyncManager._push` runs `[*cli.command, …]`, and
    on the shim path cli.command IS `[sys.executable, _fused_cli.py]` — this very
    interception. Without the marker the server's own push POSTs back to the
    endpoint, is refused because a push is already running (itself), and the
    refusal is recorded as a CLI failure: canvas sync dead at push_seq 0.

    Checked before the argv match, so it costs nothing on unrelated commands."""
    server = _Server(200, _watching())
    monkeypatch.setenv(_canvas_push.INTERNAL_ENV, "1")
    code, out, err = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
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

    env = canvases._cli_env(fusedcli.FusedCli(command=["fused"], external=False), OTHER_ENV)
    assert env[_canvas_push.INTERNAL_ENV] == "1"
    # The canvas's own env, never the preference: both envs sync at once.
    assert env["FUSED_ENV"] == OTHER_ENV


def test_a_busy_refusal_reads_as_retryable_not_as_a_broken_canvas(root, monkeypatch):
    """A held publish (server move held for the turn, or the canvas checked
    out elsewhere) is shown with its reason — still exit 0: nothing for the
    session to do."""
    server = _Server(200, _watching(
        legs={"disk_to_server": {"state": "held", "reason": "checked out by fused-render:bob@x",
                                 "last_push_at": None, "push_seq": 0}}))
    code, out, _ = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 0
    assert "Publishing is held: checked out by fused-render:bob@x." in out


def test_no_server_origin_falls_through(root, monkeypatch):
    """Nothing published an origin, so there is no fused-render around. Never a
    guessed port: 1777 is wrong under any --port override."""
    server = _Server(200, _watching())
    code, _, _ = _intercept(["workbench", "canvas", "push", str(root / "user" / "alpha")],
                            monkeypatch, server, origin=None)
    assert code is None
    assert server.calls == []


def test_a_clone_of_the_other_environment_is_refused_on_every_fall_through(root, monkeypatch):
    """A FUSED_ENV naming the other environment (an explicit `FUSED_ENV=x
    fused ...`, or the wrapper's default when run outside the clone) must
    never reach the raw CLI with a clone: no origin, `--canvas` naming
    another canvas, and an unreachable server — the three shapes that fall
    through — all refuse."""
    env, other = ENV, OTHER_ENV
    clone = root.parent / other / "user" / "alpha"
    clone.mkdir(parents=True)
    monkeypatch.setenv("FUSED_ENV", env)
    cases = [
        (["workbench", "canvas", "push", str(clone)], None, _Server(200, _watching())),
        (["workbench", "canvas", "push", str(clone), "--canvas", "zeta"],
         "http://127.0.0.1:9999", _Server(200, _watching())),
        (["workbench", "canvas", "push", str(clone)], "http://127.0.0.1:9999",
         _Server(None, {})),
    ]
    for args, origin, server in cases:
        code, _, err = _intercept(args, monkeypatch, server, origin=origin)
        assert code == 1, (args, origin)
        assert other in err and env in err
        # The remedy names the clone's own env, not a Preferences switch.
        assert f"FUSED_ENV={other}" in err and "Preferences" not in err


def test_a_custom_clone_refuses_a_set_fused_env_and_falls_through_unset(root, monkeypatch):
    """The custom env is the one an UNSET FUSED_ENV targets (the CLI keeps
    `~/.fused/settings.toml`'s endpoint): on a fall-through a custom clone
    refuses any named env, and with FUSED_ENV unset runs the real CLI."""
    clone = root.parent / "custom" / "user" / "alpha"
    clone.mkdir(parents=True)
    args = ["workbench", "canvas", "push", str(clone)]
    monkeypatch.setenv("FUSED_ENV", "prod")
    code, _, err = _intercept(args, monkeypatch, _Server(200, _watching()), origin=None)
    assert code == 1
    assert "custom environment" in err and "FUSED_ENV unset" in err
    monkeypatch.delenv("FUSED_ENV")
    server = _Server(200, _watching())
    code, _, _ = _intercept(args, monkeypatch, server, origin=None)
    assert code is None
    # With a server, the push routes to the custom manager like any env's.
    code, _, _ = _intercept(args, monkeypatch, server)
    assert code == 0
    assert [c[1:] for c in server.calls] == [("alpha", "custom")]


def test_a_workbench_env_flag_naming_another_env_is_refused_on_fall_through(root, monkeypatch):
    """`workbench --env` beats FUSED_ENV in the CLI, so it does here: a flag
    naming another env refuses even with FUSED_ENV unset (or matching), for a
    custom clone and a named one alike."""
    custom = root.parent / "custom" / "user" / "alpha"
    custom.mkdir(parents=True)
    monkeypatch.delenv("FUSED_ENV", raising=False)
    for clone, flag in ((custom, "prod"), (root / "user" / "alpha", OTHER_ENV)):
        args = ["workbench", "--env", flag, "canvas", "push", str(clone)]
        code, _, err = _intercept(args, monkeypatch, _Server(200, _watching()), origin=None)
        assert code == 1, clone
        assert "(--env)" in err
    monkeypatch.setenv("FUSED_ENV", ENV)
    args = ["workbench", "--env", ENV, "canvas", "push", str(root / "user" / "alpha")]
    code, _, _ = _intercept(args, monkeypatch, _Server(200, _watching()), origin=None)
    assert code is None


def test_an_unset_fused_env_is_bound_to_a_named_clones_env_on_fall_through(root, monkeypatch):
    """Unset FUSED_ENV means `~/.fused/settings.toml` (the custom env) to the
    CLI — a custom default outside the clone leaves it so. A prod/unstable
    clone's raw push must still reach its own env, so the fall-through binds
    it; a custom clone keeps it unset."""
    args = ["workbench", "canvas", "push", str(root / "user" / "alpha")]
    code, _, _ = _intercept(args, monkeypatch, _Server(200, _watching()), origin=None)
    assert code is None
    assert os.environ["FUSED_ENV"] == ENV
    custom = root.parent / "custom" / "user" / "alpha"
    custom.mkdir(parents=True)
    monkeypatch.delenv("FUSED_ENV")
    code, _, _ = _intercept(["workbench", "canvas", "push", str(custom)], monkeypatch,
                            _Server(200, _watching()), origin=None)
    assert code is None
    assert "FUSED_ENV" not in os.environ


def test_a_mismatched_fused_env_still_posts_when_the_server_takes_it(root, monkeypatch):
    """With a server to take it, FUSED_ENV is irrelevant: the push is routed
    by the clone's own env to that environment's manager. Refusing first
    would turn away a push that was never at risk."""
    clone = root.parent / OTHER_ENV / "user" / "alpha"
    clone.mkdir(parents=True)
    monkeypatch.setenv("FUSED_ENV", ENV)
    server = _Server(200, _watching())
    code, _, _ = _intercept(["workbench", "canvas", "push", str(clone)], monkeypatch, server)
    assert code == 0
    assert [c[1:] for c in server.calls] == [("alpha", OTHER_ENV)]


def test_a_clone_of_either_environment_routes_to_the_server_with_its_env(root, monkeypatch):
    """No FUSED_ENV (a bare shell), or FUSED_ENV matching the clone (the
    wrapper inside it): the push goes to the server naming the clone's env,
    which picks that environment's manager — prod's and unstable's `alpha`
    are two canvases."""
    other_clone = root.parent / OTHER_ENV / "user" / "alpha"
    other_clone.mkdir(parents=True)
    monkeypatch.delenv("FUSED_ENV", raising=False)
    server = _Server(200, _watching())
    code, _, _ = _intercept(["workbench", "canvas", "push", str(other_clone)], monkeypatch, server)
    assert code == 0
    monkeypatch.setenv("FUSED_ENV", ENV)
    code, _, _ = _intercept(["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 0
    assert [c[1:] for c in server.calls] == [("alpha", OTHER_ENV), ("alpha", ENV)]


def test_a_push_outside_the_canvases_root_falls_through(root, monkeypatch, tmp_path):
    """An ordinary canvas directory the user keeps in their own project is not
    two-way synced, so the real CLI is exactly right for it."""
    other = tmp_path / "myproject" / "mycanvas"
    other.mkdir(parents=True)
    server = _Server(200, _watching())
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
    server = _Server(200, {"watching": False, "watcher_epoch": None, "other_watchers": []})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 1, "must not silently run the raw push"
    assert "not being synced" in err
    assert "open this canvas" in err.lower()
    server = _Server(200, {"watching": False, "watcher_epoch": None,
                           "other_watchers": [{"pid": 42, "port": 1778}]})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 1 and "pid 42" in err


def test_an_unreachable_server_falls_through(root, monkeypatch):
    """The origin is set but nothing answers (server shut down mid-session).
    The real CLI is the honest fallback, not an invented failure."""
    server = _Server(None, {})
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code is None
    assert err == ""


def test_get_state_falls_through_on_a_genuine_connect_failure():
    """Nothing listens on this port — a real ConnectionRefusedError, which
    must stay in the `status is None` (safe-to-fall-through) bucket."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    status, body = _canvas_push._get_state("http://127.0.0.1:%d" % port, "alpha", ENV)
    assert status is None, (status, body)


def test_pushing_a_clone_at_a_different_canvas_falls_through(root, monkeypatch):
    """`--canvas beta` from alpha's folder is a cross-push. The endpoint is
    keyed on the watcher for THIS folder and cannot express it."""
    server = _Server(200, _watching())
    code, _, _ = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha"), "--canvas", "beta"],
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
        "    def do_GET(self):\n"
        "        assert '/api/canvases/sync/state?' in self.path, self.path\n"
        "        assert 'files=1' in self.path, self.path\n"
        "        out = json.dumps({'name': 'alpha', 'watching': True,\n"
        "                          'watcher_epoch': 'e1', 'base_revision': 1,\n"
        "                          'server_revision': 1, 'pending_files': 0,\n"
        "                          'legs': {}}).encode()\n"
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
        env["FUSED_RENDER_CANVASES_DIR"] = str(root.parent)
        env["FUSED_RENDER_ORIGIN"] = "http://127.0.0.1:%d" % port

        done = subprocess.run(
            [sys.executable, _SHIM, "workbench", "canvas", "push",
             str(root / "user" / "alpha")],
            capture_output=True, text=True, env=env, timeout=60)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert "REAL CLI RAN" not in done.stderr, "the raw CLI push was dispatched"
        assert "This folder publishes itself" in done.stdout, done.stdout

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
        [sys.executable, _SHIM, "workbench", "canvas", "push", str(root / "user" / "alpha")],
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
    path; this is what keeps the copy honest — the base, and the environment
    choices that name the folder under it."""
    from fused_render import canvases

    assert _canvas_push.canvases_base() == canvases.canvases_base()
    assert _canvas_push._ENVS == fusedcli.WORKBENCH_ENV_CHOICES
    assert str(root) == canvases.canvases_root(ENV)
    # ...and the scope folders under an environment, clones in `user/` (D1476).
    from fused_render import canvas_marker

    assert _canvas_push._SCOPES == canvas_marker.SCOPES == canvases.SCOPE_DIRS
    assert str(root / _canvas_push._CLONES_SCOPE) == canvases.clones_root(ENV)
    assert canvases._canvas_dir(ENV, "alpha") == str(root / "user" / "alpha")
    _ = json


@pytest.mark.parametrize("extra", [[], ["--force"], ["--dry-run"], ["-f", "--show-diff"]])
def test_a_pull_into_a_clone_is_refused(root, monkeypatch, extra):
    """Any pull whose output is the clone (or inside it) is refused, exit 1,
    whatever its flags: the watcher merges server changes here itself."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    for args in (["workbench", "canvas", "pull", "alpha", "-o", str(root / "user" / "alpha")],
                 ["workbench", "canvas", "pull", "alpha", f"--output={root / 'user' / 'alpha'}"],
                 ["--env", "unstable", "workbench", "canvas", "pull", "alpha", "-o", "."]):
        monkeypatch.chdir(root / "user" / "alpha")
        out, err = io.StringIO(), io.StringIO()
        code = _canvas_push.maybe_intercept([*args, *extra], out, err)
        assert code == 1, args
        assert "Do not pull." in err.getvalue()
    # From inside the clone with no -o: the default output is a subfolder of
    # the clone — refused too.
    out, err = io.StringIO(), io.StringIO()
    assert _canvas_push.maybe_intercept(["workbench", "canvas", "pull", "alpha", *extra],
                                        out, err) == 1
    # From the clones folder: `pull alpha` writes INTO the clone.
    monkeypatch.chdir(root / "user")
    out, err = io.StringIO(), io.StringIO()
    assert _canvas_push.maybe_intercept(["workbench", "canvas", "pull", "alpha", *extra],
                                        out, err) == 1
    assert "Do not pull." in err.getvalue()
    # From the env folder, as before the scoped layout: `pull alpha` would
    # plant a canvas folder beside `user/` — still refused.
    monkeypatch.chdir(root)
    assert _canvas_push.maybe_intercept(["workbench", "canvas", "pull", "alpha", *extra],
                                        io.StringIO(), io.StringIO()) == 1


@pytest.mark.parametrize("extra", [[], ["--force"]])
def test_a_pull_into_the_environment_folder_itself_is_refused(root, monkeypatch, extra):
    """`-o <base>/<env>`: the pull would write over every clone, scope folder
    and `.sync/` at once. The environment folder is part of the tree."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    assert _canvas_push._locate(str(root)) == {
        "kind": "tree", "env": ENV, "name": "", "root": False}
    for target in (str(root), str(root) + os.sep):
        out, err = io.StringIO(), io.StringIO()
        code = _canvas_push.maybe_intercept(
            ["workbench", "canvas", "pull", "alpha", "-o", target, *extra], out, err)
        assert code == 1, target
        assert "canvases tree" in err.getvalue()
    # A push from it is still nothing this module knows about.
    assert _canvas_push._clone_name(str(root)) is None
    assert _canvas_push._reference_of(str(root)) is None
    assert _canvas_push._legacy_of(str(root)) is None
    # The base above the environments, and an unknown environment, are not.
    assert _canvas_push._locate(str(root.parent)) is None
    assert _canvas_push._locate(str(root.parent / "staging")) is None


def test_a_pull_outside_any_clone_falls_through(root, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    assert _canvas_push.maybe_intercept(
        ["workbench", "canvas", "pull", "beta", "-o", str(tmp_path / "beta")], out, err) is None
    # Pulling ANOTHER canvas out of a clone into a folder elsewhere is fine.
    monkeypatch.chdir(root / "user" / "alpha")
    assert _canvas_push.maybe_intercept(
        ["workbench", "canvas", "pull", "beta", "-o", str(tmp_path / "beta")],
        out, err) is None


@pytest.mark.parametrize("tail, output", [
    (["-o", "/tmp/canvas=dev/alpha"], "/tmp/canvas=dev/alpha"),
    (["-o/tmp/canvas=dev/alpha"], "/tmp/canvas=dev/alpha"),
    (["--output=/tmp/canvas=dev/alpha"], "/tmp/canvas=dev/alpha"),
    (["--output", "/tmp/canvas=dev/alpha"], "/tmp/canvas=dev/alpha"),
    ([], None),
])
def test_parse_pull_keeps_an_equals_sign_inside_the_output_path(tail, output):
    """An attached `-o` value is the rest of the token, `=` and all: only
    `--output=` splits (a clone under a path holding `=` must still be seen)."""
    parsed = _canvas_push.parse_pull(["workbench", "canvas", "pull", "alpha", *tail])
    assert parsed == {"canvas_ref": "alpha", "output": output}


# -- the scoped layout (D1476): clones in user/, reference copies beside them ----


def _mark(folder, sync, scope="user"):
    """A `.fused-canvas.json` as the mirror command (or fused-render) writes it."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".fused-canvas.json").write_text(json.dumps({
        "version": 1, "sync": sync, "scope": scope, "id": "col-1",
        "name": folder.name, "owner": None, "revision": 3,
        "last_updated": "2026-11-01T10:00:00Z",
        "pulled_at": "2026-11-02T10:00:00Z"}), encoding="utf-8")


def test_a_clone_is_user_name_and_never_the_scope_folder(root):
    """The rule must not walk up from `<env>/user/alpha` and
    call the canvas `user`."""
    clone = root / "user" / "alpha"
    (clone / "sub" / "deeper").mkdir(parents=True)
    assert _canvas_push._clone_name(str(clone)) == ("alpha", ENV)
    assert _canvas_push._clone_of(str(clone)) == ("alpha", ENV)
    assert _canvas_push._clone_of(str(clone / "sub")) == ("alpha", ENV)
    assert _canvas_push._clone_of(str(clone / "sub" / "deeper")) == ("alpha", ENV)
    # The clones folder itself is not a canvas, by either rule.
    assert _canvas_push._clone_name(str(root / "user")) is None
    assert _canvas_push._clone_of(str(root / "user")) is None
    # A live marker changes nothing; a missing one is a clone all the same.
    _mark(clone, "live")
    assert _canvas_push._clone_name(str(clone)) == ("alpha", ENV)


def test_a_clone_push_names_the_canvas_not_the_scope_segment(root, monkeypatch):
    """The push of the clone root asks the server about (alpha, env) — the
    canvas's name, not `user`."""
    server = _Server(200, _watching())
    code, _, _ = _intercept(
        ["workbench", "canvas", "push", str(root / "user" / "alpha")], monkeypatch, server)
    assert code == 0
    assert [c[1:] for c in server.calls] == [("alpha", ENV)]


@pytest.mark.parametrize("scope", ["team", "public"])
def test_a_push_from_a_reference_folder_is_refused(root, monkeypatch, scope):
    """`team/X` and `public/X` hold someone else's canvas: a raw push would
    replace (or create) the user's OWN canvas named X. Refused before the
    server is even asked, with or without one, whatever the flags."""
    ref = root / scope / "Some_Canvas"
    _mark(ref, "mirror", scope)
    (ref / "sub").mkdir()
    for target in (ref, ref / "sub", root / scope):
        for extra in ([], ["--canvas", "mine"], ["--no-validate"]):
            server = _Server(200, _watching())
            code, out, err = _intercept(
                ["workbench", "canvas", "push", str(target), *extra], monkeypatch, server)
            assert code == 1, (target, extra)
            assert "read-only reference copy" in err and out == ""
            assert server.calls == []
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    out, err = io.StringIO(), io.StringIO()
    assert _canvas_push.maybe_intercept(
        ["workbench", "canvas", "push", str(ref)], out, err) == 1
    assert _canvas_push._clone_name(str(ref)) is None
    assert _canvas_push._clone_of(str(ref)) is None


def test_a_push_from_a_reference_folder_is_refused_without_its_marker(root, monkeypatch):
    """`team/` and `public/` are reference folders by their place; a marker
    someone deleted does not turn one into something pushable."""
    ref = root / "team" / "x"
    ref.mkdir(parents=True)
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(ref)], monkeypatch, _Server(200, _watching()))
    assert code == 1 and "read-only reference copy" in err


def test_a_push_from_a_mirror_marked_user_folder_is_refused(root, monkeypatch):
    """A reference copy of one of the user's OWN canvases sits at
    `user/<name>` until they open it. It is not a clone: no state line, no
    fall-through to the raw CLI."""
    mirror = root / "user" / "beta"
    _mark(mirror, "mirror")
    server = _Server(200, _watching())
    code, out, err = _intercept(
        ["workbench", "canvas", "push", str(mirror)], monkeypatch, server)
    assert code == 1 and "read-only reference copy" in err and out == ""
    assert server.calls == []
    assert _canvas_push._clone_name(str(mirror)) is None
    # A broken marker that still SAYS mirror is refused the same way.
    (mirror / ".fused-canvas.json").write_text('{"sync": "mirror"}', encoding="utf-8")
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(mirror)], monkeypatch, server)
    assert code == 1 and "read-only reference copy" in err


@pytest.mark.parametrize("extra", [[], ["--force"], ["--dry-run"]])
def test_a_pull_into_a_reference_folder_is_refused(root, monkeypatch, extra):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    team = root / "team" / "x"
    _mark(team, "mirror", "team")
    mine = root / "user" / "beta"
    _mark(mine, "mirror")
    for target in (team, root / "public" / "new_one", root / "team", mine, mine / "sub"):
        out, err = io.StringIO(), io.StringIO()
        code = _canvas_push.maybe_intercept(
            ["workbench", "canvas", "pull", "x", "-o", str(target), *extra], out, err)
        assert code == 1, target
        assert "read-only reference copy" in err.getvalue()
    # From inside one, with no -o.
    monkeypatch.chdir(team)
    out, err = io.StringIO(), io.StringIO()
    assert _canvas_push.maybe_intercept(
        ["workbench", "canvas", "pull", "x", *extra], out, err) == 1
    assert "read-only reference copy" in err.getvalue()


def test_a_fused_web_clone_beside_the_environments_falls_through(root, monkeypatch):
    """Fused Web keeps its own clones at `<base>/<name>`, beside the
    environment folders. Neither rule may claim one."""
    web = root.parent / "webcanvas"
    (web / "sub").mkdir(parents=True)
    (web / "canvas.toml").write_text('type = "canvas"\n', encoding="utf-8")
    assert _canvas_push._locate(str(web)) is None
    assert _canvas_push._clone_name(str(web)) is None
    assert _canvas_push._clone_of(str(web / "sub")) is None
    server = _Server(200, _watching())
    code, _, _ = _intercept(["workbench", "canvas", "push", str(web)], monkeypatch, server)
    assert code is None and server.calls == []
    out, err = io.StringIO(), io.StringIO()
    assert _canvas_push.maybe_intercept(
        ["workbench", "canvas", "pull", "webcanvas", "-o", str(web)], out, err) is None
    # A Fused Web canvas literally named like a scope folder, too.
    scope_named = root.parent / "user"
    (scope_named / "alpha").mkdir(parents=True)
    assert _canvas_push._locate(str(scope_named / "alpha")) is None


def test_a_clone_still_at_its_old_folder_gets_neither_a_raw_push_nor_a_pull(root, monkeypatch):
    """A clone the layout move deferred (a session is working in it) sits at
    `<env>/<name>` with nothing syncing it. The session there must not reach
    the raw CLI, and must not be told the folder publishes itself."""
    legacy = root / "gamma"
    (legacy / "sub").mkdir(parents=True)
    (legacy / "canvas.toml").write_text('type = "canvas"\n', encoding="utf-8")
    server = _Server(200, _watching())
    code, out, err = _intercept(
        ["workbench", "canvas", "push", str(legacy)], monkeypatch, server)
    assert code == 1 and "old location" in err and out == ""
    assert server.calls == []
    out, err = io.StringIO(), io.StringIO()
    assert _canvas_push.maybe_intercept(
        ["workbench", "canvas", "pull", "gamma", "-o", str(legacy / "sub")], out, err) == 1
    assert "old location" in err.getvalue()
    # A clone literally named `user`, not yet moved: the same.
    (root / "user" / "alpha").rmdir()
    (root / "user" / "canvas.toml").write_text('type = "canvas"\n', encoding="utf-8")
    assert _canvas_push._locate(str(root / "user")) == {
        "kind": "legacy", "env": ENV, "name": "user", "root": True}
    code, _, err = _intercept(
        ["workbench", "canvas", "push", str(root / "user")], monkeypatch, server)
    assert code == 1 and "old location" in err


def test_a_plain_folder_under_an_environment_is_not_a_clone(root, monkeypatch):
    """Before D1476 any `<env>/<name>` was a clone. Now a folder there with no
    canvas.toml is nothing of ours: its push falls through."""
    stray = root / "notes"
    stray.mkdir()
    assert _canvas_push._clone_name(str(stray)) is None
    server = _Server(200, _watching())
    code, _, _ = _intercept(["workbench", "canvas", "push", str(stray)], monkeypatch, server)
    assert code is None and server.calls == []
