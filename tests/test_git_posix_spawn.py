"""Every git subprocess must reach `posix_spawn`, never `fork()`.

THE ROOT CAUSE of "the Git side panel is disabled for every repository".

With `libproj` resident in the server process — and it becomes resident the
moment any map / geotiff / zarr template or daemon imports rasterio or pyproj —
a plain `fork()` runs PROJ's `pthread_atfork` child handler, which SIGSEGVs
*before* `exec`. The child dies with signal 11, so:

    returncode == -11,  stdout == b"",  stderr == b""

No exception is raised, because the spawn itself succeeded. Every git call site
in the app fails CLOSED on that: `/api/fs/conditions` reports `"git": false`,
`/api/fs/git-repo` reports `is_repo_root: false` for a real root, and
`/api/fs/list` stops dimming `.git`. All of it silent, all of it for every
repository at once, and all of it indistinguishable from "not a repository" —
which is exactly how it presented, and why it was mis-diagnosed twice.

Measured in the live server:

    WARNING fused_render.templates.git.condition: the git mode is being hidden
    for /Users/iamsdas/Documents/Fused and it looks wrong: git exited -11 saying
    '(nothing)'

`close_fds=False` was believed to be the fix, and three of these modules already
passed it with a comment saying so. **It is necessary and NOT sufficient.**
CPython takes the posix_spawn path only when ALL of these hold
(`subprocess.py::_execute_child`):

    _USE_POSIX_SPAWN and os.path.dirname(executable) and preexec_fn is None
    and not close_fds and not pass_fds and cwd is None and ... and umask < 0

Two of those were being violated everywhere:

  * `os.path.dirname(executable)` — the argv started with the BARE NAME `"git"`,
    whose dirname is `""`. Falsy. So every call forked, `close_fds=False` or not.
  * `cwd is None` — the template gate passed `cwd=<the directory>`, which forces
    fork on its own even with an absolute executable.

So the rule this file pins is the whole rule, not the half of it that reads
plausibly: an ABSOLUTE git path, `close_fds=False`, and no `cwd=` (git already
gets `-C`). It is pinned as a BEHAVIOUR — the recorded kwargs are run through
CPython's own condition — rather than as a grep, so it cannot pass by accident.
"""
import importlib.util
import os
import subprocess

import pytest

_TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "git")


def assert_posix_spawnable(argv, kwargs, where):
    """CPython's own preconditions for taking the posix_spawn path.

    Mirrors `subprocess.py::_execute_child`'s condition, minus the parts no
    caller here sets (uid/gid/umask/process_group). A violation means this call
    forks — and a fork in a process with libproj resident dies with SIGSEGV
    before it ever execs git.
    """
    executable = argv[0]
    assert os.path.dirname(executable), (
        f"{where}: argv[0] is {executable!r} — a bare name has no dirname, so "
        "CPython forks instead of posix_spawn'ing. Resolve git to an absolute "
        "path.")
    assert os.path.isabs(executable), f"{where}: {executable!r} is not absolute"
    assert kwargs.get("close_fds") is False, (
        f"{where}: close_fds must be explicitly False to reach posix_spawn")
    assert kwargs.get("cwd") is None, (
        f"{where}: cwd={kwargs.get('cwd')!r} forces the fork path on its own — "
        "git already takes -C, so pass no cwd")
    assert kwargs.get("preexec_fn") is None, f"{where}: preexec_fn forces fork"
    assert not kwargs.get("pass_fds"), f"{where}: pass_fds forces fork"
    assert not kwargs.get("start_new_session"), (
        f"{where}: start_new_session forces fork")


@pytest.fixture
def recorder(monkeypatch):
    """Capture every subprocess spawn without running one."""
    calls = []

    class _Fake:
        returncode = 0
        stdout = b""
        stderr = b""

        def __init__(self):
            self.stdin = None

        def communicate(self, *a, **k):
            return b"", b""

    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Fake()

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "Popen", record)
    return calls


# ------------------------------------------------------- the server-side sites


def test_repo_toplevel_reaches_posix_spawn(recorder, tmp_path):
    from fused_render.server import gitignore

    gitignore._repo_toplevel(str(tmp_path))
    assert recorder, "no git call was recorded"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "gitignore._repo_toplevel")


def test_git_ignored_reaches_posix_spawn(recorder, tmp_path):
    from fused_render.server import gitignore

    gitignore._git_ignored(str(tmp_path), ["a", "b"])
    assert recorder, "no git call was recorded"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "gitignore._git_ignored")


def test_ignore_oracle_reaches_posix_spawn(recorder, tmp_path):
    from fused_render.server import gitignore

    (tmp_path / ".git").mkdir()          # a real repo: no empty-GIT_DIR graft
    gitignore._IgnoreOracle(str(tmp_path))
    assert recorder, "no git call was recorded"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "gitignore._IgnoreOracle")


# ----------------------------------------------------- the git template's own


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_TPL, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_gate_reaches_posix_spawn(recorder, tmp_path):
    """The gate is the surface the user hit: it decides `"git": false`."""
    gate = _load("git_condition_spawn", "condition.py")
    gate.main(str(tmp_path))
    assert recorder, "the gate made no git call"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "templates/git/condition.py")


def test_the_reader_reaches_posix_spawn(recorder, tmp_path):
    reader = _load("git_log_spawn", "log.py")
    reader.main(file=str(tmp_path))
    assert recorder, "the reader made no git call"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "templates/git/log.py")


def test_the_writer_reaches_posix_spawn(recorder, tmp_path):
    ops = _load("git_ops_spawn", "ops.py")
    ops.main(file=str(tmp_path), op="fetch")
    assert recorder, "the writer made no git call"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "templates/git/ops.py")


# ------------------------------------------------------------- the real thing


def test_a_signal_death_is_not_mistaken_for_a_missing_repo(monkeypatch, caplog,
                                                           tmp_path):
    """The symptom, reproduced exactly: rc -11, no output, no exception.

    It must still fail closed (it is not a repo answer we can trust), and it must
    now be LOUD — a silent -11 is what cost two investigations.
    """
    from fused_render.server import gitignore

    (tmp_path / ".git").mkdir()
    gitignore._reset_spawn_failure_throttle()

    class _Segv:
        returncode = -11
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Segv())
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._is_repo_root(str(tmp_path)) is False
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "-11" in joined, "a SIGSEGV'd git was reported without its exit status"
