"""Turning a reference copy into a clone, and the holds around `user/` (D1477).

`fused canvas mirror` leaves a read-only copy of each of the user's own
canvases at `user/<name>`, marked `mirror`. Opening one clones it IN PLACE.
What these pin:

* the crash-safe order — flag, marker live, pull, marker live again — so no
  mirror run swaps the folder mid-pull and its stale files never become a
  sync baseline (never adopt without pulling);
* the lookup by collection id when the copy sits under another folder name;
* that `team/` and `public/` copies are never adopted;
* R8 — a mirror run in this process is stopped for the adoption, and one in
  another process loses to the marker;
* rename and delete under the same hold, and the two gaps stage 1 reported
  (a reference copy's desk row, the stale "not cloned" card).
"""
import json
import os
import subprocess
import sys
import time

import pytest

import fused_render.canvases as canvases_mod
from fused_render import canvas_layout, canvas_marker, canvas_mirror, current_apps
from test_canvas_mirror import MIRROR_ARGV, _alive, _slow, _wait_started
from test_canvas_sync_watcher import _poll, _quiet_watcher, _start
from test_canvases import (  # noqa: F401 — fixtures are used by name
    ENV,
    GUARD,
    _BASE_FILES,
    SyncShims,
    _reset_module_state,
    _wait_for,
    fake_agent,
    harness,
)

MARKER = canvas_marker.MARKER_NAME
STALE = {"canvas.toml": 'type = "canvas"\n', "a.py": "a-last-week\n", "gone.py": "removed since\n"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(canvas_mirror, "workbench_env", lambda: ENV)
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.5)
    canvas_layout.reset()
    yield
    canvas_layout.reset()


def _reference_copy(folder, cid="col-a", scope="user", files=STALE, name=None):
    """A folder as the mirror command writes it."""
    folder.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        (folder / rel).write_text(content, encoding="utf-8")
    (folder / MARKER).write_text(json.dumps({
        "version": 1, "sync": "mirror", "scope": scope, "id": cid,
        "name": name or folder.name, "owner": "ada", "revision": 3,
        "last_updated": "2026-11-01T10:00:00Z",
        "pulled_at": "2026-11-02T10:00:00Z"}), encoding="utf-8")
    return folder


def _listing(monkeypatch, **ids):
    """The stored catalog listing: canvas name -> collection id."""
    monkeypatch.setattr(canvases_mod, "_listing_cache_read", lambda env: [
        {"name": n, "id": i} for n, i in ids.items()] if env == ENV else None)


def _clone(harness, name="alpha"):
    return harness.client.post("/api/canvases/clone", json={"env": ENV, "name": name},
                               headers=GUARD)


def _cloned(harness, name="alpha"):
    listed = harness.client.get("/api/canvases/list?env=unstable", headers=GUARD).json()
    return {c["name"]: c["cloned"] for c in listed["canvases"]}[name]


def _flag(harness, name="alpha"):
    return harness.root / ".sync" / f"{name}.adopting"


def _pulls(harness):
    return [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "pull"]
            and "--force" in c]


def _shim_harness(harness, tmp_path, monkeypatch, **ids):
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    shims = SyncShims(harness, tmp_path, monkeypatch)
    _listing(monkeypatch, **(ids or {"alpha": "col-a"}))
    return shims


# -- adoption ------------------------------------------------------------------


def test_opening_a_reference_copy_clones_it_in_place(harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    shims = _shim_harness(harness, tmp_path, monkeypatch)
    folder = _reference_copy(harness.clones / "alpha")
    assert _cloned(harness) is False                 # so the page asks for a clone
    res = _clone(harness)
    assert res.status_code == 200 and res.json() == {"ok": True, "dir": str(folder)}
    # Fresh content: the pull replaced the copy's stale files.
    assert (folder / "a.py").read_text() == "a1\n" and not (folder / "gone.py").exists()
    marker = canvas_marker.read(str(folder))
    assert (marker.sync, marker.scope, marker.id, marker.name) == ("live", "user", "col-a", "alpha")
    assert not _flag(harness).exists()
    assert _cloned(harness) is True and len(_pulls(harness)) == 1
    # It baselines like any fresh clone: no sync point came with the copy,
    # and the first sync finds nothing to push.
    assert not (harness.root / ".sync" / "alpha.json").exists()
    shims.set_manifest("t1")
    shims.set_remote_files(_BASE_FILES)
    m = _start(harness)
    _poll(m)
    with m._flight_lock:
        m._tick()
    assert m.push_state == "idle" and m._dirty_since is None and m._base_collection_id == "c1"
    assert [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]] == []
    # The id the sync learned is the marker's now.
    assert canvas_marker.read(str(folder)).id == "c1"


def test_the_marker_is_live_before_the_pull_starts(harness, tmp_path, monkeypatch):
    """What a mirror run in another process sees mid-clone: `live`, so its
    re-check before a swap skips the folder. And not yet a clone here."""
    _shim_harness(harness, tmp_path, monkeypatch)
    folder = _reference_copy(harness.clones / "alpha")
    seen = {}
    real = canvases_mod._run_cli

    def run_cli(args, timeout, env):
        if args[:3] == ["workbench", "canvas", "pull"]:
            seen["sync"] = canvas_marker.sync_of(str(folder))
            seen["id"] = canvas_marker.read(str(folder)).id
            seen["flag"] = _flag(harness).exists()
            seen["clone"] = canvases_mod._is_live_clone(ENV, "alpha")
            seen["held"] = canvas_mirror.status(ENV)["held"]
        return real(args, timeout, env)

    monkeypatch.setattr(canvases_mod, "_run_cli", run_cli)
    assert _clone(harness).status_code == 200
    assert seen == {"sync": "live", "id": "col-a", "flag": True, "clone": False, "held": True}
    assert canvas_mirror.status(ENV)["held"] is False


def test_a_cli_that_preserves_the_marker_on_pull_still_ends_live(harness, tmp_path, monkeypatch):
    """Contract v1.1: `pull --force` keeps `.fused-canvas.json`. Marked live
    first, the folder is a clone afterwards; left `mirror`, it would have
    stayed "not cloned" and the page would loop."""
    _shim_harness(harness, tmp_path, monkeypatch)
    harness.set_scenario({"pull_files": _BASE_FILES, "pull_keep_marker": True})
    folder = _reference_copy(harness.clones / "alpha")
    assert _clone(harness).status_code == 200
    assert canvas_marker.sync_of(str(folder)) == "live"
    assert _cloned(harness) is True


def test_a_failed_pull_leaves_a_live_marked_folder_the_next_open_pulls_again(
        harness, tmp_path, monkeypatch):
    """Never adopt without pulling: marked live (the mirror command keeps its
    hands off), but NOT a clone — no sync may take the copy's stale files as
    its baseline — until a pull lands."""
    _shim_harness(harness, tmp_path, monkeypatch)
    harness.set_scenario({"pull_fail": True})
    folder = _reference_copy(harness.clones / "alpha")
    res = _clone(harness)
    assert res.status_code == 502, res.text
    assert canvas_marker.sync_of(str(folder)) == "live"
    assert (folder / "a.py").read_text() == "a-last-week\n"        # still the old copy
    assert _flag(harness).exists()
    assert canvases_mod._is_live_clone(ENV, "alpha") is False
    assert _cloned(harness) is False
    res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 409 and (ENV, "alpha") not in canvases_mod._syncs
    assert canvases_mod._revive_for_session(ENV, "alpha", "start hook") is None
    assert canvases_mod._mark_live(ENV, "alpha", "col-a") is False
    # The next open: the page still sees "not cloned", clones again, and this
    # time the pull lands.
    harness.set_scenario({"pull_files": _BASE_FILES})
    assert _clone(harness).status_code == 200
    assert (folder / "a.py").read_text() == "a1\n" and not (folder / "gone.py").exists()
    assert not _flag(harness).exists() and _cloned(harness) is True
    assert canvas_marker.read(str(folder)).sync == "live"


def test_a_crash_after_the_marker_went_live_is_pulled_on_the_next_open(
        harness, tmp_path, monkeypatch):
    """The state a crash mid-adoption leaves: flag, live marker, old files."""
    _shim_harness(harness, tmp_path, monkeypatch)
    folder = _reference_copy(harness.clones / "alpha")
    canvas_marker.write_live(str(folder), name="alpha", collection_id="col-a")
    _flag(harness).parent.mkdir(parents=True, exist_ok=True)
    _flag(harness).write_text("1")
    assert _cloned(harness) is False
    assert _clone(harness).status_code == 200
    assert (folder / "a.py").read_text() == "a1\n" and not _flag(harness).exists()
    assert _cloned(harness) is True


def test_with_an_external_cli_the_copy_is_unmarked_not_marked(harness, monkeypatch):
    """No live marker may be written there (the `pull --dry-run` probe would
    report it). Removing the mirror marker takes the folder out of the
    command's hands just the same: unmanaged."""
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    _listing(monkeypatch, alpha="col-a")
    folder = _reference_copy(harness.clones / "alpha")
    seen = {}
    real = canvases_mod._run_cli

    def run_cli(args, timeout, env):
        if args[:3] == ["workbench", "canvas", "pull"]:
            seen["marker"] = (folder / MARKER).exists()
        return real(args, timeout, env)

    monkeypatch.setattr(canvases_mod, "_run_cli", run_cli)
    assert _clone(harness).status_code == 200
    assert seen == {"marker": False}
    assert sorted(os.listdir(folder)) == sorted(_BASE_FILES)
    assert _cloned(harness) is True and not _flag(harness).exists()


def test_a_copy_with_no_known_id_is_adopted_with_its_own(harness, tmp_path, monkeypatch):
    _shim_harness(harness, tmp_path, monkeypatch, other="col-x")     # alpha is in no listing
    folder = _reference_copy(harness.clones / "alpha", cid="col-from-the-copy")
    assert _clone(harness).status_code == 200
    assert canvas_marker.read(str(folder)).id == "col-from-the-copy"
    # A marker that only SAYS mirror has no id to keep: unmarked, still ours.
    broken = harness.clones / "beta"
    broken.mkdir()
    (broken / "canvas.toml").write_text("")
    (broken / MARKER).write_text('{"sync": "mirror"}')
    assert _clone(harness, "beta").status_code == 200
    assert not (broken / MARKER).exists()
    assert canvases_mod._cloned_names(ENV) == {"alpha", "beta"}


def test_a_copy_under_another_folder_name_is_found_by_id_and_moved_into_place(
        harness, tmp_path, monkeypatch):
    """The mirror's collision suffix (or a canvas renamed since): the copy of
    THIS canvas is `user/alpha-12345678`, and `user/alpha` is free."""
    _shim_harness(harness, tmp_path, monkeypatch)
    suffixed = _reference_copy(harness.clones / "alpha-12345678", name="alpha")
    other = _reference_copy(harness.clones / "beta", cid="col-b")
    res = _clone(harness)
    assert res.status_code == 200, res.text
    folder = harness.clones / "alpha"
    assert not suffixed.exists()
    assert (folder / "a.py").read_text() == "a1\n"
    marker = canvas_marker.read(str(folder))
    assert (marker.sync, marker.id, marker.name) == ("live", "col-a", "alpha")
    assert canvas_marker.sync_of(str(other)) == "mirror"     # another canvas's copy: untouched
    assert sorted(os.listdir(harness.clones)) == ["alpha", "beta"]
    assert _cloned(harness) is True


def test_an_empty_folder_at_the_path_does_not_stop_the_move(harness, tmp_path, monkeypatch):
    _shim_harness(harness, tmp_path, monkeypatch)
    _reference_copy(harness.clones / "alpha-12345678", name="alpha")
    (harness.clones / "alpha").mkdir()
    assert _clone(harness).status_code == 200
    assert sorted(os.listdir(harness.clones)) == ["alpha"]
    assert canvas_marker.read(str(harness.clones / "alpha")).sync == "live"


def test_a_path_taken_by_a_foreign_folder_refuses_the_clone(harness, tmp_path, monkeypatch):
    """`user/alpha` holds something that is neither a clone nor a reference
    copy, and this canvas's copy sits elsewhere: nothing is moved over it."""
    _shim_harness(harness, tmp_path, monkeypatch)
    suffixed = _reference_copy(harness.clones / "alpha-12345678", name="alpha")
    foreign = harness.clones / "alpha"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("mine\n")
    res = _clone(harness)
    assert res.status_code == 409 and res.json()["code"] == "exists"
    assert str(foreign) in res.json()["error"]
    assert (foreign / "notes.txt").read_text() == "mine\n" and os.listdir(foreign) == ["notes.txt"]
    assert canvas_marker.sync_of(str(suffixed)) == "mirror"
    assert _pulls(harness) == [] and not _flag(harness).exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_linked_reference_copy_is_not_adopted(harness, tmp_path, monkeypatch):
    _shim_harness(harness, tmp_path, monkeypatch)
    elsewhere = _reference_copy(tmp_path / "elsewhere", name="alpha")
    harness.clones.mkdir(parents=True)
    os.symlink(elsewhere, harness.clones / "alpha")
    res = _clone(harness)
    assert res.status_code == 409 and res.json()["code"] == "exists"
    assert canvas_marker.sync_of(str(elsewhere)) == "mirror"
    assert (elsewhere / "a.py").read_text() == "a-last-week\n" and _pulls(harness) == []


def test_team_and_public_copies_are_never_adopted(harness, tmp_path, monkeypatch):
    """Only `user/` is looked in. A copy of this very id under `team/` or
    `public/` stays what it is; the clone is a plain first clone."""
    _shim_harness(harness, tmp_path, monkeypatch)
    team = _reference_copy(harness.root / "team" / "alpha", scope="team")
    public = _reference_copy(harness.root / "public" / "alpha-12345678", scope="public")
    before = {p: sorted(os.listdir(p)) for p in (team, public)}
    assert _clone(harness).status_code == 200
    folder = harness.clones / "alpha"
    assert (folder / "a.py").read_text() == "a1\n"
    assert canvas_marker.read(str(folder)).sync == "live"
    for ref in (team, public):
        assert sorted(os.listdir(ref)) == before[ref]
        assert canvas_marker.sync_of(str(ref)) == "mirror"
        assert (ref / "a.py").read_text() == "a-last-week\n"
    # Nor do they read as clones, or get a watcher.
    assert canvases_mod.clone_dir_for(str(team)) is None
    assert canvases_mod._cloned_names(ENV) == {"alpha"}


def test_a_clone_is_recloned_as_before_whatever_copies_exist(harness, tmp_path, monkeypatch):
    """An existing clone at `user/alpha` and a duplicate copy of its id
    beside it: the clone is pulled as a re-clone always was, and the copy is
    the mirror command's to remove ("a mirror duplicated by a live folder")."""
    _shim_harness(harness, tmp_path, monkeypatch)
    assert _clone(harness).status_code == 200
    duplicate = _reference_copy(harness.clones / "alpha-12345678", name="alpha")
    assert _clone(harness).status_code == 200
    assert canvas_marker.sync_of(str(duplicate)) == "mirror"
    assert canvas_marker.read(str(harness.clones / "alpha")).sync == "live"
    assert len(_pulls(harness)) == 2 and not _flag(harness).exists()


# -- R8: adoption against a mirror run -----------------------------------------


def test_a_clone_stops_this_processs_mirror_run_first(harness, tmp_path, monkeypatch):
    """The run in flight would swap `user/alpha` after its sleep. It is gone
    before the pull starts, and a run is started again afterwards."""
    _shim_harness(harness, tmp_path, monkeypatch)
    folder = _reference_copy(harness.clones / "alpha")
    swap = [{"path": "user/alpha", "files": {"a.py": "from the mirror run\n"}}]
    started = _slow(harness, tmp_path, mirror_delay=3, mirror_swap=swap,
                    pull_files=_BASE_FILES)
    assert canvas_mirror.kick(ENV, "timer", force=True)["started"] is True
    pid = _wait_started(started)
    started.unlink()
    assert _clone(harness).status_code == 200
    assert not _alive(pid)                                   # stopped, not waited out
    assert (folder / "a.py").read_text() == "a1\n"
    assert canvas_marker.read(str(folder)).sync == "live"
    # The run made up for after the hold finds a live folder and leaves it.
    again = _wait_started(started)
    assert again != pid
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "ok", timeout=15)
    assert (folder / "a.py").read_text() == "a1\n"
    assert len([c for c in harness.calls() if c[:6] == MIRROR_ARGV]) == 2


def test_a_mirror_run_in_another_process_loses_to_the_marker(harness, tmp_path, monkeypatch):
    """A terminal's `fused canvas mirror`, not ours to stop. It planned to
    replace `user/alpha` and re-reads the marker before it does: by then the
    folder is live, and it ends live with the pulled content."""
    _shim_harness(harness, tmp_path, monkeypatch)
    folder = _reference_copy(harness.clones / "alpha")
    started = tmp_path / "mirror-started"
    harness.set_scenario({
        "pull_files": _BASE_FILES, "mirror_delay": 1.5,
        "mirror_started_file": str(started),
        "mirror_swap": [{"path": "user/alpha", "files": {"a.py": "from the other run\n"}},
                        {"path": "user/beta", "files": {"a.py": "from the other run\n"}}]})
    beta = _reference_copy(harness.clones / "beta", cid="col-b")
    stub = tmp_path / "fused_stub.py"
    other = subprocess.Popen(
        [sys.executable, str(stub), *MIRROR_ARGV, str(harness.root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_started(started)
        assert _clone(harness).status_code == 200
        assert other.poll() is None                          # still planning its swap
        assert other.wait(timeout=20) == 0
    finally:
        if other.poll() is None:
            other.kill()
            other.wait()
    assert (folder / "a.py").read_text() == "a1\n"
    assert canvas_marker.read(str(folder)).sync == "live"
    # The control: a copy nobody adopted WAS rewritten by that run.
    assert (beta / "a.py").read_text() == "from the other run\n"


# -- rename and delete ---------------------------------------------------------


def _no_sessions(monkeypatch):
    from fused_render import claude_session_move

    monkeypatch.setattr(claude_session_move, "held_sessions", lambda root: [])
    monkeypatch.setattr(claude_session_move, "relocate",
                        lambda old, new, **kw: {"moved": [], "pending": [], "complete": True})


def test_rename_removes_a_reference_copy_blocking_the_new_name(harness, tmp_path, monkeypatch):
    _shim_harness(harness, tmp_path, monkeypatch)
    _no_sessions(monkeypatch)
    assert _clone(harness).status_code == 200
    stale = _reference_copy(harness.clones / "gamma", cid="col-old-gamma")
    res = harness.client.post("/api/canvases/rename", headers=GUARD,
                              json={"env": ENV, "name": "alpha", "new_name": "gamma"})
    assert res.status_code == 200, res.text
    assert not (harness.clones / "alpha").exists()
    assert (stale / "a.py").read_text() == "a1\n" and not (stale / "gone.py").exists()
    marker = canvas_marker.read(str(stale))
    assert (marker.sync, marker.id, marker.name) == ("live", "col-a", "gamma")


@pytest.mark.parametrize("kind", ["clone", "unmarked", "broken"])
def test_rename_never_removes_a_folder_that_is_not_a_removable_copy(
        harness, tmp_path, monkeypatch, kind):
    _shim_harness(harness, tmp_path, monkeypatch)
    _no_sessions(monkeypatch)
    assert _clone(harness).status_code == 200
    target = harness.clones / "gamma"
    target.mkdir()
    (target / "keep.txt").write_text("keep\n")
    if kind == "clone":
        (target / "canvas.toml").write_text("")
        canvas_marker.write_live(str(target), name="gamma", collection_id="col-g")
    elif kind == "broken":
        (target / MARKER).write_text('{"sync": "mirror"}')     # says mirror, proves nothing
    res = harness.client.post("/api/canvases/rename", headers=GUARD,
                              json={"env": ENV, "name": "alpha", "new_name": "gamma"})
    assert res.status_code == 409 and res.json()["code"] == "exists"
    assert (target / "keep.txt").read_text() == "keep\n"
    assert (harness.clones / "alpha" / "canvas.toml").is_file()
    assert [c for c in harness.calls() if "rename" in c] == []


def test_rename_and_delete_run_with_the_mirror_held(harness, tmp_path, monkeypatch):
    _shim_harness(harness, tmp_path, monkeypatch)
    _no_sessions(monkeypatch)
    assert _clone(harness).status_code == 200
    held = {}
    real = canvases_mod._run_cli

    def run_cli(args, timeout, env):
        if args[:3] in (["workbench", "canvas", "rename"], ["workbench", "canvas", "delete"]):
            held[args[2]] = canvas_mirror.status(ENV)["held"]
        return real(args, timeout, env)

    monkeypatch.setattr(canvases_mod, "_run_cli", run_cli)
    started = _slow(harness, tmp_path, pull_files=_BASE_FILES)
    canvas_mirror.kick(ENV, "timer", force=True)
    pid = _wait_started(started)
    res = harness.client.post("/api/canvases/rename", headers=GUARD,
                              json={"env": ENV, "name": "alpha", "new_name": "gamma"})
    assert res.status_code == 200, res.text
    assert not _alive(pid)
    harness.set_scenario({})
    res = harness.client.post("/api/canvases/delete", headers=GUARD,
                              json={"env": ENV, "name": "gamma"})
    assert res.status_code == 200, res.text
    assert held == {"rename": True, "delete": True}
    assert canvas_mirror.status(ENV)["held"] is False
    assert not (harness.clones / "gamma").exists()


def test_delete_removes_a_reference_copy_and_its_adoption_flag(harness, tmp_path, monkeypatch):
    _shim_harness(harness, tmp_path, monkeypatch)
    harness.set_scenario({"pull_fail": True})
    folder = _reference_copy(harness.clones / "alpha")
    assert _clone(harness).status_code == 502                # mid-adoption: flag left
    assert _flag(harness).exists()
    harness.set_scenario({})
    other = _reference_copy(harness.clones / "beta", cid="col-b")
    for name in ("alpha", "beta"):
        res = harness.client.post("/api/canvases/delete", headers=GUARD,
                                  json={"env": ENV, "name": name})
        assert res.status_code == 200, res.text
    assert not folder.exists() and not other.exists() and not _flag(harness).exists()


# -- the two gaps stage 1 reported ---------------------------------------------


def test_a_reference_copys_desk_row_is_not_a_canvas_row(harness):
    """A row for `user/<name>` opens the canvas page, which a reference copy
    does not have until it is cloned."""
    copy = _reference_copy(harness.clones / "alpha")
    clone = harness.clones / "beta"
    clone.mkdir()
    (clone / "canvas.toml").write_text("")
    assert current_apps._canvas_of(str(copy)) is None
    assert current_apps._canvas_of(str(clone)) == {"env": ENV, "name": "beta"}
    canvas_marker.write_live(str(clone), name="beta", collection_id="col-b")
    assert current_apps._canvas_of(str(clone)) == {"env": ENV, "name": "beta"}
    # A clone that is gone keeps its canvas row (the desk's own rule).
    assert current_apps._canvas_of(str(harness.clones / "deleted")) == {
        "env": ENV, "name": "deleted"}
    # Adopted, the copy's row is a canvas row.
    canvas_marker.write_live(str(copy), name="alpha", collection_id="col-a")
    assert current_apps._canvas_of(str(copy)) == {"env": ENV, "name": "alpha"}


def test_a_stale_not_cloned_card_does_not_pull_over_a_clone_that_just_moved(
        harness, monkeypatch):
    """The page listed the catalog while the clone was still at its old path
    ("not cloned"); another path's pass then moved it. The card's click asks
    for a clone: answered with the folder as it is, unpublished work and all."""
    harness.log_in()
    legacy = harness.root / "alpha"
    legacy.mkdir(parents=True)
    (legacy / "canvas.toml").write_text('type = "canvas"\n')
    (legacy / "a.py").write_text("unpublished work\n")
    assert canvas_layout.ensure_layout(ENV).moved == ["alpha"]      # e.g. the startup pass
    res = _clone(harness)
    assert res.status_code == 200 and res.json()["dir"] == str(harness.clones / "alpha")
    assert (harness.clones / "alpha" / "a.py").read_text() == "unpublished work\n"
    assert _pulls(harness) == []
    # Past the window a clone request is the re-clone it always was.
    monkeypatch.setattr(canvas_layout, "MOVED_RECENTLY_S", 0.0)
    assert _clone(harness).status_code == 200
    assert len(_pulls(harness)) == 1
    assert canvas_layout.moved_recently(ENV, "never-moved") is False
