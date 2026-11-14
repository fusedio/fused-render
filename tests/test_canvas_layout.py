"""Moving canvas clones into `<env>/user/`, once (canvas_layout.py, D1476).

Before D1476 a clone lived at `<env>/<name>`. `ensure_layout` moves each one
to `<env>/user/<name>` and carries what names it by absolute path: the desk
row and the Claude transcripts. What these pin:

* a clone another fused-render is syncing is deferred, and a folder
  already at the new path is NEVER overwritten.
* the desk row and the chat history follow the clone; a session at work
  in it defers the move and a later pass makes it; a crash between the
  rename and the carry is finished from the journal.
* a clone literally named `user`, `team` or `public` ends up at
  `user/<that name>`, with `.sync/` untouched, and a second pass changes
  nothing.

The harness is test_canvases.py's (stub CLI); stores and the Claude session
store are pointed at the test's own tmp folders.
"""
import json
import os
import subprocess
import sys

import pytest

import fused_render.canvases as canvases_mod
from fused_render import (
    canvas_layout,
    canvas_marker,
    canvas_sync_state,
    claude_session_move,
    current_apps,
)
from fused_render._view_url_codec import canonical_fs_path
from test_canvas_sync_watcher import _HOLDER
from test_canvases import (  # noqa: F401 — fixtures are used by name
    ENV,
    GUARD,
    OTHER_ENV,
    SyncShims,
    _reset_module_state,
    harness,
)

MARKER = canvas_marker.MARKER_NAME


@pytest.fixture(autouse=True)
def _fresh_layout_state():
    canvas_layout.reset()
    yield
    canvas_layout.reset()


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """A private state home (the desk) and Claude session store."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    projects = tmp_path / "claude" / "projects"
    sessions = tmp_path / "claude" / "sessions"
    projects.mkdir(parents=True)
    sessions.mkdir(parents=True)
    monkeypatch.setattr(claude_session_move, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(claude_session_move, "SESSIONS_DIR", str(sessions))
    return projects, sessions


def _legacy(root, name, files=None):
    """A clone at the pre-D1476 path `<env>/<name>`."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {"canvas.toml": f'name = "{name}"\n',
                                   "a.py": f"# {name}\n"}).items():
        (folder / rel).write_text(content, encoding="utf-8")
    return folder


def _tree(folder):
    """{relative path: bytes} of everything under `folder`."""
    out = {}
    for dirpath, _dirs, files in os.walk(folder):
        for f in files:
            path = os.path.join(dirpath, f)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, folder)] = fh.read()
    return out


def _transcript(projects, cwd, sid):
    """A finished Claude session whose working directory was `cwd`."""
    bucket = projects / claude_session_move.munge(cwd)
    bucket.mkdir(exist_ok=True)
    lines = [
        json.dumps({"type": "permission-mode", "permissionMode": "auto"}),
        json.dumps({"type": "user", "cwd": cwd,
                    "message": {"content": "edit %s" % os.path.join(cwd, "a.py")}}),
        json.dumps({"type": "assistant", "cwd": cwd, "message": {"content": "ok"}}),
    ]
    (bucket / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bucket


def _desk():
    return [a["path"] for a in current_apps.read_state()["apps"]]


def _journal(root):
    path = root / ".sync" / canvas_layout.JOURNAL_NAME
    if not path.exists():
        return None
    # Without `since` (when the move was made): the tests compare the moves.
    return [{k: v for k, v in m.items() if k != "since"}
            for m in json.loads(path.read_text())["moves"]]


# -- detection -----------------------------------------------------------------


def test_a_legacy_clone_is_found_by_structure(harness):
    root = harness.root
    _legacy(root, "alpha")
    _legacy(root, "Zeta")
    _legacy(root, "beta", {"a.py": "no canvas.toml here\n"})       # not a clone
    _legacy(root, "bad-name")                                      # not a canvas name
    _legacy(root, ".hidden")
    (root / ".sync" / "push" / "alpha").mkdir(parents=True)
    (root / ".sync" / "canvas.toml").write_text("")                # never a name
    _legacy(root / "user", "gamma")                                # new layout already
    _legacy(root / "team", "delta")
    assert canvas_layout.legacy_clones(ENV) == ["alpha", "Zeta"]
    assert canvas_layout.legacy_clones(OTHER_ENV) == []


def test_reserved_names_come_first_user_before_everything(harness):
    for name in ("alpha", "public", "team", "user"):
        _legacy(harness.root, name)
    assert canvas_layout.legacy_clones(ENV) == ["user", "team", "public", "alpha"]


def test_a_scope_folder_of_the_new_layout_is_not_a_legacy_clone(harness):
    for scope in ("user", "team", "public"):
        _legacy(harness.root / scope, "alpha")
    assert canvas_layout.legacy_clones(ENV) == []
    report = canvas_layout.ensure_layout(ENV)
    assert not report.pending() and report.moved == []


# -- the move ------------------------------------------------------------------


def test_ensure_layout_moves_every_clone_into_user_and_leaves_sync_alone(harness, stores):
    root = harness.root
    before = {name: _tree(_legacy(root, name)) for name in ("alpha", "beta")}
    (root / ".sync" / "trash" / "alpha" / "t1").mkdir(parents=True)
    (root / ".sync" / "alpha.json").write_text('{"files": {}}')
    (root / ".sync" / "alpha.tombstones.json").write_text("{}")
    sync_before = _tree(root / ".sync")
    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == ["alpha", "beta"]
    assert not report.pending() and report.deferred == {} and report.conflicts == {}
    for name, files in before.items():
        assert not (root / name).exists()
        assert _tree(root / "user" / name) == files
    # The merge base, tombstones and trash stay keyed by name at the root.
    assert _tree(root / ".sync") == sync_before
    assert sorted(os.listdir(root)) == [".sync", "user"]
    assert canvases_mod._cloned_names(ENV) == {"alpha", "beta"}
    assert canvas_layout.legacy_clones(ENV) == []


def test_a_second_pass_changes_nothing(harness, stores, monkeypatch):
    root = harness.root
    _legacy(root, "alpha")
    canvas_layout.ensure_layout(ENV)
    after = _tree(root)
    runs = []
    real = canvas_layout._run
    monkeypatch.setattr(canvas_layout, "_run",
                        lambda *a, **k: runs.append(a) or real(*a, **k))
    # Remembered for the life of the process: not even a listdir.
    for _ in range(3):
        report = canvas_layout.ensure_layout(ENV)
        assert report.moved == [] and not report.pending()
    assert runs == []
    # Forced, it looks again and still finds nothing to do.
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert len(runs) == 1 and report.moved == [] and not report.pending()
    assert _tree(root) == after
    assert os.listdir(root / ".sync") == []       # no lock, no journal left behind


def test_nothing_to_move_creates_nothing(harness):
    """A machine that never opened a canvas keeps a bare `~/Fused`."""
    report = canvas_layout.ensure_layout(ENV)
    assert not report.pending()
    assert not harness.base.exists()
    canvas_layout.ensure_all()
    assert not harness.base.exists()


def test_only_the_environment_folders_are_walked(harness, stores):
    """Fused Web keeps its own clones at `<base>/<name>`, beside the
    environment folders; another environment moves on its own call."""
    web = _legacy(harness.base, "webcanvas")
    other = _legacy(harness.base / OTHER_ENV, "alpha")
    _legacy(harness.root, "alpha")
    canvas_layout.ensure_layout(ENV)
    assert (web / "canvas.toml").is_file() and (other / "canvas.toml").is_file()
    assert (harness.clones / "alpha" / "canvas.toml").is_file()
    canvas_layout.ensure_all()
    assert (harness.base / OTHER_ENV / "user" / "alpha" / "canvas.toml").is_file()
    assert (web / "canvas.toml").is_file()
    assert not (harness.base / "user").exists()


# -- what names the clone by path follows it -------------------------------


def test_the_desk_row_and_the_chat_history_follow_the_clone(harness, stores):
    projects, _sessions = stores
    root = harness.root
    old = _legacy(root, "alpha")
    new = root / "user" / "alpha"
    _legacy(root, "alphabet")                       # a sibling sharing the prefix
    current_apps.add(str(old))
    current_apps.add(str(root / "alphabet"))
    current_apps.add(str(harness.base.parent / "elsewhere"))
    _transcript(projects, str(old), "sid-clone")
    _transcript(projects, str(root / "alphabet"), "sid-sibling")
    assert canonical_fs_path(str(old)) in _desk()

    assert canvas_layout.ensure_layout(ENV).moved == ["alpha", "alphabet"]

    desk = _desk()
    assert canonical_fs_path(str(new)) in desk
    assert canonical_fs_path(str(root / "user" / "alphabet")) in desk
    assert canonical_fs_path(str(old)) not in desk
    assert canonical_fs_path(str(harness.base.parent / "elsewhere")) in desk
    # The desk row now IS a canvas row again (the clone under `user/`).
    assert current_apps._canvas_of(canonical_fs_path(str(new))) == {"env": ENV, "name": "alpha"}

    moved = projects / claude_session_move.munge(str(new)) / "sid-clone.jsonl"
    assert moved.is_file()
    assert not (projects / claude_session_move.munge(str(old)) / "sid-clone.jsonl").exists()
    rows = [json.loads(line) for line in moved.read_text().splitlines()]
    assert [r["cwd"] for r in rows if "cwd" in r] == [str(new), str(new)]
    assert rows[1]["message"]["content"] == "edit %s" % (new / "a.py")
    # Each transcript went to its OWN clone's bucket.
    sibling = projects / claude_session_move.munge(str(root / "user" / "alphabet"))
    assert (sibling / "sid-sibling.jsonl").is_file()
    assert _journal(root) is None


def test_a_session_at_work_defers_the_move_and_a_later_pass_makes_it(harness, stores):
    projects, sessions = stores
    root = harness.root
    old = _legacy(root, "alpha")
    _legacy(root, "beta")
    before = _tree(old)
    current_apps.add(str(old))
    _transcript(projects, str(old), "sid-live")
    # A live CLI holds the session (its registry entry names a running pid).
    (sessions / "1.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "sid-live", "cwd": str(old)}))

    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == ["beta"]                      # the others are not held up
    assert "Claude is working in this canvas" in report.deferred["alpha"]
    assert report.refusal("alpha") == report.deferred["alpha"]
    assert report.refusal("beta") is None
    assert _tree(old) == before and not (root / "user" / "alpha").exists()
    assert canonical_fs_path(str(old)) in _desk()
    assert (projects / claude_session_move.munge(str(old)) / "sid-live.jsonl").is_file()
    # Nothing was left half-claimed for the retry to trip on.
    assert not (root / ".sync" / "alpha.lock").exists()
    assert canvas_sync_state.lock_holder(str(root / ".sync" / canvas_layout.LOCK_NAME)) is None
    assert _journal(root) is None

    # Throttled: a poll-driven call moments later answers from the last pass.
    assert canvas_layout.ensure_layout(ENV) is report
    # The session ended.
    (sessions / "1.json").unlink()
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.moved == ["alpha"] and not report.pending()
    new = root / "user" / "alpha"
    assert _tree(new) == before and not old.exists()
    assert canonical_fs_path(str(new)) in _desk()
    assert (projects / claude_session_move.munge(str(new)) / "sid-live.jsonl").is_file()


def test_the_clone_route_refuses_a_canvas_whose_clone_cannot_move_yet(harness, stores):
    """Pulling a fresh copy to the new path would leave two clones, the old
    one holding the user's unpublished work."""
    projects, sessions = stores
    harness.log_in()
    old = _legacy(harness.root, "alpha", {"canvas.toml": 'name = "alpha"\n',
                                          "a.py": "unpublished work\n"})
    _transcript(projects, str(old), "sid-live")
    (sessions / "1.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "sid-live", "cwd": str(old)}))

    listed = harness.client.get("/api/canvases/list?env=unstable", headers=GUARD).json()
    assert {c["name"]: c["cloned"] for c in listed["canvases"]}["alpha"] is False
    for route, body in (("clone", {}), ("sync/start", {}), ("delete", {}),
                        ("rename", {"new_name": "gamma"})):
        res = harness.client.post(f"/api/canvases/{route}", headers=GUARD,
                                  json={"env": ENV, "name": "alpha", **body})
        assert res.status_code == 409, (route, res.text)
        assert res.json()["code"] == "layout_pending", route
        assert "Claude is working in this canvas" in res.json()["error"]
    assert not (harness.clones / "alpha").exists()
    assert (old / "a.py").read_text() == "unpublished work\n"
    assert [c for c in harness.calls() if "pull" in c or "delete" in c or "rename" in c] == []
    # Another canvas is not held up by it.
    assert harness.client.post("/api/canvases/clone", headers=GUARD,
                               json={"env": ENV, "name": "beta"}).status_code == 200

    # The session ended. The page's card still says "not cloned", so it asks
    # for a clone: the folder moves, WITH its unpublished work, and no pull
    # replaces it with the server's copy.
    (sessions / "1.json").unlink()
    res = harness.client.post("/api/canvases/clone", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "dir": str(harness.clones / "alpha")}
    assert (harness.clones / "alpha" / "a.py").read_text() == "unpublished work\n"
    assert not old.exists()
    assert [c for c in harness.calls() if "pull" in c and "alpha" in c] == []
    res = harness.client.post("/api/canvases/sync/start", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text


class _Crash(BaseException):
    """Stands in for the process dying: not an Exception, so nothing in the
    module swallows it."""


def test_a_crash_between_the_rename_and_the_carry_is_finished_from_the_journal(
        harness, stores, monkeypatch):
    projects, _sessions = stores
    root = harness.root
    old = _legacy(root, "alpha")
    new = root / "user" / "alpha"
    current_apps.add(str(old))
    _transcript(projects, str(old), "sid-1")

    def die(*_a, **_k):
        raise _Crash()

    real_carry = canvas_layout._carry
    monkeypatch.setattr(canvas_layout, "_carry", die)
    with pytest.raises(_Crash):
        canvas_layout.ensure_layout(ENV)
    # The folder moved; nothing that names it did; the journal says so.
    assert (new / "canvas.toml").is_file() and not old.exists()
    assert canonical_fs_path(str(old)) in _desk()
    assert _journal(root) == [{"name": "alpha", "src": "alpha",
                               "dst": os.path.join("user", "alpha")}]
    # The dying pass released what it held (the kernel would have).
    assert canvas_sync_state.lock_holder(str(root / ".sync" / canvas_layout.LOCK_NAME)) is None

    # The next start: no legacy clone is left to find, only the journal.
    monkeypatch.setattr(canvas_layout, "_carry", real_carry)
    canvas_layout.reset()
    assert canvas_layout.legacy_clones(ENV) == []
    report = canvas_layout.ensure_layout(ENV)
    assert not report.pending()
    assert canonical_fs_path(str(new)) in _desk() and canonical_fs_path(str(old)) not in _desk()
    assert (projects / claude_session_move.munge(str(new)) / "sid-1.jsonl").is_file()
    assert _journal(root) is None
    assert canvases_mod._cloned_names(ENV) == {"alpha"}


def test_a_crash_before_the_rename_just_runs_the_move_again(harness, stores, monkeypatch):
    root = harness.root
    old = _legacy(root, "alpha")
    current_apps.add(str(old))
    real_rename = os.rename

    def die(src, dst):
        if str(src) == str(old):
            raise _Crash()
        return real_rename(src, dst)

    monkeypatch.setattr(canvas_layout.os, "rename", die)
    with pytest.raises(_Crash):
        canvas_layout.ensure_layout(ENV)
    assert (old / "canvas.toml").is_file() and _journal(root) is not None
    monkeypatch.setattr(canvas_layout.os, "rename", real_rename)
    canvas_layout.reset()
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]
    assert canonical_fs_path(str(root / "user" / "alpha")) in _desk()
    assert _journal(root) is None


def test_a_journal_entry_whose_source_is_back_is_dropped_unreplayed(harness, stores):
    """Replaying a prefix rewrite while a folder lives at the old path would
    repoint THAT folder's rows. Only "renamed, nothing at the source" replays."""
    root = harness.root
    old = _legacy(root, "alpha")                 # something is at the old path again
    _legacy(root / "user", "alpha")              # ...and the moved clone at the new one
    current_apps.add(str(old))
    (root / ".sync").mkdir()
    (root / ".sync" / canvas_layout.JOURNAL_NAME).write_text(json.dumps(
        {"version": 1, "moves": [{"name": "alpha", "src": "alpha", "dst": "user/alpha"},
                                 {"name": "gone", "src": "gone", "dst": "user/gone"},
                                 "junk", {"name": "x"}]}))
    report = canvas_layout.ensure_layout(ENV)
    assert _journal(root) is None
    assert canonical_fs_path(str(old)) in _desk()         # not repointed
    assert "alpha" in report.conflicts                    # and never overwritten


def test_a_transcript_held_at_the_move_is_carried_on_a_later_pass(harness, stores, monkeypatch):
    """A turn that opened between the check and the rename: the clone moved,
    the transcript could not. The journal keeps the move until it can."""
    projects, sessions = stores
    root = harness.root
    old = _legacy(root, "alpha")
    new = root / "user" / "alpha"
    _transcript(projects, str(old), "sid-late")
    (sessions / "1.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "sid-late", "cwd": str(old)}))
    monkeypatch.setattr(canvas_layout, "_held", lambda root, rel: [])   # the check saw nothing
    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == ["alpha"] and (new / "canvas.toml").is_file()
    assert _journal(root) == [{"name": "alpha", "src": "alpha",
                               "dst": os.path.join("user", "alpha")}]
    assert (projects / claude_session_move.munge(str(old)) / "sid-late.jsonl").is_file()
    # The pass that made the move knows it is not finished: the answer is not
    # remembered as "nothing left", so an ordinary (unforced) call comes back.
    assert report.carry_owed and report.pending()

    monkeypatch.setattr(canvas_layout, "RETRY_S", 0.0)
    report = canvas_layout.ensure_layout(ENV)
    assert report.carry_owed and report.pending() and report.moved == []
    (sessions / "1.json").unlink()
    report = canvas_layout.ensure_layout(ENV)
    assert not report.pending() and _journal(root) is None
    assert (projects / claude_session_move.munge(str(new)) / "sid-late.jsonl").is_file()
    # ...and now it IS remembered.
    monkeypatch.setattr(canvas_layout, "_run", lambda *a, **k: 1 / 0)
    assert not canvas_layout.ensure_layout(ENV).pending()


# -- never under another process, never over a folder ----------------------


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_a_clone_another_fused_render_is_syncing_is_deferred(harness, stores):
    """The installed app beside a dev server: its watcher holds the clone's
    writer lock. Moving the folder from under it would have it re-clone at
    the old path."""
    root = harness.root
    old = _legacy(root, "alpha")
    _legacy(root, "beta")
    before = _tree(old)
    lock = root / ".sync" / "alpha.lock"
    lock.parent.mkdir(parents=True)
    other = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "ready"
        report = canvas_layout.ensure_layout(ENV)
        assert report.moved == ["beta"]
        assert "synced by another fused-render" in report.deferred["alpha"]
        assert f"pid {other.pid}" in report.deferred["alpha"]
        assert _tree(old) == before and not (root / "user" / "alpha").exists()
        assert json.loads(lock.read_text())["pid"] == other.pid     # its lock, untouched
    finally:
        other.kill()
        other.wait()
    # It quit: the kernel dropped its lock, and the next pass moves the clone.
    assert canvas_layout.ensure_layout(ENV, force=True).moved == ["alpha"]
    assert _tree(root / "user" / "alpha") == before


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_two_processes_never_move_one_environment_at_once(harness, stores):
    root = harness.root
    _legacy(root, "alpha")
    lock = root / ".sync" / canvas_layout.LOCK_NAME
    lock.parent.mkdir(parents=True)
    other = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "ready"
        report = canvas_layout.ensure_layout(ENV)
        assert report.moved == [] and "another fused-render" in report.deferred["alpha"]
        assert (root / "alpha" / "canvas.toml").is_file()
    finally:
        other.kill()
        other.wait()
    assert canvas_layout.ensure_layout(ENV, force=True).moved == ["alpha"]


def test_a_clone_this_process_is_syncing_is_not_moved_from_under_it(harness, stores):
    """Cannot happen through the routes (a manager only exists for a clone
    at the new path); the guard is for a caller that got it wrong."""
    _legacy(harness.root, "alpha")
    with canvases_mod._SYNC_LOCK:
        canvases_mod._DELETING.add((ENV, "alpha"))
    try:
        report = canvas_layout.ensure_layout(ENV)
    finally:
        with canvases_mod._SYNC_LOCK:
            canvases_mod._DELETING.discard((ENV, "alpha"))
    assert report.moved == [] and "alpha" in report.deferred
    assert (harness.root / "alpha" / "canvas.toml").is_file()


@pytest.mark.parametrize("target", ["clone", "live", "unmanaged", "file"])
def test_a_folder_already_at_the_new_path_is_never_overwritten(harness, stores, target):
    root = harness.root
    old = _legacy(root, "alpha", {"canvas.toml": 'name = "alpha"\n', "a.py": "legacy\n"})
    new = root / "user" / "alpha"
    if target == "file":
        new.parent.mkdir(parents=True)
        new.write_text("not even a folder\n")
    else:
        new.mkdir(parents=True)
        (new / "a.py").write_text("already here\n")
        if target != "unmanaged":
            (new / "canvas.toml").write_text('name = "alpha"\n')
        if target == "live":
            canvas_marker.write_live(str(new), name="alpha", collection_id="col-1")
    current_apps.add(str(old))
    before_old = _tree(old)
    before_new = _tree(new) if new.is_dir() else new.read_bytes()

    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == [] and "alpha" in report.conflicts
    assert str(new) in report.conflicts["alpha"]
    assert _tree(old) == before_old
    assert (_tree(new) if new.is_dir() else new.read_bytes()) == before_new
    assert canonical_fs_path(str(old)) in _desk()                 # nothing carried
    assert not (root / ".sync" / "alpha.lock").exists() and _journal(root) is None
    # Syncing what is at the new path is not refused. Making a clone THERE
    # is, unless that folder is a live clone in its own right: a pull into
    # anything else would strand the old folder for good.
    assert report.refusal("alpha") is None
    assert report.refusal("alpha", creating=True) == report.conflicts["alpha"]
    assert report.refusal("alpha", creating=True, has_clone=True) is None
    assert canvas_layout.pending(ENV)["conflicts"] == report.conflicts
    # Stable under repetition.
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert "alpha" in report.conflicts and _tree(old) == before_old


def test_an_empty_folder_at_the_new_path_is_no_obstacle(harness, stores):
    root = harness.root
    _legacy(root, "alpha")
    (root / "user" / "alpha").mkdir(parents=True)        # a clone that never pulled
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]
    assert (root / "user" / "alpha" / "canvas.toml").is_file()


def test_a_reference_copy_at_the_new_path_makes_way_for_the_clone(harness, stores):
    """`user/<name>` holding the mirror command's copy of the very canvas
    this clone is the live one of: removable, by all three of the contract's
    conditions."""
    root = harness.root
    old = _legacy(root, "alpha", {"canvas.toml": 'name = "alpha"\n', "a.py": "mine\n"})
    mirror = root / "user" / "alpha"
    mirror.mkdir(parents=True)
    (mirror / "canvas.toml").write_text('name = "alpha"\n')
    (mirror / "a.py").write_text("the server's, last week\n")
    (mirror / MARKER).write_text(json.dumps({
        "version": 1, "sync": "mirror", "scope": "user", "id": "col-1", "name": "alpha",
        "owner": None, "revision": 2, "last_updated": "t", "pulled_at": "t"}))
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]
    assert (mirror / "a.py").read_text() == "mine\n"
    assert not old.exists()
    assert canvas_marker.sync_of(str(mirror)) == "none"      # the mirror marker went with it
    assert canvases_mod._is_live_clone(ENV, "alpha") is True


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_reference_copy_at_the_new_path_is_not_removed(harness, stores, tmp_path):
    root = harness.root
    _legacy(root, "alpha")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep\n")
    (elsewhere / MARKER).write_text(json.dumps({
        "version": 1, "sync": "mirror", "scope": "user", "id": "c", "name": "alpha"}))
    (root / "user").mkdir()
    os.symlink(elsewhere, root / "user" / "alpha")
    report = canvas_layout.ensure_layout(ENV)
    assert "alpha" in report.conflicts
    assert (elsewhere / "precious.txt").read_text() == "keep\n"
    assert (root / "alpha" / "canvas.toml").is_file()


# -- canvases named like the scope folders ---------------------------------


def test_a_clone_named_user_ends_at_user_user(harness, stores):
    projects, _sessions = stores
    root = harness.root
    user_clone = _legacy(root, "user", {"canvas.toml": 'name = "user"\n',
                                        "a.py": "the canvas called user\n",
                                        "team.py": "a node, not a scope\n"})
    alpha = _legacy(root, "alpha")
    files = {"user": _tree(user_clone), "alpha": _tree(alpha)}
    (root / ".sync").mkdir()
    (root / ".sync" / "user.json").write_text('{"collection_id": "col-user"}')
    sync_before = _tree(root / ".sync")
    current_apps.add(str(user_clone))
    current_apps.add(str(alpha))
    _transcript(projects, str(user_clone), "sid-user")
    _transcript(projects, str(alpha), "sid-alpha")

    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == ["user", "alpha"] and not report.pending()
    assert _tree(root / "user" / "user") == files["user"]
    assert _tree(root / "user" / "alpha") == files["alpha"]
    assert sorted(os.listdir(root)) == [".sync", "user"]            # no `.legacy-user` left
    assert sorted(os.listdir(root / "user")) == ["alpha", "user"]
    assert _tree(root / ".sync") == sync_before                     # `.sync/user.json` untouched
    assert canvases_mod._cloned_names(ENV) == {"user", "alpha"}
    # Each row went to its own clone: `alpha` was NOT swept into `user/user/`
    # by the rewrite of the path `user` used to have.
    desk = _desk()
    assert canonical_fs_path(str(root / "user" / "user")) in desk
    assert canonical_fs_path(str(root / "user" / "alpha")) in desk
    assert len(desk) == 2
    for name, sid in (("user", "sid-user"), ("alpha", "sid-alpha")):
        bucket = projects / claude_session_move.munge(str(root / "user" / name))
        rows = [json.loads(x) for x in (bucket / f"{sid}.jsonl").read_text().splitlines()]
        assert {r["cwd"] for r in rows if "cwd" in r} == {str(root / "user" / name)}, name
    assert _journal(root) is None

    # Idempotent: the scope folder is not mistaken for the clone it held.
    after = _tree(root)
    assert canvas_layout.legacy_clones(ENV) == []
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.moved == [] and not report.pending()
    assert _tree(root) == after
    assert _desk() == desk


@pytest.mark.parametrize("name", ["team", "public"])
def test_a_clone_named_like_a_reference_scope_moves_out_of_its_way(harness, stores, name):
    root = harness.root
    files = _tree(_legacy(root, name))
    current_apps.add(str(root / name))
    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == [name] and not report.pending()
    assert _tree(root / "user" / name) == files
    assert sorted(os.listdir(root)) == [".sync", "user"]   # `<env>/<name>` is free again
    assert os.listdir(root / ".sync") == []                # no lock, no journal left
    assert _desk() == [canonical_fs_path(str(root / "user" / name))]
    assert canvas_layout.ensure_layout(ENV, force=True).moved == []


def test_a_reserved_clone_stopped_halfway_is_finished(harness, stores, monkeypatch):
    """Crashed after `<env>/user` -> `<env>/.legacy-user`, before the second
    rename: found again by structure, with nothing at `<env>/user` yet."""
    root = harness.root
    files = _tree(_legacy(root, "user"))
    current_apps.add(str(root / "user"))
    real_rename = os.rename

    def die_on_second(src, dst):
        if str(src) == str(root / ".legacy-user"):
            raise _Crash()
        return real_rename(src, dst)

    monkeypatch.setattr(canvas_layout.os, "rename", die_on_second)
    with pytest.raises(_Crash):
        canvas_layout.ensure_layout(ENV)
    assert _tree(root / ".legacy-user") == files
    assert _desk() == [canonical_fs_path(str(root / ".legacy-user"))]
    assert canvas_layout.legacy_clones(ENV) == ["user"]

    monkeypatch.setattr(canvas_layout.os, "rename", real_rename)
    canvas_layout.reset()
    assert canvas_layout.ensure_layout(ENV).moved == ["user"]
    assert _tree(root / "user" / "user") == files
    assert not (root / ".legacy-user").exists()
    assert _desk() == [canonical_fs_path(str(root / "user" / "user"))]
    assert _journal(root) is None


def test_a_clone_named_user_that_cannot_move_holds_the_whole_environment(harness, stores):
    """Every other clone's new home is INSIDE its folder: nothing moves there,
    and nothing is cloned there, until it is out of the way."""
    projects, sessions = stores
    harness.log_in()
    root = harness.root
    user_clone = _legacy(root, "user")
    _legacy(root, "alpha")
    before = _tree(root)
    _transcript(projects, str(user_clone), "sid-live")
    (sessions / "1.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "sid-live", "cwd": str(user_clone)}))

    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == [] and report.blocked
    assert "user" in report.deferred and "alpha" in report.deferred
    # A canvas with no clone at all: nothing to wait for, but no new folder
    # may be made under `user/` either.
    assert report.refusal("beta") is None
    assert report.refusal("beta", creating=True) == report.blocked
    res = harness.client.post("/api/canvases/clone", headers=GUARD,
                              json={"env": ENV, "name": "beta"})
    assert res.status_code == 409 and res.json()["code"] == "layout_pending"
    assert _tree(root) == before                             # nothing planted inside it
    assert canvases_mod._cloned_names(ENV) == set()

    (sessions / "1.json").unlink()
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.moved == ["user", "alpha"] and not report.blocked


def test_a_clone_named_user_holding_another_canvas_folder_is_left_alone(harness, stores):
    """An older fused-render cloned a canvas named `user` AFTER this one made
    `user/alpha`. Moving it would carry `alpha` into `user/user/alpha`."""
    root = harness.root
    _legacy(root, "user")
    _legacy(root / "user", "alpha")
    before = _tree(root)
    report = canvas_layout.ensure_layout(ENV)
    assert "user" in report.conflicts and report.blocked
    assert "alpha" in report.conflicts["user"]
    assert _tree(root) == before


# -- the marker and the seeded files -------------------------------------------


def test_a_moved_clone_is_marked_live_when_its_id_is_known(harness, stores, tmp_path,
                                                          monkeypatch):
    SyncShims(harness, tmp_path, monkeypatch)        # the shim path: markers are written
    root = harness.root
    _legacy(root, "alpha")
    _legacy(root, "beta")
    old_claude_md = "# from before: see ../.sync/trash/\n"
    (root / "alpha" / "CLAUDE.md").write_text(old_claude_md)
    (root / "alpha" / ".fusedignore").write_text("CLAUDE.md\n.fusedignore\n")
    (root / ".sync").mkdir()
    (root / ".sync" / "alpha.json").write_text(json.dumps({"collection_id": "col-a"}))
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha", "beta"]
    marker = canvas_marker.read(str(root / "user" / "alpha"))
    assert (marker.sync, marker.scope, marker.id, marker.name) == ("live", "user", "col-a", "alpha")
    assert MARKER in (root / "user" / "alpha" / ".fusedignore").read_text().splitlines()
    # The seeded CLAUDE.md is the current text: the trash is two levels up now.
    text = (root / "user" / "alpha" / "CLAUDE.md").read_text()
    assert text != old_claude_md and "../../.sync/trash/" in text
    # No id known for `beta`: no marker, never a guessed one. Still a clone.
    assert not (root / "user" / "beta" / MARKER).exists()
    assert canvases_mod._cloned_names(ENV) == {"alpha", "beta"}


def test_the_external_cli_path_moves_the_clone_and_writes_nothing_into_it(harness, stores):
    root = harness.root
    files = _tree(_legacy(root, "alpha"))
    (root / ".sync").mkdir()
    (root / ".sync" / "alpha.json").write_text(json.dumps({"collection_id": "col-a"}))
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]
    assert _tree(root / "user" / "alpha") == files          # no marker, no seeded files


# -- the routes run it ---------------------------------------------------------


def test_the_catalog_lists_a_legacy_clone_as_cloned_after_moving_it(harness, stores):
    harness.log_in()
    _legacy(harness.root, "beta")
    res = harness.client.get("/api/canvases/list?env=unstable", headers=GUARD)
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["beta"]["cloned"] is True and canvases["beta"]["n_udfs"] == 1
    assert (harness.clones / "beta" / "canvas.toml").is_file()
    assert not (harness.root / "beta").exists()


def test_status_moves_every_environments_clones(harness, stores):
    _legacy(harness.root, "alpha")
    _legacy(harness.base / OTHER_ENV, "beta")
    body = harness.client.get("/api/canvases/status").json()
    assert body["envs"][ENV]["cloned_count"] == 1
    assert body["envs"][OTHER_ENV]["cloned_count"] == 1
    assert (harness.base / OTHER_ENV / "user" / "beta" / "canvas.toml").is_file()


def test_opening_a_legacy_clone_moves_it_and_starts_its_watcher(harness, stores):
    harness.log_in()
    old = _legacy(harness.root, "alpha", {"canvas.toml": 'type = "canvas"\n',
                                          "a.py": "unpublished\n"})
    res = harness.client.post("/api/canvases/sync/start", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text
    manager = canvases_mod._syncs[(ENV, "alpha")]
    assert manager.dir == str(harness.clones / "alpha")
    assert (harness.clones / "alpha" / "a.py").read_text() == "unpublished\n"
    assert not old.exists()
    # No pull ran: the clone moved with its own files, it was not re-fetched.
    assert [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "pull"]
            and "--force" in c] == []


def test_a_session_hook_in_a_moved_clone_finds_it(harness, stores):
    """The first hook after an upgrade names the NEW path (the transcript's
    cwd was rewritten); the pass has already run by then, or runs here."""
    harness.log_in()
    _legacy(harness.root, "alpha")
    assert canvases_mod._revive_for_session(ENV, "alpha", "start hook") is not None
    assert (harness.clones / "alpha" / "canvas.toml").is_file()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_rows_recorded_through_the_resolved_path_are_carried_too(harness, stores, tmp_path,
                                                                 monkeypatch):
    """A task started through the CLI wrapper's `pwd -P` names the clone by
    its resolved path; the page names it by the configured one."""
    projects, _sessions = stores
    real_base = tmp_path / "real-canvases"
    (real_base / ENV).mkdir(parents=True)
    link = tmp_path / "link-canvases"
    os.symlink(real_base, link)
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(link))
    _legacy(real_base / ENV, "alpha")
    configured, resolved = link / ENV / "alpha", real_base / ENV / "alpha"
    current_apps.add(str(configured))
    _transcript(projects, str(resolved), "sid-resolved")
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]
    assert _desk() == [canonical_fs_path(str(link / ENV / "user" / "alpha"))]
    bucket = projects / claude_session_move.munge(str(real_base / ENV / "user" / "alpha"))
    assert (bucket / "sid-resolved.jsonl").is_file()


def test_a_pass_that_is_asked_about_the_layout_from_inside_does_not_wait_on_itself(
        harness, stores, monkeypatch):
    """The carry calls into the stores. One of them asking "which canvases
    are cloned?" comes back into `ensure_layout` on the thread that holds
    the root's lock; it must get an answer, not a deadlock."""
    _legacy(harness.root, "alpha")
    real_carry = canvas_layout._carry
    inner = []

    def carry(root, src, dst):
        inner.append(canvas_layout.ensure_layout(ENV, force=True).moved)
        inner.append(sorted(canvases_mod._cloned_names(ENV)))
        return real_carry(root, src, dst)

    monkeypatch.setattr(canvas_layout, "_carry", carry)
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]
    assert inner == [[], ["alpha"]]
    # The guard is the pass's own: a later call on this thread runs normally.
    _legacy(harness.root, "beta")
    assert canvas_layout.ensure_layout(ENV, force=True).moved == ["beta"]


def test_a_pending_clone_is_logged_once_not_on_every_pass(harness, stores, monkeypatch, caplog):
    """A deferred clone is retried every RETRY_S for as long as a status poll
    runs; the log says why once, and again only when the reason changes."""
    import logging

    _legacy(harness.root, "alpha")
    _legacy(harness.clones, "alpha")                  # a conflict that will not go away
    monkeypatch.setattr(canvas_layout, "RETRY_S", 0.0)
    with caplog.at_level(logging.INFO, logger="fused_render.canvas_layout"):
        for _ in range(4):
            assert "alpha" in canvas_layout.ensure_layout(ENV).conflicts
    said = [r for r in caplog.records if "left at its old path" in r.getMessage()]
    assert len(said) == 1


# -- what a pass must never do, and what it reports ----------------------------


def test_no_reserved_file_under_sync_can_be_a_canvass(harness):
    """`.sync/` holds `<canvas name>.lock`, `.json`, `.log`, ... per clone.
    Every file this code keeps there for ITSELF must have a stem no canvas
    name can be (`layout.lock` would be canvas `layout`'s writer lock)."""
    from fused_render import canvas_mirror

    reserved = [canvas_layout.LOCK_NAME, canvas_layout.JOURNAL_NAME, canvas_mirror.STATUS_NAME]
    assert len(set(reserved)) == 3
    for name in reserved:
        stem = name.split(".")[0]
        assert not canvases_mod._NAME_RE.match(stem), name
        assert not canvases_mod._NAME_RE.match(name.rsplit(".", 1)[0]), name
        for canvas in ("layout", "mirror", "layout_journal", "alpha"):
            assert name not in {os.path.basename(p)
                                for p in canvases_mod._clone_leftovers(ENV, canvas)}
            assert name != os.path.basename(canvases_mod._adopting_path(ENV, canvas))
    assert os.path.basename(canvases_mod._lock_path(ENV, "layout")) != canvas_layout.LOCK_NAME
    assert os.path.basename(canvas_mirror._status_path("unstable")) != "mirror.json"


def test_a_pass_does_not_release_the_writer_lock_of_a_canvas_named_layout(harness, stores):
    """This process syncs canvas `layout` while a pass runs: the
    pass must leave that clone's writer lock held, or a second fused-render
    could claim the clone under the running watcher."""
    root = harness.root
    harness.log_in()
    _legacy(root / "user", "layout")
    res = harness.client.post("/api/canvases/sync/start", headers=GUARD,
                              json={"env": ENV, "name": "layout"})
    assert res.status_code == 200, res.text
    lock = canvases_mod._lock_path(ENV, "layout")
    assert lock in canvas_sync_state._HELD
    _legacy(root, "alpha")                       # work for the pass...
    _legacy(root, "beta")
    _legacy(root / "user", "beta")               # ...and something that stays pending
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.moved == ["alpha"] and "beta" in report.conflicts
    assert lock in canvas_sync_state._HELD and os.path.exists(lock)
    assert not (root / ".sync" / canvas_layout.LOCK_NAME).exists()
    # ...and a legacy clone literally named `layout` moves like any other.
    canvases_mod._syncs.pop((ENV, "layout")).stop()
    import shutil
    shutil.rmtree(root / "user" / "layout")
    _legacy(root, "layout")
    assert canvas_layout.ensure_layout(ENV, force=True).moved == ["layout"]


def test_a_conflict_refuses_a_clone_that_would_strand_the_old_folder(harness, stores):
    """The clone sits at the old path with unpublished work and
    something that is not a clone sits at the new one. The catalog says "not
    cloned"; the card's click must not pull a second copy in beside it."""
    root = harness.root
    harness.log_in()
    old = _legacy(root, "alpha", {"canvas.toml": 'name = "alpha"\n', "a.py": "unpublished work\n"})
    (root / "user" / "alpha").mkdir(parents=True)
    (root / "user" / "alpha" / "notes.txt").write_text("mine")
    listed = harness.client.get("/api/canvases/list?env=unstable", headers=GUARD).json()
    assert {c["name"]: c["cloned"] for c in listed["canvases"]}["alpha"] is False
    res = harness.client.post("/api/canvases/clone", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 409 and res.json()["code"] == "layout_pending"
    assert str(old) in res.json()["error"] and "in the way" in res.json()["error"]
    assert os.listdir(root / "user" / "alpha") == ["notes.txt"]
    assert (old / "a.py").read_text() == "unpublished work\n"
    assert [c for c in harness.calls() if "pull" in c] == []
    # The page can show it: on the status route, per environment, and on the
    # mirror status.
    body = harness.client.get("/api/canvases/status").json()
    assert list(body["envs"][ENV]["layout"]["conflicts"]) == ["alpha"]
    assert body["envs"][OTHER_ENV]["layout"] == {
        "deferred": {}, "conflicts": {}, "blocked": "", "error": ""}
    mirror = harness.client.get(f"/api/canvases/mirror/status?env={ENV}").json()
    assert list(mirror["layout"]["conflicts"]) == ["alpha"]
    # The way cleared, the next open moves the clone, work and all.
    (root / "user" / "alpha" / "notes.txt").unlink()
    res = harness.client.post("/api/canvases/clone", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text
    assert (root / "user" / "alpha" / "a.py").read_text() == "unpublished work\n"
    assert harness.client.get("/api/canvases/status").json()["envs"][ENV]["layout"][
        "conflicts"] == {}


def test_a_conflict_with_a_live_clone_at_the_new_path_does_not_refuse_it(harness, stores):
    """What is at the new path IS a clone. It is
    used as one; the legacy folder stays reported."""
    harness.log_in()
    _legacy(harness.root, "alpha")
    _legacy(harness.clones, "alpha")
    res = harness.client.post("/api/canvases/clone", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text
    res = harness.client.post("/api/canvases/sync/start", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text
    assert list(canvas_layout.pending(ENV)["conflicts"]) == ["alpha"]


@pytest.mark.parametrize("route, body", [("delete", {}), ("rename", {"new_name": "gamma"})])
def test_a_conflict_refuses_a_delete_or_rename_of_that_canvas(harness, stores, route, body):
    """A live clone at `user/alpha` AND a clone left at `<env>/alpha`. A
    delete would remove the canvas and `user/alpha` but leave the old one,
    which the next pass would move into the freed `user/alpha`: a clone of a
    deleted canvas. A rename would leave it under the old name the same way.
    Both refuse until the user clears it; sync of the live clone still runs."""
    root = harness.root
    harness.log_in()
    old = _legacy(root, "alpha", {"canvas.toml": 'name = "alpha"\n', "a.py": "old work\n"})
    _legacy(harness.clones, "alpha")
    res = harness.client.post(f"/api/canvases/{route}", headers=GUARD,
                              json={"env": ENV, "name": "alpha", **body})
    assert res.status_code == 409 and res.json()["code"] == "layout_pending", res.text
    assert str(old) in res.json()["error"]
    assert [c for c in harness.calls() if route in c] == []
    assert (old / "a.py").read_text() == "old work\n"
    assert (harness.clones / "alpha" / "canvas.toml").is_file()
    assert not (harness.clones / "gamma").exists()
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.refusal("alpha", retiring=True) == report.conflicts["alpha"]
    assert report.refusal("alpha") is None
    res = harness.client.post("/api/canvases/sync/start", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text


def test_a_blocked_environment_refuses_a_delete_or_rename(harness, stores):
    """A clone named `user` still holds `<env>/user`: `user/<name>` is a
    folder inside that clone, never a canvas's folder to delete or move."""
    report = canvas_layout.LayoutReport(env=ENV, blocked="canvas 'user' must move first")
    assert report.refusal("beta", retiring=True) == report.blocked
    assert report.refusal("beta") is None


def test_a_pass_that_failed_refuses_every_canvas_route(harness, stores, monkeypatch):
    """Nothing is known about the old paths after a failed pass: a
    clone with unpublished work may be waiting there. Fail closed."""
    root = harness.root
    harness.log_in()
    old = _legacy(root, "alpha", {"canvas.toml": 'name = "alpha"\n', "a.py": "unpublished work\n"})

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    real = canvas_layout._run
    monkeypatch.setattr(canvas_layout, "_run", boom)
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.error and report.refusal("alpha") == report.error
    assert report.refusal("alpha", creating=True, has_clone=True) == report.error
    # Only where a clone could be waiting: a canvas with no folder at the
    # old path is not locked out by a pass that keeps failing.
    assert report.refusal("beta") is None and report.refusal("beta", creating=True) is None
    assert harness.client.post("/api/canvases/clone", headers=GUARD,
                               json={"env": ENV, "name": "beta"}).status_code == 200
    for route, body in (("clone", {}), ("sync/start", {}), ("delete", {}),
                        ("rename", {"new_name": "gamma"})):
        res = harness.client.post(f"/api/canvases/{route}", headers=GUARD,
                                  json={"env": ENV, "name": "alpha", **body})
        assert res.status_code == 409 and res.json()["code"] == "layout_pending", route
    assert not (root / "user" / "alpha").exists()
    assert (old / "a.py").read_text() == "unpublished work\n"
    assert [c for c in harness.calls() if "alpha" in c] == []
    assert harness.client.get("/api/canvases/status").json()["envs"][ENV]["layout"]["error"]
    # A pass that works clears it.
    monkeypatch.setattr(canvas_layout, "_run", real)
    assert harness.client.post("/api/canvases/clone", headers=GUARD,
                               json={"env": ENV, "name": "alpha"}).status_code == 200
    assert (root / "user" / "alpha" / "a.py").read_text() == "unpublished work\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("relative", [True, False])
def test_a_legacy_clone_that_is_a_link_is_a_conflict_not_a_move(harness, stores, tmp_path,
                                                                relative):
    """Renaming the link one level deeper leaves a relative one
    dangling; the real folder is not ours to move."""
    root = harness.root
    real = _legacy(tmp_path / "elsewhere", "alpha")
    root.mkdir(parents=True)
    os.symlink(os.path.relpath(real, root) if relative else str(real), root / "alpha")
    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == [] and "link" in report.conflicts["alpha"]
    assert os.path.islink(root / "alpha") and (root / "alpha" / "canvas.toml").is_file()
    assert not (root / "user").exists()
    assert (real / "canvas.toml").is_file()


def test_a_first_step_carry_left_owed_is_not_dropped_for_a_clone_named_team(
        harness, stores, monkeypatch):
    """`team` and `public` move in ONE step, so the journal entry
    a held transcript leaves is `team -> user/team` and replays as any other."""
    projects, _sessions = stores
    root = harness.root
    old = _legacy(root, "team")
    _transcript(projects, str(old), "sid1")
    real = claude_session_move.relocate
    calls = []

    def relocate(src, dst, **kw):
        calls.append((src, dst))
        if len(calls) == 1:
            return {"moved": [], "pending": ["sid1"], "complete": False}
        return real(src, dst, **kw)

    monkeypatch.setattr(claude_session_move, "relocate", relocate)
    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == ["team"] and report.carry_owed
    assert not (root / ".legacy-team").exists()
    assert _journal(root) == [{"name": "team", "src": "team",
                               "dst": os.path.join("user", "team")}]
    assert canvas_layout.owed_scope_names(ENV) == ["team"]
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert not report.pending() and _journal(root) is None
    assert (projects / claude_session_move.munge(str(root / "user" / "team"))
            / "sid1.jsonl").is_file()
    assert canvas_layout.owed_scope_names(ENV) == []


def test_a_clone_named_user_waits_at_its_halfway_house_for_its_first_carry(
        harness, stores, monkeypatch):
    """The one name that still takes two steps. Once
    `<env>/user` is the clones folder its prefix can never be rewritten
    again, so the second rename waits until the first one's carry is done —
    and the environment stays held meanwhile."""
    projects, _sessions = stores
    root = harness.root
    old = _legacy(root, "user")
    _legacy(root, "alpha")
    _transcript(projects, str(old), "sid1")
    real = claude_session_move.relocate
    calls = []

    def relocate(src, dst, **kw):
        calls.append((src, dst))
        if len(calls) <= 2:
            return {"moved": [], "pending": ["sid1"], "complete": False}
        return real(src, dst, **kw)

    monkeypatch.setattr(claude_session_move, "relocate", relocate)
    for _ in range(2):                                   # the pass, then a retry
        report = canvas_layout.ensure_layout(ENV, force=True)
        assert report.moved == [] and report.blocked and "user" in report.deferred
        assert (root / ".legacy-user" / "canvas.toml").is_file()
        assert not (root / "user").exists() and (root / "alpha" / "canvas.toml").is_file()
        assert _journal(root) == [{"name": "user", "src": "user", "dst": ".legacy-user"}]
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.moved == ["user", "alpha"] and not report.pending()
    assert _journal(root) is None
    assert (projects / claude_session_move.munge(str(root / "user" / "user"))
            / "sid1.jsonl").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_a_pass_another_process_is_running_still_holds_the_environment_for_a_clone_named_user(
        harness, stores):
    """This process could not look (the layout lock is taken), but
    a clone named `user` is in the way all the same: no folder is made
    inside it."""
    root = harness.root
    harness.log_in()
    _legacy(root, "user", {"canvas.toml": 'name = "user"\n', "a.py": "x\n"})
    lock = root / ".sync" / canvas_layout.LOCK_NAME
    lock.parent.mkdir(parents=True)
    other = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "ready"
        report = canvas_layout.ensure_layout(ENV, force=True)
        assert report.blocked and report.refusal("beta", creating=True) == report.blocked
        res = harness.client.post("/api/canvases/clone", headers=GUARD,
                                  json={"env": ENV, "name": "beta"})
        assert res.status_code == 409 and res.json()["code"] == "layout_pending"
        assert sorted(os.listdir(root / "user")) == ["a.py", "canvas.toml"]
    finally:
        other.kill()
        other.wait()


def test_a_carry_that_threw_is_owed_not_done(harness, stores, monkeypatch):
    """A store rewrite that raised leaves the move in the journal
    for the next pass; reporting it complete would drop the desk row and the
    chat history for good."""
    from fused_render import app_state_move

    root = harness.root
    old = _legacy(root, "alpha")
    current_apps.add(str(old))
    real = app_state_move.rewrite_stores
    calls = []

    def rewrite(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise RuntimeError("disk said no")
        return real(src, dst)

    monkeypatch.setattr(app_state_move, "rewrite_stores", rewrite)
    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == ["alpha"] and report.carry_owed
    assert _journal(root) == [{"name": "alpha", "src": "alpha",
                               "dst": os.path.join("user", "alpha")}]
    assert canonical_fs_path(str(old)) in _desk()
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert not report.pending() and _journal(root) is None
    assert _desk() == [canonical_fs_path(str(root / "user" / "alpha"))]


def test_a_store_that_failed_inside_rewrite_stores_keeps_the_move_owed(
        harness, stores, monkeypatch):
    """`rewrite_stores` swallows a store's failure so the next store still
    runs, but it says so: the carry is not complete, the journal keeps the
    move, and a later pass rewrites that store."""
    from fused_render import app_state_move

    root = harness.root
    old = _legacy(root, "alpha")
    current_apps.add(str(old))
    real = app_state_move._rewrite_current_apps
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise OSError("disk said no")
        return real(src, dst)

    monkeypatch.setattr(app_state_move, "_rewrite_current_apps", flaky)
    report = canvas_layout.ensure_layout(ENV)
    assert report.moved == ["alpha"] and report.carry_owed
    assert _journal(root) == [{"name": "alpha", "src": "alpha",
                               "dst": os.path.join("user", "alpha")}]
    assert canonical_fs_path(str(old)) in _desk()
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert not report.pending() and _journal(root) is None
    assert _desk() == [canonical_fs_path(str(root / "user" / "alpha"))]


def test_a_clone_that_moves_clears_an_adoption_flag_left_under_its_name(harness, stores):
    """A flag with no folder under `user/` (an adoption that failed and whose
    copy is gone) and a clone of that canvas at the old path, with
    unpublished work. The folder that arrives IS a clone: the flag would
    make it read "not cloned" and the next open would pull over it."""
    harness.log_in()
    flag = harness.root / ".sync" / "alpha.adopting"
    flag.parent.mkdir(parents=True)
    flag.write_text("1")
    _legacy(harness.root, "alpha", {"canvas.toml": 'type = "canvas"\n',
                                    "a.py": "unpublished work\n"})
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]
    assert not flag.exists()
    assert canvases_mod._is_live_clone(ENV, "alpha") is True
    res = harness.client.post("/api/canvases/sync/start", headers=GUARD,
                              json={"env": ENV, "name": "alpha"})
    assert res.status_code == 200, res.text
    assert (harness.clones / "alpha" / "a.py").read_text() == "unpublished work\n"


def test_a_carry_is_given_up_after_a_bounded_wait(harness, stores, monkeypatch, caplog):
    """A session left open for hours (or a store that keeps failing) must not
    hold the environment, or its mirror, forever: past the limit the move is
    dropped from the journal, said once at WARNING, and nothing waits on it.
    The transcript stays in its old bucket; nothing is lost on disk."""
    import logging

    projects, sessions = stores
    root = harness.root
    old = _legacy(root, "team")
    _transcript(projects, str(old), "sid-open")
    (sessions / "1.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "sid-open", "cwd": str(old)}))
    monkeypatch.setattr(canvas_layout, "_held", lambda root, rel: [])
    assert canvas_layout.ensure_layout(ENV).carry_owed
    assert canvas_layout.owed_scope_names(ENV) == ["team"]
    assert canvas_layout.ensure_layout(ENV, force=True).carry_owed     # still inside the limit
    monkeypatch.setattr(canvas_layout, "CARRY_GIVE_UP_S", 0.0)
    with caplog.at_level(logging.WARNING, logger="fused_render.canvas_layout"):
        report = canvas_layout.ensure_layout(ENV, force=True)
        canvas_layout.ensure_layout(ENV, force=True)
    assert not report.pending() and _journal(root) is None
    assert canvas_layout.owed_scope_names(ENV) == []
    assert len([r for r in caplog.records if "giving up" in r.getMessage()]) == 1
    assert (projects / claude_session_move.munge(str(old)) / "sid-open.jsonl").is_file()


def test_a_clone_named_user_stops_waiting_for_its_first_carry_after_the_limit(
        harness, stores, monkeypatch):
    projects, _sessions = stores
    root = harness.root
    old = _legacy(root, "user")
    _legacy(root, "alpha")
    _transcript(projects, str(old), "sid1")
    monkeypatch.setattr(claude_session_move, "relocate", lambda src, dst, **kw: {
        "moved": [], "pending": ["sid1"], "complete": False})
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.blocked and report.moved == []
    monkeypatch.setattr(canvas_layout, "CARRY_GIVE_UP_S", 0.0)
    report = canvas_layout.ensure_layout(ENV, force=True)
    assert report.moved == ["user", "alpha"] and not report.blocked
    assert (root / "user" / "user" / "canvas.toml").is_file()
