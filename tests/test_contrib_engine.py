"""contrib/engine.py + replay.py + follow.py against REAL git repos
(docs/CONTRIB_LOOP.md §14): a local bare repo stands in for the author's
GitHub repo, reached through `url.<path>.insteadOf https://github.com/…` so
the engine fetches by the record's GitHub URL exactly as in production.

The data-loss paths: an unrelated author push keeps Bob's changes; an
overlapping one records needs-merge and leaves the tree alone; a dirty
overlap defers; discard backs up; a committed `.fused/` stays clean and
still updates; a proposal survives an integrate and A4's return keeps the
unproposed edits; an own copy's divergence replays and pushes; and with the
flags off `_sync_locked` is exactly today's.
"""
import json
import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import git, git_available, write  # noqa: E402

from fused_render import git_upstream  # noqa: E402
from fused_render.contrib import classify, engine, replay, store  # noqa: E402

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")

REPO = "alice/sine"
URL = "https://github.com/" + REPO


# --------------------------------------------------------------- fixtures


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    home.mkdir()
    (ws / "app").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    # Our own git calls must not see the developer's ~/.gitconfig either.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(git_upstream, "_checked", {})
    monkeypatch.setattr(git_upstream, "_state", {})
    monkeypatch.setattr(git_upstream, "_sync_failures", {})
    monkeypatch.setattr(git_upstream, "_pulled_events", [])
    monkeypatch.setattr(git_upstream, "_pending", {})
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(classify, "_identity", lambda: None)
    monkeypatch.setattr(classify, "ensure_in_background", lambda app_dir: False)
    engine._status_cache.clear()
    if git_upstream._check_slot.acquire(timeout=git_upstream.TIMEOUT_S + 5):
        git_upstream._check_slot.release()

    class Env:
        pass

    e = Env()
    e.tmp, e.home, e.ws = tmp_path, home, ws
    e.remote = str(tmp_path / "remote.git")
    e.alice = str(tmp_path / "alice")
    git(str(tmp_path), "init", "-q", "--bare", "-b", "main", e.remote)
    os.makedirs(e.alice)
    git(e.alice, "init", "-q", "-b", "main")
    _ident(e.alice)
    write(e.alice, "index.html", "<p>hello</p>\n")
    write(e.alice, "a.txt", "one\ntwo\nthree\n")
    git(e.alice, "add", "-A")
    git(e.alice, "commit", "-q", "-m", "c1")
    git(e.alice, "remote", "add", "origin", e.remote)
    git(e.alice, "push", "-q", "-u", "origin", "main")
    # The engine fetches https://github.com/alice/sine; git maps it here.
    # Both spellings: the engine's own fallback fetches URL, gh.fetch URL.git
    # (insteadOf is a prefix rewrite, so the longer one must be its own row).
    monkeypatch.delenv("FUSED_RENDER_CONTRIB_FAKE_GH", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{e.remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", URL + ".git")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.{e.remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", URL)
    from fused_render import claude_session_move
    monkeypatch.setattr(claude_session_move, "relocate",
                        lambda old, new: {"moved": [], "pending": []})
    return e


def _ident(repo):
    git(repo, "config", "user.name", "Fixture Author")
    git(repo, "config", "user.email", "fixture@example.com")


def _flags(e, **on):
    keys = {"sources": "contrib_sources_enabled", "changes": "contrib_changes_enabled",
            "inbox": "contrib_inbox_enabled", "loop": "contrib_loop_enabled",
            "remix": "contrib_remix_enabled"}
    with open(e.home / "prefs.json", "w") as f:
        json.dump({keys[k]: v for k, v in on.items()}, f)


def _all_on(e):
    _flags(e, sources=True, changes=True, inbox=True, loop=True)


def _bob(e, name="sine", kind="github", repo=REPO):
    """Bob's copy: a clone at <ws>/app/<name> with a record, as installed."""
    bob = os.path.join(str(e.ws), "app", name)
    git(str(e.tmp), "clone", "-q", e.remote, bob)
    _ident(bob)
    rec = store.new_record(kind, repo=repo, branch="main", classified_by="install")
    store.put(bob, rec)
    return os.path.realpath(bob)


def _alice_commit(e, name, text, msg):
    write(e.alice, name, text)
    git(e.alice, "add", "-A")
    git(e.alice, "commit", "-q", "-m", msg)
    git(e.alice, "push", "-q", "origin", "main")
    return git(e.alice, "rev-parse", "HEAD").strip()


def _commit(repo, name, text, msg):
    write(repo, name, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").strip()


def _head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def _read(repo, name):
    with open(os.path.join(repo, name)) as f:
        return f.read()


def _sync(root, push=False):
    return git_upstream.sync_repo(root, action="test", push=push)


def _worktrees(repo):
    return [ln for ln in git(repo, "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")]


def _no_operation(repo):
    assert git_upstream._operation_in_flight(repo) is None
    assert len(_worktrees(repo)) == 1


# ------------------------------------------------------------------ owns


def test_flags_off_sync_locked_is_unchanged(env):
    """Every flag off: a recorded GitHub-link copy is synced by today's code
    — a divergence is still today's standing `diverged` failure."""
    bob = _bob(env)
    _commit(bob, "b.txt", "bob\n", "bob change")
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    assert engine.owns(bob) is False

    result = _sync(bob, push=True)

    assert result["status"] == "failed"
    assert result["failure"]["reason"] == "diverged"
    assert [f["reason"] for f in git_upstream.sync_failures()] == ["diverged"]
    assert store.get(bob)["needs_merge"] is None
    assert store.get(bob)["events"] == []


def test_flags_off_plain_fast_forward_unchanged(env):
    bob = _bob(env)
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")
    result = _sync(bob)
    assert result["status"] == "synced" and result["pulled"] == 1
    assert _head(bob) == tip
    assert not git(bob, "for-each-ref", "refs/fused/").strip()


def test_owns_needs_the_phase_flag(env):
    bob = _bob(env)
    _flags(env, sources=True)
    assert engine.owns(bob) is False
    _flags(env, changes=True)  # without its prerequisite
    assert engine.owns(bob) is False
    _flags(env, sources=True, changes=True)
    assert engine.owns(bob) is True


# ---------------------------------------------------------- github copies


def test_unrelated_author_push_keeps_changes(env):
    _all_on(env)
    bob = _bob(env)
    mine = _commit(bob, "b.txt", "bob\n", "bob change")
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")

    result = _sync(bob)

    assert result["status"] == "synced"
    assert _read(bob, "b.txt") == "bob\n" and _read(bob, "c.txt") == "alice\n"
    git(bob, "merge-base", "--is-ancestor", tip, "HEAD")  # raises if not
    assert git(bob, "log", "-1", "--format=%s").strip() == "bob change"
    assert git_upstream.sync_failures() == []
    st = engine.status(bob)
    assert st["state"] == "changes" and st["changes"] == 1 and st["behind"] == 0
    assert st["event"]["kind"] == "updated-kept" and st["event"]["who"] == "alice"
    # The pre-replay tip is pinned.
    backups = git(bob, "for-each-ref", "--format=%(objectname)", "refs/fused/backup/").split()
    assert mine in backups
    assert store.get(bob)["backups"][-1]["why"] == "update-kept"
    _no_operation(bob)


def test_overlapping_push_needs_merge_and_leaves_tree(env):
    _all_on(env)
    bob = _bob(env)
    mine = _commit(bob, "a.txt", "one\nBOB\nthree\n", "bob edits line two")
    tip = _alice_commit(env, "a.txt", "one\nALICE\nthree\n", "alice edits line two")

    result = _sync(bob)

    assert result["status"] == "skipped" and result["why"] == "needs-merge"
    assert _head(bob) == mine
    assert _read(bob, "a.txt") == "one\nBOB\nthree\n"
    assert git(bob, "status", "--porcelain").strip() == ""
    nm = store.get(bob)["needs_merge"]
    assert nm["onto"] == tip and nm["head"] == mine and nm["purpose"] == "needs-merge"
    assert nm["files"] == ["a.txt"]
    assert git_upstream.sync_failures() == []
    assert engine.status(bob)["state"] == "needs-merge"
    _no_operation(bob)
    # A second trigger with nothing new does not re-replay or change anything.
    assert _sync(bob)["why"] == "needs-merge"
    assert _head(bob) == mine


def test_follower_fast_forwards_and_records_pull(env):
    _all_on(env)
    bob = _bob(env)
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")
    result = _sync(bob)
    assert result["status"] == "synced" and result["pulled"] == 1
    assert _head(bob) == tip
    assert [p["count"] for p in git_upstream.recent_pulls()] == [1]
    st = engine.status(bob)
    assert st["state"] == "following" and st["event"] is None
    assert store.get(bob)["events"][-1]["kind"] == "updated"  # stored, born seen
    # A fast-forward loses nothing, so it pins nothing.
    assert git(bob, "for-each-ref", "refs/fused/backup/").strip() == ""


def test_dirty_overlap_is_committed_never_waits_forever(env):
    """Uncommitted edits the update would overwrite become "Your edits" and
    the update comes in on top: an overlap is the page's decision, a clean
    one lands — never a silent forever-defer."""
    _all_on(env)
    bob = _bob(env)
    write(bob, "a.txt", "one\nUNSAVED\nthree\n")
    old = _head(bob)
    _alice_commit(env, "a.txt", "one\nALICE\nthree\n", "alice edits")

    result = _sync(bob)

    assert result["status"] == "skipped" and result["why"] == "needs-merge"
    assert git(bob, "log", "-1", "--format=%s").strip() == "Your edits"
    assert git(bob, "rev-parse", "HEAD~1").strip() == old
    assert _read(bob, "a.txt") == "one\nUNSAVED\nthree\n"
    assert git(bob, "status", "--porcelain").strip() == ""
    assert git_upstream.sync_failures() == []
    assert engine.status(bob)["state"] == "needs-merge"


def test_dirty_edit_where_author_changes_other_lines_lands(env):
    _all_on(env)
    bob = _bob(env)
    write(bob, "a.txt", "one\ntwo\nthree\nUNSAVED\n")
    tip = _alice_commit(env, "a.txt", "ALICE\ntwo\nthree\n", "alice edits line one")
    assert _sync(bob)["status"] == "synced"
    assert _read(bob, "a.txt") == "ALICE\ntwo\nthree\nUNSAVED\n"
    git(bob, "merge-base", "--is-ancestor", tip, "HEAD")  # raises if not
    assert engine.status(bob)["changes"] == 1


def test_dirty_edit_elsewhere_is_carried(env):
    _all_on(env)
    bob = _bob(env)
    _commit(bob, "b.txt", "bob\n", "bob change")
    write(bob, "index.html", "<p>unsaved</p>\n")
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    assert _sync(bob)["status"] == "synced"
    assert _read(bob, "c.txt") == "alice\n"
    assert _read(bob, "index.html") == "<p>unsaved</p>\n"


def test_follows_the_record_not_origin(env):
    """App code re-pointing `origin` at another repo changes nothing: the
    follow fetch goes by the record's URL."""
    _all_on(env)
    bob = _bob(env)
    other = str(env.tmp / "evil.git")
    git(str(env.tmp), "init", "-q", "--bare", "-b", "main", other)
    git(bob, "remote", "set-url", "origin", other)
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")
    assert _sync(bob)["status"] == "synced"
    assert _head(bob) == tip


def test_discard_backs_up_commits_and_edits(env):
    _all_on(env)
    bob = _bob(env)
    mine = _commit(bob, "b.txt", "bob\n", "bob change")
    write(bob, "a.txt", "one\nUNSAVED\nthree\n")
    write(bob, "notes/new.txt", "made by hand\n")       # untracked, not ignored
    write(bob, "__pycache__/x.pyc", "generated\n")       # never "your edits"
    write(bob, ".fused/data/db.json", "{}\n")            # machine state
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")
    assert engine.status(bob)["dirty"] is True

    out = engine.discard(bob, withdraw=False)

    assert out["ok"] is True
    assert "git" not in out["message"].lower()
    assert _head(bob) == tip
    assert not os.path.exists(os.path.join(bob, "b.txt"))
    assert _read(bob, "a.txt") == "one\ntwo\nthree\n"
    assert not os.path.exists(os.path.join(bob, "notes"))
    assert _read(bob, "__pycache__/x.pyc") == "generated\n"
    assert _read(bob, ".fused/data/db.json") == "{}\n"
    assert git(bob, "stash", "list") == ""
    refs = dict(line.split(" ", 1)[::-1] for line in git(
        bob, "for-each-ref", "--format=%(objectname) %(refname)", "refs/fused/backup/").splitlines())
    head_ref = next(r for r, sha in refs.items() if sha == mine)
    wip = refs[head_ref + "-wip"]
    assert git(bob, "show", f"{wip}:a.txt") == "one\nUNSAVED\nthree\n"
    assert git(bob, "show", f"{wip}:notes/new.txt") == "made by hand\n"
    assert git(bob, "rev-parse", f"{wip}^").strip() == mine
    files = git(bob, "ls-tree", "-r", "--name-only", wip).split()
    assert "__pycache__/x.pyc" not in files and ".fused/data/db.json" not in files
    assert store.get(bob)["events"][-1]["kind"] == "discarded"
    assert engine.status(bob)["state"] == "following"
    assert engine.status(bob)["dirty"] is False


def test_untracked_file_alone_is_dirty_and_backup_leaves_tree(env):
    bob = _bob(env)
    assert replay.dirty(bob) is False
    write(bob, "extra.txt", "x\n")
    assert replay.edits(bob) == [("??", "extra.txt")]
    head = _head(bob)
    wip = replay.wip_commit_locked(bob, head)
    assert wip and git(bob, "show", f"{wip}:extra.txt") == "x\n"
    # The real index and tree are untouched: still untracked, nothing staged.
    assert git(bob, "status", "--porcelain").strip() == "?? extra.txt"
    assert not [n for n in os.listdir(git_upstream._real_gitdir(bob))
                if n.startswith("fused-backup-index")]


def test_two_copies_of_one_repo_keep_their_own_state(env):
    _all_on(env)
    one = _bob(env, "sine")
    two = _bob(env, "sine-2")
    _commit(one, "b.txt", "bob\n", "bob change")
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    _sync(one)
    _sync(two)
    assert engine.status(one)["changes"] == 1
    assert engine.status(two)["changes"] == 0
    assert store.get(two)["events"][-1]["kind"] == "updated"
    assert len(store.find_by_repo(REPO)) == 2


# ------------------------------------------------------ committed .fused/


def test_committed_fused_dir_stays_clean_and_still_updates(env):
    """A repo that commits `.fused/`: after the real install steps the tree
    is clean, and an author update that CHANGES the committed file still
    lands — with Bob's machine copy of it kept."""
    from fused_render import app_fused_dir, app_git, appfile

    write(env.alice, ".fused/meta.json", json.dumps({"app_dir": str(env.tmp / "gone")}))
    write(env.alice, ".fused/appfile.json", "{}\n")
    git(env.alice, "add", "-f", "-A")
    git(env.alice, "commit", "-q", "-m", "commit .fused")
    git(env.alice, "push", "-q", "origin", "main")
    _all_on(env)
    bob = os.path.join(str(env.ws), "app", "sine")
    git(str(env.tmp), "clone", "-q", env.remote, bob)
    _ident(bob)
    app_git._ensure_excludes(bob)
    app_fused_dir.ensure(bob)
    appfile.write_stamp(bob, {"name": "sine", "entry": "index.html"}, str(env.tmp / "x.fused"),
                        git={"url": URL, "commit": _head(bob)})
    classify.record_install(bob, URL)
    bob = os.path.realpath(bob)
    mine = _read(bob, ".fused/appfile.json")
    assert mine != "{}\n"
    assert git(bob, "status", "--porcelain").strip() == ""
    assert store.get(bob)["kind"] == "github"

    # The author changes the committed file AND code; Bob also has a change.
    write(env.alice, ".fused/appfile.json", '{"alice": 2}\n')
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    _commit(bob, "b.txt", "bob\n", "bob change")

    assert _sync(bob)["status"] == "synced"
    assert _read(bob, "c.txt") == "alice\n" and _read(bob, "b.txt") == "bob\n"
    assert _read(bob, ".fused/appfile.json") == mine
    assert git(bob, "status", "--porcelain").strip() == ""
    assert not os.path.exists(os.path.join(git_upstream._real_gitdir(bob), "fused-aside"))
    # And a plain fast-forward over another committed-.fused change.
    git(bob, "reset", "-q", "--keep", "HEAD~1")
    write(env.alice, ".fused/appfile.json", '{"alice": 3}\n')
    tip = _alice_commit(env, "d.txt", "d\n", "another")
    assert _sync(bob)["status"] == "synced"
    assert _head(bob) == tip
    assert _read(bob, ".fused/appfile.json") == mine


def test_protect_is_gated_on_the_flag(env):
    write(env.alice, ".fused/meta.json", "{}\n")
    git(env.alice, "add", "-f", "-A")
    git(env.alice, "commit", "-q", "-m", "commit .fused")
    git(env.alice, "push", "-q", "origin", "main")
    bob = _bob(env)
    write(bob, ".fused/meta.json", '{"bob": 1}\n')
    assert classify.protect_fused_dir(bob) == 0
    assert git(bob, "status", "--porcelain").strip() == "M .fused/meta.json"
    _flags(env, sources=True)
    assert classify.protect_fused_dir(bob) == 1
    assert git(bob, "status", "--porcelain").strip() == ""


# --------------------------------------------------- proposals and return


def _propose(bob, head):
    def fn(r):
        r["proposal"] = {"repo": REPO, "number": 7, "url": "u", "head_repo": "bob/sine",
                         "head_branch": "fused-sine", "proposed_head": head,
                         "proposed_onto": None, "title": "t", "opened_at": 0,
                         "cache": {"state": "open", "merge_commit_sha": None,
                                   "comment": None, "checked_at": 0},
                         "settled_seen": False}
    store.update(bob, fn)


def test_proposal_survives_integrate_and_return_keeps_unproposed(env):
    _all_on(env)
    bob = _bob(env)
    proposed = _commit(bob, "a.txt", "one\nBOB\nthree\n", "bob proposes")
    _propose(bob, proposed)
    _commit(bob, "u.txt", "later\n", "bob, not proposed")
    assert engine.status(bob)["unproposed"] == 1

    # The author pushes something unrelated: both commits are replayed and
    # the proposal follows its commit.
    _alice_commit(env, "c.txt", "alice\n", "unrelated")
    assert _sync(bob)["status"] == "synced"
    moved = store.get(bob)["proposal"]["proposed_head"]
    assert moved != proposed
    assert git(bob, "log", "-1", "--format=%s", moved).strip() == "bob proposes"
    assert git(bob, "rev-parse", replay.PROPOSED_REF).strip() == moved
    st = engine.status(bob)
    assert st["state"] == "proposed" and st["changes"] == 2 and st["unproposed"] == 1

    # The author squash-merges it with an edit of their own to the same line.
    squash = _alice_commit(env, "a.txt", "one\nBOB, tidied\nthree\n", "Bob's change (#7)")

    def merged(r):
        r["proposal"]["cache"] = {"state": "merged", "merge_commit_sha": squash,
                                  "comment": None, "checked_at": 1}
    store.update(bob, merged)

    assert _sync(bob)["status"] == "synced"
    assert _read(bob, "a.txt") == "one\nBOB, tidied\nthree\n"
    assert _read(bob, "u.txt") == "later\n"
    git(bob, "merge-base", "--is-ancestor", squash, "HEAD")  # raises if not
    st = engine.status(bob)
    assert st["changes"] == 1 and st["state"] == "merged"
    assert st["event"]["kind"] == "merged-back"
    assert store.get(bob)["proposal"]["returned_at"]
    store.ack(bob, "settled")
    engine._status_cache.clear()
    assert engine.status(bob)["state"] == "changes"
    _no_operation(bob)


def test_return_to_following_when_nothing_unproposed(env):
    _all_on(env)
    bob = _bob(env)
    proposed = _commit(bob, "b.txt", "bob\n", "bob proposes")
    _propose(bob, proposed)
    squash = _alice_commit(env, "b.txt", "bob\n", "Bob's change (#7)")

    def merged(r):
        r["proposal"]["cache"] = {"state": "merged", "merge_commit_sha": squash,
                                  "comment": None, "checked_at": 1}
    store.update(bob, merged)
    out = engine.return_to_following(bob)
    assert out["ok"] is True and "git" not in out["message"].lower()
    assert _head(bob) == squash
    assert engine.status(bob)["changes"] == 0


# --------------------------------------------------------------- own copies


def test_own_divergence_replays_and_pushes(env):
    """Alice's own copy with an unpushed edit while her main moved on GitHub
    (she accepted a proposal there): replayed onto the new tip and pushed,
    no sync failure."""
    _flags(env, sources=True, inbox=True)
    mine = _bob(env, "own", kind="own")
    _commit(mine, "mine.txt", "mine\n", "alice local edit")
    accepted = _alice_commit(env, "pr.txt", "pr\n", "Merge pull request #7")

    result = _sync(mine, push=True)

    assert result["status"] == "synced" and result["pushed"] is True
    assert git_upstream.sync_failures() == []
    remote_tip = git(env.remote, "rev-parse", "refs/heads/main").strip()
    assert remote_tip == _head(mine)
    git(mine, "merge-base", "--is-ancestor", accepted, "HEAD")  # raises if not
    assert _read(mine, "mine.txt") == "mine\n"
    st = engine.status(mine)
    assert st["kind"] == "own" and st["state"] == "own"


def test_own_overlap_needs_merge_without_failure(env):
    _flags(env, sources=True, inbox=True)
    mine = _bob(env, "own", kind="own")
    head = _commit(mine, "a.txt", "one\nMINE\nthree\n", "alice local")
    _alice_commit(env, "a.txt", "one\nTHEIRS\nthree\n", "accepted proposal")
    result = _sync(mine, push=True)
    assert result["status"] == "skipped" and result["why"] == "needs-merge"
    assert git_upstream.sync_failures() == []
    assert _head(mine) == head
    assert store.get(mine)["needs_merge"]["purpose"] == "own-sync"
    _no_operation(mine)


def test_standing_failure_is_cleared_for_an_owned_root(env):
    bob = _bob(env)
    _commit(bob, "b.txt", "bob\n", "bob change")
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    _sync(bob, push=True)  # flags off: today's diverged failure
    assert git_upstream.sync_failures()
    _all_on(env)
    _sync(bob)
    assert git_upstream.sync_failures() == []


# ----------------------------------------------------------------- replay


def test_replay_conflict_with_job_keeps_worktree_until_cleanup(env):
    bob = _bob(env)
    mine = _commit(bob, "a.txt", "one\nBOB\nthree\n", "bob")
    base = git(bob, "rev-parse", "HEAD~1").strip()
    tip = _alice_commit(env, "a.txt", "one\nALICE\nthree\n", "alice")
    git(bob, "fetch", "-q", "origin")
    res = replay.replay(bob, onto=tip, upstream=base, branch="main", job_id="job1")
    assert res["conflict"] is True and res["files"] == ["a.txt"]
    wt = res["worktree"]
    assert wt and os.path.isdir(wt) and wt.startswith(replay.work_dir())
    assert "<<<<<<<" in _read(wt, "a.txt")
    assert _head(bob) == mine and git_upstream._operation_in_flight(bob) is None
    replay.cleanup(wt)
    assert not os.path.exists(wt)
    assert len(_worktrees(bob)) == 1


def test_land_refuses_when_branch_moved(env):
    bob = _bob(env)
    old = _head(bob)
    new = _commit(bob, "b.txt", "b\n", "moved")
    assert replay.land(bob, branch="main", old_tip=old, new_tip=old, why="t") is False
    assert _head(bob) == new


# ---------------------------------------------- flag off: back to today


def test_flag_off_unprotects_once(env):
    write(env.alice, ".fused/meta.json", "{}\n")
    git(env.alice, "add", "-f", "-A")
    git(env.alice, "commit", "-q", "-m", "commit .fused")
    git(env.alice, "push", "-q", "origin", "main")
    bob = _bob(env)
    write(bob, ".fused/meta.json", '{"bob": 1}\n')
    _all_on(env)
    assert classify.protect_fused_dir(bob) == 1
    assert store.get(bob)["protected"] is True
    assert git(bob, "status", "--porcelain").strip() == ""

    _flags(env)  # every flag off
    _sync(bob)
    assert git(bob, "status", "--porcelain").strip() == "M .fused/meta.json"
    assert git(bob, "ls-files", "-v", "--", ".fused").split()[0] == "H"
    assert "protected" not in store.get(bob)
    assert _read(bob, ".fused/meta.json") == '{"bob": 1}\n'
    assert engine.settle_when_off(bob) is False  # once


# ------------------------------------------------------- doctor git row


def _git_row(app_dir):
    from fused_render import app_doctor

    return app_doctor.report_one(app_dir, "git")


def test_doctor_git_row_for_engine_copies(env):
    bob = _bob(env)
    _commit(bob, "b.txt", "bob\n", "bob change")
    plain = _git_row(bob)
    assert "Your changes stay" not in plain["detail"]  # flags off: today's row

    _all_on(env)
    row = _git_row(bob)
    assert row["state"] == "pass"
    assert row["detail"] == "Your changes stay on this computer until you propose them."
    assert row["behind"] == 0 and row["ahead"] == 0 and row["findings"] == []

    store.update(bob, lambda r: r.__setitem__("needs_merge", {"onto": "x"}))
    row = _git_row(bob)
    assert row["state"] == "fail" and "open the app page" in row["detail"]

    own = _bob(env, "own", kind="own")
    _flags(env, sources=True, inbox=True)
    assert "couldn't be sent" not in _git_row(own)["detail"]
    store.update(own, lambda r: r.__setitem__("sync_error", {"reason": "auth", "at": 0}))
    row = _git_row(own)
    assert row["state"] == "fail" and "couldn't be sent to GitHub" in row["detail"]
    assert row["contrib"] is True

    # Fix all never hands a contrib row to a fix session.
    from fused_render import app_doctor

    other = {"id": "readme", "label": "README", "state": "fail", "kind": "fact",
             "detail": "no README", "findings": []}
    prompt = app_doctor.doctor_prompt_all(os.path.join(own, "index.html"), [row, other])
    assert "couldn't be sent" not in prompt and "`readme`" in prompt

    # The page's copy carries a sentence, the code separately.
    st = engine.status(own)
    assert st["sync_error_code"] == "auth"
    assert "git" not in st["sync_error"].lower().replace("github", "")


def test_status_passes_the_merge_job_through(env, monkeypatch):
    from fused_render.contrib import merge_job

    bob = _bob(env)
    job = {"id": "j1", "status": "landing", "summary": "s", "files": ["a.txt"],
           "purpose": "needs-merge", "error": None, "started_at": 12.0}
    monkeypatch.setattr(merge_job, "current", lambda root: dict(job))
    assert engine.status(bob)["merge_job"] == job
    assert engine._job_open(bob) is True


# ------------------------------------------- no old Update card for copies


def test_auto_sync_off_owned_copy_gets_no_update_card(env, monkeypatch):
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: False)
    bob = _bob(env)
    mine = _commit(bob, "b.txt", "bob\n", "bob change")
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")

    # Flags off: today's card.
    git_upstream.note_app_opened(bob, _runner=lambda fn: fn())
    assert [r["root"] for r in git_upstream.known_repos()] == [bob]

    _all_on(env)
    assert git_upstream.known_repos() == []  # an old state no longer surfaces
    git_upstream._checked.clear()
    git_upstream.note_app_opened(bob, _runner=lambda fn: fn())
    assert git_upstream.known_repos() == []
    assert git_upstream.repo_state_for(bob) is None
    assert _head(bob) == mine  # auto-sync off: nothing moved
    assert git(bob, "rev-parse", replay.FOLLOW_REF).strip() == tip  # but fetched
    assert engine.status(bob)["behind"] == 1

    for fn in (git_upstream.update_repo, git_upstream.switch_repo):
        out = fn(bob)
        assert out["ok"] is False and out["reason"] == "contrib"
        assert "git" not in out["message"].lower()
    assert _head(bob) == mine

    git_upstream.force_check(bob, _runner=lambda fn: fn())
    assert git_upstream.repo_state_for(bob) is None


def test_backup_names_fall_off_but_refs_stay_until_age_out(env):
    """r2_04: the record names the newest BACKUPS_KEEP; no ref is deleted
    for falling off. Only a plain update's backup older than 30 days ages
    out — a Discard's, a race's and every -wip stay."""
    bob = _bob(env)
    head = _head(bob)
    made = []
    with replay.slot() as held:
        assert held
        for _ in range(store.BACKUPS_KEEP + 4):
            refs = replay.backup_locked(bob, head)
            replay.name_backups(bob, refs, "update", bob)
            made += refs
        kept = replay.backup_locked(bob, head, why="discard")
        race = replay.backup_locked(bob, head, why="race")
        named = {b["ref"] for b in store.get(bob)["backups"]}
        present = set(git(bob, "for-each-ref", "--format=%(refname)",
                          "refs/fused/backup/").split())
        assert len(named) == store.BACKUPS_KEEP and set(made) | set(kept) | set(race) == present
        assert replay.prune_backups_locked(bob, time.time() + 29 * 86400) == 0
        assert replay.prune_backups_locked(bob, time.time() + 31 * 86400) == len(made)
    left = set(git(bob, "for-each-ref", "--format=%(refname)", "refs/fused/backup/").split())
    assert left == set(kept) | set(race)


# ------------------------------------------------- engine review (r00–r06)


def test_own_copy_never_republishes_what_the_author_rewrote_away(env):
    """r01: a pushed commit the author removed with a force-push elsewhere is
    not replayed and pushed back; the copy follows the rewrite (backed up)."""
    _flags(env, sources=True, inbox=True)
    lap = _bob(env, "own", kind="own")
    leaked = _commit(lap, "secret.txt", "API_KEY=sk\n", "add key")
    git(lap, "push", "-q", "origin", "main")
    other = str(env.tmp / "other")
    git(str(env.tmp), "clone", "-q", env.remote, other)
    _ident(other)
    git(other, "reset", "-q", "--hard", "HEAD~1")
    fix = _commit(other, "index.html", "<p>v2</p>\n", "fix")
    git(other, "push", "-q", "--force", "origin", "main")

    result = _sync(lap, push=True)

    assert result["status"] == "synced"
    assert git(env.remote, "rev-parse", "refs/heads/main").strip() == fix
    assert git(env.remote, "show", "main:secret.txt", check=False) == ""
    assert _head(lap) == fix
    assert leaked in git(lap, "for-each-ref", "--format=%(objectname)", "refs/fused/backup/")


def test_own_copy_unknown_sync_point_is_a_plain_sentence(env):
    _flags(env, sources=True, inbox=True)
    lap = _bob(env, "own", kind="own")
    head = _commit(lap, "mine.txt", "mine\n", "local edit")
    _alice_commit(env, "pr.txt", "pr\n", "accepted")
    git(lap, "update-ref", "-d", "refs/remotes/origin/main")  # nothing to trust
    result = _sync(lap, push=True)
    assert result["status"] == "skipped" and result["why"] == "rewritten"
    assert _head(lap) == head
    assert engine.merge_plan(lap) is None
    st = engine.status(lap)
    assert st["needs_merge"] and "Your edits are safe here" in st["needs_merge_message"]


def test_commit_racing_the_land_is_put_back_and_pinned(env, monkeypatch):
    """r02: a commit that lands between the check and the reset is not
    reverted: the branch goes back to it, a backup ref pins it, the land
    defers."""
    _all_on(env)
    bob = _bob(env)
    _commit(bob, "a.txt", "one\nBOB\nthree\n", "bob edits a")
    _alice_commit(env, "index.html", "<p>v2</p>\n", "alice v2")
    real = replay._protect_after_move
    raced = []

    def racing(root):
        if not raced:
            raced.append(_commit(bob, "claude.txt", "WORK\n", "Claude: add work"))
        return real(root)

    monkeypatch.setattr(replay, "_protect_after_move", racing)
    result = _sync(bob)
    monkeypatch.setattr(replay, "_protect_after_move", real)

    assert result["status"] == "skipped" and result["why"] == "land"
    assert _head(bob) == raced[0]
    assert _read(bob, "claude.txt") == "WORK\n"
    pins = git(bob, "for-each-ref", "--format=%(objectname)", "refs/fused/backup/").split()
    assert raced[0] in pins
    # The next trigger brings the update in on top of it.
    assert _sync(bob)["status"] == "synced"
    assert _read(bob, "claude.txt") == "WORK\n" and _read(bob, "index.html") == "<p>v2</p>\n"


def test_no_land_or_discard_while_claude_is_working(env, monkeypatch):
    _all_on(env)
    bob = _bob(env)
    mine = _commit(bob, "b.txt", "bob\n", "bob change")
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    monkeypatch.setattr(engine, "_claude_busy", lambda root: True)
    result = _sync(bob)
    assert result["status"] == "skipped" and result["why"] == "claude"
    out = engine.discard(bob, withdraw=False)
    assert out["ok"] is False and out["message"] == engine.CLAUDE_BUSY_MESSAGE
    assert _head(bob) == mine
    assert store.get(bob)["backups"] == []


def test_merge_in_range_is_brought_in_by_a_merge(env):
    """r06: a merge commit (with a hand resolution) in Bob's range is never
    flattened away by a rebase."""
    _all_on(env)
    bob = _bob(env)
    _sync(bob)
    git(bob, "checkout", "-q", "-b", "side")
    _commit(bob, "a.txt", "SIDE\ntwo\nthree\n", "side edit")
    git(bob, "checkout", "-q", "main")
    _commit(bob, "a.txt", "one\ntwo\nMAIN\n", "main edit")
    git(bob, "merge", "-q", "side", "-m", "merge side", check=False)
    write(bob, "a.txt", "SIDE\ntwo\nMAIN\nADDED IN THE MERGE\n")
    git(bob, "add", "a.txt")
    git(bob, "commit", "-q", "--amend", "--no-edit")  # a hand edit inside the merge
    git(bob, "branch", "-q", "-D", "side")
    tip = _alice_commit(env, "index.html", "<p>unrelated</p>\n", "alice unrelated")

    assert _sync(bob)["status"] == "synced"
    assert _read(bob, "a.txt") == "SIDE\ntwo\nMAIN\nADDED IN THE MERGE\n"
    assert _read(bob, "index.html") == "<p>unrelated</p>\n"
    parents = git(bob, "log", "-1", "--format=%P").split()
    assert len(parents) == 2 and tip in parents


def test_a_rebase_that_differs_from_the_merge_is_not_landed(env):
    """The yardstick itself: a replayed tip whose tree is not the three-way
    merge is reported, never landed."""
    bob = _bob(env)
    base = _head(bob)
    _commit(bob, "b.txt", "bob\n", "bob change")
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")
    git(bob, "fetch", "-q", "origin")
    head = _head(bob)
    res = {}
    wrong = git(bob, "commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", "lost bob").strip()
    assert replay._replay_unsound(bob, base, tip, head, wrong, res) is True
    assert res["mismatch"] and res["conflict"]
    good = replay.replay(bob, onto=tip, upstream=base, branch=head)
    assert good["ok"] and not replay._replay_unsound(bob, base, tip, head, good["tip"], {})


def test_return_after_proposed_head_was_rewritten_drops_it_by_patch(env):
    """r04: proposed_head out of HEAD's history (a Merge with Claude accept)
    — the return keeps only what was never proposed, with no spurious
    conflict about Bob's own merged change."""
    _all_on(env)
    bob = _bob(env)
    _sync(bob)
    _commit(bob, "a.txt", "one\nX\nthree\n", "bob c1")
    c2 = _commit(bob, "a.txt", "one\nY\nthree\n", "bob c2")
    later = _commit(bob, "u.txt", "later\n", "bob, not proposed")
    rewritten = git(bob, "commit-tree", f"{c2}^{{tree}}", "-p", f"{c2}~1", "-m", "bob c2").strip()
    git(bob, "update-ref", replay.PROPOSED_REF, rewritten)
    squash = _alice_commit(env, "a.txt", "one\nY\nthree\n", "bob's change (squashed)")
    _propose(bob, rewritten)

    def merged(r):
        r["proposal"]["cache"] = {"state": "merged", "merge_commit_sha": squash,
                                  "comment": None, "checked_at": 1}
    store.update(bob, merged)
    assert later

    assert _sync(bob)["status"] == "synced"
    assert store.get(bob)["needs_merge"] is None
    assert _read(bob, "u.txt") == "later\n" and _read(bob, "a.txt") == "one\nY\nthree\n"
    assert git(bob, "log", "--format=%s", f"{squash}..HEAD").split("\n")[0] == "bob, not proposed"
    assert engine.status(bob)["changes"] == 1


def test_merged_proposal_with_the_loop_off_defers(env):
    _flags(env, sources=True, changes=True)  # no loop
    bob = _bob(env)
    proposed = _commit(bob, "a.txt", "one\nBOB\nthree\n", "bob proposes")
    _propose(bob, proposed)
    squash = _alice_commit(env, "a.txt", "one\nBOB, tidied\nthree\n", "squashed (#7)")

    def merged(r):
        r["proposal"]["cache"] = {"state": "merged", "merge_commit_sha": squash,
                                  "comment": None, "checked_at": 1}
    store.update(bob, merged)
    result = _sync(bob)
    assert result["why"] == "merged-proposal"
    assert store.get(bob)["needs_merge"] is None and _head(bob) == proposed


def test_local_insteadof_redirect_is_refused(env):
    """r05: one line of the repo's own config must not re-point the follow."""
    _all_on(env)
    bob = _bob(env)
    head = _head(bob)
    evil = str(env.tmp / "evil.git")
    git(str(env.tmp), "clone", "-q", "--bare", env.remote, evil)
    git(bob, "config", f"url.{evil}.insteadOf", URL)
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    result = _sync(bob)
    assert result["status"] == "skipped" and result["why"] == "redirected"
    assert _head(bob) == head and replay.rev(bob, replay.FOLLOW_REF) is None
    st = engine.status(bob)
    assert st["sync_error_code"] == "redirected"
    assert st["sync_error"] == "This app's settings point GitHub somewhere else, so fused won't update it."


def test_ignored_file_the_update_would_track_is_never_overwritten(env):
    """Decision row 8 / r2 item 10: an ignored local file at a path the
    author's update newly tracks (a `.env`) — the update waits, with a plain
    sentence naming the file and no Merge with Claude; renaming it lets the
    update in."""
    _all_on(env)
    bob = _bob(env)
    with open(os.path.join(bob, ".git", "info", "exclude"), "a") as f:
        f.write(".env\n")
    write(bob, ".env", "BOB_SECRET=1\n")
    old = _head(bob)
    tip = _alice_commit(env, ".env", "EXAMPLE=1\n", "add .env template")
    result = _sync(bob)
    assert result["status"] == "skipped" and result["why"] == "clobber"
    assert _head(bob) == old and _read(bob, ".env") == "BOB_SECRET=1\n"
    st = engine.status(bob)
    assert st["needs_merge"] and st["merge_button"] is False
    assert st["needs_merge_message"] == ("alice's update adds a file you already have "
                                         "here (.env). Rename yours to get the update.")
    assert engine.merge_plan(bob) is None
    os.rename(os.path.join(bob, ".env"), os.path.join(bob, ".env.mine"))
    assert _sync(bob)["status"] == "synced" and _head(bob) == tip
    assert store.get(bob)["needs_merge"] is None


def test_ignored_file_overwrite_fallback_is_backed_up(env):
    """The never-happens fallback below the decision: should `land` ever be
    asked to move over an ignored file, the file is in the backup first."""
    bob = _bob(env)
    with open(os.path.join(bob, ".git", "info", "exclude"), "a") as f:
        f.write(".env\n")
    write(bob, ".env", "BOB_SECRET=1\n")
    old = _head(bob)
    tip = _alice_commit(env, ".env", "EXAMPLE=1\n", "add .env template")
    git(bob, "fetch", "-q", "origin")
    assert replay.land(bob, branch="main", old_tip=old, new_tip=tip, why="t") is True
    wips = [r for r in git(bob, "for-each-ref", "--format=%(refname)",
                           "refs/fused/backup/").split() if r.endswith("-wip")]
    assert wips and git(bob, "show", f"{wips[0]}:.env") == "BOB_SECRET=1\n"
    assert git(bob, "rev-parse", f"{wips[0]}^").strip() == old


def test_author_force_push_under_a_copy_without_changes_is_an_update(env):
    _all_on(env)
    bob = _bob(env)
    _alice_commit(env, "c.txt", "c\n", "to be dropped")
    _sync(bob)
    git(env.alice, "reset", "-q", "--hard", "HEAD~1")
    write(env.alice, "d.txt", "d\n")
    git(env.alice, "add", "-A")
    git(env.alice, "commit", "-q", "-m", "rewritten")
    git(env.alice, "push", "-q", "--force", "origin", "main")
    tip = _head(env.alice)
    assert _sync(bob)["status"] == "synced"
    assert _head(bob) == tip
    assert store.get(bob)["events"][-1]["kind"] == "updated"
    assert engine.status(bob)["changes"] == 0


def test_bring_in_latest_with_auto_sync_off(env, monkeypatch):
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: False)
    _all_on(env)
    bob = _bob(env)
    _commit(bob, "b.txt", "bob\n", "bob change")
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")
    _sync(bob)
    st = engine.status(bob)
    assert st["auto_sync"] is False and st["behind"] == 1 and st["owner_checked"] is True
    out = engine.bring_in_latest(bob)
    assert out["ok"] is True and out["message"] == "Updated to alice's latest, your changes kept."
    git(bob, "merge-base", "--is-ancestor", tip, "HEAD")  # raises if not
    assert engine.status(bob)["behind"] == 0
    assert engine.bring_in_latest(bob)["message"] == "You already have alice's latest."


def test_the_slot_is_free_during_fetch_and_replay(env, monkeypatch):
    """MED 7: minutes of rebase never hold the process-wide slot — only the
    land does — and the caller's slot is held again when sync returns."""
    _all_on(env)
    bob = _bob(env)
    _commit(bob, "b.txt", "bob\n", "bob change")
    _alice_commit(env, "c.txt", "alice\n", "alice change")
    seen = {}
    real_replay, real_land = replay.replay, replay.land_locked

    def spy_replay(*a, **k):
        seen["replay"] = git_upstream._check_slot.locked()
        return real_replay(*a, **k)

    def spy_land(*a, **k):
        seen["land"] = git_upstream._check_slot.locked()
        return real_land(*a, **k)

    monkeypatch.setattr(replay, "replay", spy_replay)
    monkeypatch.setattr(replay, "land_locked", spy_land)
    with git_upstream._mutation_slot() as held:
        assert held
        result = git_upstream._sync_locked(bob, action="t", push=False)
        assert git_upstream._check_slot.locked()
    assert result["status"] == "synced"
    assert seen == {"replay": False, "land": True}


def test_slot_reacquire_is_bounded_and_never_frees_anothers_lock(env, monkeypatch):
    """When another thread takes the slot while sync had given it up, the
    bounded re-acquire times out, the sync reports busy, and the caller's
    `finally` does NOT release the other thread's lock."""
    import threading

    _all_on(env)
    bob = _bob(env)
    head = _head(bob)
    _alice_commit(env, "a.txt", "one\nALICE\nthree\n", "alice")
    write(bob, "a.txt", "one\ntwo\nthree\n")  # nothing dirty, just a follower
    monkeypatch.setattr(replay, "_reacquire_timeout", lambda: 0.2)
    monkeypatch.setattr(engine, "_claude_busy", lambda root: True)  # defer, no land
    taken, release = threading.Event(), threading.Event()
    real = engine._facts

    def thief():
        git_upstream._check_slot.acquire()
        taken.set()
        release.wait(10)
        git_upstream._check_slot.release()

    def take_then_steal(*a, **k):
        threading.Thread(target=thief, daemon=True).start()
        taken.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(engine, "_facts", take_then_steal)
    result = git_upstream.sync_repo(bob, action="t", push=False)
    try:
        assert result["status"] == "skipped" and result["why"] == "busy"
        assert git_upstream._check_slot.locked()  # still the thief's
        assert _head(bob) == head
    finally:
        release.set()
    deadline = time.time() + 5
    while git_upstream._check_slot.locked() and time.time() < deadline:
        time.sleep(0.02)
    assert not git_upstream._check_slot.locked()
    assert getattr(git_upstream._slot_lost, "lost", False) is False


def test_merge_tree_names_non_ascii_conflicts_plainly(env):
    """`merge_tree` parses `-z` output: a conflicted `café.html` comes back
    as that name, never git's C-quoted `"caf\\303\\251.html"`."""
    bob = _bob(env)
    base = _head(bob)
    write(env.alice, "café.html", "one\n")
    git(env.alice, "add", "-A")
    git(env.alice, "commit", "-q", "-m", "add")
    git(env.alice, "push", "-q", "origin", "main")
    git(bob, "pull", "-q", "--ff-only", "origin", "main")
    base = _head(bob)
    mine = _commit(bob, "café.html", "BOB\n", "bob")
    tip = _alice_commit(env, "café.html", "ALICE\n", "alice")
    git(bob, "fetch", "-q", "origin")
    mt = replay.merge_tree(bob, tip, mine, base=base)
    assert mt["supported"] and not mt["clean"]
    assert mt["files"] == ["café.html"]
    clean = replay.merge_tree(bob, tip, tip, base=base)
    assert clean["clean"] and clean["files"] == [] and clean["tree"] == replay.tree_of(bob, tip)


def test_owns_error_falls_back_to_the_plain_sync(env, monkeypatch):
    bob = _bob(env)
    tip = _alice_commit(env, "c.txt", "alice\n", "alice change")

    def boom(root):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(engine, "owns", boom)
    result = _sync(bob)
    assert result["status"] == "synced" and _head(bob) == tip


# --------------------------------------------- the decision table, row by row

_F = dict(kind="github", branch="main", head="H", tip="T", base="B", tip_in_head=False,
          head_in_tip=False, rewritten=False, claude_busy=False, job_open=False,
          merged_proposal=False, loop=True, standing=None, blocking_edits=(), clobber=(),
          push=False, who="alice")


def _d(**kw):
    return engine.decide(engine.Facts(**{**_F, **kw}))


@pytest.mark.parametrize("facts, kind, why", [
    # GitHub-link copy, rows 1-11 of `decide`'s table
    (dict(claude_busy=True, job_open=True), "wait", "claude"),
    (dict(job_open=True, merged_proposal=True), "skip", "merge-job"),
    (dict(merged_proposal=True, standing="return"), "standing", "needs-merge"),
    (dict(merged_proposal=True), "return", None),
    (dict(merged_proposal=True, loop=False), "wait", "merged-proposal"),
    (dict(tip_in_head=True, standing="needs-merge"), "noop", None),
    (dict(standing="needs-merge", clobber=(".env",)), "standing", "needs-merge"),
    (dict(clobber=(".env",), blocking_edits=("a",)), "blocked", "clobber"),
    (dict(blocking_edits=("a",), head_in_tip=True), "commit-edits", None),
    (dict(head_in_tip=True), "move", "update"),
    (dict(base="H"), "move", "update"),
    (dict(), "integrate", "update-kept"),
    # own copy, rows 1-9
    (dict(kind="own", claude_busy=True), "wait", "claude"),
    (dict(kind="own", job_open=True), "skip", "merge-job"),
    (dict(kind="own", tip_in_head=True, push=True), "noop", None),
    (dict(kind="own", standing="own-sync"), "standing", "needs-merge"),
    (dict(kind="own", clobber=(".env",)), "blocked", "clobber"),
    (dict(kind="own", head_in_tip=True, base=None), "move", "own-sync"),
    (dict(kind="own", base=None), "rewritten", "rewritten"),
    (dict(kind="own", base="H", rewritten=True), "move", "own-sync"),
    (dict(kind="own", blocking_edits=("a",)), "integrate", "own-sync"),  # never committed for
])
def test_decide_table(facts, kind, why):
    a = _d(**facts)
    assert a.kind == kind
    if why is not None:
        assert a.why == why


def test_decide_events():
    assert _d(head_in_tip=True).event == "updated"
    assert _d().event == "updated-kept"
    assert _d(merged_proposal=True).event == "merged-back"
    assert _d(kind="own").event is None


# ------------------------------------------------------- round-2 repros


def test_r2_01_plain_fetch_never_makes_an_own_copy_rewritten(env):
    """r2_01: an own copy with an unpushed commit, origin fast-forwarded,
    and a NON-engine fetch first (Doctor, terminal): the published point
    comes from the tracking ref's reflog, so it integrates normally."""
    _flags(env, sources=True, inbox=True)
    lap = _bob(env, "own", kind="own")
    _commit(lap, "u.txt", "unpushed\n", "local work")
    pr = _alice_commit(env, "m.txt", "merged pr\n", "Merge PR #1")
    git(lap, "fetch", "-q", "--", "origin", "main")
    result = _sync(lap, push=True)
    assert result["status"] == "synced" and result["pushed"] is True
    git(lap, "merge-base", "--is-ancestor", pr, "HEAD")  # raises if not
    assert git(env.remote, "rev-parse", "refs/heads/main").strip() == _head(lap)
    assert store.get(lap)["needs_merge"] is None


def test_r2_07_busy_open_never_runs_a_plain_fetch_on_an_owned_root(env):
    """r2_07: an app open that finds the copy busy integrating does not fall
    back to `check_repo` (a plain fetch moving the tracking ref)."""
    _flags(env, sources=True, inbox=True)
    lap = _bob(env, "own", kind="own")
    _commit(lap, "u.txt", "unpushed\n", "local work")
    _alice_commit(env, "m.txt", "merged pr\n", "Merge PR #1")
    before = replay.rev(lap, "refs/remotes/origin/main")
    lock = engine._copy_lock(lap)
    lock.acquire()
    try:
        assert git_upstream._check_slot.acquire(blocking=False)
        git_upstream._background_check(lap, runner=lambda fn: fn())  # releases the slot
    finally:
        lock.release()
    assert replay.rev(lap, "refs/remotes/origin/main") == before


def test_r2_03_merge_in_range_does_not_ratchet_changes(env):
    """r2_03: a merge commit with no hand resolution in Bob's range — the
    replay is sound, so later author pushes add no merge commits and
    `changes` stays put."""
    _all_on(env)
    bob = _bob(env)
    _sync(bob)
    git(bob, "checkout", "-q", "-b", "side")
    _commit(bob, "a.txt", "SIDE\ntwo\nthree\n", "side edit")
    git(bob, "checkout", "-q", "main")
    _commit(bob, "a.txt", "one\ntwo\nMAIN\n", "main edit")
    git(bob, "merge", "-q", "side", "-m", "merge side", check=False)
    git(bob, "branch", "-q", "-D", "side")
    before = engine.status(bob)["changes"]
    assert before == 2
    for i in range(3):
        _alice_commit(env, f"f{i}.txt", f"{i}\n", f"alice unrelated {i}")
        assert _sync(bob)["status"] == "synced"
        engine._status_cache.clear()
        assert engine.status(bob)["changes"] == before
    assert _read(bob, "a.txt") == "SIDE\ntwo\nMAIN\n"
    assert git(bob, "rev-list", "--merges", "--count", "HEAD").strip() == "0"


def test_r2_05_your_edits_never_takes_a_file_only_bob_touched(env):
    """r2_05: an uncommitted edit to Bob's OWN file, the author changing a
    different one — nothing is committed for Bob; the edit is carried."""
    _all_on(env)
    bob = _bob(env)
    _sync(bob)
    _commit(bob, "mine.txt", "v1\n", "bob adds mine.txt")
    write(bob, "mine.txt", "v2 half-typed\n")
    _alice_commit(env, "index.html", "<p>alice</p>\n", "alice edits index only")
    assert _sync(bob)["status"] == "synced"
    assert git(bob, "log", "-1", "--format=%s").strip() == "bob adds mine.txt"
    assert _read(bob, "mine.txt") == "v2 half-typed\n" and replay.dirty(bob)


def test_r2_08_return_with_nothing_of_bobs_adds_no_merge_commit(env):
    """r2_08: a merged proposal whose head is gone (an adopted record on a
    fresh clone) on a copy already at the merged tip: no extra merge."""
    _all_on(env)
    merged = _alice_commit(env, "pr.txt", "bob's change, squashed\n", "Bob's proposal (#7)")
    bob = _bob(env)

    def fn(r):
        r["proposal"] = {"repo": REPO, "number": 7, "proposed_head": "0" * 40,
                         "cache": {"state": "merged", "merge_commit_sha": merged},
                         "settled_seen": False}
    store.update(bob, fn)
    assert _sync(bob)["status"] == "synced"
    assert _head(bob) == merged
    assert engine.status(bob)["changes"] == 0


def test_job_open_precedes_the_merged_proposal_return(env, monkeypatch):
    _all_on(env)
    bob = _bob(env)
    proposed = _commit(bob, "b.txt", "bob\n", "bob proposes")
    _propose(bob, proposed)
    squash = _alice_commit(env, "b.txt", "bob\n", "squashed (#7)")
    store.update(bob, lambda r: r["proposal"].__setitem__(
        "cache", {"state": "merged", "merge_commit_sha": squash}))
    monkeypatch.setattr(engine, "_job_open", lambda root: True)
    assert _sync(bob)["why"] == "merge-job"
    assert _head(bob) == proposed
