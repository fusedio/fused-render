"""fused_render/git_upstream.py — the throttled "has origin moved?" check
triggered from GET /render's D301 block (SPEC §33 / §36).

Uses the same real-repo fixtures as tests/test_community_marketplace_fixes.py
(tests/_git_repo.py): a mocked subprocess would test our own fiction of what
git says, not what it actually says.
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import bare_repo, git, git_available, with_remote, write  # noqa: E402

from fused_render import git_upstream


def _sync(fn):
    """A `_runner` that runs the check inline instead of on a thread, so a
    test can call `known_repos()` immediately after `note_app_opened`."""
    fn()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test gets its own throttle/result state — these are process-wide
    module dicts, and a stale entry from one test must never leak into the
    next."""
    monkeypatch.setattr(git_upstream, "_checked", {})
    monkeypatch.setattr(git_upstream, "_state", {})
    # The slot is a real Lock; a test that acquired it and never released
    # (an exception mid-check) must not wedge every test after it.
    if git_upstream._check_slot.locked():
        git_upstream._check_slot.release()


def _clone_with_remote_ahead(tmp_path, name="repo"):
    """A local clone whose `origin/main` is `ahead_by` commits ahead of HEAD:
    push once, then advance the remote directly (a second, throwaway working
    copy) without ever fetching it into the clone under test."""
    remote = str(tmp_path / f"{name}.git")
    local = str(tmp_path / name)
    os.makedirs(local)
    git(local, "init", "-q")
    # A LOCAL (not just `_git_repo.git()`'s own per-invocation env) identity,
    # persisted to this repo's own .git/config: some tests below drive a raw
    # `git rebase` (or other commit-creating command) directly against this
    # repo, standing in for a terminal, with none of `_git_repo`'s identity
    # env vars — exactly like a real user's own git commands, which always
    # have identity configured already (git would have refused to create
    # those commits otherwise). A CI runner has no such ambient identity (no
    # global config, and the `runner` user's /etc/passwd GECOS is empty, so
    # even git's own name-from-hostname fallback fails with "empty ident
    # name"), so without this, a commit-creating step fails in CI in a way
    # that could never happen on a real machine — a test-rig gap, not a
    # product bug.
    git(local, "config", "user.name", "Fixture Author")
    git(local, "config", "user.email", "fixture@example.com")
    write(local, "a.txt", "1\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "c1")
    with_remote(local, remote)

    # A second checkout of the same remote advances it — the clone under
    # test never sees these commits until it fetches.
    other = str(tmp_path / f"{name}-other")
    git(tmp_path.__str__(), "clone", "-q", remote, other)
    write(other, "b.txt", "2\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "c2")
    write(other, "b.txt", "3\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "c3")
    git(other, "push", "-q", "origin", "HEAD:main")
    return local


pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def test_behind_count_is_correct(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)

    started = git_upstream.note_app_opened(local, _runner=_sync)

    assert started
    repos = git_upstream.known_repos()
    assert len(repos) == 1
    assert repos[0]["root"] == os.path.realpath(local)
    assert repos[0]["behind"] == 2
    assert repos[0]["ahead"] == 0
    assert repos[0]["default_branch"] == "main"
    assert repos[0]["branch"] == "main"
    assert repos[0]["on_default"] is True


def test_ahead_count_is_correct_for_a_branch_with_unpushed_commits(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    write(local, "mine.txt", "not pushed\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "unpushed")

    started = git_upstream.note_app_opened(local, _runner=_sync)

    assert started
    repos = git_upstream.known_repos()
    assert len(repos) == 1
    assert repos[0]["ahead"] == 1
    assert repos[0]["behind"] == 2


def test_a_second_app_in_the_same_repo_does_not_refetch_inside_the_window(tmp_path, monkeypatch):
    local = _clone_with_remote_ahead(tmp_path)
    calls = []
    real_check = git_upstream.check_repo

    def spy(root):
        calls.append(root)
        return real_check(root)

    monkeypatch.setattr(git_upstream, "check_repo", spy)

    sub = os.path.join(local, "sub")
    os.makedirs(sub)
    assert git_upstream.note_app_opened(local, _runner=_sync)
    # A second app, elsewhere in the SAME repo, opened right after — well
    # inside CHECK_TTL_S. A background attempt is still DISPATCHED (the
    # slot is free again by the time this runs, since `_sync` already ran
    # the first one to completion) — `note_app_opened`'s return value only
    # ever says whether a dispatch happened, matching
    # index.note_folder_opened's own convention (see that function's
    # docstring). What actually proves the throttle is `calls` staying at 1:
    # the dispatched check itself finds the root not due and returns
    # without ever calling `check_repo` a second time.
    assert git_upstream.note_app_opened(sub, _runner=_sync)

    assert len(calls) == 1


def test_a_preview_render_triggers_no_check(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    d = tmp_path / "Fused" / "local" / "sine"
    d.mkdir(parents=True)
    (d / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>hi</body></html>')
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))

    calls = []
    monkeypatch.setattr(git_upstream, "note_app_opened",
                        lambda path, **kw: calls.append(path))

    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.get("/render", params={"path": str(d / "index.html"), "_preview": "1"})

    assert r.status_code == 200
    assert calls == []


def test_a_real_render_of_an_app_in_a_repo_triggers_a_check(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    remote = str(tmp_path / "app.git")
    d = tmp_path / "Fused" / "local" / "sine"
    d.mkdir(parents=True)
    git(str(d), "init", "-q")
    (d / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>hi</body></html>')
    git(str(d), "add", "-A")
    git(str(d), "commit", "-q", "-m", "c1")
    with_remote(str(d), remote)
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))

    calls = []
    monkeypatch.setattr(git_upstream, "note_app_opened",
                        lambda path, **kw: calls.append(path))

    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.get("/render", params={"path": str(d / "index.html")})

    assert r.status_code == 200
    assert calls == [str(d)]


def test_an_app_not_in_a_repo_triggers_nothing(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    # A dispatch still happens (the slot was free) — resolving "is this
    # even a repo" is itself git work, and that now runs entirely inside
    # the dispatched call (finding #10: the request thread must do none of
    # it). What "triggers nothing" actually asserts is the visible effect:
    # no repo ever gets recorded.
    started = git_upstream.note_app_opened(str(plain), _runner=_sync)

    assert started
    assert git_upstream.known_repos() == []


def test_an_unreachable_remote_records_nothing_and_raises_nothing(tmp_path):
    local = str(tmp_path / "repo")
    os.makedirs(local)
    git(local, "init", "-q")
    write(local, "a.txt", "1\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "c1")
    # A remote that cannot be reached — a path that never existed, standing
    # in for "offline" without an actual network dependency in the suite.
    git(local, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))

    started = git_upstream.note_app_opened(local, _runner=_sync)

    assert started  # a check DID run — it just found nothing to report
    assert git_upstream.known_repos() == []


def test_a_mount_backed_path_is_refused(tmp_path, monkeypatch):
    local = _clone_with_remote_ahead(tmp_path)
    monkeypatch.setattr(git_upstream.shell_mounts, "is_mount_backed", lambda p: True)

    # Dispatched (the slot was free); the mount refusal happens inside the
    # dispatched call, before any fetch — what matters is that it produces
    # no recorded repo, not the return value (see the sibling test above).
    started = git_upstream.note_app_opened(local, _runner=_sync)

    assert started
    assert git_upstream.known_repos() == []


# -------------------------------------------------------------- update / switch


def test_update_ff_only_pulls_on_a_clean_default_branch(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)

    res = git_upstream.update_repo(local)

    assert res["ok"] is True, res
    assert git(local, "log", "-1", "--format=%s").strip() == "c3"


def test_update_refuses_a_dirty_tree(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    write(local, "a.txt", "dirty\n")

    res = git_upstream.update_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "dirty"
    # untouched — the pull never ran
    assert git(local, "log", "-1", "--format=%s").strip() == "c1"
    with open(os.path.join(local, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "dirty\n"


def test_update_refuses_a_mount_backed_repo(tmp_path, monkeypatch):
    local = _clone_with_remote_ahead(tmp_path)
    monkeypatch.setattr(git_upstream.shell_mounts, "is_mount_backed", lambda p: True)

    res = git_upstream.update_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "mount"


def test_update_refuses_a_repo_with_no_origin(tmp_path):
    local = str(tmp_path / "solo")
    os.makedirs(local)
    git(local, "init", "-q")
    write(local, "a.txt", "1\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "c1")

    res = git_upstream.update_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "no-remote"


def test_switch_checks_out_the_default_branch(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")

    res = git_upstream.switch_repo(local)

    assert res["ok"] is True, res
    assert res["op"] == "switch"
    assert git(local, "symbolic-ref", "--short", "HEAD").strip() == "main"


def test_switch_refuses_a_dirty_tree(tmp_path):
    # A TRACKED-file edit, like test_update_refuses_a_dirty_tree — switch_repo
    # uses the same include_untracked=False preflight as update_repo, so an
    # untracked file must not block it (that is the whole point of the
    # looser check), but a change to a tracked file must still refuse.
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    write(local, "a.txt", "uncommitted\n")

    res = git_upstream.switch_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "dirty"
    assert git(local, "symbolic-ref", "--short", "HEAD").strip() == "feature"


def test_switch_is_not_refused_by_an_unrelated_untracked_file(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    write(local, "scratch.tmp", "not tracked, not touched by the checkout\n")

    res = git_upstream.switch_repo(local)

    assert res["ok"] is True, res
    with open(os.path.join(local, "scratch.tmp"), encoding="utf-8") as f:
        assert f.read() == "not tracked, not touched by the checkout\n"


def test_switch_resolves_a_detached_head_instead_of_refusing_it(tmp_path):
    # check_repo reports `branch: None` / `on_default: False` for a detached
    # HEAD, which used to make Switch the row's PRIMARY action — and then
    # switch_repo refused with "detached", a guaranteed dead end (code
    # review, task 9). A checkout of the default branch is exactly the
    # resolution for a detached HEAD, so this is the one mutation that must
    # not refuse it.
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "--detach")
    assert git_upstream._current_branch(local) is None  # fixture sanity

    res = git_upstream.switch_repo(local)

    assert res["ok"] is True, res
    assert git(local, "symbolic-ref", "--short", "HEAD").strip() == "main"


def test_switch_still_refuses_a_dirty_detached_head(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "--detach")
    write(local, "a.txt", "uncommitted\n")

    res = git_upstream.switch_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "dirty"


def test_update_still_refuses_a_detached_head(tmp_path):
    # Only switch_repo's preflight is loosened — update_repo's own "there is
    # nothing to update" refusal for a detached HEAD is unchanged.
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "--detach")

    res = git_upstream.update_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "detached"


def test_switch_successful_recheck_updates_the_stale_branch_and_on_default(tmp_path):
    # Bugbot finding (17a): a re-check that SUCCEEDS after switch has always
    # replaced the row wholesale via check_repo's own fresh result — this
    # pins that the success path still does, now that a FAILED re-check
    # (below) takes a different one.
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    root = os.path.realpath(local)
    git_upstream._record({"root": root, "branch": "feature", "default_branch": "main",
                          "on_default": False, "ahead": 0, "behind": 2, "checked_at": 0.0})

    res = git_upstream.switch_repo(local)

    assert res["ok"] is True, res
    row = git_upstream._state.get(root)
    assert row is not None
    assert row["branch"] == "main"
    assert row["on_default"] is True
    # A real check_repo ran and found this repo still behind post-switch —
    # this is the "legitimately comes back as Update" row the fix exists
    # for, not a stale pre-mutation number.
    assert row["behind"] == 2
    assert row["checked_at"] > 0.0


def test_switch_failed_recheck_keeps_the_row_instead_of_dropping_it(tmp_path, monkeypatch):
    # Bugbot finding (17a): switch's own checkout already succeeded by the
    # time `_refresh_after_mutation` runs — a re-check that then fails
    # (network blip) must not delete the row the way update's own failed
    # re-check correctly does (see
    # test_a_successful_update_clears_a_stale_row_even_if_the_recheck_fails).
    # Deleting it here drops the follow-up "you're on main now, and it's
    # behind" Update row a user would otherwise see once they retry.
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    root = os.path.realpath(local)
    git_upstream._record({"root": root, "branch": "feature", "default_branch": "main",
                          "on_default": False, "ahead": 0, "behind": 2, "checked_at": 0.0})

    monkeypatch.setattr(git_upstream, "check_repo", lambda r: None)

    res = git_upstream.switch_repo(local)

    assert res["ok"] is True, res
    row = git_upstream._state.get(root)
    assert row is not None, "a failed re-check after switch must not drop the row"
    # branch/on_default are known LOCALLY the moment the checkout succeeds —
    # no network round trip needed to know these two fields are stale.
    assert row["branch"] == "main"
    assert row["on_default"] is True
    # No fresh ahead/behind was available (the re-check itself is what
    # failed); the pre-mutation numbers survive rather than being guessed.
    assert row["behind"] == 2


def test_switch_checkout_argument_order_does_not_read_the_branch_as_a_pathspec(tmp_path):
    # `git checkout -- <branch>` (`--` BEFORE the branch) makes git read the
    # branch name as a PATHSPEC — "restore this path from the index" — not a
    # branch switch, and fails with "pathspec '<branch>' did not match any
    # file(s)". That was the mistake in the original task handoff (code
    # review, task 10). The correct order is `git checkout <branch> --`
    # (branch first), which disambiguates without changing the meaning.
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")

    res = git_upstream.switch_repo(local)

    assert res["ok"] is True, res
    assert "pathspec" not in (res.get("message") or "").lower()
    assert git(local, "symbolic-ref", "--short", "HEAD").strip() == "main"


def test_switch_names_the_worktree_already_holding_the_default_branch(tmp_path):
    # A worktree-per-branch flow (this repo's own) puts every non-default
    # worktree in exactly this state: the default branch is checked out in
    # the MAIN checkout, so `git checkout <default>` from a linked worktree
    # fails deterministically with "already used by worktree at ...". Task
    # 10 (code review): every off-default row in such a worktree used to get
    # a primary button that always failed, surfacing raw git text instead of
    # naming the worktree. A rebase-onto-default secondary action was once
    # offered as the way out of this specific refusal too; it was removed as
    # too dangerous to offer (D554 amendment), so the refusal now just names
    # the fact and stops — the fix is a terminal, same as every other
    # refusal this module surfaces.
    local = _clone_with_remote_ahead(tmp_path, name="wtswitch")
    worktree = str(tmp_path / "wtswitch-linked")
    git(local, "worktree", "add", "-q", "-b", "feature", worktree, "HEAD")

    res = git_upstream.switch_repo(worktree)

    assert res["ok"] is False
    assert res["reason"] == "checked-out-elsewhere"
    # OS-NATIVE separators (code review, 2026-08-27): git's own "already used
    # by worktree at '<path>'" always uses forward slashes, even on Windows
    # (confirmed by the CI failure this pins: a Windows run reported
    # "C:/Users/..." in git's text) — showing that raw to a Windows user is a
    # path their own shell/Explorer never writes. `os.path.normpath` on BOTH
    # sides is what makes this assertion pass on macOS AND Windows without an
    # `os.name` branch: on POSIX it is a no-op for slash direction, and on
    # Windows it turns `os.path.realpath`'s native backslash form and the
    # message's now-normalized backslash form into the same string.
    assert os.path.normpath(os.path.realpath(local)) in res["message"]
    # ASCII only (code review, 2026-08-27): a CI log rendered this message's
    # em-dash as a mojibake replacement character on the Windows runner —
    # confirm nothing in it needs a code page wider than ASCII to survive a
    # console.
    res["message"].encode("ascii")
    # Not a silent action swap: switch_repo still tried `switch`, and still
    # refused rather than quietly taking some other action on the user's
    # behalf.
    assert git(worktree, "symbolic-ref", "--short", "HEAD").strip() == "feature"


# ------------------------------------------------------------- is_known_repo


def test_is_known_repo_is_false_for_a_root_never_checked(tmp_path):
    assert not git_upstream.is_known_repo(str(tmp_path / "never-seen"))


def test_is_known_repo_is_true_once_a_check_has_recorded_it(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    assert git_upstream.note_app_opened(local, _runner=_sync)
    assert git_upstream.is_known_repo(os.path.realpath(local))


def test_is_known_repo_stays_true_after_the_repo_is_brought_up_to_date(tmp_path):
    # A repo the check just zeroed out (behind == 0) drops out of
    # known_repos()'s filtered view but must still be a legitimate POST
    # target — a card race (poll said behind, click lands after a
    # concurrent check already caught the update) must not 403.
    local = _clone_with_remote_ahead(tmp_path)
    root = os.path.realpath(local)
    git_upstream._record({"root": root, "branch": "main", "default_branch": "main",
                          "on_default": True, "behind": 0, "checked_at": 0.0})
    assert not any(r["root"] == root for r in git_upstream.known_repos())
    assert git_upstream.is_known_repo(root)


# ---------------------------------------------------------- POST /api/git-upstream


def _client(tmp_path):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    workspace = tmp_path / "Fused"
    workspace.mkdir()
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_post_without_x_fused_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    client = _client(tmp_path)

    r = client.post("/api/git-upstream", json={"action": "update", "root": "/tmp/whatever"})

    assert r.status_code == 403


def test_post_for_an_unrecorded_root_is_refused_even_with_x_fused(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    client = _client(tmp_path)

    r = client.post("/api/git-upstream",
                    json={"action": "update", "root": "/any/repo/on/disk"},
                    headers={"X-Fused": "1"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "unknown-repo"


def test_post_for_a_recorded_root_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    client = _client(tmp_path)
    local = _clone_with_remote_ahead(tmp_path)
    root = os.path.realpath(local)
    assert git_upstream.note_app_opened(local, _runner=_sync)
    assert git_upstream.is_known_repo(root)

    r = client.post("/api/git-upstream",
                    json={"action": "update", "root": root},
                    headers={"X-Fused": "1"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body


def test_post_switch_action_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    client = _client(tmp_path)
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    root = os.path.realpath(local)
    assert git_upstream.note_app_opened(local, _runner=_sync)
    assert git_upstream.is_known_repo(root)

    r = client.post("/api/git-upstream",
                    json={"action": "switch", "root": root},
                    headers={"X-Fused": "1"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body
    assert body["op"] == "switch"


def test_post_rebase_action_is_refused_bad_action(tmp_path, monkeypatch):
    # A secondary Rebase button once posted `action: "rebase"` here; removed
    # as too dangerous to offer (D554 amendment), along with the endpoint's
    # own handling of it — this pins that a client still sending it (a stale
    # tab, a hand-written request) is refused rather than falling through to
    # some other mutation.
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    client = _client(tmp_path)
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    root = os.path.realpath(local)
    assert git_upstream.note_app_opened(local, _runner=_sync)
    assert git_upstream.is_known_repo(root)

    r = client.post("/api/git-upstream",
                    json={"action": "rebase", "root": root},
                    headers={"X-Fused": "1"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "bad-action"
    # No mutation happened — the branch is exactly where it started.
    assert git(local, "symbolic-ref", "--short", "HEAD").strip() == "feature"


# --------------------------------------------- untracked files (finding #3)


def test_update_is_not_refused_by_an_unrelated_untracked_file(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    write(local, "scratch.tmp", "not tracked, not touched by the pull\n")

    res = git_upstream.update_repo(local)

    assert res["ok"] is True, res
    with open(os.path.join(local, "scratch.tmp"), encoding="utf-8") as f:
        assert f.read() == "not tracked, not touched by the pull\n"


# ---------------------------------------- mid-operation preflight (finding #4)


def _rebase_conflict_repo(tmp_path, name="conflict"):
    """A repo whose current branch, rebased onto `origin/main`, genuinely
    conflicts on one line of `a.txt` — the same shape
    tests/test_git_conflicts.py's `rebase_conflict_repo` builds. This module
    offers no rebase mutation of its own (removed as too dangerous to offer
    — D554 amendment); the tests below drive the rebase with a raw `git
    rebase`, standing in for a terminal, since detecting a rebase already in
    flight must work regardless of what started it."""
    remote = str(tmp_path / f"{name}.git")
    local = str(tmp_path / name)
    os.makedirs(local)
    git(local, "init", "-q")
    # See _clone_with_remote_ahead's own comment on this: a conflicting
    # rebase step halts before it would need identity, but a persisted
    # local identity here keeps this fixture consistent (and safe against
    # a future edit that resolves the conflict and finalizes a commit).
    git(local, "config", "user.name", "Fixture Author")
    git(local, "config", "user.email", "fixture@example.com")
    write(local, "a.txt", "one\ntwo\nthree\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "base")
    with_remote(local, remote)

    other = str(tmp_path / f"{name}-other")
    git(tmp_path.__str__(), "clone", "-q", remote, other)
    write(other, "a.txt", "one\nTHEIRS\nthree\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs")
    git(other, "push", "-q", "origin", "HEAD:main")

    git(local, "checkout", "-q", "-b", "feature")
    write(local, "a.txt", "one\nOURS\nthree\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "ours")
    return local


def test_update_reports_a_mid_rebase_repo_accurately_not_as_dirty(tmp_path):
    local = _rebase_conflict_repo(tmp_path)

    git(local, "fetch", "-q", "origin", "main")
    git(local, "rebase", "origin/main", check=False)  # conflicts by construction
    assert os.path.isdir(os.path.join(local, ".git", "rebase-merge")) or \
        os.path.isdir(os.path.join(local, ".git", "rebase-apply"))

    res = git_upstream.update_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "in-progress"
    assert "rebase" in res["message"]


def _rebase_conflict_in_linked_worktree(tmp_path, name="wtconflict"):
    """The same conflicting-rebase shape as `_rebase_conflict_repo`, but the
    branch with the local commit lives in a LINKED WORKTREE rather than the
    main checkout: `<worktree>/.git` there is a FILE containing
    `gitdir: <real path>`, never a directory — the exact shape
    `_operation_in_flight`'s old `os.path.isdir(root/".git")` probe went
    blind to (it always answered False, so a conflicted rebase inside a
    linked worktree was never detected)."""
    remote = str(tmp_path / f"{name}.git")
    local = str(tmp_path / name)
    os.makedirs(local)
    git(local, "init", "-q")
    # See _clone_with_remote_ahead's own comment on this: a persisted local
    # identity, because the CI runner has no ambient one.
    git(local, "config", "user.name", "Fixture Author")
    git(local, "config", "user.email", "fixture@example.com")
    write(local, "a.txt", "one\ntwo\nthree\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "base")
    with_remote(local, remote)

    other = str(tmp_path / f"{name}-other")
    git(str(tmp_path), "clone", "-q", remote, other)
    write(other, "a.txt", "one\nTHEIRS\nthree\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs")
    git(other, "push", "-q", "origin", "HEAD:main")

    worktree = str(tmp_path / f"{name}-wt")
    git(local, "worktree", "add", "-q", "-b", "feature", worktree, "HEAD")
    write(worktree, "a.txt", "one\nOURS\nthree\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "ours")
    return worktree


def test_a_conflicted_rebase_in_a_linked_worktree_is_named_rebase_not_dirty(tmp_path):
    """`_operation_in_flight` used to resolve `root/".git"` as a directory and
    give up (`return None`) the instant that was false — true for a plain
    checkout, but `.git` is a FILE in a linked worktree, so this path was
    ALWAYS blind there. The consequence: `_mutation_preflight` fell through
    to the generic "dirty" refusal, whose advice (commit, stash, or discard)
    can destroy an in-progress rebase instead of continuing or aborting it.
    Pins that the real gitdir is resolved (`git rev-parse
    --absolute-git-dir`) so a conflicted rebase in a linked worktree is
    named "rebase", exactly as it already is for a plain checkout."""
    worktree = _rebase_conflict_in_linked_worktree(tmp_path)
    assert not os.path.isdir(os.path.join(worktree, ".git")), (
        "fixture assumption: .git must be a FILE in a linked worktree")

    git(worktree, "fetch", "-q", "origin", "main")
    git(worktree, "rebase", "origin/main", check=False)  # conflicts by construction

    res = git_upstream.update_repo(worktree)

    assert res["ok"] is False
    assert res["reason"] == "in-progress"
    assert "rebase" in res["message"]


# --------------------------------------------------- throttle stamp (finding #5)


def test_a_busy_slot_does_not_stamp_the_throttle_for_a_different_repo(tmp_path):
    a = _clone_with_remote_ahead(tmp_path, name="repo-a")
    b = _clone_with_remote_ahead(tmp_path, name="repo-b")
    root_b = os.path.realpath(b)

    # Hold the slot as repo A's check would, mid-flight.
    assert git_upstream._check_slot.acquire(blocking=False)
    try:
        started = git_upstream.note_app_opened(b, _runner=_sync)
        assert not started  # the slot refused the dispatch outright
        # The bug this pins: B must not be marked "just checked" by a
        # dispatch that never ran — it must still be due right away.
        assert git_upstream._due(root_b, git_upstream.time.time())
    finally:
        git_upstream._check_slot.release()


# ------------------------------------------------ stale row on success (finding #9)


def test_a_successful_update_clears_a_stale_row_even_if_the_recheck_fails(tmp_path, monkeypatch):
    local = _clone_with_remote_ahead(tmp_path)
    root = os.path.realpath(local)
    # Seed a stale "behind" entry, as if an earlier background check found
    # this repo behind before the user clicked Update.
    git_upstream._record({"root": root, "branch": "main", "default_branch": "main",
                          "on_default": True, "behind": 2, "checked_at": 0.0})
    assert any(r["root"] == root for r in git_upstream.known_repos())

    # The re-check `update_repo` fires after a successful pull fails (a
    # flaky second fetch, say) — the stale entry must not survive that.
    monkeypatch.setattr(git_upstream, "check_repo", lambda r: None)

    res = git_upstream.update_repo(local)

    assert res["ok"] is True, res
    assert not any(r["root"] == root for r in git_upstream.known_repos())
