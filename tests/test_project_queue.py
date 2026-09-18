"""project_queue: which folder a task edits, and what its runs tree is doing.

Two things are pinned here, and they are the two the rest of the feature codes
against: `queue_key` (the folder identity, including the paths that may never be
one) and the bounded, memoized walk of the agent's run dirs (`scan_runs` and its
readers — `run_sessions`, `run_key`, `run_permissions`, `run_alive`,
`run_waiting`). WHO OWNS A FOLDER AND WHAT ORDER THE LINE MOVES IN ARE NOT HERE
(PR 2, 2026-09-17): they are `queue_manager.py` and tests/test_queue_manager.py.
`read_legacy_held_answers` — one read of the store that layer left behind — is
the last of it, pinned once below.

Everything runs against tmp dirs: a tmp `$HOME`, a tmp `~/.claude` for the live
registry and the transcripts, a tmp tasks state dir for the store, and a fake
agent module over a real runs tree — the same stand-in the tasks router's own
suite uses, because agent.py is a template outside the import graph (SPEC PY-15)
and what is under test is what this module does with its answers.
"""
import json
import os
import time

import pytest

from fused_render import current_apps, project_queue as pq
from fused_render import registered_apps, session_liveness, tasks_store, tasks_watch
from fused_render._view_url_codec import canonical_fs_path

SID = "11111111-1111-1111-1111-111111111111"
SID2 = "22222222-2222-2222-2222-222222222222"


def folder_key(path) -> str:
    """`path` spelled the way this module spells a folder — the canonical
    forward-slash form (`canonical_fs_path`), which is what `queue_key` answers
    and therefore what a scanned run is filed under (`run_key`).

    On POSIX it is `str(path)` and this helper is invisible. On Windows
    `str(WindowsPath(...))` is backslashed and `queue_key` never produces that
    spelling, so a test that asserted against it would be asserting the wrong
    contract — and one that keyed a fake holder on it would file the folder
    under a name no production caller will ever look up.
    """
    return canonical_fs_path(str(path))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A machine of our own: `$HOME`, the workspace, the claude dirs and the
    tasks state, all under tmp. `reset_cache` on both sides because the folder
    key and the agent module are memoized for the life of a process and every
    case here moves the layout underneath them."""
    house = tmp_path / "home"
    (house / ".fused-render").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(house))
    monkeypatch.setenv("USERPROFILE", str(house))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(house / ".fused-render"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(house / "Fused"))

    claude = tmp_path / "claude"
    (claude / "projects" / "-proj").mkdir(parents=True)
    (claude / "sessions").mkdir()
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(claude / "sessions"))
    monkeypatch.setattr(session_liveness, "PROJECTS_DIR", str(claude / "projects"))

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(state))

    tasks_watch.reset()
    pq.reset_cache()
    yield house
    tasks_watch.reset()
    pq.reset_cache()
    tasks_watch.reset()


# ----------------------------------------------------------- the fake agent

class FakeAgent:
    """The parts of the claude template's agent.py this module reads.

    `_write_decision` is the real O_EXCL latch rather than a recorder: the
    expiry path is only meaningful if a second writer really cannot overwrite
    the verdict, and that is the property `validate_held_answers` leans on."""

    def __init__(self, runs_dir):
        self.RUNS = str(runs_dir)

    def _alive(self, run_dir):
        return os.path.exists(os.path.join(run_dir, "alive"))

    def _permissions(self, run_dir):
        try:
            with open(os.path.join(run_dir, "perms.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return []

    def _session_from_out(self, run_dir):
        return ""

    def _perm_dir(self, run_dir):
        return os.path.join(run_dir, "perm")

    def _write_decision(self, perm_dir, request_id, payload):
        os.makedirs(perm_dir, exist_ok=True)
        path = os.path.join(perm_dir, request_id + ".res.json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return True


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    fake = FakeAgent(runs)
    monkeypatch.setattr(pq, "agent_module", lambda: fake)
    fake.dir = runs
    return fake


@pytest.fixture
def flag(home):
    """The pref ON, for the tests that exercise a path only the queue takes —
    naming a run through the live registry, claiming a folder back. The pid
    lookup is gated (flag-off audit, 2026-09-12): with the flag off
    `_parked_runs` walks the same run dirs and must not open the registry."""
    prefs_path = home / ".fused-render" / "prefs.json"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({"project_queue_enabled": True}))
    pq.reset_cache()
    return prefs_path


def stage_run(agent, name, file, session_id="", resumed_from="", alive=True,
              perms=(), pid=None):
    """One run dir as the agent writes it: `meta.json` (the target file and the
    session it resumed), the `session` file the first poll leaves, the `pid`
    file the session host overwrites with the CLI's own pid, and this suite's
    two stand-ins for liveness and a perm directory."""
    run_dir = agent.dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"file": file, "resumed_from": resumed_from, "message": "go"}))
    if session_id:
        (run_dir / "session").write_text(session_id)
    if pid is not None:
        (run_dir / "pid").write_text(str(pid))
    if alive:
        (run_dir / "alive").write_text("1")
    (run_dir / "perms.json").write_text(json.dumps(list(perms)))
    return run_dir


_STAMP = [time.time() + 10]


def registry(sid, status="busy", pid=None, name="p"):
    """One `~/.claude/sessions/<pid>.json` row, then a tick to read it in. The
    mtime is nudged a whole second per write because two rewrites inside the
    clock's granularity are, correctly, no change to the watcher."""
    path = os.path.join(tasks_watch.SESSIONS_DIR, name + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid() if pid is None else pid, "sessionId": sid,
                   "cwd": "/proj", "status": status,
                   "updatedAt": int(time.time() * 1000)}, fh)
    _STAMP[0] += 1
    os.utime(path, (_STAMP[0], _STAMP[0]))
    tasks_watch.tick()


# ================================================================ queue_key


def test_an_empty_or_relative_project_has_no_folder():
    """`""` is "no folder", and no folder is never held — so a task the server
    cannot place runs rather than queueing behind something it cannot name."""
    assert pq.queue_key("") == ""
    assert pq.queue_key("relative/path") == ""
    assert pq.queue_key(None) == ""


def test_home_and_the_filesystem_root_are_never_keys(home):
    """They are not a project, they are the machine. Keying on either would put
    every unrelated task on the box in one line behind one another."""
    assert pq.queue_key(str(home)) == ""
    assert pq.queue_key(str(home) + "/") == ""
    assert pq.queue_key("/") == ""


def test_a_plain_folder_is_its_own_key(home):
    work = home / "work" / "scratch"
    work.mkdir(parents=True)
    assert pq.queue_key(str(work)) == folder_key(work)


def test_the_key_is_the_canonical_forward_slash_spelling(home):
    """THE CONTRACT EVERY OTHER READER LEANS ON. A key is `canonical_fs_path`'s
    spelling of the folder — forward slashes — and never the OS's own. On
    Windows `str(Path)` is backslashed, so the two spellings of one folder are
    visibly different there and only this one is ever a key: a holder filed
    under the other, or a `is_free` asked about it, silently misses.

    Pinned here rather than left implicit in thirty assertions, because it is
    the single fact `holders`, `is_free`, `reserve`, `holder_for`, the
    scheduler's `_queue_position` and the router's `_queue_lines` all agree on.
    """
    work = home / "work" / "scratch"
    work.mkdir(parents=True)
    key = pq.queue_key(str(work))
    assert key == canonical_fs_path(os.path.abspath(str(work)))
    assert key == canonical_fs_path(key)  # idempotent: a key is already canonical
    # …and that spelling is forward slashes wherever a drive letter appears,
    # which is the half of the rule this suite cannot otherwise reach on POSIX.
    assert canonical_fs_path("C:\\Users\\a\\proj") == "C:/Users/a/proj"


def test_every_reader_speaks_the_spelling_queue_key_answers(home, agent,
                                                            monkeypatch):
    """WINDOWS IN MINIATURE, on any OS.

    There a folder has two spellings — `str(Path)`'s backslashes and
    `canonical_fs_path`'s forward slashes — and everything that files or looks
    up a folder must speak the second. Simulated by making the canonical
    spelling visibly different from the path on disk (through the one branch
    that returns without stat'ing what it answers, a wedged mount), so the
    invariant is tested rather than hidden behind a POSIX identity: whatever
    `queue_key` answers is what a scanned run is filed under (`run_key`), and
    the raw path is not a key at all.
    """
    class AlwaysWedged:
        def blocks(self, path):
            return True

    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")

    pq.reset_cache()
    monkeypatch.setattr(pq, "canonical_fs_path", lambda path: path + "~canon")
    monkeypatch.setattr(pq, "_GUARD", AlwaysWedged())

    target = str(work / "page.html")
    key = pq.queue_key(target)
    assert key == target + "~canon"

    run = pq.scan_runs()[0]
    assert run["run_id"] == "r-1"
    assert pq.run_key(run) == key      # …and the raw spelling is not a key
    assert pq.run_key(run) != target


def test_a_file_target_keys_on_its_folder(home):
    """A scheduled entry's `target` is a page file, a task row's `project` is a
    cwd; two tasks on two files in one directory must still queue on each
    other."""
    work = home / "work"
    work.mkdir()
    (work / "page.html").write_text("x")
    (work / "other.html").write_text("x")
    assert pq.queue_key(str(work / "page.html")) == folder_key(work)
    assert pq.queue_key(str(work / "other.html")) == folder_key(work)


def test_two_subfolders_of_one_app_share_a_key(home, monkeypatch):
    app = home / "linked" / "myapp"
    (app / "a").mkdir(parents=True)
    (app / "b").mkdir(parents=True)
    monkeypatch.setattr(registered_apps, "read_entries",
                        lambda: [{"path": str(app)}])
    pq.reset_cache()
    assert pq.queue_key(str(app / "a")) == folder_key(app)
    assert pq.queue_key(str(app / "b")) == folder_key(app)
    assert pq.queue_key(str(app)) == folder_key(app)


def test_the_nearest_git_ancestor_wins_over_a_deeper_folder(home):
    repo = home / "code" / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "src" / "inner"
    deep.mkdir(parents=True)
    assert pq.queue_key(str(deep)) == folder_key(repo)


def test_a_worktree_keys_on_itself_not_on_the_repo_it_came_from(home):
    """`git worktree add` writes a `.git` FILE, and a worktree is precisely the
    thing the user set up so two agents could run at once — reading only
    directories here would serialise exactly the case the feature exists to
    keep parallel."""
    repo = home / "code" / "repo"
    (repo / ".git").mkdir(parents=True)
    tree = home / "code" / "repo-wt" / "branch"
    tree.mkdir(parents=True)
    (tree / ".git").write_text("gitdir: " + str(repo / ".git" / "worktrees" / "b"))
    assert pq.queue_key(str(tree)) == folder_key(tree)
    assert pq.queue_key(str(repo)) == folder_key(repo)
    assert pq.queue_key(str(tree)) != pq.queue_key(str(repo))


def test_a_git_dir_above_home_is_not_climbed_to(home):
    """The climb stops at `$HOME`: a stray `.git` in the home directory (or
    anywhere above it) must not make every project on the machine one key."""
    (home / ".git").mkdir()
    work = home / "notes"
    work.mkdir()
    assert pq.queue_key(str(work)) == folder_key(work)


def test_a_wedged_mount_answers_with_the_path_and_makes_no_syscall(
        home, monkeypatch):
    """MountGuard is consulted before anything is stat'd, so a project on a dead
    NFS server costs a string comparison rather than a thirty-second hang."""
    from fused_render.index.ignore import MountGuard

    wedged = home / "mnt"
    monkeypatch.setattr(pq, "_GUARD",
                        MountGuard(mounts_dir=str(wedged), home_dirs=[]))

    def boom(_project):
        raise AssertionError("app_dir_for must not be reached on a wedged mount")

    monkeypatch.setattr(current_apps, "app_dir_for", boom)
    assert pq.queue_key(str(wedged / "share" / "proj")) == folder_key(wedged / "share" / "proj")


def test_the_key_is_memoized_and_reset_cache_forgets_it(home):
    """A folder identity is a property of the layout on disk and is asked per
    task row per poll, so it is cached — which does mean a `.git` planted later
    needs the reset."""
    work = home / "work"
    work.mkdir()
    assert pq.queue_key(str(work)) == folder_key(work)
    (work / ".git").mkdir()
    sub = work / "sub"
    sub.mkdir()
    assert pq.queue_key(str(work)) == folder_key(work)  # cached, unchanged
    pq.reset_cache()
    assert pq.queue_key(str(sub)) == folder_key(work)


# ============================================================ the runs tree


def test_a_parked_run_reads_as_waiting_until_its_card_is_answered(home, agent):
    """The whole reason the queue can move at all: a run blocked on a card
    nobody has answered may sit there until tomorrow, holds nothing, and the
    next task runs (`run_waiting`, the manager's `blocked`)."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID,
              perms=[{"id": "p1", "tool": "Bash", "decision": ""}])
    registry(SID, status="busy")
    assert pq.run_waiting(agent, pq.scan_runs()[0]) is True

    # Answered: the run is ordinary again, with nothing to undo. The card list
    # is read once per scan window (`run_permissions`), and rewriting it inside
    # one is a run changing under a caller that has just walked the tree —
    # which is what `invalidate_holders` is for.
    stage_run(agent, "r-1", str(work / "page.html"), SID,
              perms=[{"id": "p1", "tool": "Bash", "decision": "allow"}])
    pq.invalidate_holders()
    assert pq.run_waiting(agent, pq.scan_runs()[0]) is False


def test_a_run_answers_to_the_session_it_resumed(home, agent):
    """Both spellings: a run knows the session it RESUMED and the one the CLI
    minted for it, and either can be the id a task row carries."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              resumed_from=SID2)
    registry(SID2, status="busy")
    run_dir = str(agent.dir / "r-1")
    assert pq.run_sessions(agent, run_dir, {"resumed_from": SID2}) == {SID2}


def test_an_unreadable_run_dir_costs_that_run_and_nothing_else(home, agent):
    work = home / "work"
    work.mkdir()
    broken = agent.dir / "r-broken"
    broken.mkdir()
    (broken / "meta.json").write_text("{not json")
    (agent.dir / "r-empty").mkdir()
    stage_run(agent, "r-ok", str(work / "page.html"), SID)
    registry(SID, status="busy")
    # The half-written one and the empty one cost themselves and nothing else.
    assert [r["run_id"] for r in pq.scan_runs()] == ["r-ok"]
    assert pq.run_key(pq.scan_runs()[0]) == folder_key(work)


def test_the_run_scan_is_bounded_to_the_newest_dirs(home, agent, monkeypatch):
    """Nothing prunes the runs tree. A live run buried under a hundred newer
    ones does not exist — and the scan must stay the same size on a machine
    that has been chatting for weeks."""
    monkeypatch.setattr(pq, "RUN_SCAN_LIMIT", 3)
    work = home / "work"
    work.mkdir()
    stage_run(agent, "20260101-000000-old", str(work / "page.html"), SID)
    registry(SID, status="busy")
    for n in range(3):
        stage_run(agent, "20260912-00000%d-new" % n, str(work / "other.html"),
                  session_id="", alive=False)
    assert [r["run_id"] for r in pq.scan_runs()] == [
        "20260912-000002-new", "20260912-000001-new", "20260912-000000-new"]


# =============================================================== the memo


def test_one_walk_of_the_runs_tree_answers_every_reader_in_a_second(home, agent,
                                                                   monkeypatch):
    """The tasks router's parked scan and the manager's per-task `blocked`
    check both walk the runs tree, and a single /api/tasks used to pay for both
    — 120 directories opened twice for one answer. They share one memoized walk
    (`SCAN_TTL`)."""
    walks = []
    real = pq._read_runs
    monkeypatch.setattr(pq, "_read_runs",
                        lambda agent, names: (walks.append(1), real(agent, names))[1])
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")

    assert pq.scan_runs()[0]["run_id"] == "r-1"
    assert pq.run_waiting(agent, pq.scan_runs()[0]) is False
    assert pq.scan_runs()[0]["run_id"] == "r-1"
    assert walks == [1]

    # …and a new run dir is never waited out: the memo is keyed on the listing.
    stage_run(agent, "r-2", str(work / "other.html"), SID2)
    assert [r["run_id"] for r in pq.scan_runs()] == ["r-2", "r-1"]
    assert walks == [1, 1]


# ================================================ the old held-answers store


def test_read_legacy_held_answers_parses_the_old_file(home):
    """ALL THAT IS LEFT OF THE STORE (PR 2, 2026-09-17): one read, so the queue
    manager can take over decisions a previous version parked in
    `held_answers.json`. Nothing writes it any more.

    Three rules in one case, because they are one function: the records come
    back oldest first by `at` (the place in the line, not the position in the
    file), a record that is not one is dropped silently rather than migrated
    half-formed, and a file this code does not know reads as empty."""
    path = os.path.join(tasks_store.STATE_DIR, pq.HELD_ANSWERS_FILE)
    good = {"queue_key": "/w", "session_id": SID, "run_id": "r-1",
            "request_id": "p1", "payload": {"raw": {"decision": "allow"}},
            "at": 200.0}
    older = dict(good, request_id="p0", at=100.0)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": pq.STORE_VERSION,
                   "answers": [good, older,
                               dict(good, run_id=""),        # no run to deliver to
                               dict(good, payload="allow"),  # not a payload
                               dict(good, at="soon"),        # no place in the line
                               "not a record"]}, fh)

    got = pq.read_legacy_held_answers()
    assert [(a["request_id"], a["at"]) for a in got] == [("p0", 100.0),
                                                         ("p1", 200.0)]
    assert got[0]["payload"] == {"raw": {"decision": "allow"}}

    # A version this code does not know, a file that is not JSON, and no file
    # at all: three ways of holding nothing.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": pq.STORE_VERSION + 1, "answers": [good]}, fh)
    assert pq.read_legacy_held_answers() == []
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert pq.read_legacy_held_answers() == []
    os.remove(path)
    assert pq.read_legacy_held_answers() == []


# ==================================================================== enabled


def test_the_flag_is_off_by_default_and_read_fresh(home):
    """Read per call, so a toggle applies to the very next send with no
    restart."""
    assert pq.enabled() is False
    prefs_path = home / ".fused-render" / "prefs.json"
    prefs_path.write_text(json.dumps({"project_queue_enabled": True}))
    assert pq.enabled() is True
    prefs_path.write_text(json.dumps({"project_queue_enabled": "yes"}))
    assert pq.enabled() is False


# ======================================================= the memo's lazy slots


def test_the_walk_resolves_no_folder_key_of_its_own(home, agent):
    """`scan_runs` is the LISTING's walk as much as the queue's — the tasks
    router's parked scan reads it with the flag OFF — and a folder key is only
    ever read under the flag. Resolving one per run dir cost a registry read, a
    mount check and a climb for `.git` for an answer nobody was asking for."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)

    run = pq.scan_runs()[0]
    assert "key" not in run
    assert pq.run_key(run) == folder_key(work)
    assert run["key"] == folder_key(work)          # …and remembered on the record


def test_one_card_list_per_run_per_scan_window(home, agent, monkeypatch):
    """TWO READERS, ONE LISTING. `run_waiting` asks whether a run is parked and
    the tasks router's parked scan asks what it is parked ON — a directory
    listing plus a file per card, paid twice for one answer until the record
    started carrying it."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID,
              perms=[{"id": "p1", "tool": "Bash", "decision": "allow"}])
    registry(SID, status="busy")
    reads = []
    real = agent._permissions
    monkeypatch.setattr(agent, "_permissions",
                        lambda run_dir: (reads.append(run_dir), real(run_dir))[1])

    run = pq.scan_runs()[0]
    assert pq.run_waiting(agent, run) is False
    assert [p["id"] for p in pq.run_permissions(agent, run)] == ["p1"]
    assert len(reads) == 1

    # …and a fresh walk pays for a fresh answer.
    pq.invalidate_holders()
    pq.run_permissions(agent, pq.scan_runs()[0])
    assert len(reads) == 2


def test_an_unreadable_perm_dir_is_no_cards_and_not_a_raise(home, agent,
                                                            monkeypatch):
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)

    def boom(run_dir):
        raise OSError("gone")

    monkeypatch.setattr(agent, "_permissions", boom)
    run = pq.scan_runs()[0]
    assert pq.run_permissions(agent, run) == []
    # …and the reader that decides whether a folder is held reads the same
    # nothing rather than calling the run parked and freeing its tree.
    assert pq.run_waiting(agent, run) is False


# ================================================================ the caches


def test_the_folder_key_table_is_capped(home, monkeypatch):
    """Memoized per PATH, and the paths are task rows' projects — bounded in
    life by how many folders this machine has chatted in and unbounded only in
    principle. Cleared WHOLE rather than evicted one at a time: refilling costs
    one `.git` climb per live project, which is the cheap half of what the memo
    saves."""
    monkeypatch.setattr(pq, "KEY_CACHE_MAX", 3)
    for n in range(3):
        folder = home / "work" / f"p{n}"
        folder.mkdir(parents=True)
        assert pq.queue_key(str(folder)) == folder_key(folder)
    assert len(pq._KEY_CACHE) == 3

    folder = home / "work" / "p3"
    folder.mkdir()
    assert pq.queue_key(str(folder)) == folder_key(folder)
    assert len(pq._KEY_CACHE) == 1


# ============================================ ids that would become a path


def test_bad_id_spells_the_agents_rule(home):
    """`agent._bad_id`, a second time, because agent.py is a TEMPLATE outside
    the package's import graph and the server cannot import the one it has —
    the same deliberate duplication as the store's path."""
    assert pq.bad_id("run-1") is False
    assert pq.bad_id("20260912-090000-abc") is False
    for bad in ("", ".", "..", "./x", "a/b", "a\\b", "d:x", None, 7):
        assert pq.bad_id(bad) is True, bad


def test_with_the_flag_off_a_run_is_still_named_through_the_registry(home, agent):
    """Reversed on 2026-09-16 (Akshil): the flag guards the one-task-per-folder
    rule, not the sync improvements. Knowing which session a run is — off the
    registry row the CLI writes against its pid, seconds before the run dir's
    own `session` file — is liveness bookkeeping every listing benefits from."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              pid=os.getpid())
    registry(SID, status="busy")
    assert pq.enabled() is False
    run = pq.scan_runs(agent)[0]
    assert SID in pq.run_sessions(agent, run["run_dir"], run.get("meta") or {})
