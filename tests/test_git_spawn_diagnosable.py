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
import os
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


# ---------------------------------------------------------------- git RAN and
# ---------------------------------------------------------------- said no
#
# The second, harder half — and the one that let a real investigation reach the
# wrong conclusion. A `git` that is spawned fine and answers in the NEGATIVE is
# indistinguishable from "not a repository", and every call site here threw
# git's stderr away (`stderr=DEVNULL`), so there was nothing to read. The
# absence of a "could not be run" warning was then taken as proof that git was
# healthy, when in fact git had run and refused.
#
# Two negative shapes have to be visible, because they have different causes:
#
#   * a non-zero exit whose stderr is NOT the ordinary "not a git repository" —
#     `detected dubious ownership`, a bad config, an unreadable object store, a
#     cwd that has been deleted under the process.
#   * exit ZERO with a negative ANSWER — which is what a polluted `GIT_DIR` /
#     `GIT_WORK_TREE` / `GIT_CEILING_DIRECTORIES` in the server process
#     produces. No stderr at all, so stderr capture alone would not explain it;
#     the inherited git environment has to be in the report.
#
# What must stay silent is the ordinary negative: a folder that is genuinely not
# a repository, which is most folders a user opens.


class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _returns(proc):
    def _run(*a, **k):
        return proc
    return _run


def test_abnormal_nonzero_exit_is_reported_with_gits_own_words(
        monkeypatch, caplog, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(gitignore.subprocess, "run", _returns(_Proc(
        returncode=128,
        stderr=b"fatal: detected dubious ownership in repository at '/x'\n")))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._is_repo_root(str(tmp_path)) is False   # still fails closed
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "dubious ownership" in joined, \
        "git's own complaint was thrown away instead of logged"


def test_ordinary_not_a_repository_stays_silent(monkeypatch, caplog, tmp_path):
    """Most folders a user opens are not repositories. Warning on that buries
    the signal this whole mechanism exists to produce."""
    monkeypatch.setattr(gitignore.subprocess, "run", _returns(_Proc(
        returncode=128,
        stderr=b"fatal: not a git repository (or any of the parent directories): .git\n")))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._is_repo_root(str(tmp_path)) is False
    assert not caplog.records


def test_a_zero_exit_negative_on_a_dot_git_path_is_reported(
        monkeypatch, caplog, tmp_path):
    """git exited 0 and pointed somewhere else — the shape a polluted GIT_DIR
    produces. There is no stderr to read, so the report has to carry the
    inherited git environment instead."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(gitignore.subprocess, "run", _returns(_Proc(
        returncode=0, stdout=b"/somewhere/else\n")))
    monkeypatch.setenv("GIT_DIR", "/tmp/stray/.git")
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._is_repo_root(str(tmp_path)) is False
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "GIT_DIR" in joined, "the inherited git environment was not reported"
    assert "/tmp/stray/.git" in joined


def test_a_plain_directory_with_no_dot_git_stays_silent(
        monkeypatch, caplog, tmp_path):
    """The contradiction is "git says no but a .git is right there". Without the
    .git there is no contradiction, and a subdirectory of a repo legitimately
    reports a different toplevel — that is what _is_repo_root is FOR."""
    monkeypatch.setattr(gitignore.subprocess, "run", _returns(_Proc(
        returncode=0, stdout=b"/somewhere/else\n")))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._is_repo_root(str(tmp_path)) is False
    assert not caplog.records


def test_a_real_repo_root_never_warns(caplog, tmp_path):
    """The happy path, against real git: no warning, and the right answer."""
    import subprocess as sp
    if sp.run(["git", "init", "-q", str(tmp_path)],
              stdout=sp.DEVNULL, stderr=sp.DEVNULL).returncode != 0:
        pytest.skip("git init failed")
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._is_repo_root(str(tmp_path)) is True
    assert not caplog.records


def test_refusal_warning_is_throttled(monkeypatch, caplog, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(gitignore.subprocess, "run", _returns(_Proc(
        returncode=128, stderr=b"fatal: detected dubious ownership\n")))
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        for _ in range(50):
            gitignore._is_repo_root(str(tmp_path))
    assert len(caplog.records) == 1


def test_spawn_and_refusal_throttles_are_independent(monkeypatch, caplog, tmp_path):
    """A storm of one kind must not hide the first of the other kind — they have
    different causes and different fixes."""
    (tmp_path / ".git").mkdir()
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        monkeypatch.setattr(gitignore.subprocess, "run", _returns(_Proc(
            returncode=128, stderr=b"fatal: detected dubious ownership\n")))
        gitignore._is_repo_root(str(tmp_path))
        monkeypatch.setattr(gitignore.subprocess, "run",
                            _raise(OSError(24, "Too many open files")))
        gitignore._is_repo_root(str(tmp_path))
    assert len(caplog.records) == 2


# ----------------------------------------------------------------- the GATE
#
# `templates/git/condition.py` is the surface the USER hit: it is what makes
# /api/fs/conditions answer `"git": false`, which is what disables the Git side
# panel. It had the same blind spot as the package sites above, plus one of its
# own — `server.templates._run_condition` re-execs the module on every stat, so
# it cannot hold a throttle in a module global. The throttle therefore lives on
# the persistent `logging.Logger` object, which the logging manager caches for
# the life of the process. Still stdlib-only; the template imports no
# fused_render (SPEC PY-15).

import importlib.util
import logging
from unittest import mock

_GATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "git", "condition.py")
_GATE_LOGGER = "fused_render.templates.git.condition"


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("git_condition_diag", _GATE)
    # Asserted, never ignored: a None spec or loader means the module did not
    # load, so every assertion below would be checking a module that was never
    # executed — a green test over nothing.
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The throttle rides on the persistent logger, so it survives between the
    # re-execs this fixture simulates — and has to be cleared per test.
    for attr in list(vars(logging.getLogger(_GATE_LOGGER))):
        if attr.startswith("_fused_warned_"):
            delattr(logging.getLogger(_GATE_LOGGER), attr)
    return mod


def test_gate_reports_a_refusal_with_gits_words(gate, caplog, tmp_path):
    (tmp_path / ".git").mkdir()
    proc = _Proc(returncode=128,
                 stderr=b"fatal: detected dubious ownership in repository\n")
    with mock.patch.object(subprocess, "run", return_value=proc):
        with caplog.at_level("WARNING", logger=_GATE_LOGGER):
            assert gate.main(str(tmp_path)) is False        # still fails closed
    assert "dubious ownership" in " ".join(r.getMessage() for r in caplog.records)


def test_gate_reports_a_zero_exit_negative_on_a_repo(gate, caplog, tmp_path,
                                                     monkeypatch):
    """The exact shape the user saw: git exits 0, prints `false`, and the panel
    goes away. No stderr, so the environment is the report."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("GIT_DIR", "/tmp/stray/.git")
    with mock.patch.object(subprocess, "run",
                           return_value=_Proc(returncode=0, stdout=b"false\n")):
        with caplog.at_level("WARNING", logger=_GATE_LOGGER):
            assert gate.main(str(tmp_path)) is False
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "GIT_DIR" in joined and "/tmp/stray/.git" in joined


def test_gate_stays_silent_on_an_ordinary_non_repo(gate, caplog, tmp_path):
    proc = _Proc(returncode=128,
                 stderr=b"fatal: not a git repository (or any parent): .git\n")
    with mock.patch.object(subprocess, "run", return_value=proc):
        with caplog.at_level("WARNING", logger=_GATE_LOGGER):
            assert gate.main(str(tmp_path)) is False
    assert not caplog.records


def test_gate_stays_silent_on_a_real_repo(gate, caplog, tmp_path):
    if subprocess.run(["git", "init", "-q", str(tmp_path)],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        pytest.skip("git init failed")
    with caplog.at_level("WARNING", logger=_GATE_LOGGER):
        assert gate.main(str(tmp_path)) is True
    assert not caplog.records


def test_gate_warning_is_throttled_across_re_execs(gate, caplog, tmp_path):
    """The gate is re-exec'd per stat, so a module-global throttle would reset
    every call and write one line per directory the user opens."""
    (tmp_path / ".git").mkdir()
    proc = _Proc(returncode=128, stderr=b"fatal: detected dubious ownership\n")
    with mock.patch.object(subprocess, "run", return_value=proc):
        with caplog.at_level("WARNING", logger=_GATE_LOGGER):
            for _ in range(20):
                spec = importlib.util.spec_from_file_location("git_cond_re", _GATE)
                assert spec is not None and spec.loader is not None
                fresh = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(fresh)      # a new module object each time
                fresh.main(str(tmp_path))
    assert len(caplog.records) == 1
