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
    base = dict(head="h", tip="t", who="alice", user=False, changes_on=True,
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


@pytest.mark.parametrize("facts,action", [
    (_f(changes_on=False, clobber=("x",)), Action("skip", "changes-off")),         # 5
    (_f(changes_on=False, blocking=("x",)), Action("skip", "changes-off")),        # 6
    (_f(changes_on=False, merged=True), Action("skip", "changes-off")),            # 7
    (_f(changes_on=False, same_tree=True), Action("skip", "changes-off")),         # 10
    (_f(changes_on=False), Action("skip", "changes-off")),                         # 11
    (_f(changes_on=False, tip_in_head=True), Action("noop")),                      # 8 runs
    (_f(changes_on=False, head_in_tip=True), Action("ff")),                        # 9 runs
    (_f(changes_on=False, standing=True), Action("standing", "needs-merge")),      # 3 runs
    (_f(changes_on=False, merged=True, tip_in_head=True), Action("noop")),         # 7 → 8
])
def test_decide_flags_off_only_rows_1_to_4_8_9(facts, action):
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

    def refresh(self, app_dir, *, force=False):
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
        store.update(app_dir, fn)
        return {}

    def settle_direct(self, app_dir, sha):
        self.settled.append(sha)

    def withdraw(self, app_dir):
        self.withdrawn.append(app_dir)
        return {"ok": True}


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


def test_flags_off_never_creates_a_buttoned_state(w, monkeypatch):
    _flags(monkeypatch, changes=False)
    mine = claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    edit(w.app, "app.py", APP_LOUD)
    tip = _alice(w, {"index.html": PAGE.replace("blue", "green"),
                     "app.py": APP_PY + "# a\n"}, "green")
    for _ in range(2):
        res = w.sync(push=True)
        assert res["status"] == "skipped" and res["why"] == "changes-off"
    rec = store.get(w.app)
    assert rec["needs_merge"] is None and w.head() == mine
    assert read(w.app, "app.py") == APP_LOUD           # nothing committed for him
    assert w.g("status", "--porcelain") == "M app.py"
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
    assert engine.status(w.app)["state"] == "merged"
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
    assert w.sync()["why"] == "changes-off"
    assert w.head() == head and w.proposals.refreshes == 0


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


def test_owns_is_kind_github_whatever_the_flags(w, monkeypatch):
    _flags(monkeypatch, sources=False, changes=False, inbox=False, loop=False, remix=False)
    assert engine.owns(w.app)


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
