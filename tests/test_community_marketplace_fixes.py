"""Regression tests for a Cursor Bugbot review pass on the community
marketplace backend (fused_render/community.py):

- force-update no longer discards local edits when the "Local edits before
  community update" snapshot commit itself fails (a pre-commit hook, in this
  suite) — it aborts with an error instead of running `_replace_contents`.
- a yanked-upstream but still-installed app's `detail` skips `_materialize`
  entirely (the slug is gone from the cache repo's tree, so it would always
  fail) so Open/Uninstall keep working from the detail page.
- a failed `sparse-checkout set`/`checkout` right after a fresh clone leaves
  no half-set-up `.git` behind — a later refresh re-clones instead of taking
  the fetch/merge branch against a cache with no `index.json`.
- `_cache_lock` is a real cross-process lock: a call that can't acquire it
  within the timeout fails loudly instead of racing the holder.
"""
import json
import os
import stat
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import bare_repo, git, git_available, write  # noqa: E402


@pytest.fixture()
def community_mod(tmp_path, monkeypatch):
    from fused_render import community as mod

    state = tmp_path / "state"
    cache = state / "repo"
    monkeypatch.setattr(mod, "STATE_DIR", str(state))
    monkeypatch.setattr(mod, "CACHE_REPO", str(cache))
    monkeypatch.setattr(mod, "INSTALLS_JSON", str(state / "installs.json"))
    monkeypatch.setattr(mod, "OPENED_JSON", str(state / "opened.json"))
    monkeypatch.setattr(mod, "LOCK_PATH", str(state / ".lock"))
    return mod


def _write_installs(mod, installs):
    os.makedirs(mod.STATE_DIR, exist_ok=True)
    with open(mod.INSTALLS_JSON, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "installs": installs}, f)


def _write_index(mod, apps, commit=None):
    os.makedirs(mod.CACHE_REPO, exist_ok=True)
    with open(os.path.join(mod.CACHE_REPO, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"apps": apps, "commit": commit}, f)


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_force_update_aborts_when_snapshot_commit_fails(tmp_path, community_mod):
    mod = community_mod
    app_dir = str(tmp_path / "installed" / "widget")
    os.makedirs(app_dir)
    git(app_dir, "init", "-q")
    write(app_dir, "app.py", "old\n")
    git(app_dir, "add", "-A")
    git(app_dir, "commit", "-q", "-m", "Install from community")
    local_commit = git(app_dir, "rev-parse", "HEAD").strip()

    # A hook that always refuses the commit — stands in for gpgsign/lock
    # contention/anything else that can fail "Local edits before update".
    hooks = os.path.join(app_dir, ".git", "hooks")
    os.makedirs(hooks, exist_ok=True)
    hook_path = os.path.join(hooks, "pre-commit")
    with open(hook_path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook_path, os.stat(hook_path).st_mode | stat.S_IEXEC)

    # User edit, never committed — this is what must survive.
    write(app_dir, "app.py", "MY EDIT\n")

    _write_installs(mod, {
        "widget": {"path": app_dir, "commit": "old-sha", "local_commit": local_commit,
                    "version": "1", "installed_at": "2026-01-01T00:00:00Z"},
    })
    cache_slug = os.path.join(mod.CACHE_REPO, "widget")
    os.makedirs(cache_slug, exist_ok=True)
    with open(os.path.join(cache_slug, "app.py"), "w", encoding="utf-8") as f:
        f.write("new upstream content\n")
    os.makedirs(os.path.join(mod.CACHE_REPO, ".git"), exist_ok=True)  # _cache_ready()
    _write_index(mod, [{"slug": "widget", "name": "Widget", "commit": "new-sha"}])

    res = mod.main(action="update", slug="widget", force=True)

    assert res["status"] == "error"
    with open(os.path.join(app_dir, "app.py"), encoding="utf-8") as f:
        assert f.read() == "MY EDIT\n"  # never overwritten by _replace_contents
    assert git(app_dir, "status", "--porcelain").strip()  # still dirty, not silently committed


def test_detail_skips_materialize_for_yanked_app(tmp_path, community_mod):
    mod = community_mod
    app_dir = str(tmp_path / "installed" / "gone")
    os.makedirs(app_dir)  # _install_state() only counts it installed if the folder is still there
    _write_installs(mod, {
        "gone": {"path": app_dir, "commit": "sha", "local_commit": "sha",
                  "version": "1", "installed_at": "2026-01-01T00:00:00Z"},
    })
    # No index.json at all — _cache_ready()/_materialize would raise if this
    # path ever reached them (no CACHE_REPO/.git either).
    d = mod.main(action="detail", slug="gone")

    assert d.get("status") != "error"
    assert d["yanked"] is True
    assert d["folder"] is None
    assert d["preview_entry"] is None
    assert d["installed"] is True


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_cleans_up_half_set_up_clone(tmp_path, community_mod, monkeypatch):
    mod = community_mod
    remote = str(tmp_path / "remote.git")
    seed = str(tmp_path / "seed")
    os.makedirs(seed)
    git(seed, "init", "-q")
    write(seed, "index.json", json.dumps({"apps": [], "commit": "abc"}))
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    bare_repo(remote)
    git(seed, "remote", "add", "origin", remote)
    git(seed, "push", "-q", "-u", "origin", "HEAD")
    monkeypatch.setattr(mod, "REPO_URL", remote)

    real_git_ok = mod._git_ok

    def flaky_git_ok(cwd, *args, **kw):
        if args and args[0] == "checkout":
            raise mod.ActionError("simulated checkout failure")
        return real_git_ok(cwd, *args, **kw)

    monkeypatch.setattr(mod, "_git_ok", flaky_git_ok)

    res = mod.main(action="refresh")

    assert res["status"] == "error"
    assert not os.path.exists(mod.CACHE_REPO), (
        "a half-set-up clone must not survive a failed post-clone setup step")


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
