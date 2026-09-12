"""project_queue: which folder a task edits, who is holding it, and the line.

Four things are pinned here, and they are the four the rest of the feature codes
against: `queue_key` (the folder identity, including the paths that may never be
one), `holders` (the derivation — alive, live, not parked — plus the scheduler's
claimed entries and the short in-memory reservation), `order_key` (the order the
line moves in), and the held-answers store (a card decision parked while its
folder was busy).

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
    and therefore what `holders()` is filed under and what `is_free`, `reserve`
    and `holder_for` are asked about.

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
    monkeypatch.setattr(tasks_watch, "HISTORY_PATH", str(claude / "history.jsonl"))
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


def transcript(sid, ago=1.0):
    """A transcript whose newest real record is `ago` seconds old — the
    fallback the registry defers to when it has no opinion at all."""
    path = os.path.join(session_liveness.PROJECTS_DIR, "-proj", sid + ".jsonl")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": stamp, "cwd": "/proj",
                             "message": {"role": "user", "content": "go"}}) + "\n")
    return path


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
    `queue_key` answers is what `holders` is keyed on and what `is_free` and
    `holder_for` are asked about — and the raw path is not a key at all.
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

    held = pq.holders()
    assert held[key]["run_id"] == "r-1"
    assert target not in held          # the raw spelling is not a key
    assert pq.holder_for(key)["kind"] == "run"
    assert pq.is_free(key, SID) is True
    assert pq.is_free(key, SID2) is False


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


# ================================================================== holders


def test_a_live_unparked_run_holds_its_folder(home, agent):
    work = home / "work"
    work.mkdir()
    stage_run(agent, "20260912-100000-aaa", str(work / "page.html"), SID)
    registry(SID, status="busy")

    held = pq.holders()
    assert held == {folder_key(work): {
        "session_id": SID, "run_id": "20260912-100000-aaa",
        "task_key": SID, "kind": "run"}}
    assert pq.holder_for(folder_key(work))["kind"] == "run"


def test_a_shell_status_holds_too_and_idle_does_not(home, agent):
    """`busy` and `shell` are the two statuses that mean a turn is open;
    `waiting` is Claude waiting on the user and `idle` is nothing at all."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    for status, expected in (("shell", True), ("busy", True),
                             ("waiting", False), ("idle", False)):
        registry(SID, status=status)
        assert (folder_key(work) in pq.holders()) is expected, status


def test_a_parked_run_does_not_hold_the_folder(home, agent):
    """The whole reason the queue can move at all: a run blocked on a card
    nobody has answered may sit there until tomorrow, and the next task runs."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID,
              perms=[{"id": "p1", "tool": "Bash", "decision": ""}])
    registry(SID, status="busy")
    assert pq.holders() == {}

    # Answered: the run is ordinary again, with nothing to undo. The card list
    # is read once per scan window (`run_permissions`), and rewriting it inside
    # one is a run changing under a caller that has just walked the tree —
    # which is what `invalidate_holders` is for.
    stage_run(agent, "r-1", str(work / "page.html"), SID,
              perms=[{"id": "p1", "tool": "Bash", "decision": "allow"}])
    pq.invalidate_holders()
    assert pq.holders()[folder_key(work)]["kind"] == "run"


def test_a_killed_holder_stops_holding_at_once(home, agent):
    """A dead pid is not a live session however fresh the transcript looks —
    a stored lease is exactly what could not say this."""
    work = home / "work"
    work.mkdir()
    run_dir = stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    transcript(SID, ago=1.0)
    assert folder_key(work) in pq.holders()

    os.remove(run_dir / "alive")
    assert pq.holders() == {}


def test_a_departed_registry_row_beats_a_warm_transcript(home, agent):
    """`tasks_watch` remembers that a session's process went away, and that is
    an opinion: the 45s transcript tail must not paint a holder for a `claude`
    that exited four seconds ago."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    transcript(SID, ago=1.0)
    assert folder_key(work) in pq.holders()

    os.remove(os.path.join(tasks_watch.SESSIONS_DIR, "p.json"))
    tasks_watch.tick()
    assert pq.holders() == {}


def test_a_registry_row_with_a_dead_pid_holds_nothing(home, agent):
    """A `kill -9` leaves the row behind untouched. The watcher asks the pid
    every tick for exactly this, and a folder whose holder was killed has to
    free within a tick or two rather than staying locked until the file is
    cleaned up by something that is no longer running."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    transcript(SID, ago=1.0)
    assert folder_key(work) in pq.holders()

    registry(SID, status="busy", pid=2 ** 22 - 1)   # a pid nothing can hold
    assert pq.holders() == {}


def test_with_no_registry_opinion_the_transcript_decides(home, agent):
    """An older CLI writes no registry row at all. The tail rule is the
    fallback — and only the fallback, since the registry knows the pid."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    assert pq.holders() == {}          # nothing registered, nothing written

    transcript(SID, ago=1.0)
    assert pq.holders()[folder_key(work)]["kind"] == "run"

    transcript(SID, ago=600.0)         # cold: not mid-turn
    assert pq.holders() == {}


def test_a_live_run_that_has_not_named_itself_yet_holds_its_folder(home, agent):
    """FLIPPED 2026-09-12 (review): this used to assert the opposite — a run
    with no session id was nobody's holder, and the short reservation was said
    to cover the gap. It did not. A cold `claude` start in a real repo takes
    seconds to write its session file, the reservation was five, and for the
    rest of that start the folder derived as FREE with a live process already
    in it — which is the one thing the gate exists to prevent. A live process
    in a working tree is what is being protected, id or not."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="")
    held = pq.holders()[folder_key(work)]
    assert held["kind"] == "starting"
    assert held["run_id"] == "r-1"
    # Nobody can match an anonymous holder, so everything queues behind it…
    assert pq.is_free(folder_key(work), SID) is False
    # …and the rule the flip does NOT touch: a parked run still holds nothing.
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              perms=[{"id": "p1", "tool": "Bash", "decision": ""}])
    pq.invalidate_holders()      # a card raised inside one scan window
    assert pq.holders() == {}


def test_a_reservation_names_the_run_that_has_not_named_itself(home, agent):
    """The reservation and the starting run describe one spawn from opposite
    ends, so the conversation that just started a turn is still free to send
    into its own run — the inbox-absorb case, which would otherwise queue
    behind its own process."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="")
    pq.reserve(folder_key(work), SID)

    held = pq.holders()[folder_key(work)]
    assert held["kind"] == "starting"
    assert held["session_id"] == SID and held["task_key"] == SID
    assert pq.is_free(folder_key(work), SID) is True
    assert pq.is_free(folder_key(work), SID2) is False


def test_a_run_is_named_by_the_live_registry_through_its_pid(home, agent):
    """The fix for the `starting` window (Akshil, 2026-09-12). A brand-new
    chat's run dir names no session — `resumed_from` is empty and the `session`
    file is written by the first poll — but the CLI registers itself the moment
    it comes up, and the run dir has carried its pid since the host spawned it.
    So the run stops being anonymous in seconds rather than in
    `STARTING_GRACE` minutes."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              pid=os.getpid())
    registry(SID, status="busy")

    held = pq.holders()[folder_key(work)]
    assert held["kind"] == "run"                 # not "starting" any more
    assert held["session_id"] == SID and held["run_id"] == "r-1"
    assert pq.is_free(folder_key(work), SID) is True
    assert pq.is_free(folder_key(work), SID2) is False


def test_a_registry_named_run_that_is_idle_holds_nothing(home, agent):
    """And the other half of Akshil's case: once the run HAS a name, an idle
    session host holds nothing — which is what lets the second message of a
    finished conversation run at all."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              pid=os.getpid())
    registry(SID, status="idle")

    assert pq.holders() == {}
    assert pq.is_free(folder_key(work), SID) is True


def test_a_run_with_no_pid_or_no_registry_row_is_still_just_starting(home,
                                                                    agent):
    """The lookup is a bonus, never a requirement: a run whose pid nothing has
    registered is exactly the anonymous holder it always was."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              pid=2 ** 22 + 12345)
    assert pq.holders()[folder_key(work)]["kind"] == "starting"


def test_the_chat_that_started_a_run_is_free_to_send_into_it_by_run_id(home,
                                                                      agent):
    """AKSHIL'S BUG, at this module's level. The first message of a new chat is
    admitted with no session id, so the reservation it leaves names nobody and
    the run it spawns is anonymous — and the second message, which by then DOES
    carry a session, was told the folder was busy with a run it could not match.
    The run id is the name the conversation has before it has a session."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="")
    pq.reserve(folder_key(work), "")          # a new chat: no session yet

    assert pq.is_free(folder_key(work), SID) is False
    assert pq.is_free(folder_key(work), SID, run_id="r-1") is True
    # …and it is the RUN that matches, not any run id at all.
    assert pq.is_free(folder_key(work), SID, run_id="r-2") is False
    assert pq.is_free(folder_key(work), "", run_id="r-1") is True


def test_a_run_id_matches_a_named_holder_too(home, agent):
    """`run` and `starting` both carry the run they are, so the rule does not
    depend on which kind the holder happens to be this second."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id=SID)
    registry(SID, status="busy")

    assert pq.holders()[folder_key(work)]["kind"] == "run"
    assert pq.is_free(folder_key(work), SID2) is False
    assert pq.is_free(folder_key(work), SID2, run_id="r-1") is True


def test_a_reservation_made_with_no_session_is_renamed_by_its_own_chat(home,
                                                                      agent):
    """A reservation taken before the session existed must be CLAIMABLE by the
    chat that took it, not a wall it then queues behind. Admitting again with a
    session and the run id rewrites it."""
    work = home / "work"
    work.mkdir()
    key = folder_key(work)
    stage_run(agent, "r-1", str(work / "page.html"), session_id="")
    pq.reserve(key, "", run_id="r-1")

    assert pq.reserved(key) == "" and pq.reserved_run(key) == "r-1"
    assert pq.reserve_if_free(key, SID, "r-1") is True
    assert pq.reserved(key) == SID
    assert pq.reserved_run(key) == "r-1"
    # Another conversation still cannot take it.
    assert pq.reserve_if_free(key, SID2) is False


def test_a_reservation_with_no_session_still_names_the_run_it_authorised(home,
                                                                        agent):
    """Before the run dir even exists: the admission knew the run, so the
    holder it derives can be recognised by it."""
    work = home / "work"
    work.mkdir()
    key = folder_key(work)
    pq.reserve(key, "", run_id="r-9")

    held = pq.holders()[key]
    assert held["kind"] == "reserved" and held["run_id"] == "r-9"
    assert pq.is_free(key, SID) is False
    assert pq.is_free(key, SID, run_id="r-9") is True


def test_a_reservation_cannot_be_claimed_back_from_a_real_holder(home, agent):
    """The reservation is a claim on the folder; once something real is in
    there, the real thing is the only one that counts."""
    work = home / "work"
    work.mkdir()
    key = folder_key(work)
    pq.reserve(key, "", run_id="r-mine")
    stage_run(agent, "r-other", str(work / "page.html"), session_id=SID2)
    registry(SID2, status="busy")

    assert pq.holders()[key]["run_id"] == "r-other"
    assert pq.is_free(key, SID, run_id="r-mine") is False
    assert pq.reserve_if_free(key, SID, "r-mine") is False


def test_an_unnamed_run_stops_holding_once_it_is_plainly_not_starting(home,
                                                                     agent):
    """`_alive` is a pid probe and a pid is eventually recycled. Without a clock
    one abandoned run dir would hold its folder for ever and nothing could free
    it — a lease, which is the thing this module exists not to be."""
    work = home / "work"
    work.mkdir()
    run_dir = stage_run(agent, "r-1", str(work / "page.html"), session_id="")
    old = time.time() - pq.STARTING_GRACE - 60
    os.utime(run_dir, (old, old))
    pq.invalidate_holders()

    assert pq.holders() == {}


def test_a_run_answers_to_the_session_it_resumed(home, agent):
    """Both spellings: a run knows the session it RESUMED and the one the CLI
    minted for it, and either can be the id a task row carries."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              resumed_from=SID2)
    registry(SID2, status="busy")
    assert pq.holders()[folder_key(work)]["session_id"] == SID2


def test_an_unreadable_run_dir_costs_that_run_and_nothing_else(home, agent):
    work = home / "work"
    work.mkdir()
    broken = agent.dir / "r-broken"
    broken.mkdir()
    (broken / "meta.json").write_text("{not json")
    (agent.dir / "r-empty").mkdir()
    stage_run(agent, "r-ok", str(work / "page.html"), SID)
    registry(SID, status="busy")
    assert pq.holders()[folder_key(work)]["run_id"] == "r-ok"


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
    assert pq.holders() == {}
    assert [r["run_id"] for r in pq.scan_runs()] == [
        "20260912-000002-new", "20260912-000001-new", "20260912-000000-new"]


def test_a_claimed_scheduler_entry_holds_the_folder(home, agent, monkeypatch):
    """`sending` is a folder about to be busy: the tick claimed the entry and
    the process has not started, so nothing in the runs tree says so yet.
    Counting it is what stops one pass dispatching two entries into one
    folder."""
    from fused_render import schedule

    work = home / "work"
    work.mkdir()
    monkeypatch.setattr(schedule, "list_entries", lambda: [
        {"id": "e1", "state": schedule.SENDING, "target": str(work / "page.html"),
         "session_id": "", "claude_session_id": ""},
        {"id": "e2", "state": schedule.PENDING, "target": str(work / "page.html")},
    ])
    held = pq.holders()[folder_key(work)]
    assert held["kind"] == "sending"
    assert held["task_key"] == "pending:e1"


def test_a_running_run_outranks_a_claimed_entry_in_the_same_folder(
        home, agent, monkeypatch):
    from fused_render import schedule

    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    monkeypatch.setattr(schedule, "list_entries", lambda: [
        {"id": "e1", "state": schedule.SENDING, "target": str(work),
         "session_id": SID2}])
    assert pq.holders()[folder_key(work)]["kind"] == "run"


def test_a_starting_run_outranks_a_claimed_entry_in_the_same_folder(
        home, agent, monkeypatch):
    """NEVER DOWNGRADE A LIVE PROCESS (round-3 review, 2026-09-12). `sending`
    used to overwrite `starting`, so the moment a spawned run appeared in a
    folder a claim also named, the map stopped saying a process was in there —
    the claim reads as a folder ABOUT to be busy and the run dir is one that
    already is."""
    from fused_render import schedule

    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), session_id="")
    monkeypatch.setattr(schedule, "list_entries", lambda: [
        {"id": "e1", "state": schedule.SENDING, "target": str(work),
         "session_id": SID2}])

    held = pq.holders()[folder_key(work)]
    assert held["kind"] == "starting"
    assert held["run_id"] == "r-1"
    # …and nobody can match it, which is the whole point of holding it.
    assert pq.is_free(folder_key(work), SID2) is False


def test_an_unreadable_schedule_holds_nothing(home, agent, monkeypatch):
    from fused_render import schedule

    def boom():
        raise OSError("store is being rewritten")

    monkeypatch.setattr(schedule, "list_entries", boom)
    assert pq.holders() == {}


# ============================================================== reservation


def test_a_reservation_holds_the_folder_until_the_registry_catches_up(home, agent):
    """The gap between "run" and the registry row appearing is real and small;
    without this a second send lands in it."""
    work = home / "work"
    work.mkdir()
    key = pq.queue_key(str(work))
    assert pq.holders() == {}
    pq.reserve(key, SID)
    assert pq.reserved(key) == SID
    assert pq.holders()[key] == {"session_id": SID, "run_id": "",
                                 "task_key": SID, "kind": "reserved"}


def test_a_reservation_expires_on_its_own(home, agent):
    work = home / "work"
    work.mkdir()
    key = pq.queue_key(str(work))
    pq.reserve(key, SID, ttl=0.0)
    assert pq.reserved(key) == ""
    assert pq.holders() == {}


def test_a_reservation_never_masks_a_real_holder(home, agent):
    """Folded in LAST and only where nothing real was found — a stale
    reservation must never be able to say a folder is held by the wrong task."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    pq.reserve(folder_key(work), SID2)
    assert pq.holders()[folder_key(work)]["session_id"] == SID


def test_a_holder_on_a_clock_says_how_long_it_has_left(home, agent):
    """The two grace-period kinds are the two holds nothing rings for, so the
    scheduler asks them when to look again (`schedule._rearm`). Seconds from
    now, not a deadline: the reservation counts on `time.monotonic` and the run
    dir on wall time, and only a delta compares."""
    work = home / "work"
    work.mkdir()
    key = pq.queue_key(str(work))
    pq.reserve(key, SID, ttl=12.0)
    assert 11.0 < pq.holder_expires_in(key, pq.holders()[key]) <= 12.0

    pq.reserve(key, SID, ttl=0.0)
    stage_run(agent, "r-1", str(work / "page.html"), session_id="")
    run_dir = agent.dir / "r-1"
    aged = time.time() - pq.STARTING_GRACE + 30
    os.utime(run_dir, (aged, aged))
    pq.invalidate_holders()
    starting = pq.holders()[key]
    assert starting["kind"] == "starting"
    assert 29.0 < pq.holder_expires_in(key, starting) <= 30.0


def test_a_holder_that_ends_on_an_event_asks_for_no_timer(home, agent):
    """`run` ends when its turn ends and `sending` inside the pass that claimed
    it — both are rung, so both answer 0.0 and no timer is set for them."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    key = pq.queue_key(str(work))

    assert pq.holder_expires_in(key, pq.holders()[key]) == 0.0
    assert pq.holder_expires_in(key, None) == 0.0
    assert pq.holder_expires_in("", {"kind": "reserved"}) == 0.0


def test_reserving_nothing_is_a_no_op(home, agent):
    pq.reserve("", SID)
    assert pq.reserved("") == ""
    assert pq.holders() == {}


def test_reserve_if_free_lets_the_first_send_in_and_queues_the_second(home,
                                                                     agent):
    """The check-then-act race, which `is_free` followed by `reserve` could not
    close: two sends into one free folder both heard "free" and both reserved,
    and two `claude` processes started in one working tree. One call decides and
    takes the reservation, so the second is told to queue.

    `holders` answers `{}` for the whole test — nothing is on disk, exactly as
    it is in the window this covers — so the ONLY thing that can refuse the
    second send is the reservation the first one took."""
    work = home / "work"
    work.mkdir()
    key = pq.queue_key(str(work))

    assert pq.reserve_if_free(key, SID) is True
    assert pq.reserve_if_free(key, SID2) is False
    # The first reservation stands rather than being overwritten by the loser.
    assert pq.reserved(key) == SID


def test_reserve_if_free_admits_the_session_that_already_holds_the_folder(home, agent):
    """A second message into a conversation that is already running is the
    inbox-absorb case, and it refreshes the reservation rather than queueing."""
    work = home / "work"
    work.mkdir()
    key = pq.queue_key(str(work))

    assert pq.reserve_if_free(key, SID) is True
    assert pq.reserve_if_free(key, SID) is True
    assert pq.reserved(key) == SID


def test_reserve_if_free_queues_behind_a_real_holder(home, agent):
    """Not just the table: a folder a live run is holding refuses the send and
    stores nothing, so a failed admission cannot leave a reservation behind."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")

    assert pq.reserve_if_free(folder_key(work), SID2) is False
    assert pq.reserved(folder_key(work)) == ""


def test_a_folder_with_no_key_is_admitted_and_nothing_is_stored(home, agent):
    assert pq.reserve_if_free("", SID) is True
    assert pq.reserved("") == ""


def test_one_walk_of_the_runs_tree_answers_every_reader_in_a_second(home, agent,
                                                                   monkeypatch):
    """`holders()` and the tasks router's parked scan both used to walk the runs
    tree, and a single /api/tasks paid for both — 120 directories opened twice
    for one answer. They share one memoized walk now (`SCAN_TTL`)."""
    walks = []
    real = pq._read_runs
    monkeypatch.setattr(pq, "_read_runs",
                        lambda agent, names: (walks.append(1), real(agent, names))[1])
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")

    assert pq.holders()[folder_key(work)]["kind"] == "run"
    assert pq.holders()[folder_key(work)]["kind"] == "run"
    assert pq.scan_runs()[0]["run_id"] == "r-1"
    assert walks == [1]

    # …and a new run dir is never waited out: the memo is keyed on the listing.
    stage_run(agent, "r-2", str(work / "other.html"), SID2)
    assert [r["run_id"] for r in pq.scan_runs()] == ["r-2", "r-1"]
    assert walks == [1, 1]


# =========================================================== free / holder_for


def test_a_free_folder_is_free_for_anybody(home, agent):
    work = home / "work"
    work.mkdir()
    assert pq.holder_for(folder_key(work)) is None
    assert pq.is_free(folder_key(work), SID) is True
    assert pq.is_free(folder_key(work), "") is True


def test_a_folder_with_no_key_is_always_free():
    assert pq.holder_for("") is None
    assert pq.is_free("", SID) is True


def test_the_holder_is_free_to_send_into_its_own_folder(home, agent):
    """A second message into a conversation that is already running is the
    inbox-absorb case the chat has always had; gating it would refuse a send
    that touches nothing new."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    assert pq.is_free(folder_key(work), SID) is True
    assert pq.is_free(folder_key(work), SID2) is False
    # A task with no session id yet can never be the holder, so it waits.
    assert pq.is_free(folder_key(work), "") is False


# ------------------------------- the chat that cannot name itself at all


def test_a_holder_in_teardown_frees_its_folder_when_the_registry_says_idle(
        home, agent):
    """The first wall under the self-queue window (browser QA, 2026-09-12): a
    process that is still ALIVE but whose session has left `busy` is not a
    holder. A `claude` takes a moment to go away after its last row, and a
    folder locked for those seconds is a folder the next message queues behind
    for nothing."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)
    registry(SID, status="busy")
    assert folder_key(work) in pq.holders()

    registry(SID, status="idle")            # the turn is over, the pid is not
    assert pq.holders() == {}
    assert pq.is_free(folder_key(work), SID2) is True


def test_an_anonymous_send_claims_back_the_folder_its_own_run_went_quiet_in(
        home, agent):
    """AKSHIL'S SELF-QUEUE WINDOW, this module's half (browser QA,
    2026-09-12). A brand-new chat says hello, gets its reply, and types again
    while the client still knows neither the session nor the run — so the
    admission names nothing, and the thing refusing it was the ANONYMOUS
    reservation its own first message left behind.

    The wall that keeps this narrow is the run dir: a run of this folder's own,
    gone quiet, is the proof that a chat has already had a turn here, which is
    the only way an unnamed send can be a second message."""
    work = home / "work"
    work.mkdir()
    pq.reserve(folder_key(work), "")             # the first message's claim
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              pid=os.getpid())
    registry(SID, status="idle")                 # the turn ended

    assert pq.holders()[folder_key(work)]["kind"] == "reserved"
    assert pq.is_free(folder_key(work), "") is True
    assert pq.reserve_if_free(folder_key(work), "") is True


def test_an_anonymous_send_still_queues_while_that_run_is_running(home, agent):
    """…and not one step further. The same folder, the same nameless send, a
    turn actually in flight: this is the case the whole feature exists for and
    it queues."""
    work = home / "work"
    work.mkdir()
    pq.reserve(folder_key(work), "")
    stage_run(agent, "r-1", str(work / "page.html"), session_id="",
              pid=os.getpid())
    registry(SID, status="busy")

    assert pq.holders()[folder_key(work)]["kind"] == "run"
    assert pq.is_free(folder_key(work), "") is False
    assert pq.reserve_if_free(folder_key(work), "") is False


def test_two_brand_new_chats_in_one_empty_folder_still_queue(home, agent):
    """The race the wall is there for: two conversations that have never run
    anything, in a folder with no run of its own. The second cannot claim the
    first one's reservation, because nothing in that folder proves either of
    them has had a turn."""
    work = home / "work"
    work.mkdir()
    pq.reserve(folder_key(work), "")
    assert pq.is_free(folder_key(work), "") is False
    assert pq.reserve_if_free(folder_key(work), "") is False


def test_an_anonymous_send_never_claims_a_reservation_that_has_a_name(home,
                                                                      agent):
    """A reservation naming a session belongs to a conversation that CAN be
    named, and a send that cannot name itself is not it."""
    work = home / "work"
    work.mkdir()
    pq.reserve(folder_key(work), SID)
    stage_run(agent, "r-1", str(work / "page.html"), SID, pid=os.getpid())
    registry(SID, status="idle")

    assert pq.is_free(folder_key(work), "") is False
    assert pq.is_free(folder_key(work), SID) is True


def test_the_reserved_sessions_are_the_sends_just_admitted(home, agent):
    """What the listing reads to say `in_progress` the instant a send is
    admitted. A reservation with no session names nobody and is left out — an
    empty id would match every task on the machine that has no session yet."""
    work = home / "work"
    work.mkdir()
    other = home / "other"
    other.mkdir()
    assert pq.reserved_sessions() == set()

    pq.reserve(folder_key(work), SID)
    pq.reserve(folder_key(other), "")
    assert pq.reserved_sessions() == {SID}

    pq.reserve(folder_key(work), SID2, ttl=0.0)
    assert pq.reserved_sessions() == set()


# ================================================================= order_key


def _entry(entry_id, due, priority=False):
    return {"id": entry_id, "due": due, "priority": priority}


def test_priority_jumps_the_line():
    entries = [_entry("b", "2026-09-12T10:00:00+00:00"),
               _entry("c", "2026-09-12T11:00:00+00:00", priority=True),
               _entry("a", "2026-09-12T09:00:00+00:00")]
    assert [e["id"] for e in sorted(entries, key=pq.order_key)] == ["c", "a", "b"]


def test_a_priority_tie_falls_to_the_older_due():
    """Skipping two things must not reshuffle them."""
    entries = [_entry("y", "2026-09-12T11:00:00+00:00", priority=True),
               _entry("x", "2026-09-12T09:00:00+00:00", priority=True)]
    assert [e["id"] for e in sorted(entries, key=pq.order_key)] == ["x", "y"]


def test_a_due_tie_falls_to_the_entry_id():
    """A total order with no coin flips is what lets a position number shown in
    the UI still be true on the next poll."""
    same = "2026-09-12T09:00:00+00:00"
    entries = [_entry("20260912-090000-fff", same), _entry("20260912-090000-aaa", same)]
    assert [e["id"] for e in sorted(entries, key=pq.order_key)] == [
        "20260912-090000-aaa", "20260912-090000-fff"]


def test_an_unparsable_due_sorts_last_instead_of_raising():
    """The store is a JSON file a human may edit: one bad stamp costs that entry
    its place, never the whole ordering."""
    entries = [_entry("bad", "not a timestamp"), _entry("good", "2026-09-12T09:00:00+00:00")]
    assert [e["id"] for e in sorted(entries, key=pq.order_key)] == ["good", "bad"]
    assert pq.order_key({})[0] is True


# ============================================================= held answers


def _payload(decision="allow"):
    return {"decision": decision, "scope": "once"}


def test_a_held_answer_round_trips(home):
    record = pq.hold_answer("/work", SID, "r-1", "p1", _payload())
    assert record["queue_key"] == "/work"
    assert record["payload"] == _payload()
    assert isinstance(record["at"], float)
    assert pq.held_answers() == [record]
    stored = json.loads(
        (open(os.path.join(tasks_store.STATE_DIR, "held_answers.json"))).read())
    assert stored["version"] == pq.STORE_VERSION


def test_one_card_takes_one_answer_first_writer_wins(home):
    """The same latch `agent._write_decision` applies on disk: a double-click,
    or a cancel landing on a card the user just allowed, must not queue two
    answers to one question."""
    first = pq.hold_answer("/work", SID, "r-1", "p1", _payload("allow"))
    second = pq.hold_answer("/work", SID, "r-1", "p1", _payload("deny"))
    assert second == first
    assert [a["payload"]["decision"] for a in pq.held_answers()] == ["allow"]


def test_malformed_records_are_dropped_silently_on_read(home):
    pq.hold_answer("/work", SID, "r-1", "p1", _payload())
    path = os.path.join(tasks_store.STATE_DIR, "held_answers.json")
    state = json.loads(open(path).read())
    state["answers"] = [
        state["answers"][0],
        {"queue_key": "/work"},                                  # no run id
        {"queue_key": "", "run_id": "r", "request_id": "p",
         "session_id": "", "payload": {}, "at": 1.0},            # no folder
        {"queue_key": "/w", "run_id": "r", "request_id": "p",
         "session_id": "", "payload": "allow", "at": 1.0},       # payload not a dict
        {"queue_key": "/w", "run_id": "r", "request_id": "p",
         "session_id": "", "payload": {}, "at": True},           # a bool is not a time
        "a string",
    ]
    open(path, "w").write(json.dumps(state))
    assert [a["request_id"] for a in pq.held_answers()] == ["p1"]


def test_a_store_from_another_version_reads_as_empty(home):
    path = os.path.join(tasks_store.STATE_DIR, "held_answers.json")
    open(path, "w").write(json.dumps({"version": 99, "answers": [
        {"queue_key": "/w", "session_id": "", "run_id": "r", "request_id": "p",
         "payload": {}, "at": 1.0}]}))
    assert pq.held_answers() == []


def test_a_missing_or_corrupt_store_reads_as_empty(home):
    assert pq.held_answers() == []
    open(os.path.join(tasks_store.STATE_DIR, "held_answers.json"), "w").write("{[")
    assert pq.held_answers() == []


def test_popping_takes_one_folder_oldest_first_and_leaves_the_rest(home):
    """One call, because delivery is the moment the folder frees: a read then a
    delete would let a second tick write the same decisions twice."""
    a = pq.hold_answer("/work", SID, "r-1", "p1", _payload())
    b = pq.hold_answer("/work", SID, "r-1", "p2", _payload())
    other = pq.hold_answer("/other", SID2, "r-2", "p3", _payload())
    assert pq.pop_held_answers("/work") == [a, b]
    assert pq.held_answers() == [other]
    assert pq.pop_held_answers("/work") == []
    assert pq.pop_held_answers("") == []


def test_dropping_one_answer(home):
    pq.hold_answer("/work", SID, "r-1", "p1", _payload())
    assert pq.drop_held_answer("r-1", "p1") is True
    assert pq.drop_held_answer("r-1", "p1") is False
    assert pq.drop_held_answer("", "p1") is False
    assert pq.held_answers() == []


def test_a_held_answer_for_a_dead_run_expires_on_validation(home, agent):
    """The server-restart case: the decision was made for a process that no
    longer exists. Written out as `expired` — the same verdict `agent._decide`
    already writes when it is handed a dead run — so a page re-attaching to the
    old run sees a latched answer rather than a card it can still click."""
    live = stage_run(agent, "r-live", "/work/page.html", SID, alive=True)
    dead = stage_run(agent, "r-dead", "/work/page.html", SID2, alive=False)
    kept = pq.hold_answer("/work", SID, "r-live", "p1", _payload())
    pq.hold_answer("/work", SID2, "r-dead", "p2", _payload())

    dropped = pq.validate_held_answers(agent)
    assert [a["run_id"] for a in dropped] == ["r-dead"]
    assert pq.held_answers() == [kept]
    assert json.loads((dead / "perm" / "p2.res.json").read_text()) == {
        "decision": "expired"}
    assert not (live / "perm").exists()
    assert pq.validate_held_answers(agent) == []


def test_validation_without_an_agent_module_drops_nothing(home):
    pq.hold_answer("/work", SID, "r-1", "p1", _payload())
    assert pq.validate_held_answers(None) == []
    assert len(pq.held_answers()) == 1


def test_an_unwritable_state_dir_costs_the_write_and_not_the_caller(
        home, monkeypatch, tmp_path):
    """The caller is a user clicking Allow. An exception there would lose a
    decision that could at least have been delivered had the folder been free."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(blocked / "state"))
    record = pq.hold_answer("/work", SID, "r-1", "p1", _payload())
    assert record["run_id"] == "r-1"       # best effort: the record, unstored
    assert pq.held_answers() == []
    assert pq.pop_held_answers("/work") == []
    assert pq.drop_held_answer("r-1", "p1") is False


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
    ever read by `holders()`, which only runs with the flag on. Resolving one
    per run dir cost a registry read, a mount check and a climb for `.git` for
    an answer nobody was asking for."""
    work = home / "work"
    work.mkdir()
    stage_run(agent, "r-1", str(work / "page.html"), SID)

    run = pq.scan_runs()[0]
    assert "key" not in run
    assert pq.run_key(run) == folder_key(work)
    assert run["key"] == folder_key(work)          # …and remembered on the record


def test_one_card_list_per_run_per_scan_window(home, agent, monkeypatch):
    """TWO READERS, ONE LISTING. `holders()` asks whether a run is parked and
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

    assert pq.holders()[folder_key(work)]["kind"] == "run"
    run = pq.scan_runs()[0]
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


@pytest.mark.parametrize("run_id, request_id", [
    ("../../etc/passwd", "p1"),
    (".hidden", "p1"),
    ("a/b", "p1"),
    ("", "p1"),
    ("r-1", "../../../etc/passwd"),
    ("r-1", ".hidden"),
    ("r-1", "a\\b"),
    ("r-1", "c:evil"),
    ("r-1", ""),
])
def test_an_id_that_would_become_a_path_is_never_held(home, run_id, request_id):
    """DEFENCE IN DEPTH, and the depth is real: the endpoint checks these too,
    but what is written here is joined onto `agent.RUNS` and onto the perm
    directory (`<request_id>.res.json`) by a scheduler tick minutes later, or by
    `validate_held_answers` on the next launch — where nothing is left to say
    where the string came from."""
    assert pq.hold_answer("/work", SID, run_id, request_id, _payload()) is None
    assert pq.held_answers() == []


def test_bad_id_spells_the_agents_rule(home):
    """`agent._bad_id`, a second time, because agent.py is a TEMPLATE outside
    the package's import graph and the server cannot import the one it has —
    the same deliberate duplication as the store's path."""
    assert pq.bad_id("run-1") is False
    assert pq.bad_id("20260912-090000-abc") is False
    for bad in ("", ".", "..", "./x", "a/b", "a\\b", "d:x", None, 7):
        assert pq.bad_id(bad) is True, bad
