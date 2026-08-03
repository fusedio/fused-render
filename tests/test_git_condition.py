"""The `git` template's condition.py gate (SPEC CT-12, §33 / GT-3).

The gate runs on EVERY directory AND every source/config/prose file the user
opens, so what matters as much as the verdict is what the gate is allowed to do
to reach it. Three properties are tested directly:

* **It never enumerates.** `os.listdir`/`os.scandir`/`os.walk`/`glob` are made
  fatal for the whole detection suite, so a listing added later fails here
  rather than shipping (the rule `zarr_aoi/condition.py` documents). Detection
  is `git rev-parse` in one bounded subprocess — not a search of the tree.
* **A mount-backed path is False, always** (GT-4), for the same reason
  `graph/condition.py` refuses one: the reader shells out to git, and git over
  an rclone-NFS mount is the shape that wedges it.
* **It fails closed.** No git binary, a timeout, a non-zero exit, an
  unavailable mount detector, an unreadable path — every one is False.
"""
import contextlib
import importlib.util
import os
import subprocess
from unittest import mock

import pytest

from _git_repo import build_repo, empty_repo, git, git_available, write

CONDITION = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "git", "condition.py")

pytestmark = pytest.mark.skipif(not git_available(), reason="git binary not installed")


def _load():
    spec = importlib.util.spec_from_file_location("git_condition", CONDITION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


@contextlib.contextmanager
def _no_enumeration():
    """Any directory enumeration inside a gate call is a test failure.

    Patched around the CALL, not as an autouse fixture: pytest's tmp_path
    machinery lists directories, so a blanket patch would fail every test's
    setup instead of the thing under test.
    """
    import glob as glob_mod

    def forbidden(*args, **kwargs):
        raise AssertionError("the gate must never enumerate a directory (CT-12)")

    with mock.patch.object(os, "listdir", forbidden), \
            mock.patch.object(os, "scandir", forbidden), \
            mock.patch.object(os, "walk", forbidden), \
            mock.patch.object(glob_mod, "glob", forbidden), \
            mock.patch.object(glob_mod, "iglob", forbidden):
        yield


@pytest.fixture(scope="module")
def gate():
    """The real gate, called with directory enumeration made fatal."""
    main = _load()

    def call(path):
        with _no_enumeration():
            return main(path)

    return call


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return build_repo(str(tmp_path_factory.mktemp("gated-repo")))


# ------------------------------------------------------------------- it detects


def test_the_repository_root_is_offered(gate, repo):
    assert gate(repo) is True


def test_a_subdirectory_with_no_dot_git_of_its_own_is_offered(gate, repo):
    # The case a `.git` stat probe alone cannot answer, and the reason the CLI is
    # the authority: nothing named `.git` exists at this level.
    nested = os.path.join(repo, "pkg")
    assert not os.path.exists(os.path.join(nested, ".git"))
    assert gate(nested) is True


def test_a_tracked_file_is_offered(gate, repo):
    # File modes gate on the file itself; the cwd is its parent directory.
    assert gate(os.path.join(repo, "pkg", "core.py")) is True


def test_an_untracked_file_inside_the_repo_is_offered(gate, repo):
    # "In a repository" is the question, not "tracked" — the view has a useful
    # answer for an untracked file too (it shows up as `??`).
    assert gate(os.path.join(repo, "pkg", "fresh.txt")) is True


def test_a_repository_with_no_commits_is_still_offered(gate, tmp_path):
    # An initialized-but-empty repo IS a repo, and the view has a real empty
    # state for it (GT-9) — refusing here would hide the mode on a fresh project.
    assert gate(empty_repo(str(tmp_path / "fresh"))) is True


def test_a_worktree_whose_dot_git_is_a_FILE_is_offered(gate, repo, tmp_path):
    # A linked worktree (and a submodule) has a `.git` *file*, not a directory —
    # the other shape a naive stat probe gets wrong.
    linked = str(tmp_path / "linked")
    git(repo, "worktree", "add", "-q", "-b", "side", linked)
    assert os.path.isfile(os.path.join(linked, ".git"))
    assert gate(linked) is True


# ------------------------------------------------------------------- it refuses


def test_a_plain_directory_is_not_offered(gate, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert gate(str(plain)) is False


def test_a_file_outside_any_repository_is_not_offered(gate, tmp_path):
    loose = tmp_path / "loose.py"
    loose.write_text("x = 1\n", encoding="utf-8")
    assert gate(str(loose)) is False


def test_a_missing_path_is_not_offered(gate, tmp_path):
    assert gate(str(tmp_path / "nope")) is False
    assert gate(str(tmp_path / "nope" / "deeper" / "file.py")) is False


def test_the_git_directory_itself_is_not_offered(gate, repo):
    # Inside `.git` there is no work tree, so there is no history to scope to a
    # path — `--is-inside-work-tree` says false and so do we.
    assert gate(os.path.join(repo, ".git")) is False


def test_a_bare_repository_is_not_offered(gate, tmp_path):
    # No work tree means no `git status`, so the view's whole top half is
    # meaningless; not offered rather than offered-then-broken.
    bare = str(tmp_path / "bare.git")
    os.makedirs(bare)
    git(bare, "init", "-q", "--bare")
    assert gate(bare) is False


def test_an_empty_path_is_not_offered(gate):
    assert gate("") is False


# ----------------------------------------------------------------- mount refusal


def test_a_mount_backed_path_is_never_offered(gate, repo, monkeypatch):
    assert gate(repo) is True  # the same repo, before it looks mount-backed
    # The env contract the app exports (FUSED_RENDER_MOUNTS_DIR) — how the gate
    # learns the mounts root without importing fused_render (SPEC PY-15).
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", repo)
    assert gate(repo) is False


def test_an_unavailable_mount_detector_fails_closed(gate, repo, monkeypatch):
    # "Cannot tell" reads as "refuse": the gate exists to keep a subprocess off
    # a mount, and a guess is not good enough for that.
    import builtins
    import sys

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "appenv", raising=False)
    assert gate(repo) is False


def test_the_mount_check_precedes_the_subprocess(gate, repo, monkeypatch):
    # Ordering matters, not just the verdict: a refusal that still forked git at
    # the mount would have already paid the cost the refusal exists to avoid.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", repo)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git was invoked on a mount-backed path")))
    assert gate(repo) is False


# ------------------------------------------------------------------ fails closed


def test_a_missing_git_binary_fails_closed(gate, repo, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert gate(repo) is False


def test_a_timeout_fails_closed(gate, repo, monkeypatch):
    def hangs(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", hangs)
    assert gate(repo) is False


def test_the_subprocess_carries_a_timeout(gate, repo, monkeypatch):
    # A gate with no timeout is a gate that can hang the stat pipeline; assert
    # the bound is passed rather than trusting it stays there.
    seen = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert gate(repo) is True
    assert isinstance(seen.get("timeout"), (int, float)) and 0 < seen["timeout"] <= 5


def test_a_nonzero_exit_fails_closed(gate, repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=a[0] if a else [], returncode=128, stdout=b"", stderr=b"fatal: nope"))
    assert gate(repo) is False


def test_unexpected_stdout_fails_closed(gate, repo, monkeypatch):
    # Exit 0 but not the literal answer we asked for: refuse rather than guess.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=a[0] if a else [], returncode=0, stdout=b"maybe\n", stderr=b""))
    assert gate(repo) is False


def test_a_stat_error_fails_closed(gate, repo, monkeypatch):
    monkeypatch.setattr(os.path, "isdir",
                        lambda p: (_ for _ in ()).throw(OSError("boom")))
    assert gate(repo) is False


# -------------------------------------------------------------- non-interactive


def test_git_is_invoked_non_interactively(gate, repo, monkeypatch):
    # A gate that can block on a credential or GPG prompt is a gate that hangs
    # the shell. The env hardening is part of the contract, so it is asserted.
    seen = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert gate(repo) is True
    env = seen["kwargs"].get("env") or {}
    assert seen["argv"][0] == "git"
    assert "--no-pager" in seen["argv"]
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env.get("GIT_OPTIONAL_LOCKS") == "0"
    # stdin must never be the caller's: a prompt that slips through has to hit
    # EOF instead of waiting on a terminal.
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL


def test_untracked_file_in_a_repo_that_git_cannot_read_fails_closed(gate, tmp_path):
    # A path whose parent is a FILE (a stale/typo'd path) must not blow up.
    parent = tmp_path / "notadir"
    parent.write_text("x", encoding="utf-8")
    assert gate(str(parent / "child.py")) is False


def test_a_file_in_a_repo_subdirectory_resolves_from_its_parent(gate, repo):
    # Regression guard for the file case: passing the FILE as `cwd` to git is an
    # ENOTDIR, so the gate must use its parent.
    write(repo, "pkg/deep/leaf.txt", "leaf\n")
    assert gate(os.path.join(repo, "pkg", "deep", "leaf.txt")) is True
