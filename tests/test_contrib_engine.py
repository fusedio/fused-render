"""contrib/engine.py + replay.py + follow.py — the v3 git model
(docs/CONTRIB_LOOP.md §17) against REAL git: Alice's repo on the FakeGitHub
(real bare repos), Bob's GitHub-link copy cloned from it on its own branch
`fused/mine` (`_contrib_gh_world`). Every move of §17.3 (ff, bring in,
settle, discard/match, keep working, move aside, go back, restore,
push_to_main), the derived states of §17.2, the fetch row's mirror and its
checked-out rule, the v2 → v3 migration, and the audit repros that still
apply. Every step that moves something asserts I3 and I4 (`inv`).

proposals.py is another workstream's: a stand-in module answers `refresh`
from the FakeGitHub's pull request (GitHub is the truth) and records
`settle_direct`/`withdraw` calls. Flags are prefs.json (`write_prefs`),
never a monkeypatch.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _contrib_gh_world import (  # noqa: E402
    APP_PY, PAGE, assert_home_invariants, claude_commit, edit, git, make_world, read,
    write_prefs)

from fused_render import git_upstream  # noqa: E402
from fused_render.contrib import classify, engine, follow, replay, store  # noqa: E402

REPO = "alice/sine"
HOME = classify.HOME_BRANCH
APP_LOUD = APP_PY.replace("return 1", "return 9")


# ---------------------------------------------------------------- the world


class _Proposals(types.ModuleType):
    """proposals.py's surface the engine calls, GitHub-true over the fake."""

    def __init__(self, w):
        super().__init__("fused_render.contrib.proposals")
        self.w = w
        self.offline = False
        self.refreshes = 0
        self.settled = []
        self.withdrawn = []
        self.merge_accepted_calls = []

    def refresh(self, app_dir, *, force=False, tip=None):
        self.refreshes += 1
        p = (store.get(app_dir) or {}).get("proposal")
        if not isinstance(p, dict) or self.offline:
            return None
        pr = self.w.gh.get_pr(p["repo"], p["number"])

        def fn(r):
            q = r["proposal"]
            q["cache"] = {"state": "merged" if pr["merged"] else pr["state"],
                          "merge_commit_sha": pr["merge_commit_sha"] if pr["merged"] else None,
                          "head_sha": pr.get("head_sha")}
        store.update(app_dir, fn)
        return {}

    def settle_direct(self, app_dir, sha):
        self.settled.append(sha)

    def withdraw(self, app_dir):
        self.withdrawn.append(app_dir)
        return {"ok": True}

    def merge_accepted(self, app_dir):
        self.merge_accepted_calls.append(app_dir)


@pytest.fixture
def w(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    for name, value in (("_checked", {}), ("_state", {}), ("_sync_failures", {}),
                        ("_pulled_events", []), ("_pending", {})):
        monkeypatch.setattr(git_upstream, name, value)
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(classify, "ensure_in_background", lambda app_dir: False)
    monkeypatch.setattr(engine, "_claude_busy", lambda root: False)
    engine._status_cache.clear()
    world = make_world(tmp_path, monkeypatch)
    fake = _Proposals(world)
    import fused_render.contrib as pkg
    monkeypatch.setitem(sys.modules, "fused_render.contrib.proposals", fake)
    monkeypatch.setattr(pkg, "proposals", fake, raising=False)
    world.proposals = fake
    world.g = lambda *a, check=True: git(world.app, *a, check=check)
    world.sync = lambda push=False: git_upstream.sync_repo(world.app, action="test", push=push)
    world.head = lambda: world.g("rev-parse", "HEAD")
    world.alice = lambda: world.fake.tip(REPO, "main")
    return world


def inv(w):
    """I3 + I4 (§17.1) after a move."""
    assert_home_invariants(w.app)


def _off():
    write_prefs(contrib_enabled=False)


def _alice(w, files, msg):
    return w.fake.commit(REPO, "main", files, msg, author="alice")


def _rewind(w, sha):
    """Alice force-pushes her main back to `sha` (a rewind on GitHub)."""
    git(w.fake.bare_path(REPO), "update-ref", "refs/heads/main", sha)


def _backups(w):
    return w.g("for-each-ref", "--format=%(refname)", replay.BACKUP_PREFIX).split()


def _activity(w):
    return [e for e in (store.get(w.app) or {}).get("activity") or []]


def _texts(w):
    return [e["text"] for e in _activity(w)]


def _anc(w, a, b):
    return replay.is_ancestor(w.app, a, b)


def _remote_untouched(w, alice_tip):
    """P2: nothing of Bob's reached GitHub — Alice's main as she left it, no
    copy of the repo under Bob's name."""
    assert w.alice() == alice_tip
    assert not os.path.exists(w.fake.bare_path("bob/sine"))


def _propose(w, title="Red wave"):
    """What Send leaves behind (WS-2's), done directly: Bob's HEAD on a
    branch of his fork, a pull request, the record's v3 proposal."""
    gh = w.gh
    head = w.head()
    fork = gh.ensure_fork(REPO)
    branch = "fused-sine-20261023-1200-ab12"
    gh.push(w.app, head, fork, branch)
    pr = gh.open_pr(REPO, title=title, body="b", head=f"{fork.split('/')[0]}:{branch}",
                    base="main")

    def fn(r):
        r["proposal"] = {"repo": REPO, "number": pr["number"], "url": pr.get("url"),
                         "head_repo": fork, "head_branch": branch, "title": title,
                         "sent_at": 1.0, "settled_seen": False, "landed_at": None,
                         "cache": {"state": "open", "head_sha": head}}
    store.update(w.app, fn)
    return pr["number"]


def _merged_lines(w):
    return [e for e in _activity(w) if e["kind"] == "merged"]


# ------------------------------------------------------------- ff / bring in


def test_ff_on_following_says_what_came_in(w):
    inv(w)
    a = _alice(w, {"NOTES.md": "n\n"}, "Add notes")
    res = w.sync(push=True)
    assert res["status"] == "synced"
    assert w.head() == a
    inv(w)
    assert _texts(w)[-1] == "Got alice's latest: “Add notes”."
    assert store.activity(w.app)[0]["undo"]             # every automatic move has Undo
    _remote_untouched(w, a)
    assert (store.get(w.app) or {}).get("last_checked")
    assert store.get(w.app)["last_head"] == a           # adoption after a Finder move
    st = engine.status(w.app)
    assert st["state"] == "following" and st["ahead"] == 0 and st["behind"] == 0
    # The sync's check_repo-shaped state (§17.6).
    assert res["state"]["on_default"] is True and res["state"]["branch"] == HOME
    assert res["state"]["default_branch"] == "main"


@pytest.mark.parametrize("bob_has_changes", [False, True])
def test_undo_of_an_automatic_move_holds(w, bob_has_changes):
    """Acceptance step 2: Undo of an automatic ff / bring-in holds — opening
    the app does not bring the same tip straight back (`kept-mine`); the
    Bring in button does, and clears the hold. fused's own "Claude: "
    commit prefix never reaches the line."""
    before = w.head()
    if bob_has_changes:
        before = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    tip = _alice(w, {"NOTES.md": "n\n"}, "Claude: Add notes")
    assert w.sync()["status"] == "synced" and _anc(w, tip, w.head())
    line = store.activity(w.app)[0]
    assert line["undo"] and "Claude:" not in line["text"] and "“Add notes”" in line["text"]
    assert engine.restore(w.app, line["id"])["ok"]
    inv(w)
    assert w.head() == before and store.get(w.app)["skip_tip"] == tip
    git_upstream.note_app_opened(w.app, _runner=lambda fn: fn())
    assert w.head() == before
    assert w.sync()["why"] == "kept-mine" and w.head() == before
    st = engine.status(w.app)
    assert st["kept_mine"] and st["behind"] == 1
    assert engine.bring_in(w.app)["ok"] and _anc(w, tip, w.head())
    inv(w)
    assert store.get(w.app)["skip_tip"] is None and not engine.status(w.app)["kept_mine"]


def test_a_newer_tip_ends_the_undo_hold(w):
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.sync()
    assert engine.restore(w.app, store.activity(w.app)[0]["id"])["ok"]
    assert w.sync()["why"] == "kept-mine"
    newer = _alice(w, {"MORE.md": "m\n"}, "more")
    assert w.sync()["status"] == "synced" and w.head() == newer
    inv(w)
    assert store.get(w.app)["skip_tip"] is None and _anc(w, tip, w.head())


def test_undo_of_a_settle_holds_too(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    w.fake.merge(REPO, n, "squash")
    w.sync()
    line = _merged_lines(w)[0]
    held = w.alice()
    assert engine.restore(w.app, line["id"])["ok"]
    undone = w.head()
    assert undone != held and store.get(w.app)["skip_tip"] == held
    assert w.sync()["why"] == "kept-mine" and w.head() == undone   # no settle again
    inv(w)
    assert engine.status(w.app)["kept_mine"] and len(_merged_lines(w)) == 1


def test_bring_in_merge_on_changes_keeps_bobs_commit_ids_r14(w):
    """R14: three unrelated author pushes; Bob's turn commits keep their
    ids, one bring-in merge each (first parent Bob's), one line each, and
    the merges never count as his (`ahead` is --no-merges)."""
    t1 = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "turn 1")
    t2 = claude_commit(w.app, {"app.py": APP_LOUD}, "turn 2")
    for i in range(3):
        before = w.head()
        tip = _alice(w, {f"N{i}.md": "n\n"}, f"notes {i}")
        res = w.sync(push=True)
        assert res["status"] == "synced"
        inv(w)
        head = w.head()
        assert _anc(w, t1, head) and _anc(w, t2, head) and _anc(w, tip, head)
        assert w.g("rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [before, tip]
        assert w.g("log", "-1", "--format=%s") == "Bring in alice's latest"
        _remote_untouched(w, tip)
    assert len(_backups(w)) == 3 and all(e["backup"] for e in _activity(w))
    assert _texts(w)[-1] == "Brought in alice's “notes 2”, your changes kept."
    assert read(w.app, "index.html") == PAGE.replace("blue", "red")
    st = engine.status(w.app)
    assert st["state"] == "changes" and st["behind"] == 0 and st["ahead"] == 2
    assert st["changes_files"] == ["app.py", "index.html"]
    # The ↑N state counts the merges too, for one release (§17.6).
    assert res["state"]["ahead"] == 5 and res["state"]["on_default"]


def test_noop_keeps_bobs_commit(w):
    t = w.alice()
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    assert w.sync(push=True)["status"] == "synced"
    assert w.head() == mine
    inv(w)
    assert _activity(w) == []
    _remote_untouched(w, t)
    st = engine.status(w.app)
    assert st["state"] == "changes" and st["changes_files"] == ["index.html"] and st["ahead"] == 1


def test_your_edits_then_merge(w):
    edit(w.app, "app.py", APP_LOUD)              # uncommitted, line 2
    edit(w.app, "index.html", PAGE + "<!-- mine -->\n")  # uncommitted, Alice never touches it
    tip = _alice(w, {"app.py": APP_PY + "\n# alice\n"}, "alice app.py")
    assert w.sync()["status"] == "synced"
    inv(w)
    assert _anc(w, tip, w.head())
    assert git(w.app, "log", "-1", "--format=%s", "HEAD^1") == "Your edits"
    assert read(w.app, "app.py") == APP_LOUD + "\n# alice\n"
    assert git(w.app, "status", "--porcelain").splitlines() == ["M index.html"]
    assert "Saved your edit to app.py so alice's update could come in." in _texts(w)


def test_untracked_file_is_never_committed(w):
    edit(w.app, "creds.json", "secret\n")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["status"] == "synced"
    inv(w)
    assert read(w.app, "creds.json") == "secret\n"
    assert "creds.json" not in w.g("ls-files")


def test_auto_sync_off_fetches_only_and_bring_in_works(w, monkeypatch):
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: False)
    head = w.head()
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "auto-sync-off"
    inv(w)                                         # the mirror moved; the tree did not
    assert w.head() == head and engine.status(w.app)["behind"] == 1
    assert engine.bring_in(w.app)["message"] == "You're on alice's latest."
    assert w.head() == tip
    inv(w)


# ------------------------------------------------------------- stuck states


def test_conflict_is_needs_merge_derived_and_match_resolves_it(w):
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    res = w.sync()
    assert res["status"] == "skipped" and res["why"] == "needs-merge"
    inv(w)
    assert w.head() == mine and "red" in read(w.app, "index.html")
    assert "needs_merge" not in store.get_raw(store.key(w.app))   # nothing recorded
    st = engine.status(w.app)
    assert st["state"] == "needs-merge" and st["needs_merge"]
    assert st["needs_merge_files"] == ["index.html"] and st["needs_merge_purpose"] == "update"
    assert engine.merge_plan(w.app) == {"purpose": "update", "ours": mine, "theirs": tip,
                                        "onto": tip, "base": None, "files": ["index.html"]}
    assert engine.conflict(w.app) == {"files": ["index.html"], "base": None}
    assert w.sync()["why"] == "needs-merge" and w.head() == mine
    out = engine.match(w.app)
    assert out["ok"] and w.head() == tip
    inv(w)
    line = _activity(w)[-1]
    assert line["text"] == "Switched to alice's version. Your version is saved."
    assert replay.rev(w.app, line["backup"]) == mine
    assert engine.status(w.app)["state"] == "following"
    _remote_untouched(w, tip)


def test_blocked_then_move_aside_and_restore(w):
    edit(w.app, ".gitignore", "data.csv\n")
    git(w.app, "add", ".gitignore")
    git(w.app, "-c", "user.name=B", "-c", "user.email=b@x", "commit", "-qm", "ignore data")
    edit(w.app, "data.csv", "bob's numbers\n")
    tip = _alice(w, {"data.csv": "alice's numbers\n"}, "ship data")
    head = w.head()
    res = w.sync()
    assert res["status"] == "skipped" and res["why"] == "clobber"
    inv(w)
    assert w.head() == head and read(w.app, "data.csv") == "bob's numbers\n"
    st = engine.status(w.app)
    assert st["state"] == "blocked" and st["blocked_files"] == ["data.csv"]
    out = engine.bring_in(w.app)
    assert out["reason"] == "blocked" and "data.csv" in out["message"]
    out = engine.move_aside(w.app)
    assert out["ok"], out
    inv(w)
    assert read(w.app, "data.csv") == "alice's numbers\n"
    assert _anc(w, tip, w.head()) and _anc(w, head, w.head())
    aside = next(e for e in _activity(w) if e["kind"] == "aside")
    assert aside["aside"]["files"] == ["data.csv"]
    assert engine.restore(w.app, aside["id"])["ok"]
    assert read(w.app, "data.csv") == "bob's numbers\n"
    _remote_untouched(w, tip)


def test_claude_busy_waits(w, monkeypatch):
    monkeypatch.setattr(engine, "_claude_busy", lambda root: True)
    head = w.head()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "claude"
    inv(w)
    assert w.head() == head
    assert engine.bring_in(w.app)["message"] == engine.CLAUDE_BUSY_MESSAGE
    assert engine.match(w.app)["reason"] == "claude"


def test_open_merge_job_skips_and_reads_merging(w, monkeypatch):
    monkeypatch.setattr(engine, "_job_open", lambda root: True)
    monkeypatch.setattr(engine, "_merge_job", lambda root: {"id": "j", "status": "ready"})
    head = w.head()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "merge-job"
    assert w.head() == head
    assert engine.status(w.app)["state"] == "merging"


# ------------------------------------------------------------- loop switched off


def _failures(w):
    return [(f["reason"], f["output"]) for f in git_upstream.sync_failures()
            if f["root"] == w.app]


def test_loop_off_still_brings_in_cleanly(w):
    """§17.7: with the switch off the engine still ff's and brings in."""
    _off()
    mine = claude_commit(w.app, {"tweak.txt": "mine\n"}, "tweak")
    for i in range(2):
        tip = _alice(w, {f"c{i}.txt": "alice\n"}, f"a{i}")
        assert w.sync(push=True)["status"] == "synced"
        inv(w)
        assert _anc(w, tip, w.head()) and _anc(w, mine, w.head())
        _remote_untouched(w, tip)
    assert _failures(w) == []


def test_loop_off_stuck_states_are_todays_failure_rows(w):
    """§17.7 (review P2 #16): with the loop off an edit in the way is still
    saved as "Your edits" on Bob's own branch and the update comes in; an
    overlap or a file in the way is git_upstream's failure row in plain
    words, tree untouched."""
    _off()
    edit(w.app, "app.py", APP_LOUD)
    tip = _alice(w, {"app.py": APP_PY + "# a\n"}, "a2")
    assert w.sync(push=True)["status"] == "synced" and _failures(w) == []
    inv(w)
    assert git(w.app, "log", "-1", "--format=%s", "HEAD^1") == "Your edits"
    assert read(w.app, "app.py") == APP_LOUD + "# a\n"
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    res = w.sync(push=True)
    assert res["status"] == "failed" and res["failure"]["reason"] == "diverged"
    assert "index.html" in _failures(w)[0][1] and w.head() == mine
    out = engine.bring_in(w.app)
    assert out["reason"] == "diverged" and "index.html" in out["message"]
    assert _anc(w, tip, w.head())
    _remote_untouched(w, w.alice())


def test_loop_off_never_overwrites_an_ignored_file(w):
    edit(w.app, ".git/info/exclude", "data.csv\n")
    edit(w.app, "data.csv", "bob's numbers\n")
    _off()
    head = w.head()
    _alice(w, {"data.csv": "alice's numbers\n"}, "ship data")
    res = w.sync()
    assert res["status"] == "failed" and res["failure"]["reason"] == "git-failed"
    assert _failures(w)[0][1] == ("alice's update adds data.csv, which you already have "
                                  "here. Rename yours to get the update.")
    assert w.head() == head and read(w.app, "data.csv") == "bob's numbers\n"


def test_github_kind_goes_to_the_engine_whatever_the_flags(w, monkeypatch):
    """P1/P2: the recorded kind decides — with the loop off a `github` copy
    is synced by `engine.sync` (never the plain, pushing body)."""
    _off()
    assert classify.kind_of(w.app) == "github"
    calls = []
    real = engine.sync
    monkeypatch.setattr(engine, "sync", lambda root, **kw: calls.append(root) or real(root, **kw))
    monkeypatch.setattr(git_upstream, "_sync_plain",
                        lambda *a, **k: pytest.fail("plain sync of a github copy"))
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    assert w.sync(push=True)["status"] == "synced"
    assert calls == [w.app]
    inv(w)
    _remote_untouched(w, tip)


# ------------------------------------------------------------- the fixes


def test_match_backs_up_everything_and_restore_undoes(w):
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    edit(w.app, "app.py", APP_LOUD)
    edit(w.app, "scratch.txt", "my notes\n")
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    w.sync()
    out = engine.match(w.app)
    assert out["ok"] and "Activity" in out["message"]
    inv(w)
    assert w.head() == tip and not os.path.exists(os.path.join(w.app, "scratch.txt"))
    assert w.g("status", "--porcelain") == ""
    line = _activity(w)[-1]
    assert line["kind"] == "match" and replay.rev(w.app, line["backup"]) == mine
    assert engine.restore(w.app, line["id"])["ok"]
    inv(w)
    assert w.head() == mine
    assert read(w.app, "app.py") == APP_LOUD
    assert read(w.app, "scratch.txt") == "my notes\n"
    assert "Brought back your version" in _texts(w)[-1]


def test_discard_withdraws_only_when_asked(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _propose(w)
    assert engine.discard(w.app, withdraw=False)["ok"]
    inv(w)
    assert w.proposals.withdrawn == []
    assert w.head() == w.alice()
    st = engine.status(w.app)
    assert st["changes_files"] == [] and st["state"] == "proposed"
    claude_commit(w.app, {"app.py": APP_LOUD}, "loud")
    assert engine.discard(w.app, withdraw=True)["ok"]
    assert w.proposals.withdrawn == [w.app]


def test_keep_working_after_a_declined_proposal(w):
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    assert engine.keep_working(w.app)["reason"] == "open"
    w.fake.close(REPO, n)
    w.proposals.refresh(w.app, force=True)
    assert engine.status(w.app)["state"] == "declined"
    out = engine.keep_working(w.app)
    assert out["ok"] and store.get(w.app)["proposal"] is None
    assert w.head() == mine                       # nothing moves
    inv(w)
    assert engine.status(w.app)["state"] == "changes"


def test_go_back_from_a_branch_claude_made_carries_its_commits(w):
    git(w.app, "checkout", "-q", "-b", "go-back")
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "not-default"
    st = engine.status(w.app)
    assert st["off_default"] and st["state"] == "other-branch" and st["behind"] == 1
    edit(w.app, "app.py", APP_LOUD)
    out = engine.go_back_main(w.app)
    assert out["reason"] == "dirty" and git_upstream._current_branch(w.app) == "go-back"
    git(w.app, "checkout", "--", "app.py")
    assert engine.go_back_main(w.app)["ok"]
    inv(w)
    assert w.head() == mine
    assert w.sync()["status"] == "synced"
    inv(w)
    assert _anc(w, mine, w.head())


def test_go_back_saves_a_diverged_version(w):
    git(w.app, "checkout", "-q", "--detach")
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    git(w.app, "checkout", "-q", HOME)
    other = claude_commit(w.app, {"app.py": APP_LOUD}, "on mine")
    git(w.app, "checkout", "-q", mine)
    out = engine.go_back_main(w.app)
    assert out["ok"] and "saved" in out["message"]
    inv(w)
    assert w.head() == other
    line = _activity(w)[-1]
    assert replay.rev(w.app, line["backup"]) == mine


def test_checked_out_main_is_never_moved_and_go_back_carries_its_commits(w):
    """§17.3 fetch row: with the author's branch checked out, only
    `origin/<b>` moves (a checked-out branch is never moved under its
    worktree); Go back puts Bob on fused/mine with the commits he made on
    main, and main becomes the mirror again."""
    git(w.app, "checkout", "-q", "main")
    before = w.g("rev-parse", "refs/heads/main")
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "not-default"
    assert w.g("rev-parse", "refs/heads/main") == before
    assert w.g("rev-parse", "refs/remotes/origin/main") == tip
    st = engine.status(w.app)
    assert st["state"] == "other-branch" and st["behind"] == 1
    on_main = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "on main")
    assert engine.go_back_main(w.app)["ok"]
    inv(w)
    assert w.head() == on_main
    assert w.sync()["status"] == "synced"
    inv(w)
    assert _anc(w, on_main, w.head()) and _anc(w, tip, w.head())


def test_author_rewind_pins_the_old_mirror(w):
    a1 = w.alice()
    a2 = _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.sync()
    assert w.head() == a2
    _rewind(w, a1)
    assert w.sync()["status"] == "synced"
    inv(w)
    assert w.g("rev-parse", "refs/heads/main") == a1
    pins = [b for b in _backups(w) if b.endswith("-rewound")]
    assert len(pins) == 1 and replay.rev(w.app, pins[0]) == a2
    line = _activity(w)[-1]
    assert line["kind"] == "rewound" and line["ref"] == pins[0] and not line["backup"]
    assert line["text"] == ("alice went back to an earlier version of their app. Your copy "
                            "and your changes are untouched.")
    assert pins[0] in store.referenced_backups(w.app)      # never pruned
    w.sync()
    assert len([e for e in _activity(w) if e["kind"] == "rewound"]) == 1


def test_the_rewound_line_has_no_undo(w):
    """Review P1 #3: Undo there would put the author's old version over
    Bob's own branch."""
    a1 = w.alice()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.sync()
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _rewind(w, a1)
    w.sync()
    line = next(e for e in store.activity(w.app) if e["kind"] == "rewound")
    assert line["undo"] is False
    assert engine.restore(w.app, line["id"])["reason"] == "nothing"
    assert _anc(w, mine, w.head())


def test_every_fetch_keeps_the_mirror_and_names_a_rewind(w):
    """Review P1 #4: a fetch outside the engine (Send/Update/remix's
    `proposals.tip_of(fetch=True)`) moves the mirror too, and a rewind it
    absorbs still gets its line, naming the pin."""
    a1 = w.alice()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.sync()
    _rewind(w, a1)
    rec, fol = engine._record(w.app)
    with replay.slot():
        follow.fetch(w.app, fol)
    inv(w)
    lines = [e for e in _activity(w) if e["kind"] == "rewound"]
    assert len(lines) == 1 and lines[0]["ref"] in store.referenced_backups(w.app)
    w.sync()
    assert len([e for e in _activity(w) if e["kind"] == "rewound"]) == 1


# ------------------------------------------------------------- merged → settle


def test_settle_after_squash_landed_at_once(w):
    """§17.2 merged row: a two-commit proposal squash-merged; the next sync
    asks GitHub first, brings the squash in (same tree) and SETTLES —
    fused/mine is the author's tip, the line says so once, with Undo."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    sent = claude_commit(w.app, {"index.html": PAGE.replace("blue", "crimson")}, "crimson")
    n = _propose(w)
    w.fake.merge(REPO, n, "squash")
    assert w.sync()["status"] == "synced"
    inv(w)
    assert w.proposals.refreshes >= 1
    assert w.head() == w.alice()
    rec = store.get(w.app)
    assert rec["proposal"]["landed_at"]
    lines = _merged_lines(w)
    assert [e["text"] for e in lines] == [
        "alice merged your change. You're on alice's latest version."]
    assert _anc(w, sent, replay.rev(w.app, lines[0]["backup"]))   # Undo has his commits
    assert not any(t.startswith("Brought in") for t in _texts(w))
    st = engine.status(w.app)
    assert st["state"] == "merged" and st["merged_in_tip"] and st["ahead"] == 0
    assert not st["taken_back"] and not st["kept_mine"]
    landed = rec["proposal"]["landed_at"]
    assert w.sync()["status"] == "synced" and w.head() == w.alice()
    assert store.get(w.app)["proposal"]["landed_at"] == landed and len(_merged_lines(w)) == 1
    assert engine.restore(w.app, lines[0]["id"])["ok"]
    inv(w)


def test_settle_after_a_merge_commit_accept_that_is_a_sibling(w):
    """A merge-commit accept is NOT a fast-forward of Bob's own bring-in
    merge: same parents, different commit (§17.2). Bring-in, then settle."""
    b1 = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    m1 = _alice(w, {"NOTES.md": "n\n"}, "M1 notes")
    w.sync()
    x = w.head()
    assert w.g("rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [b1, m1]
    m2 = w.fake.merge(REPO, n, "merge")
    parents = git(w.fake.bare_path(REPO), "rev-list", "--parents", "-n", "1", m2).split()[1:]
    assert parents == [m1, b1] and m2 != x          # same parents as X, another commit
    assert w.sync()["status"] == "synced"
    inv(w)
    assert not _anc(w, x, m2) and not _anc(w, m2, x)  # never a fast-forward
    assert w.head() == m2 == w.alice()
    lines = _merged_lines(w)
    assert len(lines) == 1 and _anc(w, x, replay.rev(w.app, lines[0]["backup"]))
    assert engine.status(w.app)["ahead"] == 0


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_merged_with_edits_since_keeps_them(w, method):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _propose(w)
    n = store.get(w.app)["proposal"]["number"]
    after = claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    edit(w.app, "NOTES.md", "draft\n")                      # untracked, carried
    w.fake.merge(REPO, n, method)
    assert w.sync()["status"] == "synced"
    inv(w)
    tip = w.alice()
    assert _anc(w, tip, w.head()) and _anc(w, after, w.head())
    assert w.g("diff", "--name-only", tip, "HEAD") == "app.py"
    assert read(w.app, "NOTES.md") == "draft\n"
    lines = _merged_lines(w)
    assert len(lines) == 1 and lines[0]["text"].endswith("Edits you made since are kept.")
    assert store.get(w.app)["proposal"]["landed_at"]
    w.sync()
    assert len(_merged_lines(w)) == 1


def test_settle_keeps_an_unsaved_edit(w):
    """Round 3b: a settle needs tree(HEAD) == tree(main) only — `reset
    --keep` keeps an unsaved edit beside, so a squash accept settles even
    while Bob has one."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    w.fake.merge(REPO, n, "squash")
    edit(w.app, "app.py", APP_LOUD)
    assert w.sync()["status"] == "synced"
    inv(w)
    assert w.head() == w.alice() and len(_merged_lines(w)) == 1
    assert read(w.app, "app.py") == APP_LOUD
    assert w.g("status", "--porcelain", "--", "app.py").endswith("M app.py")
    assert store.get(w.app)["proposal"]["landed_at"]


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_squash_accept_then_author_edits_the_proposed_lines(w, method):
    """Round 3c: after a squash/rebase accept the author edits the very
    lines Bob proposed. With git's own merge base Bob's pre-squash commit
    would conflict with that; with the proposal's head as the base his
    change is common ground — he lands on the author's version, settled."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    w.fake.merge(REPO, n, method)
    _alice(w, {"index.html": PAGE.replace("blue", "ruby")}, "ruby, not red")
    assert w.sync()["status"] == "synced"
    inv(w)
    assert w.head() == w.alice() and "ruby" in read(w.app, "index.html")
    assert len(_merged_lines(w)) == 1 and _merged_lines(w)[0]["backup"]
    st = engine.status(w.app)
    assert not st["needs_merge"] and st["ahead"] == 0


def test_squash_accept_then_author_edits_with_a_later_edit_of_bobs(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    later = claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    w.fake.merge(REPO, n, "squash")
    _alice(w, {"index.html": PAGE.replace("blue", "ruby")}, "ruby, not red")
    assert engine.status(w.app)["state"] in ("proposed", "changes", "merged")
    assert w.sync()["status"] == "synced"
    inv(w)
    tip = w.alice()
    assert w.g("rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [later, tip]
    assert "ruby" in read(w.app, "index.html") and read(w.app, "app.py") == APP_LOUD
    assert w.g("diff", "--name-only", tip, "HEAD") == "app.py"
    lines = _merged_lines(w)
    assert len(lines) == 1 and lines[0]["text"].endswith("Edits you made since are kept.")
    st = engine.status(w.app)
    assert not st["needs_merge"] and st["changes_files"] == ["app.py"]


def test_sent_commit_survives_author_pushes(w):
    """R1/R11: author pushes while Bob proposes; his proposed commit stays
    in HEAD, nothing is "unproposed" (bring-in merges never count)."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.sync()
    _propose(w)
    sent = w.head()
    for i in range(2):
        _alice(w, {f"M{i}.md": "m\n"}, f"more {i}")
        w.sync()
        inv(w)
        assert _anc(w, sent, w.head())
    st = engine.status(w.app)
    assert st["unproposed"] == 0 and st["state"] == "proposed"


# ------------------------------------------------------------- push / migration


def test_push_to_main_for_a_collaborator(w):
    w.fake.add_collaborator(REPO, "bob")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _propose(w)
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    out = engine.push_to_main(w.app)
    assert out["ok"], out
    inv(w)
    head = w.head()
    assert w.alice() == head == w.g("rev-parse", "refs/heads/main")
    assert w.proposals.settled == [head]
    assert engine.status(w.app)["changes_files"] == []
    assert _texts(w)[-1] == "Published your changes straight to alice's app."


def test_push_to_main_refused_without_rights(w):
    tip = w.alice()
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    assert engine.push_to_main(w.app)["reason"] == "forbidden"
    _remote_untouched(w, tip)


@pytest.fixture
def v2(tmp_path, monkeypatch):
    """A v2 copy: HEAD on main (Bob's work there), a v2 record."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    for name, value in (("_checked", {}), ("_state", {}), ("_sync_failures", {}),
                        ("_pulled_events", []), ("_pending", {})):
        monkeypatch.setattr(git_upstream, name, value)
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(classify, "ensure_in_background", lambda app_dir: False)
    monkeypatch.setattr(engine, "_claude_busy", lambda root: False)
    engine._status_cache.clear()
    world = make_world(tmp_path, monkeypatch, home_branch=False)
    head = git(world.app, "rev-parse", "HEAD")

    def as_v2(r):
        r["v"] = 2
        r.update(root_commit=head, needs_merge={"purpose": "update", "tip": head},
                 skip_tip=head, proposal={"repo": REPO, "number": 4, "sent": head,
                                          "checked_tip": head, "returned_at": None,
                                          "cache": {"state": "closed"}, "settled_seen": True})
    store.update(world.app, as_v2)
    world.sync = lambda: git_upstream.sync_repo(world.app, action="test", push=False)
    return world


def test_migration_from_a_v2_record_with_a_diverged_main(v2):
    w = v2
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert git_upstream._current_branch(w.app) == "main"
    assert w.sync()["status"] == "synced"
    inv(w)
    head = git(w.app, "rev-parse", "HEAD")
    assert replay.is_ancestor(w.app, mine, head) and replay.is_ancestor(w.app, tip, head)
    rec = store.get(w.app)
    assert rec["v"] == 3
    raw = store.get_raw(store.key(w.app))
    assert not {"root_commit", "needs_merge"} & set(raw)   # skip_tip: the undo hold
    assert not {"sent", "checked_tip", "returned_at"} & set(raw["proposal"])
    pins = [b for b in git(w.app, "for-each-ref", "--format=%(refname)",
                           replay.BACKUP_PREFIX).split() if "v3-move" in b]
    assert len(pins) == 1 and replay.rev(w.app, pins[0]) == mine
    texts = [e["text"] for e in rec["activity"]]
    assert texts.count(classify.MOVED_TEXT) == 1
    moved = next(e for e in rec["activity"] if e["text"] == classify.MOVED_TEXT)
    assert moved["backup"] == pins[0]                      # review P2 #20
    assert git(w.app, "config", f"branch.{HOME}.merge") == "refs/heads/main"
    w.sync()
    assert [e["text"] for e in store.get(w.app)["activity"]].count(classify.MOVED_TEXT) == 1


def test_a_refused_migration_pins_nothing_until_it_goes(v2, monkeypatch):
    """Engine review: the v3 move pinned Bob's work BEFORE making the home
    branch; while that was refused (an operation in flight) every sync wrote
    one more unreferenced pin. The pin now follows the move."""
    w = v2
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    real = classify.ensure_home_locked
    monkeypatch.setattr(classify, "ensure_home_locked", lambda *a, **k: False)
    w.sync()
    w.sync()

    def pins():
        return [b for b in git(w.app, "for-each-ref", "--format=%(refname)",
                               replay.BACKUP_PREFIX).split() if "v3-move" in b]
    assert pins() == [] and store.get(w.app)["v"] == 2
    monkeypatch.setattr(classify, "ensure_home_locked", real)
    w.sync()
    assert store.get(w.app)["v"] == 3
    assert len(pins()) == 1 and replay.rev(w.app, pins()[0]) == mine
    inv(w)


def test_migration_of_the_2026_10_23_stuck_copy(v2):
    """The case that started v3 (sine-starter, PR #13): a v2 copy whose HEAD
    is Bob's bring-in merge M1 (parents: his sent commit B1, the author's
    v4), while GitHub's main is the accept merge M (parents v4, B1) — a
    sibling of M1, not a descendant — and the proposal is merged. The first
    v3 sync migrates silently, settles onto M (M1 pinned with Undo), says
    "merged your change", and both invariants hold; the next sync is a
    noop."""
    w = v2
    b1 = claude_commit(w.app, {"app.py": APP_PY.replace("return 1", "return 3")}, "louder")
    v4 = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "v4")
    git(w.app, "fetch", "-q", "origin")
    git(w.app, "merge", "-q", "--no-edit", "-m", "Bring in alice's latest", v4)
    m1 = git(w.app, "rev-parse", "HEAD")
    assert git(w.app, "rev-list", "--parents", "-1", m1).split()[1:] == [b1, v4]
    # GitHub's merge commit: same tree, parents (v4, B1), on the author's main.
    m = git(w.app, "commit-tree", git(w.app, "rev-parse", f"{m1}^{{tree}}"), "-p", v4, "-p", b1,
            "-m", "Merge pull request #13 from bob/fused-sine")
    # The bare repo pulls it from Bob's clone (a copy never pushes: its hook).
    git(w.app, "update-ref", "refs/fused/tmp-m", m)
    git(w.fake.bare_path(REPO), "fetch", "-q", w.app, "refs/fused/tmp-m:refs/heads/main")
    git(w.app, "update-ref", "-d", "refs/fused/tmp-m")
    store.update(w.app, lambda r: r.__setitem__("proposal", {
        "repo": REPO, "number": 13, "sent": b1, "checked_tip": v4, "returned_at": None,
        "settled_seen": False, "cache": {"state": "merged", "merge_commit_sha": m,
                                         "head_sha": b1}}))
    assert git_upstream._current_branch(w.app) == "main"
    res = w.sync()
    assert res["status"] == "synced", res
    inv(w)
    assert git(w.app, "rev-parse", "HEAD") == m
    assert engine.status(w.app)["state"] == "merged"
    rec = store.get(w.app)
    assert rec["v"] == 3 and rec["proposal"]["landed_at"]
    line = next(e for e in store.activity(w.app) if "merged your change" in e["text"])
    assert line["undo"], line
    raw = next(e for e in rec["activity"] if e["id"] == line["id"])
    assert replay.rev(w.app, raw["backup"]) == m1
    lines = len(rec["activity"])
    w.sync()  # nothing more to do: no move, no second line, the stamp once
    assert git(w.app, "rev-parse", "HEAD") == m
    assert len(store.get(w.app)["activity"]) == lines
    assert store.get(w.app)["proposal"]["landed_at"] == rec["proposal"]["landed_at"]
    inv(w)


def test_migration_of_a_following_v2_copy_is_silent(v2):
    w = v2
    assert w.sync()["status"] == "synced"
    inv(w)
    assert store.get(w.app)["v"] == 3 and store.get(w.app)["activity"] == []


def test_v1_refs_go_on_the_first_v3_sync(v2):
    w = v2
    head = git(w.app, "rev-parse", "HEAD")
    for ref in replay.V1_REFS:
        git(w.app, "update-ref", ref, head)
    store.update(w.app, lambda r: r.pop("v", None))
    w.sync()
    for ref in replay.V1_REFS:
        assert replay.rev(w.app, ref) is None
    assert store.get(w.app)["v"] == 3
    inv(w)


def test_install_world_starts_on_fused_mine(w):
    inv(w)
    assert engine.status(w.app)["state"] == "following"


# ------------------------------------------------------------- odds and ends


def test_redirected_config_is_never_fetched(w):
    head = w.head()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.g("config", "url.https://evil.example/.insteadOf", "https://github.com/")
    assert w.sync()["why"] == "redirected"
    assert w.head() == head
    assert engine.status(w.app)["sync_error_code"] == "redirected"


def test_fetch_errors_reach_status_offline_stays_silent(w, monkeypatch):
    real = follow.fetch
    monkeypatch.setattr(follow, "fetch", lambda root, fol: (None, "not-found"))
    assert w.sync()["why"] == "not-found"
    st = engine.status(w.app)
    assert st["sync_error_code"] == "not-found"
    assert st["sync_error"] == "alice's app isn't on GitHub any more, or it's private now."
    assert engine.bring_in(w.app)["reason"] == "not-found"
    monkeypatch.setattr(follow, "fetch", lambda root, fol: (None, "offline"))
    assert w.sync()["status"] == "offline"
    assert engine.status(w.app)["sync_error_code"] == "not-found"   # unchanged, not news
    monkeypatch.setattr(follow, "fetch", real)
    w.sync()
    assert engine.status(w.app)["sync_error"] is None


def test_gh_fetch_reasons_map_to_codes(w, monkeypatch):
    monkeypatch.setattr(w.gh, "fetch_detail", lambda root, url, spec: (False, "signed-out"),
                        raising=False)
    fol = follow.for_record(store.get(w.app))
    assert follow.fetch(w.app, fol) == (None, "auth")


def test_explicit_identity_commits_as_that_person(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    tip = _alice(w, {"app.py": APP_LOUD}, "alice app")
    git_upstream._run(w.app, "fetch", "-q", "origin")
    m = replay.merge_into(w.app, head=w.head(), onto=tip, message="m",
                          identity={"name": "Alice A", "email": "alice@users.noreply.github.com"})
    assert git(w.app, "log", "-1", "--format=%an <%ae>|%cn", m["tip"]) == \
        "Alice A <alice@users.noreply.github.com>|Alice A"


def test_merge_landed_proposal_hands_the_send_to_proposals(w):
    engine.merge_landed(w.app, purpose="proposal", old_tip="a", new_tip="b")
    assert w.proposals.merge_accepted_calls == [w.app]
    assert _activity(w)[-1]["kind"] == "proposal"


def test_status_counts_only_bobs_files_when_behind(w, monkeypatch):
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: False)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"app.py": APP_LOUD}, "alice app")
    w.sync()
    st = engine.status(w.app)
    assert st["changes_files"] == ["index.html"] and st["behind"] == 1 and st["ahead"] == 1


def test_old_git_scratch_merge_gives_the_same_answers(w, monkeypatch):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    tip = _alice(w, {"app.py": APP_LOUD}, "alice app")
    git_upstream._run(w.app, "fetch", "-q", "origin")
    mem = replay.merge_tree(w.app, w.head(), tip)
    monkeypatch.setattr(replay, "_FORCE_SCRATCH_MERGE", True)
    scratch = replay.merge_tree(w.app, w.head(), tip)
    assert mem == scratch and mem["clean"]
    _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    git_upstream._run(w.app, "fetch", "-q", "origin")
    bad = replay.merge_tree(w.app, w.head(), w.alice())
    assert not bad["clean"] and bad["files"] == ["index.html"]
    assert w.sync()["why"] == "needs-merge"
    assert os.listdir(replay.work_dir()) == []


def test_off_default_with_the_loop_off_keeps_the_switch_card(w):
    """R5 with v3's rule (§17.6): "default" for a github copy is fused/mine;
    another version with A1 off gets a check_repo-shaped state, and Switch
    is the engine's Go back (never the author's branch)."""
    _off()
    git(w.app, "checkout", "-q", "-b", "feature")
    _alice(w, {"c.txt": "alice\n"}, "a2")
    res = w.sync()
    assert res["why"] == "not-default"
    assert (res["state"]["branch"], res["state"]["default_branch"], res["state"]["on_default"],
            res["state"]["behind"]) == ("feature", "main", False, 1)
    assert git_upstream.switch_repo(w.app)["ok"] is True
    inv(w)


def test_s3_restore_backs_up_an_ignored_file_it_overwrites(w):
    """rev-sec S3: Undo of an older line restores a version that tracks a
    path Bob now has as an IGNORED file — that file rides in the restore's
    own backup, and undoing the restore brings it back."""
    mine = claude_commit(w.app, {"settings.json": "old\n"}, "settings")
    _alice(w, {".gitignore": "settings.json\n", "NOTES.md": "n\n"}, "ignore settings")
    assert engine.match(w.app)["ok"]
    line = _activity(w)[-1]
    assert replay.rev(w.app, line["backup"]) == mine
    edit(w.app, "settings.json", "BOB-PRECIOUS-TOKEN\n")
    assert w.g("status", "--porcelain") == ""
    assert engine.restore(w.app, line["id"])["ok"]
    undo = _activity(w)[-1]
    wip = replay.rev(w.app, undo["backup"] + "-wip")
    assert wip and w.g("show", f"{wip}:settings.json") == "BOB-PRECIOUS-TOKEN"
    assert engine.restore(w.app, undo["id"])["ok"]
    assert read(w.app, "settings.json") == "BOB-PRECIOUS-TOKEN\n"


def test_s3b_go_back_backs_up_an_ignored_file_it_overwrites(w):
    git(w.app, "checkout", "-q", "-b", "side")
    git(w.app, "rm", "-q", "--cached", "app.py")
    edit(w.app, ".gitignore", "app.py\n")
    git(w.app, "add", ".gitignore")
    git(w.app, "-c", "user.name=B", "-c", "user.email=b@x", "commit", "-q", "-m", "untrack")
    git(w.app, "checkout", "-q", HOME)
    claude_commit(w.app, {"NOTES.md": "m\n"}, "on mine")
    git(w.app, "checkout", "-q", "side")
    edit(w.app, "app.py", "BOB-PRECIOUS-TOKEN\n")
    assert w.g("status", "--porcelain") == ""
    assert engine.go_back_main(w.app)["ok"]
    inv(w)
    line = _activity(w)[-1]
    wip = replay.rev(w.app, line["backup"] + "-wip")
    assert wip and w.g("show", f"{wip}:app.py") == "BOB-PRECIOUS-TOKEN"


def test_s4_repo_config_commands_never_run_from_fused_git_calls(w, tmp_path):
    """rev-sec S4: an app-written `core.fsmonitor` / `core.hooksPath` never
    runs from fused's reads, scratch checkouts or moves."""
    marker, hmark = tmp_path / "fsmon-FIRED", tmp_path / "hook-FIRED"
    script = tmp_path / "fsmon.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    script.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "post-merge", "pre-commit", "post-commit"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {hmark}\n")
        (hooks / name).chmod(0o755)
    git(w.app, "config", "--local", "core.fsmonitor", str(script))
    git(w.app, "config", "--local", "core.hooksPath", str(hooks))
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    marker.unlink(missing_ok=True)
    hmark.unlink(missing_ok=True)
    edit(w.app, "app.py", APP_LOUD)
    _alice(w, {"app.py": APP_PY + "# a\n"}, "alice app")
    engine.status(w.app)
    wt = replay.add_worktree(w.app, "HEAD", name="probe")
    replay.cleanup(wt)
    assert engine.bring_in(w.app)["ok"]          # a "Your edits" commit + a merge
    assert not marker.exists() and not hmark.exists()


def test_race_put_back_failure_gets_an_activity_line(w):
    head = w.head()
    raced = claude_commit(w.app, {"index.html": PAGE.replace("blue", "raced")}, "raced")
    git(w.app, "reset", "-q", "--hard", head)
    git(w.app, "update-ref", "ORIG_HEAD", raced)
    edit(w.app, "index.html", "unsaved\n")       # makes `reset --keep raced` refuse
    assert replay.undo_race_locked(w.app, head, []) is False
    line = _activity(w)[-1]
    assert line["kind"] == "race" and replay.rev(w.app, line["backup"]) == raced


def test_refused_match_keeps_a_ready_merge(w, monkeypatch):
    import contextlib
    dropped = []
    monkeypatch.setattr(engine, "_discard_job", lambda root: dropped.append(root))

    @contextlib.contextmanager
    def busy(**_kw):
        yield False
    monkeypatch.setattr(replay, "slot", busy)
    assert engine.match(w.app)["reason"] == "busy"
    assert dropped == []


def test_partial_unaside_keeps_restore(w, tmp_path):
    box = tmp_path / "aside"
    box.mkdir()
    (box / "a.txt").write_text("A\n")
    (box / "b.txt").write_text("B\n")
    lid = store.add_activity(w.app, "aside", "Moved aside", aside={"dir": str(box),
                                                                   "files": ["a.txt", "b.txt"]})
    edit(w.app, "b.txt", "someone else's\n")
    out = engine.restore(w.app, lid)
    assert out["ok"] and "b.txt is still set aside" in out["message"]
    assert read(w.app, "a.txt") == "A\n" and read(w.app, "b.txt") == "someone else's\n"
    assert store.activity_entry(w.app, lid)["restored_at"] is None
    os.remove(os.path.join(w.app, "b.txt"))
    assert engine.restore(w.app, lid)["ok"]
    assert read(w.app, "b.txt") == "B\n"
    assert store.activity_entry(w.app, lid)["restored_at"]


# ------------------------------------------------------------- replay primitives


def test_land_ff_locked_refuses_a_raced_commit_and_a_wrong_branch(w):
    head = w.head()
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    git_upstream._run(w.app, "fetch", "-q", "origin")
    raced = claude_commit(w.app, {"app.py": APP_LOUD}, "raced")
    assert replay.land_ff_locked(w.app, tip) == ("moved", None)
    assert w.head() == raced
    git(w.app, "reset", "-q", "--hard", head)
    edit(w.app, "NOTES.md", "mine\n")            # untracked, in the way
    assert replay.land_ff_locked(w.app, tip) == ("reset-refused", None)
    os.remove(os.path.join(w.app, "NOTES.md"))
    git(w.app, "checkout", "-q", "--detach")
    assert replay.land_ff_locked(w.app, tip) == ("moved", None)
    git(w.app, "checkout", "-q", HOME)
    assert replay.land_ff_locked(w.app, tip) == (None, None) and w.head() == tip


def test_settle_locked_needs_the_same_tree_only(w):
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    git_upstream._run(w.app, "fetch", "-q", "origin")
    follow.update_mirror(w.app, follow.for_record(store.get(w.app)))
    mine = claude_commit(w.app, {"app.py": APP_LOUD}, "loud")
    assert replay.settle_locked(w.app, main="refs/heads/main") == (False, None)  # trees differ
    git(w.app, "reset", "-q", "--hard", "HEAD~1")
    same = claude_commit(w.app, {"index.html": PAGE.replace("blue", "green")}, "same green")
    edit(w.app, "app.py", APP_LOUD)               # an unsaved edit is kept (round 3b)
    ok, backup = replay.settle_locked(w.app)      # main from fused/mine's upstream
    assert ok and w.head() == tip and replay.rev(w.app, backup) == same
    assert read(w.app, "app.py") == APP_LOUD
    inv(w)
    assert mine


# ------------------------------------------------------------- review round 4


def test_settle_onto_a_merged_tree_refuses_a_raced_commit(w):
    """Review P1 #2: `settle_locked(merged=)` is computed for one HEAD; a
    commit that landed since makes it refuse, nothing moved."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    w.fake.merge(REPO, n, "squash")
    _alice(w, {"index.html": PAGE.replace("blue", "ruby")}, "ruby")
    rec, fol = engine._record(w.app)
    with replay.slot():
        follow.fetch(w.app, fol)
    w.proposals.refresh(w.app)
    head, main = w.head(), replay.rev(w.app, "refs/heads/main")
    base = engine._merged_base(w.app, store.get(w.app), head, main)
    mt = replay.merge_tree(w.app, head, main, base=base)
    assert base and mt["clean"] and mt["tree"] == replay.tree_of(w.app, main)
    raced = claude_commit(w.app, {"app.py": APP_LOUD}, "raced")
    with replay.slot():
        assert replay.settle_locked(w.app, main="refs/heads/main", merged=mt["tree"],
                                    head=head) == (False, None)
    assert w.head() == raced
    with replay.slot():
        assert replay.settle_locked(w.app, main="refs/heads/main", merged=mt["tree"]) \
            == (False, None)                                # no head: refused too


def test_conflict_uses_the_merged_proposals_base(w, monkeypatch):
    """Review P1 #5: after a squash accept and the author editing the same
    lines, git's own base sees an overlap that is not there; `conflict` and
    `merge_plan` use the proposal's head, like status."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    w.fake.merge(REPO, n, "squash")
    _alice(w, {"index.html": PAGE.replace("blue", "ruby")}, "ruby")
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: False)
    w.sync()
    w.proposals.refresh(w.app)
    head, main = w.head(), replay.rev(w.app, "refs/heads/main")
    assert not replay.merge_tree(w.app, head, main)["clean"]      # git's own base
    c = engine.conflict(w.app)
    assert c["files"] == [] and c["base"] == store.get(w.app)["proposal"]["cache"]["head_sha"]
    assert engine.merge_plan(w.app) is None
    st = engine.status(w.app)
    assert st["state"] == "merged" and not st["needs_merge"]


def test_blocked_follows_the_working_tree_not_the_cache(w):
    """Review P1 #6: a colliding local file removed by hand clears
    `blocked` at once (the cache holds history facts only)."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    edit(w.app, "notes.md", "my own notes\n")
    _alice(w, {"notes.md": "author notes\n"}, "notes")
    w.sync()
    assert engine.status(w.app)["state"] == "blocked"
    os.remove(os.path.join(w.app, "notes.md"))
    st = engine.status(w.app)
    assert st["state"] == "changes" and st["blocked_files"] == []
    edit(w.app, "notes.md", "again\n")
    assert engine.status(w.app)["blocked_files"] == ["notes.md"]


def test_move_aside_refuses_on_another_version(w):
    """Review P1 #7: nothing leaves the folder while another version is
    checked out."""
    git(w.app, "checkout", "-q", "-b", "side")
    edit(w.app, "notes.md", "mine\n")
    _alice(w, {"notes.md": "author\n"}, "notes")
    w.sync()
    assert engine.move_aside(w.app)["reason"] == "off-default"
    assert read(w.app, "notes.md") == "mine\n"
    assert not [e for e in _activity(w) if e["kind"] == "aside"]


def _alice_git(w, tmp_path, change, msg):
    """Alice commits with real git (a symlink, which the fake's `commit`
    cannot write) and pushes to her repo."""
    work = str(tmp_path / "alice-work")
    if not os.path.isdir(work):
        git(str(tmp_path), "clone", "-q", w.fake.bare_path(REPO), work)
    change(work)
    git(work, "add", "-A")
    git(work, "-c", "user.name=alice", "-c", "user.email=alice@x", "commit", "-qm", msg)
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    w.fake.after_push(REPO, "main")
    return git(work, "rev-parse", "HEAD")


def test_a_symlinked_folder_never_moves_a_file_from_outside_the_app(w, tmp_path):
    """Security review: Alice's commit A tracks `docs -> <outside>`, B
    deletes the link and adds `docs/.zshrc`. The path reaches a file OUTSIDE
    the copy through the link: it is never "in the way" (git replaces the
    link) and never moved aside — the user's own file stays put."""
    outside = tmp_path / "home-of-bob"
    outside.mkdir()
    (outside / ".zshrc").write_text("bob's shell\n")
    _alice_git(w, tmp_path, lambda d: os.symlink(str(outside), os.path.join(d, "docs")),
               "docs link")
    assert w.sync()["status"] == "synced"
    assert os.path.islink(os.path.join(w.app, "docs"))
    head = w.head()

    def to_dir(d):
        os.unlink(os.path.join(d, "docs"))
        os.makedirs(os.path.join(d, "docs"))
        with open(os.path.join(d, "docs", ".zshrc"), "w") as f:
            f.write("alice's shell\n")
    tip = _alice_git(w, tmp_path, to_dir, "docs folder")
    git(w.app, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    assert replay.clobber_paths(w.app, head, tip) == []
    assert not replay.in_tree(w.app, "docs/.zshrc") and replay.in_tree(w.app, "docs")
    engine.move_aside(w.app)
    assert (outside / ".zshrc").read_text() == "bob's shell\n"
    assert not [e for e in _activity(w) if e["kind"] == "aside"]
    assert w.head() == tip and read(w.app, "docs/.zshrc") == "alice's shell\n"
    assert not os.path.islink(os.path.join(w.app, "docs"))
    assert (outside / ".zshrc").read_text() == "bob's shell\n"
    inv(w)


def test_repo_config_that_runs_commands_stops_every_move(w, tmp_path):
    """Security review: a repo-local filter / merge driver (or core.worktree,
    core.sshCommand, an external diff) runs during fused's own status,
    merge and reset; `-c` cannot switch them off, so every engine entry
    refuses while one is set. A plain preference (`merge.ff`) is fine."""
    marker = tmp_path / "filter-FIRED"
    git(w.app, "config", "--local", "merge.ff", "false")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["status"] == "synced"          # a preference is no hazard
    git(w.app, "config", "--local", "filter.x.clean", f"sh -c 'touch {marker}; cat'")
    edit(w.app, ".gitattributes", "* filter=x\n")
    head = w.head()
    _alice(w, {"MORE.md": "m\n"}, "more")
    res = w.sync()
    assert res["status"] == "skipped" and res["why"] == engine.UNSAFE_CONFIG
    assert w.head() == head
    err = store.get(w.app)["sync_error"]
    assert err["reason"] == engine.UNSAFE_CONFIG and err["message"] == replay.UNSAFE_CONFIG_MESSAGE
    for button in (engine.bring_in, engine.discard, engine.match, engine.move_aside):
        out = button(w.app)
        assert not out["ok"] and out["reason"] == engine.UNSAFE_CONFIG, (button, out)
    assert w.head() == head and not marker.exists()
    assert replay.config_hazards(w.app) == ["local:filter.x.clean"]
    for key in ("core.worktree", "core.sshCommand", "diff.external", "merge.x.driver",
                "diff.x.textconv"):
        assert replay._hazard(key.lower()), key
    assert not replay._hazard("merge.conflictstyle") and not replay._hazard("diff.renames")


def test_discard_and_match_wait_for_a_send(w, monkeypatch):
    """Proposals review: Discard / Use <who>'s version while Send or Update
    proposal runs would leave a live proposal of discarded changes."""
    from fused_render.contrib import jobs

    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    head = w.head()
    key = store.key(w.app)
    monkeypatch.setattr(jobs, "latest", lambda kind, app: (
        {"status": "running"} if (kind, app) == ("propose-send", key) else None))
    for button in (engine.discard, engine.match):
        out = button(w.app)
        assert not out["ok"] and out["reason"] == "sending"
    assert w.head() == head


def test_every_fetch_holds_the_slot(w, monkeypatch):
    """Engine review: Send, Update proposal and remix publish fetch through
    `proposals.tip_of(fetch=True)` with no slot of their own; the fetch
    writes git's tracking ref and the mirror, so it takes the slot itself."""
    from fused_render import git_upstream as gu
    from fused_render.contrib import gh

    held = []
    real = gh.fetch_detail

    def spy(*a, **kw):
        held.append(gu._check_slot.locked() and replay._depth() > 0)
        return real(*a, **kw)
    monkeypatch.setattr(gh, "fetch_detail", spy)
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    fol = follow.for_record(store.get(w.app))
    tip, why = follow.fetch(w.app, fol)
    assert tip == w.alice() and why is None and held == [True]
    assert not gu._check_slot.locked()
    # The slot taken elsewhere: the fetch waits, then answers "busy".
    monkeypatch.setattr(gu, "TIMEOUT_S", 0.0)
    monkeypatch.setattr(replay.gu, "TIMEOUT_S", 0.0)
    gu._check_slot.acquire()
    try:
        import threading

        out = []
        t = threading.Thread(target=lambda: out.append(follow.fetch(w.app, fol)))
        t.start()
        t.join(30)
    finally:
        gu._check_slot.release()
    assert out == [(None, "busy")]


def test_buttons_end_an_undo_hold(w):
    """Review P2 #15: Keep working and Go back end the hold as every button
    does."""
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.sync()
    assert engine.restore(w.app, store.activity(w.app)[0]["id"])["ok"]
    assert store.get(w.app)["skip_tip"]
    assert engine.keep_working(w.app)["ok"]
    assert store.get(w.app)["skip_tip"] is None
    assert engine.restore(w.app, next(e["id"] for e in store.activity(w.app)
                                      if e["kind"] == "update"))["reason"] == "nothing"
    _alice(w, {"MORE.md": "m\n"}, "more")
    w.sync()
    assert engine.restore(w.app, store.activity(w.app)[0]["id"])["ok"]
    git(w.app, "checkout", "-q", "-b", "side")
    assert engine.go_back_main(w.app)["ok"]
    assert store.get(w.app)["skip_tip"] is None


def test_one_guard_module():
    """Review P2 #11: classify and replay share one loaded guard module."""
    assert classify._guard_mod is replay._guard_module()
    assert classify.HOME_BRANCH == replay.HOME_BRANCH == classify._guard_mod.HOME_BRANCH
