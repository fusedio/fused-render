"""Regression tests for the community marketplace backend
(fused_render/community.py):

- force-update no longer discards local edits when the "Local edits before
  community update" snapshot commit itself fails (a pre-commit hook, in this
  suite) — it aborts with an error instead of running `_replace_contents`.
- `refresh` performs a FULL clone of the community repo into the workspace's
  showcase folder and serves the catalog from it.
- a pre-existing showcase folder that is not our clone is never deleted —
  refresh refuses with a friendly error instead.
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
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(mod, "STATE_DIR", str(state))
    monkeypatch.setattr(mod, "WORKSPACE", str(workspace))
    monkeypatch.setattr(mod, "SHOWCASE_DIR", str(workspace / "showcase"))
    monkeypatch.setattr(mod, "COMMUNITY_TAG_DIR", str(workspace / "local"))
    monkeypatch.setattr(mod, "INSTALLS_JSON", str(state / "installs.json"))
    monkeypatch.setattr(mod, "OPENED_JSON", str(state / "opened.json"))
    monkeypatch.setattr(mod, "LOCK_PATH", str(state / ".lock"))
    return mod


def _write_installs(mod, installs):
    os.makedirs(mod.STATE_DIR, exist_ok=True)
    with open(mod.INSTALLS_JSON, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "installs": installs}, f)


def _write_index(mod, apps, commit=None):
    os.makedirs(mod.SHOWCASE_DIR, exist_ok=True)
    with open(os.path.join(mod.SHOWCASE_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"apps": apps, "commit": commit}, f)


def _make_remote(tmp_path, apps):
    """A bare remote seeded with index.json + one folder per app slug."""
    remote = str(tmp_path / "remote.git")
    seed = str(tmp_path / "seed")
    os.makedirs(seed)
    git(seed, "init", "-q")
    write(seed, "index.json", json.dumps({"apps": apps, "commit": "abc"}))
    for a in apps:
        write(seed, os.path.join(a["slug"], "index.html"), "<html></html>")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    bare_repo(remote)
    git(seed, "remote", "add", "origin", remote)
    git(seed, "push", "-q", "-u", "origin", "HEAD")
    return remote


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
    showcase_slug = os.path.join(mod.SHOWCASE_DIR, "widget")
    os.makedirs(showcase_slug, exist_ok=True)
    with open(os.path.join(showcase_slug, "app.py"), "w", encoding="utf-8") as f:
        f.write("new upstream content\n")
    os.makedirs(os.path.join(mod.SHOWCASE_DIR, ".git"), exist_ok=True)  # _cache_ready()
    _write_index(mod, [{"slug": "widget", "name": "Widget", "commit": "new-sha"}])

    res = mod.main(action="update", slug="widget", force=True)

    assert res["status"] == "error"
    with open(os.path.join(app_dir, "app.py"), encoding="utf-8") as f:
        assert f.read() == "MY EDIT\n"  # never overwritten by _replace_contents
    assert git(app_dir, "status", "--porcelain").strip()  # still dirty, not silently committed


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_full_clones_into_workspace_showcase(tmp_path, community_mod, monkeypatch):
    mod = community_mod
    remote = _make_remote(tmp_path, [{"slug": "widget", "name": "Widget"}])
    monkeypatch.setattr(mod, "REPO_URL", remote)

    res = mod.main(action="refresh")

    assert res["status"] == "ok"
    assert res["cache_root"] == mod.SHOWCASE_DIR
    assert [a["slug"] for a in res["apps"]] == ["widget"]
    # Full clone: the app's files are on disk immediately, no materialize step.
    assert os.path.isfile(os.path.join(mod.SHOWCASE_DIR, "widget", "index.html"))
    # No staging droppings left in the workspace.
    assert not [n for n in os.listdir(mod.WORKSPACE) if n.startswith(".showcase-clone-")]


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_keeps_local_edits(tmp_path, community_mod, monkeypatch):
    mod = community_mod
    remote = _make_remote(tmp_path, [{"slug": "widget", "name": "Widget"}])
    monkeypatch.setattr(mod, "REPO_URL", remote)
    assert mod.main(action="refresh")["status"] == "ok"

    # The showcase tree is the user's: an edit must survive the next refresh.
    edited = os.path.join(mod.SHOWCASE_DIR, "widget", "index.html")
    with open(edited, "w", encoding="utf-8") as f:
        f.write("MY EDIT\n")

    res = mod.main(action="refresh")
    assert res["status"] == "ok"
    with open(edited, encoding="utf-8") as f:
        assert f.read() == "MY EDIT\n"


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_refresh_keeps_conflicting_local_edits_when_upstream_moved(
        tmp_path, community_mod, monkeypatch):
    mod = community_mod
    remote = _make_remote(tmp_path, [{"slug": "widget", "name": "Widget"}])
    monkeypatch.setattr(mod, "REPO_URL", remote)
    assert mod.main(action="refresh")["status"] == "ok"

    # Local edit AND an upstream commit touching the same file: the ff-only
    # merge can't apply, and the local tree must survive untouched.
    edited = os.path.join(mod.SHOWCASE_DIR, "widget", "index.html")
    with open(edited, "w", encoding="utf-8") as f:
        f.write("MY EDIT\n")
    seed = str(tmp_path / "seed")
    write(seed, os.path.join("widget", "index.html"), "<html>v2</html>")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "upstream change")
    git(seed, "push", "-q", "origin", "HEAD")

    res = mod.main(action="refresh")

    assert res["status"] == "ok"  # catalog still serves
    with open(edited, encoding="utf-8") as f:
        assert f.read() == "MY EDIT\n"


def test_refresh_refuses_foreign_showcase_folder(community_mod):
    mod = community_mod
    # A showcase folder the user made themselves (no .git) must never be
    # deleted or cloned over.
    os.makedirs(mod.SHOWCASE_DIR)
    marker = os.path.join(mod.SHOWCASE_DIR, "precious.txt")
    with open(marker, "w", encoding="utf-8") as f:
        f.write("keep me\n")

    res = mod.main(action="refresh")

    assert res["status"] == "error"
    assert "not the showcase clone" in res["message"]
    assert os.path.isfile(marker)


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
