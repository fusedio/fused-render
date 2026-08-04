"""Tests for the git-repo-root probe behind /api/fs/git-repo.

The Compress submenu offers the two git formats only when the right-clicked
folder is the WORK-TREE ROOT of a repository — not merely somewhere inside
one — so the probe is stricter than `rev-parse --is-inside-work-tree`. These
drive the module-level helpers directly (like test_server_fs_mutate.py):
`_is_repo_root` in server/gitignore.py and the route's `_git_repo_payload`.
"""
import json
import os
import subprocess

import pytest
from fastapi.responses import JSONResponse

from fused_render.server.gitignore import _is_repo_root
from fused_render.server.routers.fs_read import _git_repo_payload


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


# Isolated from the developer's ~/.gitconfig so a stray `init.defaultBranch`
# or template dir can't change what these repos look like.
_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-C", str(cwd), *args],
        env=_ENV, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def make_repo(root, *, commit=True):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    if commit:
        (root / "a.txt").write_text("hello\n")
        git(root, "add", "a.txt")
        git(root, "commit", "-q", "-m", "first")
    return root


# ------------------------------------------------------------- _is_repo_root

def test_repo_root_is_a_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    assert _is_repo_root(str(repo)) is True


def test_repo_without_commits_is_still_a_root(tmp_path):
    # `git init` with nothing committed still has a work tree, so the folder
    # IS a repo root; the "no commits" refusal belongs to compress, not here.
    repo = make_repo(tmp_path / "empty", commit=False)
    assert _is_repo_root(str(repo)) is True


def test_subdirectory_of_a_repo_is_not_a_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert _is_repo_root(str(sub)) is False


def test_plain_folder_is_not_a_root(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert _is_repo_root(str(d)) is False


def test_bare_repo_is_not_a_root(tmp_path):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    assert _is_repo_root(str(bare)) is False


def test_dot_git_directory_itself_is_not_a_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    assert _is_repo_root(str(repo / ".git")) is False


def test_linked_worktree_root_is_a_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    linked = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(linked))
    assert _is_repo_root(str(linked)) is True


def test_symlinked_repo_path_is_still_a_root(tmp_path):
    # A checkout reached through a symlink (or macOS's /var -> /private/var)
    # must not read as "not a root" just because the strings differ.
    repo = make_repo(tmp_path / "repo")
    link = tmp_path / "link"
    try:
        os.symlink(str(repo), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert _is_repo_root(str(link)) is True


def test_missing_path_is_not_a_root(tmp_path):
    assert _is_repo_root(str(tmp_path / "nope")) is False


def test_file_is_not_a_root(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    assert _is_repo_root(str(f)) is False


# --------------------------------------------------------- _git_repo_payload

def test_payload_reports_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    out = _data(_git_repo_payload(str(repo)))
    assert out == {"path": str(repo), "is_repo_root": True}


def test_payload_reports_non_root(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert _data(_git_repo_payload(str(d)))["is_repo_root"] is False


def test_payload_relative_path_400(tmp_path):
    resp = _git_repo_payload("relative/dir")
    assert isinstance(resp, JSONResponse) and resp.status_code == 400


def test_payload_never_probes_a_mount_backed_path(tmp_path, monkeypatch):
    # A mount path must answer False WITHOUT shelling out to git (which would
    # stat/list across the mount — the known mount-killer).
    from fused_render.server.routers import fs_read as mod
    from fused_render.shell import mounts as shell_mounts

    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: True)
    calls = []
    monkeypatch.setattr(mod, "_is_repo_root", lambda p: calls.append(p) or True)
    out = _data(_git_repo_payload(str(tmp_path / "mnt" / "repo")))
    assert out["is_repo_root"] is False
    assert calls == []
