"""The scoped canvas layout (D1476): clones in `<env>/user/`, the marker.

A clone lives at `<env>/user/<name>`; `team/` and `public/` beside it hold
read-only reference copies written by `fused canvas mirror`, and so may a
`user/<name>` folder whose `.fused-canvas.json` says `sync: "mirror"`. What
these pin (the plan's risks R2-R4):

* R2 — a reference copy is never treated as a clone: not listed as cloned,
  no sync manager, no watcher, no hook revival.
* R3 — the marker never dirties the clone, never enters the merge base and
  is never uploaded.
* R4 — the live marker survives every pull, and follows the collection id.

Reuses test_canvases.py's harness (stub CLI, manifest/zip shims). The stub is
an EXTERNAL CLI, where no marker is written at all; the tests that need one
wire the shims in first, exactly as the seeded CLAUDE.md tests do.
"""
import json
import os

import pytest

import fused_render.canvases as canvases_mod
from fused_render import canvas_layout, canvas_marker, current_apps, launcher
from test_canvas_sync_watcher import _capture_push, _poll, _quiet_watcher, _start
from test_canvases import (  # noqa: F401 — fixtures are used by name
    ENV,
    GUARD,
    OTHER_ENV,
    _BASE_FILES,
    SyncShims,
    _cloned_shim_harness,
    _manager,
    _reset_module_state,
    _sync_view,
    _wait_for,
    _wait_sync,
    fake_agent,
    harness,
)

MARKER = canvas_marker.MARKER_NAME


def _mark(folder, sync, scope="user", **over):
    """A marker as the mirror command (or fused-render) writes it."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / MARKER).write_text(json.dumps({
        "version": 1, "sync": sync, "scope": scope, "id": "col-1",
        "name": folder.name, "owner": "ada", "revision": 3,
        "last_updated": "2026-11-01T10:00:00Z",
        "pulled_at": "2026-11-02T10:00:00Z", **over}), encoding="utf-8")


def _canvas_folder(folder, sync=None, scope="user"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "canvas.toml").write_text('type = "canvas"\n', encoding="utf-8")
    (folder / "a.py").write_text("a1\n", encoding="utf-8")
    if sync is not None:
        _mark(folder, sync, scope)
    return folder


def _marker(harness, name="alpha"):
    return canvas_marker.read(str(harness.clones / name))


# -- the paths -----------------------------------------------------------------


def test_clones_live_in_the_user_scope_folder_and_sync_state_stays_at_the_root(harness):
    assert canvases_mod.SCOPE_DIRS == ("user", "team", "public")
    assert canvases_mod.clones_root(ENV) == str(harness.root / "user")
    assert canvases_mod._canvas_dir(ENV, "alpha") == str(harness.root / "user" / "alpha")
    assert canvases_mod._lock_path(ENV, "alpha") == str(harness.root / ".sync" / "alpha.lock")
    assert all(p.startswith(str(harness.root / ".sync"))
               for p in canvases_mod._clone_leftovers(ENV, "alpha"))
    harness.log_in()
    res = harness.client.post("/api/canvases/clone", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.json() == {"ok": True, "dir": str(harness.root / "user" / "alpha")}
    manager = canvases_mod._SyncManager(ENV, "alpha")
    try:
        assert manager.dir == str(harness.root / "user" / "alpha")
        assert manager.root == str(harness.root)
        assert manager.trash_root() == str(harness.root / ".sync" / "trash" / "alpha")
        assert manager._push_copy_dir() == str(harness.root / ".sync" / "push" / "alpha")
    finally:
        manager.stop()


def test_status_names_the_clones_folder(harness):
    """The page builds a clone's path — the desk row's — as
    `canvases_dir + "/" + name`."""
    body = harness.client.get("/api/canvases/status").json()
    for env in (ENV, OTHER_ENV):
        assert body["envs"][env]["canvases_dir"] == str(harness.base / env / "user")


def test_the_seeded_claude_md_points_at_the_real_trash(harness, tmp_path, monkeypatch):
    """The clone is one level deeper than it was; the relative trash path the
    session is given must still land on `.sync/trash/<canvas>`."""
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    clone = harness.clones / "alpha"
    text = (clone / "CLAUDE.md").read_text(encoding="utf-8")
    assert "`../../.sync/trash/<canvas>/<timestamp>/`" in text
    assert "`../.sync/" not in text
    assert os.path.normpath(clone / ".." / ".." / ".sync" / "trash" / "alpha") == \
        str(harness.root / ".sync" / "trash" / "alpha")


def test_clone_dir_for_is_user_name_by_shape_alone(harness):
    clone = str(harness.clones / "alpha")
    assert canvases_mod.clone_dir_for(clone) == (ENV, "alpha", clone)
    assert canvases_mod.clone_dir_for(clone + "/a.py") == (ENV, "alpha", clone)
    assert canvases_mod.clone_dir_for(clone + "/sub/deep.py") == (ENV, "alpha", clone)
    other = str(harness.base / OTHER_ENV / "user" / "beta")
    assert canvases_mod.clone_dir_for(other) == (OTHER_ENV, "beta", other)
    for not_a_clone in (
            str(harness.root),                      # the environment's root
            str(harness.clones),                    # the clones folder itself
            str(harness.root / "alpha"),            # the pre-D1476 path
            str(harness.root / "alpha" / "a.py"),
            str(harness.root / "team" / "x"),       # reference copies
            str(harness.root / "team" / "x" / "a.py"),
            str(harness.root / "public" / "y" / "a.py"),
            str(harness.root / ".sync" / "push" / "alpha"),
            str(harness.clones / "alpha-12345678"),  # the mirror's collision suffix
            str(harness.base / "webcanvas"),        # Fused Web's, beside the envs
            "relative/user/alpha", ""):
        assert canvases_mod.clone_dir_for(not_a_clone) is None, not_a_clone


def test_the_task_door_the_desk_row_and_the_launcher_follow_the_new_path(harness):
    from fused_render.server.routers import tasks as tasks_router

    tasks_router._CANVAS_ROOTS_MEMO.clear()
    clone = harness.clones / "alpha"
    assert tasks_router._task_canvas(str(clone)) == {"env": ENV, "name": "alpha", "file": ""}
    assert tasks_router._task_canvas(str(clone / "a.py")) == {
        "env": ENV, "name": "alpha", "file": "a.py"}
    assert tasks_router._task_canvas(str(clone / "canvas.toml")) is None
    for elsewhere in (harness.root / "alpha", harness.root / "alpha" / "a.py",
                      harness.root / "team" / "x", harness.root / "team" / "x" / "a.py",
                      harness.root / "user"):
        assert tasks_router._task_canvas(str(elsewhere)) is None, elsewhere
    tasks_router._CANVAS_ROOTS_MEMO.clear()

    assert current_apps._canvas_of(str(clone)) == {"env": ENV, "name": "alpha"}
    assert current_apps._canvas_of(str(clone / "a.py")) is None
    assert current_apps._canvas_of(str(harness.root / "alpha")) is None
    assert current_apps._canvas_of(str(harness.root / "public" / "y")) is None

    assert launcher.canvas_page_for(str(clone)) == f"/canvases/{ENV}/alpha"
    assert launcher.canvas_page_for(str(harness.root / "alpha")) is None
    assert launcher.canvas_page_for(str(harness.root / "team" / "alpha")) is None


# -- R2: a reference copy is never a clone -------------------------------------


def test_a_mirror_marked_folder_is_not_listed_as_cloned(harness):
    harness.log_in()
    _canvas_folder(harness.clones / "alpha", "mirror")     # a reference copy
    _canvas_folder(harness.clones / "beta")                # a clone, no marker
    res = harness.client.get("/api/canvases/list?env=unstable", headers=GUARD)
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["alpha"]["cloned"] is False
    assert canvases["alpha"]["n_udfs"] is None and canvases["alpha"]["mtime"] is None
    assert canvases["beta"]["cloned"] is True and canvases["beta"]["n_udfs"] == 1
    assert canvases_mod._cloned_names(ENV) == {"beta"}
    assert [c[:2] for c in canvases_mod.cloned_canvases()] == [(ENV, "beta")]
    assert harness.client.get("/api/canvases/status").json()["envs"][ENV]["cloned_count"] == 1


@pytest.mark.parametrize("marker, cloned", [
    (None, True),                                    # no marker: a clone
    ("", True),                                      # empty file
    ("{not json", True),                             # unreadable
    (json.dumps({"sync": "frozen"}), True),          # an unknown owner kind
    (json.dumps({"version": 1, "sync": "live", "scope": "user", "id": "c",
                 "name": "alpha"}), True),
    (json.dumps({"sync": "mirror"}), False),         # broken, but it says mirror
    (json.dumps({"version": 1, "sync": "mirror", "scope": "user", "id": "c",
                 "name": "alpha"}), False),
])
def test_a_missing_or_broken_marker_with_a_canvas_toml_is_a_live_clone(harness, marker, cloned):
    folder = _canvas_folder(harness.clones / "alpha")
    if marker is not None:
        (folder / MARKER).write_text(marker, encoding="utf-8")
    assert canvases_mod._is_live_clone(ENV, "alpha") is cloned
    assert ("alpha" in canvases_mod._cloned_names(ENV)) is cloned


def test_a_folder_without_a_canvas_toml_is_never_a_clone(harness):
    (harness.clones / "alpha").mkdir(parents=True)
    _mark(harness.clones / "beta", "live")
    assert canvases_mod._is_live_clone(ENV, "alpha") is False
    assert canvases_mod._is_live_clone(ENV, "beta") is False
    assert canvases_mod._cloned_names(ENV) == set()


def test_only_canvas_named_folders_under_user_are_clones(harness):
    """The mirror's collision folder `<name>-<id8>` holds a canvas.toml and
    can never be a clone: `-` is not in a canvas name."""
    _canvas_folder(harness.clones / "alpha-12345678")
    _canvas_folder(harness.clones / ".alpha-tmp")
    _canvas_folder(harness.root / "team" / "gamma", "mirror", "team")
    _canvas_folder(harness.root / "public" / "delta", "mirror", "public")
    assert canvases_mod._cloned_names(ENV) == set()
    assert canvases_mod.cloned_canvases() == []


def test_sync_start_refuses_a_reference_copy_and_takes_no_lock(harness):
    harness.log_in()
    _canvas_folder(harness.clones / "alpha", "mirror")
    res = harness.client.post("/api/canvases/sync/start",
                              json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert res.status_code == 409 and "not cloned yet" in res.json()["error"]
    assert (ENV, "alpha") not in canvases_mod._syncs
    assert not (harness.root / ".sync" / "alpha.lock").exists()
    # Nothing was written into the reference copy either.
    assert sorted(os.listdir(harness.clones / "alpha")) == [MARKER, "a.py", "canvas.toml"]
    assert _marker(harness).sync == "mirror"
    assert [c for c in harness.calls() if "push" in c] == []


@pytest.mark.parametrize("event", ["start", "pre", "post", "stop"])
def test_a_session_hook_in_a_reference_copy_starts_no_watcher(harness, event):
    """`{}` and no manager, for a mirror-marked `user/` folder and for a
    `team/`/`public/` one: a hook must never bring up the sync that would
    push a reference copy over the user's canvas."""
    harness.log_in()
    mine = _canvas_folder(harness.clones / "alpha", "mirror")
    team = _canvas_folder(harness.root / "team" / "alpha", "mirror", "team")
    for folder in (mine, team):
        res = harness.client.post("/api/canvases/agent/hook", headers=GUARD, json={
            "event": event, "session_id": "s1", "cwd": str(folder),
            "tool_name": "Write", "tool_input": {"file_path": str(folder / "a.py")}})
        assert res.status_code == 200
        # The one thing such a hook is ever told: the write is refused
        # (D1478). Every other event is answered with nothing.
        assert res.json() == ({"deny": canvases_mod._REFERENCE_COPY_DENY}
                              if event == "pre" else {})
    assert canvases_mod._syncs == {}
    assert canvases_mod._revive_for_session(ENV, "alpha", "start hook") is None
    assert canvases_mod.session_turn_start(str(mine), "codex") is False
    assert canvases_mod.session_turn_start(str(team / "a.py"), "codex") is False
    assert not (harness.root / ".sync").exists()


def test_a_session_hook_in_a_clone_still_starts_its_watcher(harness):
    """The control for the test above: same hook, a folder with no marker."""
    harness.log_in()
    clone = _canvas_folder(harness.clones / "alpha")
    res = harness.client.post("/api/canvases/agent/hook", headers=GUARD, json={
        "event": "start", "session_id": "s1", "cwd": str(clone)})
    assert res.status_code == 200
    assert (ENV, "alpha") in canvases_mod._syncs


# -- a session in a clone may read reference copies, never edit them (D1478) ----


def _pre(harness, cwd, file_path, tool="Edit", key="file_path"):
    res = harness.client.post("/api/canvases/agent/hook", headers=GUARD, json={
        "event": "pre", "session_id": "s1", "cwd": str(cwd), "tool_name": tool,
        "tool_input": {key: str(file_path)}})
    assert res.status_code == 200
    return res.json()


def test_the_pre_hook_refuses_an_edit_of_a_reference_copy(harness):
    """R11. The session sits in its own live clone and reaches for a file in
    a reference copy: `team/`, `public/`, or a `user/<name>` the mirror wrote
    and the user never opened. Refused every time, with a reason."""
    harness.log_in()
    clone = _canvas_folder(harness.clones / "alpha")
    team = _canvas_folder(harness.root / "team" / "gamma", "mirror", "team")
    public = _canvas_folder(harness.root / "public" / "delta", "mirror", "public")
    mine = _canvas_folder(harness.clones / "beta", "mirror")
    deny = {"deny": canvases_mod._REFERENCE_COPY_DENY}
    assert "read-only reference copy" in deny["deny"]
    for _again in range(2):                       # not a one-time refusal
        for target in (team / "a.py", public / "a.py", mine / "a.py",
                       team / "new_node.py", public / "sub" / "x.json",
                       harness.root / "team" / "loose.py",
                       harness.root / "public" / "brand_new" / "canvas.toml"):
            assert _pre(harness, clone, target) == deny, target
    assert _pre(harness, clone, team / "a.py", tool="Write") == deny
    assert _pre(harness, clone, team / "nb.ipynb", tool="NotebookEdit",
                key="notebook_path") == deny
    # A relative path is the session's cwd's.
    assert _pre(harness, clone, os.path.join("..", "..", "team", "gamma", "a.py")) == deny
    assert _pre(harness, clone, os.path.join("..", "beta", "a.py")) == deny
    # Refusing starts nothing for the reference copy.
    assert (ENV, "gamma") not in canvases_mod._syncs
    assert (ENV, "beta") not in canvases_mod._syncs
    assert not (harness.root / ".sync" / "beta.lock").exists()


def test_the_pre_hook_lets_a_session_edit_its_own_clone_and_other_clones(harness):
    """The control: the session's own files, a new file in its clone, and a
    file in ANOTHER live clone of the user's are not reference copies. (The
    last is outside the session's folder, so the CLI's own permission card
    decides it; this hook has no opinion.)"""
    harness.log_in()
    clone = _canvas_folder(harness.clones / "alpha")
    other = _canvas_folder(harness.clones / "beta", "live")
    _canvas_folder(harness.root / "team" / "gamma", "mirror", "team")
    for target in (clone / "a.py", clone / "new_node.py", clone / "canvas.toml",
                   "a.py", other / "a.py"):
        assert "deny" not in _pre(harness, clone, target), target
    assert (ENV, "alpha") in canvases_mod._syncs      # its own hook still revives
    # No file at all in the call (a tool with no path): nothing to refuse.
    res = harness.client.post("/api/canvases/agent/hook", headers=GUARD, json={
        "event": "pre", "session_id": "s1", "cwd": str(clone), "tool_name": "Edit",
        "tool_input": {}})
    assert res.json() == {}
    # Reads are never this hook's business: only `pre` carries a refusal.
    for event in ("post", "start", "stop"):
        res = harness.client.post("/api/canvases/agent/hook", headers=GUARD, json={
            "event": event, "session_id": "s1", "cwd": str(clone), "tool_name": "Read",
            "tool_input": {"file_path": str(harness.root / "team" / "gamma" / "a.py")}})
        assert "deny" not in res.json(), event


def test_the_refusal_reaches_claude_as_a_pretooluse_deny(harness):
    """End to end through the script Claude actually runs: canvas_hook.py
    reads the CLI's hook JSON on stdin, posts it to the route, and prints
    the PreToolUse decision. A reference file is denied with the reason; a
    file of the session's own clone prints nothing (the call goes ahead)."""
    import http.server
    import subprocess
    import sys
    import threading

    harness.log_in()
    clone = _canvas_folder(harness.clones / "alpha")
    team = _canvas_folder(harness.root / "team" / "gamma", "mirror", "team")

    class Relay(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            res = harness.client.post(self.path, json=body,
                                      headers={"X-Fused": self.headers.get("X-Fused", "")})
            data = res.content
            self.send_response(res.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hook = os.path.join(os.path.dirname(canvases_mod.__file__), "claude_agent",
                        "canvas_hook.py")

    def run(file_path):
        out = subprocess.run(
            [sys.executable, "-I", hook, "pre"], capture_output=True, text=True, timeout=30,
            input=json.dumps({"session_id": "s1", "cwd": str(clone), "tool_name": "Edit",
                              "hook_event_name": "PreToolUse",
                              "tool_input": {"file_path": str(file_path)}}),
            env={**os.environ,
                 "FUSED_RENDER_ORIGIN": "http://127.0.0.1:%d" % server.server_address[1]})
        assert out.returncode == 0, out.stderr
        return out.stdout

    try:
        denied = json.loads(run(team / "a.py"))["hookSpecificOutput"]
        assert denied == {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                          "permissionDecisionReason": canvases_mod._REFERENCE_COPY_DENY}
        assert run(clone / "a.py") == ""
    finally:
        server.shutdown()
        server.server_close()


def test_in_reference_copy_reads_the_place_and_the_marker(harness, tmp_path):
    clone = _canvas_folder(harness.clones / "alpha")
    live = _canvas_folder(harness.clones / "beta", "live")
    mine = _canvas_folder(harness.clones / "gamma", "mirror")
    team = harness.root / "team"
    yes = (mine, mine / "a.py", team / "x", team / "x" / "a.py", team / "loose.py",
           harness.root / "public" / "y" / "deep" / "f.py",
           harness.base / OTHER_ENV / "team" / "z" / "a.py")
    no = (clone, clone / "a.py", live / "a.py", harness.clones, team, harness.root,
          harness.root / "public", harness.root / ".sync" / "trash" / "alpha" / "a.py",
          harness.root / "alpha" / "a.py",              # the pre-D1476 path
          harness.base / "webcanvas" / "a.py",          # Fused Web's clone
          tmp_path / "elsewhere" / "team" / "x" / "a.py")
    for path in yes:
        assert canvases_mod.in_reference_copy(str(path)) is True, path
    for path in no:
        assert canvases_mod.in_reference_copy(str(path)) is False, path
    for bad in ("", "team/x/a.py", None, "/nul\x00byte/team/x"):
        assert canvases_mod.in_reference_copy(bad) is False
    # A path some tool already resolved, and one reached through a link.
    assert canvases_mod.in_reference_copy(os.path.realpath(str(team / "x" / "a.py"))) is True
    link = tmp_path / "shortcut"
    os.symlink(team, link, target_is_directory=True)
    (team / "x").mkdir(parents=True, exist_ok=True)
    assert canvases_mod.in_reference_copy(str(link / "x" / "a.py")) is True


def test_the_live_marker_is_never_written_over_a_mirror_one(harness, tmp_path, monkeypatch):
    """`_mark_live` marks clones. Turning a reference copy into one is a
    deliberate, separate step; no code path here does it by accident."""
    SyncShims(harness, tmp_path, monkeypatch)
    _canvas_folder(harness.clones / "alpha", "mirror")
    assert canvases_mod._mark_live(ENV, "alpha", "col-9", 4) is False
    assert _marker(harness).sync == "mirror" and _marker(harness).id == "col-1"


def test_delete_runs_on_the_server_and_removes_a_reference_copy_of_that_canvas(harness):
    """Deleting a canvas the user never opened: its reference copy at
    `user/<name>` goes with it, like a clone would."""
    harness.log_in()
    _canvas_folder(harness.clones / "alpha", "mirror")
    _canvas_folder(harness.root / "team" / "alpha", "mirror", "team")
    res = harness.client.post("/api/canvases/delete", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    assert ["workbench", "canvas", "delete", "alpha"] in harness.calls()
    assert not (harness.clones / "alpha").exists()
    # Another scope's folder of the same name is not this canvas's.
    assert (harness.root / "team" / "alpha" / "canvas.toml").is_file()


# -- R3: the marker is local state ---------------------------------------------


def test_a_marker_write_does_not_dirty_the_clone(harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _poll(m)
    with m._flight_lock:
        m._tick()                  # the first scan, which counts the folder once
    clone = harness.clones / "alpha"
    assert _marker(harness).sync == "live"
    fingerprint, hashes, rev = m._take_fingerprint(), m._take_file_hashes(m.dir), m.disk_rev
    assert MARKER not in fingerprint and MARKER not in hashes
    os.remove(clone / MARKER)
    assert canvases_mod._mark_live(ENV, "alpha", "c9", 12) is True
    assert _marker(harness).id == "c9"
    assert m._take_fingerprint() == fingerprint
    assert m._take_file_hashes(m.dir) == hashes
    assert m._take_file_hashes(m.dir) == m._base_files      # still at the sync point
    # Neither the fs watcher (given time to see the write) nor a scan tick
    # reads it as a change to the folder.
    assert not _wait_for(lambda: m.disk_rev != rev, timeout=0.6)
    with m._flight_lock:
        m._tick()
    assert m.disk_rev == rev and m.push_state == "idle" and m._dirty_since is None
    assert [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]] == []
    # Nothing but the marker itself appeared in the folder (no staging file).
    assert sorted(os.listdir(clone)) == sorted(
        [".fusedignore", MARKER, "CLAUDE.md", *_BASE_FILES])


def test_the_marker_is_never_in_the_pushed_copy(harness, tmp_path, monkeypatch, fake_agent):
    """Not with the seeded `.fusedignore`, and not when the user rewrote that
    file without the marker's line: the freeze drops it by name."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    seen: dict = {}
    _capture_push(monkeypatch, seen)
    m = _start(harness)
    clone = harness.clones / "alpha"
    assert (clone / MARKER).is_file()
    assert MARKER in (clone / ".fusedignore").read_text().splitlines()
    (clone / "sub").mkdir()
    (clone / "sub" / MARKER).write_text("{}", encoding="utf-8")
    (clone / "sub" / "keep.txt").write_text("k", encoding="utf-8")
    (clone / "a.py").write_text("a2\n")
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    assert MARKER not in seen["files"][0] and "a.py" in seen["files"][0]
    assert seen["sub"][0] == ["keep.txt"]
    # The user's own .fusedignore, with no line for the marker. Written with
    # the watcher paused so the reseed cannot put the line back first.
    (clone / ".fusedignore").write_text("scratch_*.csv\n")
    with m._op_lock:
        copy = m._freeze_copy()
        assert MARKER not in os.listdir(copy) and "a.py" in os.listdir(copy)
        assert os.listdir(os.path.join(copy, "sub")) == ["keep.txt"]


def test_a_marker_in_the_server_bundle_never_replaces_the_local_one(
        harness, tmp_path, monkeypatch, fake_agent):
    """Someone pushed a marker with `--no-ignore`. The merge takes the files
    and leaves this clone's own marker alone: a `mirror` one landing here
    would stop the sync, and any would lie about the collection id."""
    _quiet_watcher(monkeypatch)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _poll(m)
    clone = harness.clones / "alpha"
    before = (clone / MARKER).read_bytes()
    foreign = json.dumps({"version": 1, "sync": "mirror", "scope": "public",
                          "id": "someone-elses", "name": "alpha"})
    shims.set_remote_files({**_BASE_FILES, "b.py": "b2-remote\n", MARKER: foreign})
    shims.set_manifest("t2")
    _poll(m)
    assert (clone / "b.py").read_text() == "b2-remote\n"
    assert m.merge_seq == 1
    assert (clone / MARKER).read_bytes() == before
    assert _marker(harness).sync == "live" and _marker(harness).id == "c1"
    assert MARKER not in (m._base_files or {})
    assert canvases_mod._is_live_clone(ENV, "alpha") is True
    # ...and the trash snapshot of what the merge replaced holds no marker.
    trashed = [p.name for p in (harness.root / ".sync" / "trash" / "alpha").rglob("*")]
    assert MARKER not in trashed


# -- R4: the live marker survives pulls and follows the collection id ----------


def test_clone_marks_the_folder_live_when_the_id_is_known(harness, tmp_path, monkeypatch):
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    shims = SyncShims(harness, tmp_path, monkeypatch)
    shims.seed_base("alpha", _BASE_FILES, "t1", revision=7)      # remote.id == "c1"
    res = harness.client.post("/api/canvases/clone", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    marker = _marker(harness)
    assert (marker.sync, marker.scope, marker.id, marker.name) == ("live", "user", "c1", "alpha")
    # A re-clone runs `pull --force`, which deletes the marker like any file
    # the bundle does not hold; it is back afterwards.
    os.utime(harness.clones / "alpha" / MARKER, (1, 1))
    harness.client.post("/api/canvases/clone", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert _marker(harness).sync == "live"
    assert (harness.clones / "alpha" / MARKER).stat().st_mtime > 1   # deleted and rewritten


def test_clone_takes_the_id_from_the_stored_listing(harness, tmp_path, monkeypatch):
    harness.log_in()
    SyncShims(harness, tmp_path, monkeypatch)
    monkeypatch.setattr(canvases_mod, "_listing_cache_read", lambda env: [
        {"name": "beta", "id": "col-b"}, {"name": "alpha", "id": "col-a"}] if env == ENV else None)
    assert canvas_layout.known_collection_id(ENV, "alpha") == "col-a"
    assert canvas_layout.known_collection_id(ENV, "gamma") is None
    assert canvas_layout.known_collection_id(OTHER_ENV, "alpha") is None
    harness.client.post("/api/canvases/clone", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert _marker(harness).id == "col-a"
    # The sync point, once there is one, is the better witness.
    (harness.root / ".sync").mkdir(exist_ok=True)
    (harness.root / ".sync" / "alpha.json").write_text(
        json.dumps({"collection_id": "col-sync", "remote": {"id": "col-old"}}))
    assert canvas_layout.known_collection_id(ENV, "alpha") == "col-sync"
    (harness.root / ".sync" / "alpha.json").write_text(json.dumps({"remote": {"id": "col-old"}}))
    assert canvas_layout.known_collection_id(ENV, "alpha") == "col-old"


def test_a_clone_with_an_unknown_id_keeps_no_marker_until_the_sync_learns_it(
        harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    shims = SyncShims(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/clone", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    clone = harness.clones / "alpha"
    assert not (clone / MARKER).exists()                  # unmanaged, never a wrong id
    assert canvases_mod._is_live_clone(ENV, "alpha") is True
    shims.set_manifest("t1")
    shims.set_remote_files(_BASE_FILES)
    m = _start(harness)
    assert not (clone / MARKER).exists()                  # still nothing knows it
    _poll(m)                                              # the first look adopts the manifest
    assert m._base_collection_id == "c1"
    assert _marker(harness).sync == "live" and _marker(harness).id == "c1"


def test_sync_start_marks_a_clone_from_before_the_marker(harness, tmp_path, monkeypatch,
                                                        fake_agent):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)      # base seeded after the clone
    clone = harness.clones / "alpha"
    assert not (clone / MARKER).exists()
    _start(harness)
    assert _marker(harness).sync == "live" and _marker(harness).id == "c1"


def test_a_hard_pull_puts_the_marker_back_and_leaves_the_clone_clean(
        harness, tmp_path, monkeypatch, fake_agent):
    """`pull --force` deletes the marker; it is rewritten AFTER the dry-run
    recheck (a marker on disk before it would read as a local diff and queue
    a push of what was just pulled)."""
    _quiet_watcher(monkeypatch)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _poll(m)
    clone = harness.clones / "alpha"
    os.utime(clone / MARKER, (1, 1))
    remote = {**_BASE_FILES, "b.py": "b2-remote\n"}
    harness.set_scenario({"pull_files": remote})
    shims.set_remote_files(remote)
    shims.set_manifest("t2")
    res = harness.client.post("/api/canvases/sync/pull",
                              json={"env": ENV, "name": "alpha", "hard": True}, headers=GUARD)
    assert res.json()["ok"] is True, res.text
    assert (clone / "b.py").read_text() == "b2-remote\n" and m.pull_seq == 1
    assert _marker(harness).sync == "live" and _marker(harness).id == "c1"
    assert (clone / MARKER).stat().st_mtime > 1               # deleted by the pull, rewritten
    assert m._dirty_since is None and m.push_state == "idle"
    assert [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]] == []


def test_the_marker_follows_a_recreated_canvas_to_its_new_id(
        harness, tmp_path, monkeypatch, fake_agent):
    """Deleted and re-created under its name: a new collection id. Left
    stale, the next mirror run would pull the "new" canvas in beside this
    clone as `alpha-<id8>`."""
    _quiet_watcher(monkeypatch)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    shims.seed_base("alpha", _BASE_FILES, "t1", revision=1)
    shims.set_manifest("t1", revision=1)
    m = _start(harness)
    _poll(m)
    assert _marker(harness).id == "c1"
    shims.set_remote_files({**_BASE_FILES, "b.py": "b-recreated\n"})
    shims.set_manifest("t2", revision=1, id="c2")
    _poll(m)
    assert m._base_collection_id == "c2"
    marker = _marker(harness)
    assert (marker.sync, marker.id, marker.name) == ("live", "c2", "alpha")


def test_the_external_cli_path_writes_no_marker(harness, fake_agent, monkeypatch):
    """No shim: remote moves are detected with `pull --dry-run`, which reports
    a local-only file as a diff on every poll. The folder stays unmanaged
    (the mirror command never touches it) and is a clone all the same."""
    _quiet_watcher(monkeypatch)
    harness.log_in()
    (harness.root / ".sync").mkdir(parents=True)
    (harness.root / ".sync" / "alpha.json").write_text(json.dumps({"collection_id": "c1"}))
    harness.client.post("/api/canvases/clone", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    m = _start(harness)
    clone = harness.clones / "alpha"
    assert canvases_mod._mark_live(ENV, "alpha", "c1") is False
    res = harness.client.post("/api/canvases/sync/pull",
                              json={"env": ENV, "name": "alpha", "hard": True}, headers=GUARD)
    assert res.json()["ok"] is True, res.text
    assert sorted(os.listdir(clone)) == ["canvas.toml"]
    assert canvases_mod._is_live_clone(ENV, "alpha") is True and m.push_state == "idle"
    probe = [c for c in harness.calls() if "--dry-run" in c]
    assert probe, "the legacy pull never rechecked"


def test_rename_carries_the_marker_to_the_new_name(harness, tmp_path, monkeypatch, fake_agent):
    from fused_render import claude_session_move

    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    _start(harness)
    assert _marker(harness).name == "alpha"
    monkeypatch.setattr(claude_session_move, "held_sessions", lambda root: [])
    monkeypatch.setattr(claude_session_move, "relocate",
                        lambda old, new, **kw: {"moved": [], "pending": [], "complete": True})
    res = harness.client.post("/api/canvases/rename", headers=GUARD,
                              json={"env": ENV, "name": "alpha", "new_name": "gamma"})
    assert res.status_code == 200, res.text
    assert not (harness.clones / "alpha").exists()
    assert (harness.clones / "gamma" / "canvas.toml").is_file()
    assert (harness.root / ".sync" / "gamma.json").is_file()
    marker = _marker(harness, "gamma")
    assert (marker.sync, marker.id, marker.name) == ("live", "c1", "gamma")


def test_rename_leaves_a_reference_copy_where_it_is(harness, monkeypatch):
    """`user/<name>` holding a reference copy is not a clone to move: the
    server rename runs, and the next mirror run rewrites the copy."""
    harness.log_in()
    _canvas_folder(harness.clones / "alpha", "mirror")
    res = harness.client.post("/api/canvases/rename", headers=GUARD,
                              json={"env": ENV, "name": "alpha", "new_name": "gamma"})
    assert res.status_code == 200, res.text
    assert ["workbench", "canvas", "rename", "alpha", "gamma"] in harness.calls()
    assert (harness.clones / "alpha" / MARKER).is_file()
    assert not (harness.clones / "gamma").exists()
