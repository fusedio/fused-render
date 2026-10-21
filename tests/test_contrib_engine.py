"""contrib/engine.py + replay.py + follow.py — the v2 git model
(docs/CONTRIB_LOOP.md §16.3) against REAL git: Alice's repo on the
FakeGitHub (real bare repos), Bob's GitHub-link copy cloned from it
(`_contrib_gh_world`). Every row of `decide`'s table, the fix buttons, the
v1 → v2 cleanup, and the audit repros (R1 R3 R4 R9 R11 R14 …) asserting the
FIXED behaviour: Bob's commits keep their ids, a merged proposal returns
keeping exactly the edits made after sending, flags off never creates a
state that needs a hidden button, and the engine never pushes.

proposals.py is another workstream's: a stand-in module answers `refresh`
from the FakeGitHub's pull request (GitHub is the truth) and records
`settle_direct`/`withdraw` calls.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _contrib_gh_world import APP_PY, PAGE, claude_commit, edit, git, make_world, read  # noqa: E402

from fused_render import git_upstream  # noqa: E402
from fused_render.contrib import classify, engine, flags, replay, store  # noqa: E402
from fused_render.contrib.engine import Action, Facts, decide  # noqa: E402

REPO = "alice/sine"
APP_LOUD = APP_PY.replace("return 1", "return 9")


# ------------------------------------------------------------ the pure table


def _f(**kw):
    base = dict(head="h", tip="t", who="alice", user=False, sources_on=True, changes_on=True,
                claude_busy=False, job_open=False, standing=False, kept_mine=False,
                clobber=(), blocking=(), merged=False, tip_in_head=False,
                head_in_tip=False, same_tree=False)
    base.update(kw)
    return Facts(**base)


@pytest.mark.parametrize("facts,action", [
    (_f(claude_busy=True, head_in_tip=True), Action("wait", "claude")),            # 1
    (_f(job_open=True, head_in_tip=True), Action("skip", "merge-job")),            # 2
    (_f(standing=True), Action("standing", "needs-merge")),                        # 3
    (_f(kept_mine=True), Action("skip", "kept-mine")),                             # 4
    (_f(kept_mine=True, user=True), Action("merge")),                              # 4, a button
    (_f(clobber=("data.csv",), blocking=("a",)), Action("blocked", "clobber")),    # 5
    (_f(blocking=("app.py",), merged=True), Action("commit-edits")),               # 6 before 7
    (_f(merged=True, tip_in_head=True), Action("return")),                         # 7
    (_f(tip_in_head=True), Action("noop")),                                        # 8
    (_f(head_in_tip=True), Action("ff")),                                          # 9
    (_f(same_tree=True), Action("move")),                                          # 10
    (_f(), Action("merge")),                                                       # 11
])
def test_decide_rows(facts, action):
    assert decide(facts) == action


_OFF = dict(sources_on=False, changes_on=False)


@pytest.mark.parametrize("facts,action", [
    (_f(**_OFF, clobber=("x",)), Action("fail", "git-failed")),                    # 5
    (_f(**_OFF, blocking=("x",)), Action("fail", "dirty")),                        # 6
    (_f(**_OFF, merged=True), Action("merge")),                                    # 7 needs A2
    (_f(sources_on=True, changes_on=False, merged=True), Action("merge")),         # 7 needs A2
    (_f(**_OFF, same_tree=True), Action("move")),                                  # 10 runs
    (_f(**_OFF), Action("merge")),                                                 # 11 runs
    (_f(**_OFF, tip_in_head=True), Action("noop")),                                # 8 runs
    (_f(**_OFF, head_in_tip=True), Action("ff")),                                  # 9 runs
    (_f(**_OFF, standing=True), Action("standing", "needs-merge")),                # 3 runs
])
def test_decide_flags_off_records_todays_failures(facts, action):
    """§16.12: rows 8–11 run whatever the flags; 5 and 6 (and an
    overlapping 11, `_execute`) become today's failure rows."""
    assert decide(facts) == action


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
        if not isinstance(p, dict):
            return None

        def fn(r):
            q = r["proposal"]
            if self.offline:
                q.setdefault("cache", {})["error"] = "offline"
                return
            pr = self.w.gh.get_pr(q["repo"], q["number"])
            q["cache"] = {"state": "merged" if pr["merged"] else pr["state"],
                          "merge_commit_sha": pr["merge_commit_sha"] if pr["merged"] else None}
            if tip:
                q["checked_tip"] = tip
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


def _flags(monkeypatch, **on):
    vals = dict.fromkeys(("sources", "changes", "inbox", "loop", "remix"), True)
    vals.update(on)
    monkeypatch.setattr(flags, "_raw", lambda: dict(vals))


def _alice(w, files, msg):
    return w.fake.commit(REPO, "main", files, msg, author="alice")


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
    """What Send leaves behind (WS-P's), done directly: Bob's HEAD on a
    branch of his fork, a pull request, the record's proposal."""
    gh = w.gh
    head = w.head()
    fork = gh.ensure_fork(REPO)
    branch = "fused-sine-20261016-1200-ab12"
    gh.push(w.app, head, fork, branch)
    pr = gh.open_pr(REPO, title=title, body="b", head=f"{fork.split('/')[0]}:{branch}",
                    base="main")
    tip = w.alice()

    def fn(r):
        r["proposal"] = {"repo": REPO, "number": pr["number"], "url": pr.get("url"),
                         "head_repo": fork, "head_branch": branch, "sent": head,
                         "checked_tip": tip, "title": title, "cache": {"state": "open"}}
    store.update(w.app, fn)
    return pr["number"]


# ------------------------------------------------------------- rows 8–11


def test_row9_fast_forward_says_what_came_in(w):
    a = _alice(w, {"NOTES.md": "n\n"}, "Add notes")
    res = w.sync(push=True)
    assert res["status"] == "synced"
    assert w.head() == a
    assert _texts(w)[-1] == "Got alice's latest: “Add notes”."
    assert _backups(w) == []
    _remote_untouched(w, a)
    assert (store.get(w.app) or {}).get("last_checked")


def test_row8_noop_keeps_bobs_commit(w):
    t = w.alice()
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    assert w.sync(push=True)["status"] == "synced"
    assert w.head() == mine
    assert _activity(w) == []
    _remote_untouched(w, t)
    st = engine.status(w.app)
    assert st["state"] == "changes" and st["changes_files"] == ["index.html"]


def test_row11_clean_merge_keeps_bobs_commit_ids_r14(w):
    """R14 fixed: three unrelated author pushes; Bob's turn commits keep
    their ids (his version links keep working), no backups, one Activity
    line per bring-in."""
    t1 = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "turn 1")
    t2 = claude_commit(w.app, {"app.py": APP_LOUD}, "turn 2")
    for i in range(3):
        tip = _alice(w, {f"N{i}.md": "n\n"}, f"notes {i}")
        assert w.sync(push=True)["status"] == "synced"
        head = w.head()
        assert _anc(w, t1, head) and _anc(w, t2, head) and _anc(w, tip, head)
        assert w.g("rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == \
            [w.g("rev-parse", "HEAD^1"), tip]
        _remote_untouched(w, tip)
    assert _backups(w) == []
    assert _texts(w)[-1] == "Brought in alice's “notes 2”, your changes kept."
    assert read(w.app, "index.html") == PAGE.replace("blue", "red")
    assert read(w.app, "app.py") == APP_LOUD
    st = engine.status(w.app)
    assert st["state"] == "changes" and st["behind"] == 0
    assert st["changes_files"] == ["app.py", "index.html"]


def test_row11_conflict_records_needs_merge_and_touches_nothing(w):
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    res = w.sync()
    assert res["status"] == "skipped" and res["why"] == "needs-merge"
    assert w.head() == mine and "red" in read(w.app, "index.html")
    nm = store.get(w.app)["needs_merge"]
    assert nm["purpose"] == "update" and nm["files"] == ["index.html"]
    assert nm["tip"] == tip and nm["head"] == mine
    st = engine.status(w.app)
    assert st["state"] == "needs-merge" and st["needs_merge_files"] == ["index.html"]
    plan = engine.merge_plan(w.app)
    assert plan == {"purpose": "update", "ours": mine, "theirs": tip, "onto": tip,
                    "files": ["index.html"]}
    # Row 3: standing — the next sync changes nothing.
    since = nm["since"]
    assert w.sync()["why"] == "needs-merge"
    assert store.get(w.app)["needs_merge"]["since"] == since
    _remote_untouched(w, tip)


def test_row10_same_tree_moves_to_tip_with_undo(w):
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "green")}, "same green")
    assert w.sync()["status"] == "synced"
    assert w.head() == tip
    line = _activity(w)[-1]
    assert line["text"] == "Got alice's latest. Your copy already matched it."
    assert replay.rev(w.app, line["backup"]) == mine
    assert engine.restore(w.app, line["id"])["ok"]
    assert w.head() == mine


# ------------------------------------------------------------- rows 5, 6


def test_row5_clobber_blocks_then_move_aside_and_restore(w):
    edit(w.app, ".gitignore", "data.csv\n")
    git(w.app, "add", ".gitignore")
    git(w.app, "-c", "user.name=B", "-c", "user.email=b@x", "commit", "-qm", "ignore data")
    edit(w.app, "data.csv", "bob's numbers\n")
    tip = _alice(w, {"data.csv": "alice's numbers\n"}, "ship data")
    head = w.head()
    res = w.sync()
    assert res["status"] == "skipped" and res["why"] == "clobber"
    assert w.head() == head and read(w.app, "data.csv") == "bob's numbers\n"
    assert store.get(w.app)["needs_merge"] is None
    assert engine.status(w.app)["blocked_files"] == ["data.csv"]
    out = engine.bring_in(w.app)
    assert out["reason"] == "blocked" and "data.csv" in out["message"]

    out = engine.move_aside(w.app)
    assert out["ok"], out
    assert read(w.app, "data.csv") == "alice's numbers\n"
    assert _anc(w, tip, w.head()) and _anc(w, head, w.head())
    aside = next(e for e in _activity(w) if e["kind"] == "aside")
    assert aside["aside"]["files"] == ["data.csv"]
    assert engine.restore(w.app, aside["id"])["ok"]
    assert read(w.app, "data.csv") == "bob's numbers\n"
    _remote_untouched(w, tip)


def test_row6_saves_blocking_edit_then_merges(w):
    edit(w.app, "app.py", APP_LOUD)              # uncommitted, line 2
    edit(w.app, "index.html", PAGE + "<!-- mine -->\n")  # uncommitted, Alice never touches it
    tip = _alice(w, {"app.py": APP_PY + "\n# alice\n"}, "alice app.py")
    assert w.sync()["status"] == "synced"
    head = w.head()
    assert _anc(w, tip, head)
    assert git(w.app, "log", "-1", "--format=%s", "HEAD^1") == "Your edits"
    assert read(w.app, "app.py") == APP_LOUD + "\n# alice\n"
    assert git(w.app, "status", "--porcelain").splitlines() == ["M index.html"]
    assert "Saved your edit to app.py so alice's update could come in." in _texts(w)


def test_untracked_file_is_never_committed(w):
    edit(w.app, "creds.json", "secret\n")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["status"] == "synced"
    assert read(w.app, "creds.json") == "secret\n"
    assert "creds.json" not in w.g("ls-files")


# ------------------------------------------------------------- rows 1–4


def test_row1_claude_busy_waits(w, monkeypatch):
    monkeypatch.setattr(engine, "_claude_busy", lambda root: True)
    head = w.head()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "claude"
    assert w.head() == head
    assert engine.bring_in(w.app)["message"] == engine.CLAUDE_BUSY_MESSAGE
    assert engine.use_theirs(w.app)["reason"] == "claude"


def test_row2_open_merge_job_skips(w, monkeypatch):
    monkeypatch.setattr(engine, "_job_open", lambda root: True)
    head = w.head()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "merge-job"
    assert w.head() == head


def test_row4_keep_mine_skips_until_bring_in_or_a_newer_tip(w):
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    w.sync()
    assert engine.keep_mine(w.app)["ok"]
    rec = store.get(w.app)
    assert rec["needs_merge"] is None and rec["skip_tip"] == w.alice()
    assert w.sync()["why"] == "kept-mine"
    assert w.head() == mine and store.get(w.app)["needs_merge"] is None
    st = engine.status(w.app)
    assert st["kept_mine"] and st["state"] == "changes"
    # The button still works (row 4 is for background syncs only).
    assert engine.bring_in(w.app)["needs_merge"]
    engine.keep_mine(w.app)
    # A newer tip is a new decision.
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "needs-merge"


# ------------------------------------------------------------- flags off


_ALL_OFF = dict(sources=False, changes=False, inbox=False, loop=False, remix=False)


def _failures(w):
    return [(f["reason"], f["output"]) for f in git_upstream.sync_failures()
            if f["root"] == w.app]


def test_v1_flags_off_one_claude_commit_still_gets_updates(w, monkeypatch):
    """V1 fixed: flags off (the shipping default), one Claude commit, then
    author pushes: a clean merge runs whatever the flags (§16.12)."""
    _flags(monkeypatch, **_ALL_OFF)
    mine = claude_commit(w.app, {"tweak.txt": "mine\n"}, "tweak")
    for i in range(2):
        tip = _alice(w, {f"c{i}.txt": "alice\n"}, f"a{i}")
        assert w.sync(push=True)["status"] == "synced"
        assert _anc(w, tip, w.head()) and _anc(w, mine, w.head())
        _remote_untouched(w, tip)
    assert _failures(w) == []


def test_v2_flags_off_overlapping_hand_edit_is_todays_dirty_row(w, monkeypatch):
    """V2 fixed: flags off, an uncommitted edit to a file the author's
    update changes → today's `dirty` failure row in plain words; nothing
    committed for him, tree untouched, no v2 state."""
    _flags(monkeypatch, **_ALL_OFF)
    head = w.head()
    edit(w.app, "app.py", APP_LOUD)
    tip = _alice(w, {"app.py": APP_PY + "# a\n"}, "a2")
    res = w.sync(push=True)
    assert res["status"] == "failed" and res["failure"]["reason"] == "dirty"
    assert _failures(w) == [("dirty", "You have unsaved edits to app.py, and alice's "
                                      "update changes that file too. Save or undo them to "
                                      "get the update.")]
    assert w.head() == head and read(w.app, "app.py") == APP_LOUD
    assert store.get(w.app)["needs_merge"] is None
    _remote_untouched(w, tip)
    git(w.app, "checkout", "--", "app.py")        # undone: the row goes, the update lands
    assert w.sync()["status"] == "synced" and _failures(w) == []


def test_flags_off_overlap_is_todays_diverged_row(w, monkeypatch):
    _flags(monkeypatch, **_ALL_OFF)
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    res = w.sync(push=True)
    assert res["status"] == "failed" and res["failure"]["reason"] == "diverged"
    assert "index.html" in _failures(w)[0][1]
    assert w.head() == mine and store.get(w.app)["needs_merge"] is None
    out = engine.bring_in(w.app)
    assert out["reason"] == "diverged" and "index.html" in out["message"]
    _remote_untouched(w, tip)


def test_flags_off_still_fast_forwards_and_fetches(w, monkeypatch):
    _flags(monkeypatch, changes=False, sources=False, inbox=False, loop=False, remix=False)
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync(push=True)["status"] == "synced"
    assert w.head() == tip
    _remote_untouched(w, tip)


def test_auto_sync_off_fetches_only_and_bring_in_works(w, monkeypatch):
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: False)
    head = w.head()
    tip = _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "auto-sync-off"
    assert w.head() == head and engine.status(w.app)["behind"] == 1
    assert engine.bring_in(w.app)["message"] == "You're on alice's latest."
    assert w.head() == tip


# ------------------------------------------------------------- the fixes


def test_use_theirs_backs_up_everything_and_restore_undoes(w):
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    edit(w.app, "app.py", APP_LOUD)
    edit(w.app, "scratch.txt", "my notes\n")
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    w.sync()
    out = engine.use_theirs(w.app)
    assert out["ok"] and "Activity" in out["message"]
    assert w.head() == tip and not os.path.exists(os.path.join(w.app, "scratch.txt"))
    assert w.g("status", "--porcelain") == ""
    rec = store.get(w.app)
    assert rec["needs_merge"] is None
    line = _activity(w)[-1]
    assert line["kind"] == "use-theirs" and replay.rev(w.app, line["backup"]) == mine
    assert engine.restore(w.app, line["id"])["ok"]
    assert w.head() == mine
    assert read(w.app, "app.py") == APP_LOUD
    assert read(w.app, "scratch.txt") == "my notes\n"
    assert "Brought back your version" in _texts(w)[-1]


def test_discard_withdraws_only_when_asked_r4(w):
    """R4 fixed at the engine's end: Discard without withdraw leaves the
    proposal open and Bob's copy equal to the author's — `sent` still names
    what GitHub has, so nothing re-sends an empty proposal."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _propose(w)
    assert engine.discard(w.app, withdraw=False)["ok"]
    assert w.proposals.withdrawn == []
    assert w.head() == w.alice()
    st = engine.status(w.app)
    assert st["changes_files"] == [] and st["state"] == "proposed"
    claude_commit(w.app, {"app.py": APP_LOUD}, "loud")
    assert engine.discard(w.app, withdraw=True)["ok"]
    assert w.proposals.withdrawn == [w.app]


def test_go_back_main_moves_main_along_when_it_is_contained(w):
    git(w.app, "checkout", "-q", "-b", "go-back")
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    assert w.sync()["why"] == "not-default"
    assert engine.status(w.app)["off_default"]
    edit(w.app, "app.py", APP_LOUD)
    out = engine.go_back_main(w.app)
    assert out["reason"] == "dirty" and git_upstream._current_branch(w.app) == "go-back"
    git(w.app, "checkout", "--", "app.py")
    assert engine.go_back_main(w.app)["ok"]
    assert git_upstream._current_branch(w.app) == "main" and w.head() == mine
    assert w.sync()["status"] == "synced"
    assert _anc(w, mine, w.head())


def test_go_back_main_saves_a_diverged_version(w):
    git(w.app, "checkout", "-q", "--detach")
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    git(w.app, "-c", "user.name=B", "-c", "user.email=b@x", "branch", "-f", "main", "HEAD~1")
    git(w.app, "checkout", "-q", "main")
    other = claude_commit(w.app, {"app.py": APP_LOUD}, "on main")
    git(w.app, "checkout", "-q", mine)
    out = engine.go_back_main(w.app)
    assert out["ok"] and "saved" in out["message"]
    assert w.head() == other
    line = _activity(w)[-1]
    assert replay.rev(w.app, line["backup"]) == mine


# ------------------------------------------------------------- the return (row 7)


def test_row7_squash_of_two_commits_returns_before_merging_r3(w):
    """R3 fixed: a two-commit proposal squash-merged; the very next sync
    (no refresh had run) asks GitHub first, then returns — no false
    overlap with Bob's own merged change."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "crimson")}, "crimson")
    n = _propose(w)
    w.fake.merge(REPO, n, "squash")
    assert w.sync()["status"] == "synced"
    rec = store.get(w.app)
    assert rec["needs_merge"] is None and rec["proposal"]["returned_at"]
    assert rec["proposal"]["checked_tip"] == w.alice()
    assert w.head() == w.alice()
    assert _texts(w)[-1] == "alice merged your change. You're on alice's latest version."
    st = engine.status(w.app)
    assert st["state"] == "merged" and st["merged_in_tip"]
    assert w.sync()["status"] == "synced" and w.head() == w.alice()  # returns once


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_row7_return_keeps_exactly_the_edits_after_sending(w, method):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    after = claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    edit(w.app, "NOTES.md", "draft\n")                      # untracked, carried
    w.fake.merge(REPO, n, method)
    assert w.sync()["status"] == "synced"
    head, tip = w.head(), w.alice()
    assert w.g("rev-parse", "HEAD^") == tip                  # tip + one re-applied edit
    assert git(w.app, "log", "-1", "--format=%s%n%an") == git(
        w.app, "log", "-1", "--format=%s%n%an", after)
    assert w.g("diff", "--name-only", tip, head) == "app.py"
    assert read(w.app, "NOTES.md") == "draft\n"
    line = _activity(w)[-1]
    assert line["text"].endswith("1 edit you made since kept.")
    assert replay.rev(w.app, line["backup"]) == after
    assert engine.status(w.app)["unproposed"] == 1


def test_row7_return_with_maintainer_edits(w):
    """Alice edits Bob's proposal on GitHub, then squashes: the return lands
    her version of his change plus only what he did since."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    p = store.get(w.app)["proposal"]
    w.fake.commit(p["head_repo"], p["head_branch"], {"index.html": PAGE.replace("blue", "ruby")},
                  "alice polish", author="alice")
    claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    w.fake.merge(REPO, n, "squash")
    assert w.sync()["status"] == "synced"
    assert "ruby" in read(w.app, "index.html") and read(w.app, "app.py") == APP_LOUD
    assert w.g("diff", "--name-only", w.alice(), "HEAD") == "app.py"


def test_row7_github_update_branch_then_merge_r9(w):
    """R9 fixed: GitHub's Update branch merges Alice's M2 into the PR; after
    the merge Bob's history never carries a re-created copy of M2."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    m2 = _alice(w, {"NOTES.md": "n\n"}, "M2 notes")
    w.fake.update_branch(REPO, n)
    claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    w.fake.merge(REPO, n, "merge")
    assert w.sync()["status"] == "synced"
    subjects = w.g("log", "--format=%H %s", f"{w.alice()}..HEAD").splitlines()
    assert [s.split(" ", 1)[1] for s in subjects] == ["Claude: louder"]
    assert _anc(w, m2, w.head())


def test_row7_conflict_records_a_return_overlap(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    _alice(w, {"app.py": APP_PY.replace("return 1", "return 5")}, "alice amplitude")
    w.fake.merge(REPO, n, "squash")
    head = w.head()
    assert w.sync()["why"] == "needs-merge"
    assert w.head() == head
    nm = store.get(w.app)["needs_merge"]
    assert nm["purpose"] == "return" and nm["files"] == ["app.py"]
    assert engine.merge_plan(w.app)["purpose"] == "return"


def test_row7_flags_off_never_returns(w, monkeypatch):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    w.fake.merge(REPO, n, "squash")
    _flags(monkeypatch, changes=False)
    head = w.head()
    assert w.sync()["status"] == "synced"          # rows 8–11 still run
    assert w.proposals.refreshes == 0               # no proposal UI, no GitHub poll
    assert not store.get(w.app)["proposal"].get("returned_at")
    # Same tree as the squash: row 10, the tip with an Undo for his commit.
    assert w.head() == w.alice()
    assert replay.rev(w.app, _activity(w)[-1]["backup"]) == head


def test_offline_refresh_keeps_asking(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _propose(w)
    old = store.get(w.app)["proposal"]["checked_tip"]
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.proposals.offline = True
    w.sync()
    assert store.get(w.app)["proposal"]["checked_tip"] == old
    w.proposals.offline = False
    w.sync()
    assert store.get(w.app)["proposal"]["checked_tip"] == w.alice()
    calls = w.proposals.refreshes
    w.sync()
    assert w.proposals.refreshes == calls        # same tip: no GitHub read


def test_sent_survives_author_pushes_r1_r11(w):
    """R1/R11 fixed: author pushes while (and after) Bob proposes; his
    proposed commit stays in HEAD, nothing is "unproposed", and the PR
    branch needs no rewrite."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.sync()
    _propose(w)
    sent = store.get(w.app)["proposal"]["sent"]
    for i in range(2):
        _alice(w, {f"M{i}.md": "m\n"}, f"more {i}")
        w.sync()
        assert _anc(w, sent, w.head())
    st = engine.status(w.app)
    assert st["unproposed"] == 0 and st["state"] == "proposed"


# ------------------------------------------------------------- push / v1 / guards


def test_push_to_main_for_a_collaborator(w):
    w.fake.add_collaborator(REPO, "bob")
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _propose(w)
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    out = engine.push_to_main(w.app)
    assert out["ok"], out
    head = w.head()
    assert w.alice() == head
    assert w.proposals.settled == [head]
    assert engine.status(w.app)["changes_files"] == []
    assert _texts(w)[-1] == "Published your changes straight to alice's app."


def test_push_to_main_refused_without_rights(w):
    tip = w.alice()
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    out = engine.push_to_main(w.app)
    assert out["reason"] == "forbidden"
    _remote_untouched(w, tip)


def test_first_v2_sync_cleans_up_v1_state(w):
    head = w.head()
    for ref in replay.V1_REFS:
        w.g("update-ref", ref, head)
    w.g("update-ref", "refs/fused/backup/1-update", head)

    def fn(r):
        r.pop("v", None)
        r["needs_merge"] = {"onto": head, "upstream": head, "purpose": "needs-merge"}
        r["backups"] = [{"ref": "refs/fused/backup/1-update", "at": 1.0, "why": "update"}]
    store.update(w.app, fn)
    w.sync()
    for ref in replay.V1_REFS:
        assert replay.rev(w.app, ref) is None
    rec = store.get(w.app)
    assert rec["v"] == 2 and rec["needs_merge"] is None and "backups" not in rec
    assert [e["backup"] for e in _activity(w)] == ["refs/fused/backup/1-update"]


def test_redirected_config_is_never_fetched(w):
    head = w.head()
    _alice(w, {"NOTES.md": "n\n"}, "notes")
    w.g("config", "url.https://evil.example/.insteadOf", "https://github.com/")
    assert w.sync()["why"] == "redirected"
    assert w.head() == head
    assert engine.status(w.app)["sync_error_code"] == "redirected"


def test_github_kind_goes_to_the_engine_whatever_the_flags(w, monkeypatch):
    """P1/P2: the recorded kind decides — with every flag off a `github`
    copy is synced by `engine.sync` (never the plain, pushing body)."""
    _flags(monkeypatch, sources=False, changes=False, inbox=False, loop=False, remix=False)
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
    _remote_untouched(w, tip)


def test_fetch_errors_reach_status_offline_stays_silent(w, monkeypatch):
    from fused_render.contrib import follow
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
    from fused_render.contrib import follow
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


def test_status_counts_only_bobs_files_when_behind(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"app.py": APP_LOUD}, "alice app")
    git_upstream._run(w.app, "fetch", "-q", "origin")
    st = engine.status(w.app)
    assert st["changes_files"] == ["index.html"] and st["behind"] == 1


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


def test_flags_off_fast_forward_never_overwrites_an_ignored_file(w, monkeypatch):
    """A plain fast-forward would overwrite Bob's ignored file silently;
    with sources off it is today's failure row instead, file untouched."""
    edit(w.app, ".git/info/exclude", "data.csv\n")
    edit(w.app, "data.csv", "bob's numbers\n")
    _flags(monkeypatch, **_ALL_OFF)
    head = w.head()
    _alice(w, {"data.csv": "alice's numbers\n"}, "ship data")
    res = w.sync()
    assert res["status"] == "failed" and res["failure"]["reason"] == "git-failed"
    assert _failures(w)[0][1] == ("alice's update adds data.csv, which you already have "
                                  "here. Rename yours to get the update.")
    assert w.head() == head and read(w.app, "data.csv") == "bob's numbers\n"
    assert store.get(w.app)["needs_merge"] is None


@pytest.mark.parametrize("method", ["merge", "squash"])
def test_row7_return_through_the_old_git_path(w, monkeypatch, method):
    monkeypatch.setattr(replay, "_FORCE_SCRATCH_MERGE", True)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    w.fake.merge(REPO, n, method)
    assert w.sync()["status"] == "synced"
    assert w.g("rev-parse", "HEAD^") == w.alice()
    assert w.g("diff", "--name-only", w.alice(), "HEAD") == "app.py"
    assert os.listdir(replay.work_dir()) == []


# ------------------------------------------------------- review round 1


_SOURCES_ONLY = dict(_ALL_OFF, sources=True)


def test_v3_sources_only_status_says_how_far_behind(w, monkeypatch):
    """V3: with A1 only, an overlap is a v2 state (its buttons live under
    sources) and the status still says the author's update is waiting."""
    _flags(monkeypatch, **_SOURCES_ONLY)
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _alice(w, {"index.html": PAGE.replace("blue", "green")}, "green")
    assert w.sync()["why"] == "needs-merge"
    st = engine.status(w.app)
    assert st["behind"] == 1 and st["state"] == "needs-merge" and st["needs_merge"]
    assert _failures(w) == []


def test_v4_sources_only_collision_is_blocked_with_move_aside(w, monkeypatch):
    """V4 fixed: an untracked file the author's update adds is the blocked
    state with the Move aside fix — Bring in says so, never "changes-off"."""
    _flags(monkeypatch, **_SOURCES_ONLY)
    edit(w.app, "data.json", "{}\n")
    _alice(w, {"data.json": '{"seed": 1}\n'}, "seed")
    assert w.sync()["why"] == "clobber"
    assert engine.status(w.app)["blocked_files"] == ["data.json"]
    out = engine.bring_in(w.app)
    assert out["reason"] == "blocked" and "Move yours aside" in out["message"]
    assert engine.move_aside(w.app)["ok"] and read(w.app, "data.json") == '{"seed": 1}\n'


@pytest.mark.parametrize("on", [True, False])
def test_v5_off_default_fetches_first_and_shows_behind(w, monkeypatch, on):
    if not on:
        _flags(monkeypatch, **_ALL_OFF)
    git(w.app, "checkout", "-q", "-b", "feature")
    head = w.head()
    _alice(w, {"c.txt": "alice\n"}, "a2")
    assert w.sync()["why"] == "not-default"
    st = engine.status(w.app)
    assert st["behind"] == 1 and st["off_default"] and st["last_checked"]
    assert w.head() == head


def test_s3_restore_backs_up_an_ignored_file_it_overwrites(w):
    """rev-sec S3 fixed: Undo of an older line restores a version that
    tracks a path Bob now has as an IGNORED file — that file rides in the
    restore's own backup, and undoing the restore brings it back."""
    mine = claude_commit(w.app, {"settings.json": "old\n"}, "settings")
    _alice(w, {".gitignore": "settings.json\n", "NOTES.md": "n\n"}, "ignore settings")
    assert engine.use_theirs(w.app)["ok"]
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


def test_s3b_go_back_main_backs_up_an_ignored_file_it_overwrites(w):
    git(w.app, "checkout", "-q", "-b", "side")
    git(w.app, "rm", "-q", "--cached", "app.py")
    edit(w.app, ".gitignore", "app.py\n")
    git(w.app, "add", ".gitignore")
    git(w.app, "-c", "user.name=B", "-c", "user.email=b@x", "commit", "-q", "-m", "untrack")
    git(w.app, "checkout", "-q", "main")
    claude_commit(w.app, {"NOTES.md": "m\n"}, "on main")
    git(w.app, "checkout", "-q", "side")
    edit(w.app, "app.py", "BOB-PRECIOUS-TOKEN\n")
    assert w.g("status", "--porcelain") == ""
    assert engine.go_back_main(w.app)["ok"]
    line = _activity(w)[-1]
    wip = replay.rev(w.app, line["backup"] + "-wip")
    assert wip and w.g("show", f"{wip}:app.py") == "BOB-PRECIOUS-TOKEN"


def test_s4_repo_config_commands_never_run_from_fused_git_calls(w, tmp_path):
    """rev-sec S4 fixed: an app-written `core.fsmonitor` / `core.hooksPath`
    never runs from fused's reads, scratch checkouts or moves."""
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
    """rev-sec minor (a): when a commit that raced an update cannot be put
    back, its pin is named on an Activity line (Restore), never silent."""
    head = w.head()
    raced = claude_commit(w.app, {"index.html": PAGE.replace("blue", "raced")}, "raced")
    git(w.app, "reset", "-q", "--hard", head)
    git(w.app, "update-ref", "ORIG_HEAD", raced)
    edit(w.app, "index.html", "unsaved\n")       # makes `reset --keep raced` refuse
    assert replay.undo_race_locked(w.app, head, []) is False
    line = _activity(w)[-1]
    assert line["kind"] == "race" and replay.rev(w.app, line["backup"]) == raced


def test_refused_use_theirs_keeps_a_ready_merge(w, monkeypatch):
    """rev-sec minor (b): the slot is checked before a ready Merge with
    Claude result is thrown away."""
    import contextlib
    dropped = []
    monkeypatch.setattr(engine, "_discard_job", lambda root: dropped.append(root))

    @contextlib.contextmanager
    def busy(**_kw):
        yield False
    monkeypatch.setattr(replay, "slot", busy)
    assert engine.use_theirs(w.app)["reason"] == "busy"
    assert dropped == []


def test_partial_unaside_keeps_restore(w, tmp_path):
    """rev-sec minor (c): a set-aside line is marked restored only when
    every file came back."""
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


@pytest.mark.parametrize("method", ["merge", "squash"])
def test_merged_then_taken_back_says_so(w, method):
    """V4 wording: Alice merges, then reverts — the return lands and the
    line says she took it back; Bob's later edit is kept."""
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    n = _propose(w)
    claude_commit(w.app, {"app.py": APP_LOUD}, "louder")
    w.fake.merge(REPO, n, method)
    _alice(w, {"index.html": PAGE}, "Revert red")
    assert w.sync()["status"] == "synced"
    assert "blue" in read(w.app, "index.html") and read(w.app, "app.py") == APP_LOUD
    assert _texts(w)[-1] == ("alice merged your change, then took it back. You're on "
                             "alice's latest version. 1 edit you made since kept.")
    assert engine.status(w.app)["merged_in_tip"]


def test_merged_in_tip_false_without_a_merged_proposal(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    _propose(w)
    assert engine.status(w.app)["merged_in_tip"] is False
