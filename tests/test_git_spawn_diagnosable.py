"""A git subprocess that cannot be RUN must leave evidence in the log.

Every git call site in the app fails closed on purpose: `_is_repo_root` returns
False, `_git_ignored` returns "nothing ignored", the check-ignore oracle marks
itself broken, and the `git` template gate returns False. That posture is
deliberate and stays exactly as it is.

What was wrong is that it was also SILENT, and it conflated two very different
answers:

  * "git ran and said no"  — the ordinary negative, correctly quiet.
  * "git could not be run" — a spawn failure (no binary, EMFILE/EAGAIN under
    process-and-fd pressure) or a timeout. This one disables every git-backed
    feature in the app at once, for every repository, and produced NOTHING in
    the server log: `/api/fs/conditions` answered `"git": false` with no error
    key, `/api/fs/git-repo` answered `is_repo_root: false` for a real root, and
    `/api/fs/list` reported `.git` as not ignored — all indistinguishable from
    "not a repo". Diagnosing it took a full investigation because the log was
    empty.

These tests pin the second case to a WARNING. They do NOT relax the fail-closed
return values, which are asserted here too.
"""
import subprocess

import pytest

from fused_render.server import gitignore


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The warning is throttled per process; each test needs a clean slate."""
    gitignore._reset_spawn_failure_throttle()
    yield
    gitignore._reset_spawn_failure_throttle()


def _raise(exc):
    def _run(*a, **k):
        raise exc
    return _run


@pytest.mark.parametrize("exc", [
    FileNotFoundError(2, "No such file or directory: 'git'"),
    OSError(24, "Too many open files"),
    OSError(35, "Resource temporarily unavailable"),
    subprocess.TimeoutExpired("git", 5),
])
def test_repo_toplevel_logs_when_git_cannot_run(monkeypatch, caplog, tmp_path, exc):
    monkeypatch.setattr(gitignore.subprocess, "run", _raise(exc))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._repo_toplevel(str(tmp_path)) is None      # still fails closed
        assert gitignore._is_repo_root(str(tmp_path)) is False      # still fails closed
    assert any("git" in r.message for r in caplog.records), \
        "a git subprocess that could not run left no trace in the log"


def test_git_ignored_logs_when_git_cannot_run(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(gitignore.subprocess, "run",
                        _raise(OSError(24, "Too many open files")))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._git_ignored(str(tmp_path), [".git", "build"]) == set()
    assert caplog.records, "a check-ignore spawn failure left no trace in the log"


def test_oracle_logs_when_git_cannot_run(monkeypatch, caplog, tmp_path):
    (tmp_path / ".git").mkdir()  # look like a real repo so no empty-GIT_DIR graft
    monkeypatch.setattr(gitignore.subprocess, "Popen",
                        _raise(OSError(35, "Resource temporarily unavailable")))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        oracle = gitignore._IgnoreOracle(str(tmp_path))
    assert oracle.broken is True                                     # fails closed
    assert oracle.ignored(["build"]) == set()
    assert caplog.records, "an oracle spawn failure left no trace in the log"


def test_ordinary_negative_stays_quiet(caplog, tmp_path):
    """A directory that is genuinely not a repo must not warn — otherwise the
    log fills up on every folder the user opens and the real signal is lost."""
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        gitignore._is_repo_root(str(tmp_path))
    assert not caplog.records


def test_warning_is_throttled(monkeypatch, caplog, tmp_path):
    """The gate runs on every directory the user opens, so a broken git must not
    emit one warning per stat."""
    monkeypatch.setattr(gitignore.subprocess, "run",
                        _raise(OSError(24, "Too many open files")))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        for _ in range(50):
            gitignore._repo_toplevel(str(tmp_path))
    assert len(caplog.records) == 1, "the git-spawn warning is not throttled"
