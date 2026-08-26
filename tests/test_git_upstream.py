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


# ------------------------------------------------------------- update / rebase


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


def test_rebase_replays_local_commits_onto_the_default_branch(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    write(local, "c.txt", "mine\n")
    git(local, "add", "-A")
    git(local, "commit", "-q", "-m", "local work")

    res = git_upstream.rebase_repo(local)

    assert res["ok"] is True, res
    subjects = git(local, "log", "--format=%s").splitlines()
    assert subjects[:2] == ["local work", "c3"]


def test_rebase_refuses_a_dirty_tree(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    write(local, "c.txt", "uncommitted\n")

    res = git_upstream.rebase_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "dirty"


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


# --------------------------------------------- untracked files (finding #3)


def test_update_is_not_refused_by_an_unrelated_untracked_file(tmp_path):
    local = _clone_with_remote_ahead(tmp_path)
    write(local, "scratch.tmp", "not tracked, not touched by the pull\n")

    res = git_upstream.update_repo(local)

    assert res["ok"] is True, res
    with open(os.path.join(local, "scratch.tmp"), encoding="utf-8") as f:
        assert f.read() == "not tracked, not touched by the pull\n"


def test_rebase_is_still_refused_by_an_untracked_file(tmp_path):
    # rebase_repo keeps the stricter, untracked-inclusive check on purpose
    # (module docstring / _is_clean's own doc) — an untracked file colliding
    # with a path one of the replayed commits touches is a real way to lose
    # it, so this stays conservative rather than matching update_repo.
    local = _clone_with_remote_ahead(tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    write(local, "scratch.tmp", "untracked\n")

    res = git_upstream.rebase_repo(local)

    assert res["ok"] is False
    assert res["reason"] == "dirty"


# ---------------------------------------- mid-operation preflight (finding #4)


def _rebase_conflict_repo(tmp_path, name="conflict"):
    """A repo whose current branch, rebased onto `origin/main`, genuinely
    conflicts on one line of `a.txt` — the same shape
    tests/test_git_conflicts.py's `rebase_conflict_repo` builds for the
    template-side twin of this op."""
    remote = str(tmp_path / f"{name}.git")
    local = str(tmp_path / name)
    os.makedirs(local)
    git(local, "init", "-q")
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

    first = git_upstream.rebase_repo(local)
    assert first["ok"] is False
    assert first["reason"] == "git-failed"  # the conflict itself
    assert os.path.isdir(os.path.join(local, ".git", "rebase-merge")) or \
        os.path.isdir(os.path.join(local, ".git", "rebase-apply"))

    res = git_upstream.update_repo(local)

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
