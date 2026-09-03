"""Regression tests for the community marketplace backend
(fused_render/community.py):

- `refresh` clones the community repo into the workspace's showcase folder
  and serves the catalog from it, and never fetches or merges again once that
  clone exists. The clone is shallow but fully checked out — every app's
  files on disk, no materialize step.
- a pre-existing showcase folder that is not our clone is never deleted —
  refresh refuses with a friendly error instead.
- `_cache_lock` is a real cross-process lock: a call that can't acquire it
  within the timeout fails loudly instead of racing the holder.
"""
import json
import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import bare_repo, git, git_available, write  # noqa: E402


@pytest.fixture()
def community_mod(tmp_path, monkeypatch):
    from fused_render import community as mod

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(mod, "STATE_DIR", str(state))
    monkeypatch.setattr(mod, "WORKSPACE", str(workspace))
    monkeypatch.setattr(mod, "SHOWCASE_DIR", str(workspace / "showcase"))
    monkeypatch.setattr(mod, "LOCK_PATH", str(state / ".lock"))
    return mod


def _write_metadata(mod, slug, meta):
    folder = os.path.join(mod.SHOWCASE_DIR, slug)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _make_remote(tmp_path, apps):
    """A bare remote seeded with one folder per app slug, each carrying its
    own metadata.json (the catalog source since index.json was dropped)."""
    remote = str(tmp_path / "remote.git")
    seed = str(tmp_path / "seed")
    os.makedirs(seed)
    git(seed, "init", "-q")
    for a in apps:
        meta = {k: v for k, v in a.items() if k != "slug"}
        write(seed, os.path.join(a["slug"], "metadata.json"), json.dumps(meta))
        write(seed, os.path.join(a["slug"], "index.html"), "<html></html>")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    bare_repo(remote)
    git(seed, "remote", "add", "origin", remote)
    git(seed, "push", "-q", "-u", "origin", "HEAD")
    return remote


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_full_clones_into_workspace_showcase(tmp_path, community_mod, monkeypatch):
    mod = community_mod
    remote = _make_remote(tmp_path, [{"slug": "widget", "name": "Widget"}])
    monkeypatch.setattr(mod, "REPO_URL", remote)

    res = mod.main(action="refresh")

    assert res["status"] == "ok"
    assert res["cache_root"] == mod.SHOWCASE_DIR
    assert [a["slug"] for a in res["apps"]] == ["widget"]
    # Fully checked out (shallow history, full tree): the app's files are on
    # disk immediately, no materialize step.
    assert os.path.isfile(os.path.join(mod.SHOWCASE_DIR, "widget", "index.html"))
    # No staging droppings left in the workspace.
    assert not [n for n in os.listdir(mod.WORKSPACE) if n.startswith(".showcase-clone-")]


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_never_fetches_or_merges_once_the_clone_exists(
        tmp_path, community_mod, monkeypatch):
    """Once the showcase clone exists, refresh is a no-op that just serves
    the catalog — no fetch, no merge, ever again after the first clone. An
    edit sitting in the tree (however it got there) is never touched, and no
    upstream commit — however conflicting — is ever pulled in."""
    mod = community_mod
    remote = _make_remote(tmp_path, [{"slug": "widget", "name": "Widget"}])
    monkeypatch.setattr(mod, "REPO_URL", remote)
    assert mod.main(action="refresh")["status"] == "ok"

    edited = os.path.join(mod.SHOWCASE_DIR, "widget", "index.html")
    with open(edited, "w", encoding="utf-8") as f:
        f.write("MY EDIT\n")
    seed = str(tmp_path / "seed")
    write(seed, os.path.join("widget", "index.html"), "<html>v2</html>")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "upstream change")
    git(seed, "push", "-q", "origin", "HEAD")

    calls = []
    real_git = mod._git

    def spy(cwd, *args, **kwargs):
        calls.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(mod, "_git", spy)

    res = mod.main(action="refresh")

    assert res["status"] == "ok"
    with open(edited, encoding="utf-8") as f:
        assert f.read() == "MY EDIT\n"  # untouched — no merge ran
    assert not any(a and a[0] in ("fetch", "merge") for a in calls)


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_refuses_foreign_git_repo_at_showcase_path(tmp_path, community_mod, monkeypatch):
    mod = community_mod
    monkeypatch.setattr(mod, "REPO_URL", str(tmp_path / "remote.git"))
    # A git repo the user put at <workspace>/showcase themselves, tracking a
    # DIFFERENT remote: refresh must not fetch it, ff it, or yank its locks.
    theirs = mod.SHOWCASE_DIR
    os.makedirs(theirs)
    git(theirs, "init", "-q")
    git(theirs, "remote", "add", "origin", "https://example.com/other.git")
    write(theirs, "mine.txt", "keep me\n")
    git(theirs, "add", "-A")
    git(theirs, "commit", "-q", "-m", "theirs")
    stale_lock = os.path.join(theirs, ".git", "index.lock")
    with open(stale_lock, "w", encoding="utf-8"):
        pass

    res = mod.main(action="refresh")

    assert res["status"] == "error"
    assert "not the showcase clone" in res["message"]
    assert os.path.isfile(os.path.join(theirs, "mine.txt"))
    assert os.path.isfile(stale_lock)  # their in-flight git op untouched


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_sweeps_leftover_staging_dirs(tmp_path, community_mod, monkeypatch):
    mod = community_mod
    remote = _make_remote(tmp_path, [{"slug": "widget", "name": "Widget"}])
    monkeypatch.setattr(mod, "REPO_URL", remote)
    # An interrupted first clone (app quit mid-clone) leaves its staging dir
    # behind; the next refresh must sweep it instead of accumulating copies.
    leftover = os.path.join(mod.WORKSPACE, ".showcase-clone-dead")
    os.makedirs(os.path.join(leftover, "showcase"))

    res = mod.main(action="refresh")

    assert res["status"] == "ok"
    assert not os.path.exists(leftover)
    assert not [n for n in os.listdir(mod.WORKSPACE) if n.startswith(".showcase-clone-")]


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_leaves_git_locks_alone(tmp_path, community_mod, monkeypatch):
    mod = community_mod
    remote = _make_remote(tmp_path, [{"slug": "widget", "name": "Widget"}])
    monkeypatch.setattr(mod, "REPO_URL", remote)
    assert mod.main(action="refresh")["status"] == "ok"

    # The showcase clone is the user's tree, and refresh never fetches or
    # merges once the clone exists — so it must never touch a lockfile
    # either, fresh or ancient. (No fetch/merge means no lock contention to
    # clean up in the first place.)
    git_dir = os.path.join(mod.SHOWCASE_DIR, ".git")
    fresh = os.path.join(git_dir, "index.lock")
    with open(fresh, "w", encoding="utf-8"):
        pass
    old = os.path.join(git_dir, "HEAD.lock")
    with open(old, "w", encoding="utf-8"):
        pass
    ancient = time.time() - 3600 - 60
    os.utime(old, (ancient, ancient))

    mod.main(action="refresh")

    assert os.path.isfile(fresh)
    assert os.path.isfile(old)


@pytest.mark.skipif(sys.platform == "win32", reason="posix flock path")
def test_cache_lock_times_out_instead_of_racing(community_mod, monkeypatch):
    import fcntl

    mod = community_mod
    monkeypatch.setattr(mod, "LOCK_TIMEOUT", 0.3)
    os.makedirs(mod.STATE_DIR, exist_ok=True)
    fd = os.open(mod.LOCK_PATH, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(mod.ActionError):
            with mod._cache_lock():
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# -------------------------------------------------- normalized origin (finding #11)


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_cache_ready_tolerates_an_equivalent_but_differently_spelled_origin(
        tmp_path, community_mod, monkeypatch):
    """A genuine clone of REPO_URL, whose origin is merely spelled
    differently (an ssh remote against an https REPO_URL, a trailing
    `.git`) must still read as ready — an exact string match reported it
    `no-cache` forever, and `_refresh` then refused it outright on every
    visit ("exists but is not the showcase clone")."""
    mod = community_mod
    monkeypatch.setattr(mod, "REPO_URL", "https://github.com/fusedio/fused-render-community-apps.git")
    os.makedirs(mod.SHOWCASE_DIR)
    git(mod.SHOWCASE_DIR, "init", "-q")
    # Same repo, spelled as ssh + no trailing .git.
    git(mod.SHOWCASE_DIR, "remote", "add", "origin",
        "git@github.com:fusedio/fused-render-community-apps")

    assert mod._cache_ready()

    res = mod.main(action="refresh")
    assert res["status"] == "ok"


def test_normalize_git_url_treats_scp_and_https_and_dotgit_as_equal(community_mod):
    mod = community_mod
    forms = [
        "https://github.com/fusedio/fused-render-community-apps.git",
        "https://github.com/fusedio/fused-render-community-apps",
        "https://github.com/fusedio/fused-render-community-apps/",
        "git@github.com:fusedio/fused-render-community-apps.git",
        "git@github.com:fusedio/fused-render-community-apps",
        "ssh://git@github.com/fusedio/fused-render-community-apps.git",
        "https://GITHUB.com/fusedio/fused-render-community-apps.git",
    ]
    normalized = {mod._normalize_git_url(f) for f in forms}
    assert len(normalized) == 1, normalized


def test_normalize_git_url_still_tells_different_repos_apart(community_mod):
    mod = community_mod
    a = mod._normalize_git_url("https://github.com/fusedio/fused-render-community-apps.git")
    b = mod._normalize_git_url("https://github.com/someone-else/unrelated-repo.git")
    assert a != b
