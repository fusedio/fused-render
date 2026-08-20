"""The explicit installation loader for projects that declare dependencies
(SPEC PY-16/PY-18).

A script in a folder with no `pyproject.toml` runs on the app's own interpreter
with nothing to install (PY-17). The core templates that DO declare dependencies
need a real download, and `fused.runPython` has roughly a 30-second budget — so
a first run used to hit the timeout and surface as an opaque `EngineError` with
a resolver failure buried in it, or nothing at all.

So the venv build is moved out of the request: `/api/run` answers
`needs_install` instead of blocking, a detached worker runs `uv sync` and writes
`progress.json`, and the page polls. The shape is
`templates/docs/install_worker.py`'s, which already does exactly this for the
typst download — one pattern in the repo, not two.

What these tests are really protecting:

  * the venv is keyed on the PROJECT FOLDER and lives in our own home dir, so
    every script under that folder resolves to one environment and nothing is
    ever written into the user's tree;
  * a venv is only "installed" while it still matches the declaration it was
    built from — the sidecar digest, not an mtime;
  * a resolver failure must arrive **verbatim**. "No solution found ... because
    imagecodecs has no wheels with a matching platform tag" is the entire point
    of making this visible; folding it into a generic message would leave the
    user exactly where they started.
"""
import errno
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fused_render import engine, envinstall, projectenv

# The engine these tests describe is 3.11+ (the `[fused]` extra's wheel is
# marked `python_version >= "3.11"`), so on 3.10 the backend is never
# installed and no project venv is ever built — there is nothing here for
# 3.10 to constrain. Same reasoning as tests/test_engine_requirements.py.
pytest.importorskip("tomllib", reason="the fused engine needs Python 3.11+")

requires_fused = pytest.mark.skipif(
    not engine.available(), reason="fused package not installed (engine falls back)"
)

HEADER = '# /// script\n# dependencies = ["pip"]\n# ///\n'


@pytest.fixture(autouse=True)
def _isolated_install_state(tmp_path, monkeypatch):
    """Give every test its own home dir, hence its own venv store and progress dir.

    `progress_dir` is keyed by the venv key alone and lives under the shell home,
    which conftest sets ONCE for the whole session — so two tests using the same
    project name would otherwise share one progress record and one claim file, and
    pass or fail depending on order. Since the key is now the project folder's
    absolute path, and every project here is built under `tmp_path`, that is
    already unique per test; the home override keeps the RECORDS separate too.

    It also puts the venv store under `tmp_path` (`projectenv.venvs_root()` is
    `<home_dir()>/venvs`), which is why no test patches a venvs path any more.

    Also drops the per-process venv-validation memo (D212). That cache is keyed by
    venv DIRECTORY, so real collisions are unlikely — but a memo that outlives the
    directory it describes is exactly the thing these tests are about, and a
    leaked verdict would make a later test pass or fail on ordering.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    envinstall.reset_venv_validation_cache()


def _project(tmp_path, name="proj", deps=("some-dist",)) -> str:
    """A project folder declaring `deps`. Returns its absolute path — the key."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "pyproject.toml").write_text(
        "[project]\nname = '%s'\nversion = '0.1.0'\ndependencies = [%s]\n"
        % (name, ", ".join(repr(x) for x in deps)),
        encoding="utf-8",
    )
    return str(d)


def _mark(venv_dir, proj) -> None:
    """Mark a venv ready exactly as the worker does: sidecar first, then marker.

    The sidecar is not optional bookkeeping — `is_installed` compares its digest
    against the project's current declaration, so a venv marked without one reads
    as stale. Writing them in the worker's order keeps the fixture honest about
    the window that ordering exists to close.
    """
    os.makedirs(venv_dir, exist_ok=True)
    projectenv.write_sidecar(str(venv_dir), proj, projectenv.state_digest(proj))
    (Path(venv_dir) / envinstall.READY_MARKER).write_text("{}")


# --- the venv key is the project folder's, and the store is ours ---------------


def test_the_key_is_the_project_folders_path(tmp_path):
    """Not the requirement set, and not upstream's recipe.

    Keying on the folder is what makes every script under it — however deep —
    resolve to one environment. Two folders declaring identical dependencies get
    two keys on purpose: the environment belongs to the project, and uv's shared
    cache is what stops that costing disk.
    """
    a = _project(tmp_path, "a", deps=["cowsay"])
    b = _project(tmp_path, "b", deps=["cowsay"])

    assert envinstall.venv_key_for(a) == projectenv.venv_key_for(a)
    assert envinstall.venv_key_for(a) != envinstall.venv_key_for(b)


def test_the_key_does_not_move_when_the_dependencies_do(tmp_path):
    """Editing `pyproject.toml` re-syncs the SAME venv rather than orphaning it.

    Under the old per-requirement-set key, adding one package abandoned the whole
    environment on disk and downloaded a fresh one. The path is stable, so
    `uv sync` reconciles in place — and the digest (see below) is what notices
    that it has to.
    """
    proj = _project(tmp_path, deps=["cowsay"])
    before = envinstall.venv_key_for(proj)
    _project(tmp_path, deps=["cowsay", "altair"])
    assert envinstall.venv_key_for(proj) == before


def test_the_venv_lives_in_our_home_dir_never_in_the_project(tmp_path):
    """MD-7: derived state goes to the home dir, source travels with the file.

    An in-folder `.venv` for a core template would be destroyed by the
    release-time re-stage, costing a full re-download of numpy/pyproj/imagecodecs
    on every upgrade.
    """
    proj = _project(tmp_path)
    venv = envinstall.venv_dir_for(proj)

    assert venv == projectenv.venv_dir_for(proj)
    assert venv.startswith(str(tmp_path / "home"))
    assert not venv.startswith(proj + os.sep)


def test_the_interpreter_handed_to_the_run_is_the_projects_own(tmp_path):
    """`run_python` passes `interpreter=`, which is why the store can be ours."""
    proj = _project(tmp_path)
    assert envinstall.venv_python_for(proj) == envinstall._venv_python(
        envinstall.venv_dir_for(proj)
    )


@requires_fused
def test_the_backend_attributes_this_module_reads_still_exist():
    """Pin the private attributes the loader depends on.

    Only `_python_executable` now: it decides which interpreter `uv sync` builds
    on, and the backend runs the code, so a rename must be a red test rather than
    an environment silently built on the wrong ABI. `_venvs_path` is deliberately
    NOT here any more — the venv is ours and the backend is told about it through
    `interpreter=`, so there is no directory the two sides have to agree on.
    """
    backend = engine.get_backend()
    missing = [a for a in envinstall.BACKEND_ATTRS if not hasattr(backend, a)]
    assert not missing, (
        f"{type(backend).__name__} no longer has {missing}; envinstall reads them "
        "to stay in step with the interpreter project venvs are built on"
    )
    assert "_venvs_path" not in envinstall.BACKEND_ATTRS, (
        "project venvs live under our own home dir; reaching into upstream's "
        "private store again would reintroduce the directory-drift failure"
    )


@requires_fused
def test_a_renamed_backend_attribute_fails_loudly(monkeypatch):
    """And when it IS missing, the failure says so instead of guessing."""

    class Renamed:
        pass

    monkeypatch.setattr(engine, "get_backend", lambda: Renamed())
    with pytest.raises(RuntimeError, match="_python_executable"):
        envinstall._python_executable()


@requires_fused
def test_the_stripped_env_vars_are_read_off_fused_not_guessed():
    """engine's probe env must match what the backend really strips.

    A probe run under a different environment than the child gets is a probe that
    proves nothing — the PYTHONHOME case is exactly that. So the list is read off
    `python_compute`; this asserts the real attribute is still there, since the
    literal fallback would otherwise go stale invisibly.
    """
    from fused.agent_core.backends.local import python_compute

    assert hasattr(python_compute, "_STRIPPED_ENV_VARS")
    assert set(engine._stripped_env_vars()) == set(python_compute._STRIPPED_ENV_VARS)


def _install_worker():
    """`_env_install_worker` as a MODULE, imported by path.

    It is a script that must not import `fused_render` (D152), so it is not
    reachable as `fused_render._env_install_worker` in the way the rest of the
    package is — importing it by file is how a test reads it without changing
    that rule.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "fused_render" / "_env_install_worker.py"
    spec = importlib.util.spec_from_file_location("_env_install_worker_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_uv_child_does_not_inherit_the_apps_python_environment(monkeypatch):
    """uv's own children are pythons, and PYTHONHOME reaches them (D266).

    uv is a native binary that does not care what PYTHON* says — but a
    dependency it has to BUILD is compiled by a build backend running in an
    interpreter uv creates, and that interpreter inherits this environment.
    Inside the macOS .app, PYTHONHOME points into the bundle, so those build
    interpreters loaded the app's frozen `_distutils_hack` over the setuptools
    doing the build and every source build failed with
    `No module named 'jaraco.text'`. The user-visible symptom was an AI runner
    whose environment could not be built at all.
    """
    worker = _install_worker()
    poison = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP",
              "VIRTUAL_ENV")
    for name in poison:
        monkeypatch.setenv(name, "/Applications/FusedRender.app/Contents/Resources")
    monkeypatch.setenv("KEEP_ME", "yes")
    # Not every PYTHON* var redirects an interpreter, and the ones that do not
    # are none of this function's business.
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")

    env = worker._uv_env(UV_CACHE_DIR="/cache")

    for name in poison:
        assert name not in env, name
    # Only the poison goes: uv needs PATH, HOME, proxy settings and the rest.
    assert env["KEEP_ME"] == "yes"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["UV_CACHE_DIR"] == "/cache"


def test_the_install_worker_strips_everything_the_backend_does():
    """The worker restates the list rather than importing it, so a test pairs them.

    It cannot import `fused_render` (D152). A SUPERSET, not equality: the worker
    also drops `PYTHONEXECUTABLE` (the macOS framework build sets it), which the
    backend's own list does not carry. What must never happen is the worker
    missing something the backend knows is dangerous.
    """
    worker = _install_worker()
    assert set(worker._STRIPPED_ENV_VARS) >= set(engine._stripped_env_vars())


# --- the precedence `run_python`'s interpreter fast path rests on --------------
#
# `run_python` skips the venv entirely when the app interpreter already satisfies
# every requirement in a header, and it does that by passing `interpreter=` and NO
# requirements. That is only correct because upstream reads `interpreter` FIRST and
# never looks at `requirements` once it is set (`_execute_sync`, and stated in
# `compute_base.py`: "`interpreter`, when set … wins over `requirements`"). Nothing
# tested that precedence. If upstream ever flipped it to union the two, every
# fast-path run would silently start building the multi-GB venv the fast path
# exists to avoid — no error, just the old bill back. These two tests fail loudly
# instead.
#
# Related hazard, deliberately NOT exercised here because we never trigger it:
# `compute_base.execute()` sets `resolved_interpreter` from a uv workflow venv when
# `project`/`project_dir` is passed, AND swaps the cache `env_component` to
# `wv.env_component` — so with a project, requirements drop out of both the venv
# and the cache identity. `engine._execute` passes neither argument; were it ever
# to, "requirements were honoured" would stop being true on the venv path too.


class _StopBeforeSpawn(BaseException):
    """Aborts `_execute_sync` at the child spawn.

    A `BaseException`, so the broad `except` blocks inside `_execute_sync` cannot
    swallow it and turn a failed assertion into a plausible-looking result.
    """


@requires_fused
def test_upstream_still_lets_the_interpreter_win_over_requirements(monkeypatch):
    """Both arguments given: the interpreter runs the child, no venv is built."""
    from fused.agent_core.backends.local import python_compute as pc

    built = []
    monkeypatch.setattr(
        pc, "ensure_requirements_venv",
        lambda *a, **k: built.append(a) or "/nonexistent/venv/bin/python",
    )

    backend = engine.get_backend()
    monkeypatch.setattr(type(backend), "_ensure_dispatcher",
                        lambda self: type("D", (), {"host_dir": "/tmp"})(),
                        raising=True)
    monkeypatch.setattr(type(backend), "_ensure_venv",
                        lambda self: pytest.fail("built the bare venv"),
                        raising=True)

    spawned = []

    def _run(cmd, **kw):
        spawned.append(cmd)
        raise _StopBeforeSpawn()

    monkeypatch.setattr(pc.subprocess, "run", _run)

    with pytest.raises(_StopBeforeSpawn):
        backend._execute_sync(
            code="print(1)",
            interpreter="/the/app/interpreter",
            requirements=["pandas>=2.0.0"],
        )

    assert not built, (
        "upstream built a requirements venv despite `interpreter` being set; the "
        "app-interpreter fast path in run_python is no longer safe"
    )
    assert spawned and spawned[0][0] == "/the/app/interpreter", (
        f"the child ran on {spawned[0][0] if spawned else None}, not the interpreter"
    )


@requires_fused
def test_engine_never_hands_the_backend_both_interpreter_and_requirements(monkeypatch):
    """Our side of the same contract: the two are mutually exclusive at the call.

    Passing both would read as "install these INTO that interpreter", which is not
    what upstream does with it — and on the fast path the requirements are exactly
    the set we have just proven is already present.
    """
    import asyncio

    calls = {}

    class _Backend:
        def _execute_sync(self, **kw):
            calls["sync"] = kw
            return "ok"

        async def execute(self, **kw):
            calls["execute"] = kw
            return "ok"

    monkeypatch.setattr(engine, "get_backend", lambda: _Backend())

    asyncio.run(engine._execute("code", ["pandas>=2.0.0"], "/the/app/interpreter", {}))
    assert calls["sync"]["interpreter"] == "/the/app/interpreter"
    assert "requirements" not in calls["sync"], (
        "requirements travelled alongside interpreter; upstream would ignore them, "
        "so passing them can only mislead a future reader"
    )

    calls.clear()
    asyncio.run(engine._execute("code", ["pandas>=2.0.0"], None, {}))
    assert calls["execute"]["requirements"] == ["pandas>=2.0.0"]
    assert not calls["execute"].get("interpreter"), (
        "the venv path must not pin an interpreter, or requirements stop mattering"
    )


def test_the_bundled_uv_is_found_beside_the_interpreter(tmp_path, monkeypatch):
    """The macOS bundle has no `venv`/`ensurepip`/`pip`, so uv is not optional.

    `fused`'s venv builder calls `shutil.which("uv")` and otherwise falls back to
    `<python> -m venv`, which on a DMG fails with "No module named venv"
    (measured). So the uv shipped at `Contents/Resources/bin/uv` has to be found
    AND put on the worker's PATH.

    Deliberately not gated on `sys.frozen`: py2app's boot script sets that, so
    anything reaching this code without the app launcher would miss the bundled uv
    and fall back to a module that isn't there. A stat cannot be wrong about it.
    """
    fake_app = tmp_path / "App.app" / "Contents"
    (fake_app / "MacOS").mkdir(parents=True)
    (fake_app / "Resources" / "bin").mkdir(parents=True)
    interp = fake_app / "MacOS" / "python"
    interp.write_text("")
    uv = fake_app / "Resources" / "bin" / "uv"
    uv.write_text("")
    monkeypatch.setattr(sys, "executable", str(interp))
    monkeypatch.delenv("FUSED_RENDER_UV_BIN", raising=False)
    monkeypatch.setattr(sys, "frozen", "", raising=False)
    assert envinstall.uv_bin() == str(uv)

    # And it reaches the worker, which is the only thing that matters.
    env = envinstall._worker_env()
    assert env["PATH"].split(os.pathsep)[0] == str(uv.parent)


def test_the_bundled_uv_is_found_beside_the_interpreter_on_linux_and_windows(
    tmp_path, monkeypatch
):
    """The other two packagings put uv in the interpreter's OWN directory.

    Linux AppImage: `usr/python/bin/uv` next to `usr/python/bin/python3`
    (build_linux_appimage.sh:88). Windows: `<PythonRoot>/uv.exe` next to
    `pythonw.exe` (.ps1:185). Probing only the macOS `Contents/Resources/bin`
    layout left both of those bundled binaries unused unless their directory
    happened to be on PATH — no crash there, since those builds ship a real
    CPython with `venv`, but a shipped tool silently ignored.
    """
    bindir = tmp_path / "python" / "bin"
    bindir.mkdir(parents=True)
    interp = bindir / "python3"
    interp.write_text("")
    uv = bindir / ("uv.exe" if os.name == "nt" else "uv")
    uv.write_text("")
    monkeypatch.setattr(sys, "executable", str(interp))
    monkeypatch.delenv("FUSED_RENDER_UV_BIN", raising=False)
    assert envinstall.uv_bin() == str(uv)


def test_an_explicit_uv_override_wins(tmp_path, monkeypatch):
    real = tmp_path / "myuv"
    real.write_text("")
    monkeypatch.setenv("FUSED_RENDER_UV_BIN", str(real))
    assert envinstall.uv_bin() == str(real)


def test_a_stale_uv_override_is_ignored(tmp_path, monkeypatch):
    """Same rule as rclone_bin: a wrong override must not shadow a real uv."""
    monkeypatch.setenv("FUSED_RENDER_UV_BIN", str(tmp_path / "gone"))
    assert envinstall.uv_bin() != str(tmp_path / "gone")


# --- the interpreter script venvs are built on (D214) -------------------------


@pytest.fixture
def _fresh_script_python(monkeypatch):
    """Drop the per-process interpreter resolution before and after each use.

    The resolution is cached for the life of the process (it costs a subprocess),
    so without this a test that patches `uv_bin` would either read a verdict a
    previous test measured under different conditions, or leave its own behind.
    """
    envinstall.reset_script_python_cache()
    yield
    envinstall.reset_script_python_cache()


def _uv_stub(tmp_path, monkeypatch, *, finds=None, installs=None):
    """A fake `uv` on disk that answers `python find` (and optionally `install`).

    A real subprocess rather than a patched `subprocess.run`: the resolver's whole
    job is to decide whether an interpreter on this machine actually runs, and a
    canned return value cannot fail the way a spawn can.
    """
    uv = tmp_path / "uv"
    found = finds or ""
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "python" ] && [ "$2" = "find" ]; then\n'
        f'  [ -n "{found}" ] || exit 1\n'
        f'  echo "{found}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "python" ] && [ "$2" = "install" ]; then\n'
        f'  exit {0 if installs else 1}\n'
        "fi\n"
        "exit 2\n"
    )
    uv.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_UV_BIN", str(uv))
    return uv


def _py312_stub(tmp_path, name="py312", version="3.12"):
    """An executable that reports `version` — stands in for a real interpreter."""
    exe = tmp_path / name
    exe.write_text(f"#!/bin/sh\necho '{version}'\n")
    exe.chmod(0o755)
    return exe


def test_a_server_already_on_312_builds_script_venvs_from_ITSELF(
    monkeypatch, _fresh_script_python
):
    """None, not a path — and that is the point.

    Every packaged build (DMG's `python@3.12`, the AppImage's and the Windows
    installer's `uv python install 3.12`) already runs 3.12, and `None` is exactly
    what the backend has always been given, so `python_identity` produces the
    IDENTICAL key it produces today. Resolving a uv-managed 3.12 for these users
    instead would re-key every venv they own and re-download a second CPython to
    reach a version they already had.
    """
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 12))
    assert envinstall.script_python() is None
    assert envinstall.script_python_ready() is True


def test_a_server_on_the_WRONG_version_resolves_a_uv_managed_312(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The reported bug: a 3.14 server built every script venv on cp314, and
    anything without cp314 wheels (tensorflow) was an unresolvable dead end no
    rebuild could fix. So a server that is not on 3.12 must not hand its own
    interpreter to the builder."""
    exe = _py312_stub(tmp_path)
    _uv_stub(tmp_path, monkeypatch, finds=str(exe))
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    assert envinstall.script_python() == str(exe)
    assert envinstall.script_python_ready() is True


def test_an_interpreter_uv_NAMES_but_that_does_not_run_is_refused(
    tmp_path, monkeypatch, _fresh_script_python
):
    """Resolved is not the same as usable. A path that cannot be spawned would
    otherwise reach upstream's `python_identity`, which runs it to build the key —
    turning a bad resolution into a failure on the /api/run request path instead
    of a fact we established once, ourselves, off it."""
    _uv_stub(tmp_path, monkeypatch, finds=str(tmp_path / "not-there"))
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    assert envinstall.script_python_ready() is False


def test_an_interpreter_reporting_the_WRONG_version_is_refused(
    tmp_path, monkeypatch, _fresh_script_python
):
    """One subprocess proves both things at once: that it runs, and that it is
    the version we asked for. A 3.13 answering a 3.12 request means uv resolved
    something we did not ask for, and building on it silently defeats the pin."""
    exe = _py312_stub(tmp_path, name="py313", version="3.13")
    _uv_stub(tmp_path, monkeypatch, finds=str(exe))
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    assert envinstall.script_python_ready() is False


def test_no_managed_312_yet_is_NOT_ready_rather_than_an_error(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The state that drives the download: nothing to build on yet, but nothing
    wrong either. It has to be reportable rather than raised, because the caller
    (`is_installed`) answers a yes/no question on the request path."""
    _uv_stub(tmp_path, monkeypatch, finds=None)
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    assert envinstall.script_python_ready() is False


def test_a_machine_with_no_uv_at_all_keeps_working_as_before(
    tmp_path, monkeypatch, _fresh_script_python
):
    """A source checkout without uv must not become unusable to gain a pin.

    Without uv there is no way to find or fetch a managed 3.12, and upstream's
    builder already falls back to `<python> -m venv` there. So the answer is the
    pre-D214 behaviour — build from ours — which is worse than 3.12 but is what
    that machine could always do, and is a working server rather than a broken one.
    """
    monkeypatch.setenv("FUSED_RENDER_UV_BIN", str(tmp_path / "no-uv-here"))
    monkeypatch.setattr(envinstall, "uv_bin", lambda: None)
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    assert envinstall.script_python() is None
    assert envinstall.script_python_ready() is True


def test_an_explicit_script_python_override_wins_but_is_still_probed(
    tmp_path, monkeypatch, _fresh_script_python
):
    """Escape hatch and test seam, mirroring FUSED_RENDER_APP_PYTHON — and probed
    for the same reason: an override that is not a usable 3.12 is a
    misconfiguration to refuse, not a reason to build on it."""
    good = _py312_stub(tmp_path, name="good")
    monkeypatch.setenv(envinstall._SCRIPT_PYTHON_ENV, str(good))
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    assert envinstall.script_python() == str(good)

    envinstall.reset_script_python_cache()
    monkeypatch.setenv(envinstall._SCRIPT_PYTHON_ENV, str(tmp_path / "absent"))
    monkeypatch.setattr(envinstall, "uv_bin", lambda: None)
    assert envinstall.script_python_ready() is False


def test_the_interpreter_is_resolved_ONCE_per_process(
    tmp_path, monkeypatch, _fresh_script_python
):
    """It costs a spawn (two, with a `uv python find`), and `is_installed` runs on
    every /api/run pre-flight. Same reasoning as the venv probe: measure once."""
    exe = _py312_stub(tmp_path)
    _uv_stub(tmp_path, monkeypatch, finds=str(exe))
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))

    calls = []
    real = envinstall._probe_python

    def counted(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(envinstall, "_probe_python", counted)
    for _ in range(5):
        envinstall.script_python()
    assert len(calls) == 1, f"probed {len(calls)} times, not once: {calls}"


def test_the_interpreter_is_never_ANOTHER_VENVS_python(
    tmp_path, monkeypatch, _fresh_script_python
):
    """`--managed-python` alone is not enough, and this was measured.

    A venv counts as managed to uv when its BASE interpreter is, so run from a
    checkout whose own `.venv` is 3.12, `uv python find --managed-python` answers
    `.venv/bin/python3`. Building script venvs from that would key them (via
    `python_identity`) to a per-worktree path that `dev.sh` now DELETES on a version
    mismatch — orphaning every script venv on the machine and re-downloading the lot.
    `--system` excludes virtual environments; this pins that the flag is asked for,
    because nothing else about the resolution would look wrong until a `.venv`
    disappeared.
    """
    seen = []

    class _Proc:
        returncode = 0
        stdout = str(_py312_stub(tmp_path))
        stderr = ""

    def spy(cmd, **kw):
        seen.append(cmd)
        return _Proc()

    monkeypatch.setattr(envinstall, "uv_bin", lambda: "/usr/bin/uv")
    monkeypatch.setattr(envinstall.subprocess, "run", spy)
    monkeypatch.setattr(envinstall, "_probe_python", lambda p: True)
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    envinstall.script_python()

    find = next(c for c in seen if "find" in c)
    assert "--system" in find, (
        "without --system uv can answer with a virtual environment's python: " + repr(find)
    )
    assert "--managed-python" in find and "--no-project" in find, repr(find)


def test_a_not_ready_verdict_is_never_CACHED(tmp_path, monkeypatch, _fresh_script_python):
    """"Nothing here yet" is a fact about this instant, not about the machine.

    The download that fixes it happens in another PROCESS, so nothing in here can
    be notified when it lands. Caching the negative would leave this server
    convinced there is no 3.12 for the rest of its life — the install would
    complete and every later pre-flight would still route back to the bootstrap.
    Positive verdicts are cached (they cost a spawn); negative ones are re-measured,
    which is the same three-valued discipline the venv probe uses for the same
    reason.
    """
    exe = _py312_stub(tmp_path)
    _uv_stub(tmp_path, monkeypatch, finds=None)
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 14))
    assert envinstall.script_python_ready() is False

    # The download lands — a later `find` now answers — with nothing telling us.
    _uv_stub(tmp_path, monkeypatch, finds=str(exe))
    assert envinstall.script_python_ready() is True
    assert envinstall.script_python() == str(exe)


@requires_fused
def test_no_312_yet_reports_NOT_installed_whatever_the_marker_says(
    tmp_path, monkeypatch, _fresh_script_python
):
    """And it short-circuits ahead of the D212 validation machinery.

    With no 3.12 there is no venv directory to name: the key folds in the base
    interpreter, so any directory computed in this state belongs to a venv nobody
    will build. Probing it, unlinking its marker, or spending the one-rebuild-per-
    process budget on it would all be acting on a venv that does not exist — so this
    answers False before any of that runs.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = _marked_venv(proj, runnable=True)
    # The baseline is about the MARKER, so the interpreter must be pinned ready
    # rather than measured: `_fresh_script_python` drops conftest's pin, and on a
    # runner that is neither 3.12 nor holding a uv-managed 3.12 the real
    # resolution answers "not ready" — which is the very short-circuit under test,
    # so the baseline would assert the post-condition and pass for the wrong
    # reason on 3.12 while failing outright on 3.11 (which is what CI's
    # fused-engine job pins, and the only job where @requires_fused runs at all).
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: True)
    assert envinstall.is_installed(proj) is True  # baseline: the marker is trusted

    envinstall.reset_venv_validation_cache()
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    probed = []
    monkeypatch.setattr(envinstall, "_venv_is_usable",
                        lambda d: probed.append(d) or True)
    assert envinstall.is_installed(proj) is False
    assert not probed, "validated a venv that cannot even be named yet"
    assert os.path.exists(os.path.join(venv_dir, envinstall.READY_MARKER)), (
        "unlinked the marker of a venv the missing interpreter says nothing about"
    )


@requires_fused
def test_no_project_never_needs_an_interpreter(monkeypatch, _fresh_script_python):
    """No project means no venv, so the pin is irrelevant — and must not block."""
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    assert envinstall.is_installed(None) is True


@requires_fused
def test_the_bootstrap_reports_under_its_OWN_key_not_a_venv_key(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The interpreter download is a different THING to install, not a different
    mechanism for reporting one.

    It reports under `PYTHON_BOOTSTRAP_KEY` rather than the project's key, because
    the two rounds have to be distinguishable: the page has to be able to tell "we
    made progress, ask again" from "we installed and nothing changed", which is a
    loop.
    """
    proj = _project(tmp_path, deps=["pip"])
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    spawned = []
    monkeypatch.setattr(envinstall, "_spawn",
                        lambda key, p, **kw: spawned.append((key, p, kw)) or 4242)

    rec = envinstall.start(proj)
    assert spawned, "no installer was started"
    key, _, kw = spawned[0]
    assert key == envinstall.PYTHON_BOOTSTRAP_KEY
    assert key != envinstall.venv_key_for(proj)
    assert envinstall.valid_key(key), "the bootstrap key must still be key-SHAPED"
    assert kw.get("acquire_python") == envinstall.SCRIPT_PYTHON_VERSION
    assert rec["stage"] == "spawn"
    # And it is pollable under that key, which is what the page will do.
    assert envinstall.progress(envinstall.PYTHON_BOOTSTRAP_KEY) is not None


@requires_fused
def test_start_REPORTS_the_key_it_used_rather_than_leaving_it_to_be_recomputed(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The caller must not re-derive it, and this is why.

    /api/env/install hands the client a key to poll. Recomputing that key means two
    independent answers to "which key" — and in bootstrap mode they DISAGREE by
    design, so the page would poll the venv key while the worker reported under the
    interpreter key, read no record, and fail an install that was running fine.
    Recomputing is also racy even when both agree: readiness can flip between the
    two calls, which is exactly the window a fast download opens.
    """
    proj = _project(tmp_path, deps=["pip"])
    monkeypatch.setattr(envinstall, "_spawn", lambda *a, **kw: os.getpid())

    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    assert envinstall.start(proj)["key"] == envinstall.PYTHON_BOOTSTRAP_KEY

    monkeypatch.setattr(envinstall, "script_python_ready", lambda: True)
    envinstall.reset_venv_validation_cache()
    assert envinstall.start(proj)["key"] == envinstall.venv_key_for(proj)


@requires_fused
def test_the_resolved_script_interpreter_reaches_the_worker(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The whole point of resolving a 3.12: `uv sync --python` has to get it.

    The key no longer folds the interpreter in — it is the project's path — so the
    interpreter's only route to the environment is argv. A resolution that stopped
    short of the spawn would build every project venv on whatever the server
    happens to run, which is the cp314-has-no-tensorflow-wheels bug D214 fixed.
    """
    exe = _py312_stub(tmp_path)
    monkeypatch.setattr(envinstall, "_python_executable", lambda: str(exe))
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: True)
    proj = _project(tmp_path, deps=["pip"])

    argv = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: argv.append(cmd) or _FakePopen())
    envinstall.start(proj)

    assert argv, "no worker was spawned"
    assert str(exe) in argv[0], f"the resolved interpreter never reached argv: {argv[0]}"


class _FakePopen:
    pid = 4242


@requires_fused
def test_the_ready_marker_is_the_index_of_readiness_not_the_directory(
    tmp_path, monkeypatch
):
    """A half-built venv (no marker) must read as NOT ready.

    `ensure_requirements_venv` deletes and rebuilds a marker-less directory, so
    treating "the directory exists" as installed would skip the loader and hand
    the request the very build it was meant to move off the request path.

    Renamed (was `..._follows_the_ready_marker_not_the_directory`): the marker is
    still the INDEX — the only thing consulted to find a venv, and its absence is
    still final — but since D212 it is a *claim* that is verified once per process
    rather than proof on its own. So this test now supplies a venv whose
    interpreter actually runs, and the marker-is-not-enough half lives in
    `test_a_marked_venv_that_cannot_run_...` below. Its original intent (a
    directory is not readiness) is unchanged and still pinned by the middle
    assertion.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    assert not envinstall.is_installed(proj)

    venv_dir = Path(envinstall.venv_dir_for(proj))
    venv_dir.mkdir(parents=True)
    assert not envinstall.is_installed(proj), "a marker-less dir is half-built"

    _runnable_venv_python(str(venv_dir))
    _mark(venv_dir, proj)
    assert envinstall.is_installed(proj)


# --- the venv also has to match the declaration it was built from --------------


@requires_fused
def test_editing_the_declaration_makes_a_ready_venv_report_not_installed(
    tmp_path, monkeypatch
):
    """Adding a dependency has to reach the environment.

    The key is the folder's path, so it does not move when the dependencies do —
    which is the point (the venv is reconciled rather than orphaned), but it also
    means the key alone can no longer tell a fresh environment from a stale one.
    The sidecar digest is what does.
    """
    proj = _project(tmp_path, deps=["cowsay"])
    venv_dir = _marked_venv(proj, runnable=True)
    assert envinstall.is_installed(proj) is True

    _project(tmp_path, deps=["cowsay", "altair"])  # the user edits pyproject.toml
    assert envinstall.is_installed(proj) is False
    assert (venv_dir / envinstall.READY_MARKER).exists(), (
        "a stale venv is re-synced, not condemned — `uv sync` reconciles in place"
    )


@requires_fused
def test_a_ready_venv_with_no_sidecar_reports_not_installed(tmp_path, monkeypatch):
    """A venv that cannot say what it holds cannot be trusted to hold it.

    Also the upgrade path: environments built before the sidecar existed have
    none, and rebuilding them once is the honest answer.
    """
    proj = _project(tmp_path)
    venv_dir = Path(envinstall.venv_dir_for(proj))
    venv_dir.mkdir(parents=True)
    _runnable_venv_python(str(venv_dir))
    (venv_dir / envinstall.READY_MARKER).write_text("{}")  # marker, no sidecar

    assert envinstall.is_installed(proj) is False


@requires_fused
def test_a_stale_declaration_does_not_spend_the_rebuild_budget(tmp_path, monkeypatch):
    """A normal edit is not corruption, and must not be bounded like one.

    D212 allows exactly one repair per venv per process because for the cohort it
    exists for the rebuild is guaranteed to fail the same way. An edit is the
    opposite: it is guaranteed to CHANGE something, and a user who edits
    `pyproject.toml` three times has to get three syncs.
    """
    proj = _project(tmp_path, deps=["cowsay"])
    _marked_venv(proj, runnable=True)

    for extra in ("altair", "polars", "duckdb"):
        _project(tmp_path, deps=["cowsay", extra])
        assert envinstall.is_installed(proj) is False
        _mark(envinstall.venv_dir_for(proj), proj)  # the worker syncs and re-marks
        assert envinstall.is_installed(proj) is True

    assert envinstall._REBUILD_ATTEMPTED == set(), (
        "a declaration edit consumed the D212 repair budget"
    )


@requires_fused
def test_a_locked_project_that_gains_a_dependency_goes_stale(tmp_path, monkeypatch):
    """The requirement the digest exists for: an edit is picked up automatically.

    Under a lock-ONLY digest this was the silent failure — the manifest changed,
    the lock did not, `sidecar_matches` said fresh, no install was offered, and
    the run failed later on an ImportError with no loader and no explanation. The
    user must never have to run `uv sync` themselves to fix that.
    """
    proj = _project(tmp_path, deps=["cowsay"])
    with open(os.path.join(proj, "uv.lock"), "w", encoding="utf-8") as fh:
        fh.write("version = 1\n")
    _marked_venv(proj, runnable=True)
    assert envinstall.is_installed(proj) is True

    _project(tmp_path, deps=["cowsay", "altair"])  # the lock is NOT re-run
    assert envinstall.is_installed(proj) is False


@requires_fused
def test_a_locked_project_with_an_untouched_manifest_stays_fresh(tmp_path, monkeypatch):
    """The other half: nothing moved, so nothing is rebuilt on every request."""
    proj = _project(tmp_path, deps=["cowsay"])
    with open(os.path.join(proj, "uv.lock"), "w", encoding="utf-8") as fh:
        fh.write("version = 1\n")
    _marked_venv(proj, runnable=True)

    for _ in range(3):
        assert envinstall.is_installed(proj) is True


# --- a marker is a claim, and the claim is verified once (D212) ----------------
#
# The macOS DMG shipped an interpreter that could not self-locate without
# PYTHONHOME, and `python_compute` strips PYTHONHOME from every child — so a venv
# built from it recorded a base prefix that does not exist on the user's machine
# and every child of that venv died with `ModuleNotFoundError`. The venv cache key
# folds in only the interpreter path and version, both constants inside the .app,
# so an app upgrade did not change the key and nothing ever revalidated: the
# marker was permanent and so was the breakage. These tests pin the two halves of
# the fix — the probe, and the marker deletion that lets upstream rebuild.


def _runnable_venv_python(venv_dir: str) -> str:
    """Put a genuinely runnable interpreter where a venv keeps its own.

    A symlink to THIS interpreter rather than a stub script: the probe is a real
    `-c ""` spawn, so the thing being validated has to be a real python — a stub
    that exits 0 would pass a probe that had regressed into `os.path.exists`.
    """
    exe = envinstall._venv_python(venv_dir)
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    os.symlink(sys.executable, exe)
    return exe


@requires_fused
def test_a_marked_venv_that_cannot_run_is_not_installed_and_loses_its_marker(
    tmp_path, monkeypatch
):
    """The DMG bug, reduced: a marker over a venv whose python does not work.

    Deleting the marker is load-bearing, not tidying. Upstream's
    `ensure_requirements_venv` returns immediately when the marker exists, so
    reporting "not installed" while leaving it in place would make `/api/run`
    answer `needs_install`, the loader run the install worker, the worker find the
    marker and do nothing, and the page ask to install again — forever. The
    missing marker is what makes upstream rmtree and rebuild.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = Path(envinstall.venv_dir_for(proj))
    marker = venv_dir / envinstall.READY_MARKER
    venv_dir.mkdir(parents=True)
    _mark(venv_dir, proj)
    # No interpreter at all is the cheapest unrunnable venv and the one shape that
    # behaves the same on every OS; the exits-nonzero shape is covered below.
    assert not envinstall.is_installed(proj)
    assert not marker.exists(), "the marker must go, or upstream will not rebuild"


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX #! stub interpreter")
@requires_fused
def test_a_marked_venv_whose_python_FAILS_is_not_installed(tmp_path, monkeypatch):
    """Present but broken, which is what the real bug looked like.

    The DMG's venv python existed and was executable; it died on startup because
    its recorded base prefix was gone. So "the file is there" is not the question
    the probe asks — it has to actually run something.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = Path(envinstall.venv_dir_for(proj))
    exe = envinstall._venv_python(str(venv_dir))
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"No module named 'encodings'\" >&2\nexit 1\n")
    os.chmod(exe, 0o755)
    _mark(venv_dir, proj)

    assert not envinstall.is_installed(proj)
    assert not (venv_dir / envinstall.READY_MARKER).exists()


@requires_fused
def test_the_venv_probe_runs_at_most_once_per_venv_per_process(tmp_path, monkeypatch):
    """The cost ceiling. A probe per request would be a subprocess per request.

    `/api/run`'s pre-flight calls `is_installed` on every run of every PEP 723
    script, so the validation has to be memoized per venv directory per process —
    the same shape as `engine.app_interpreter()`'s one-probe-per-process cache.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = Path(envinstall.venv_dir_for(proj))
    venv_dir.mkdir(parents=True)
    _runnable_venv_python(str(venv_dir))
    _mark(venv_dir, proj)

    probes = []
    real = envinstall._venv_runs
    monkeypatch.setattr(
        envinstall, "_venv_runs", lambda d: (probes.append(d), real(d))[1]
    )
    for _ in range(5):
        assert envinstall.is_installed(proj)
    assert probes == [str(venv_dir)]


@requires_fused
def test_a_missing_marker_never_probes(tmp_path, monkeypatch):
    """Nothing to validate: the marker's absence is already the whole answer.

    Also the common case by count — every first open of a project — so it must
    stay a single stat, not a spawn.
    """
    proj = _project(tmp_path)
    probes = []
    monkeypatch.setattr(envinstall, "_venv_runs", lambda d: probes.append(d) or True)
    assert not envinstall.is_installed(proj)
    assert probes == []


@requires_fused
def test_a_rebuilt_venv_is_not_stuck_on_the_earlier_failed_verdict(
    tmp_path, monkeypatch
):
    """The other half of "never loop forever".

    A negative verdict cached for the life of the process would be just as
    permanent as the marker it deleted: the worker would rebuild the venv
    correctly and `is_installed` would keep saying no. The memo is dropped
    whenever the marker is absent, so a rebuild is judged on its own merits.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = Path(envinstall.venv_dir_for(proj))
    marker = venv_dir / envinstall.READY_MARKER
    venv_dir.mkdir(parents=True)
    _mark(venv_dir, proj)
    assert not envinstall.is_installed(proj)  # no interpreter -> marker removed

    _runnable_venv_python(str(venv_dir))  # what the rebuild leaves behind
    _mark(venv_dir, proj)
    assert envinstall.is_installed(proj)


# --- the probe has THREE answers, and the rebuild is bounded (D212) ------------
#
# Both of these came out of review, and both are about the same thing: the
# deletion at the end of `is_installed` destroys a venv the user paid a
# multi-hundred-MB download for, so it may only ever happen on *definite*
# evidence, and it may only ever happen once per venv per process.


def _marked_venv(proj, *, runnable=False):
    """A venv dir carrying a ready marker, with or without a working python."""
    venv_dir = Path(envinstall.venv_dir_for(proj))
    venv_dir.mkdir(parents=True, exist_ok=True)
    if runnable:
        _runnable_venv_python(str(venv_dir))
    _mark(venv_dir, proj)
    return venv_dir


@requires_fused
def test_a_probe_that_TIMES_OUT_destroys_nothing_and_reports_installed(
    tmp_path, monkeypatch
):
    """An inconclusive probe is not permission to destroy state.

    `TimeoutExpired` is a `SubprocessError`, and the budget is 5s: several
    concurrent `/api/run` calls on a loaded machine can produce one without the
    venv being wrong about anything. Treating that as corruption would unlink the
    marker of a HEALTHY venv and cost the user a full uv re-download. So the
    verdict is `None`, nothing is cached (a timeout says nothing worth
    remembering), and `is_installed` says yes so the run proceeds and surfaces
    whatever really happens — the same policy as `engine._probe`, which reports a
    failed probe and destroys nothing.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj, runnable=True)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="python", timeout=5)

    monkeypatch.setattr(envinstall.subprocess, "run", _timeout)
    assert envinstall.is_installed(proj) is True
    assert (venv_dir / envinstall.READY_MARKER).exists()
    assert str(venv_dir) not in envinstall._VALIDATED


@requires_fused
def test_a_probe_that_cannot_be_SPAWNED_destroys_nothing_and_reports_installed(
    tmp_path, monkeypatch
):
    """EAGAIN/EMFILE/EINTR are facts about the SERVER, not about the venv.

    `OSError` covers "could not fork under load" and "out of file descriptors"
    just as much as "no such interpreter", and only the last of those is evidence
    about the venv. So the generic `OSError` is inconclusive; the exe-specific
    ones are classified as definite by the test below.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj, runnable=True)

    def _eagain(*a, **k):
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(envinstall.subprocess, "run", _eagain)
    assert envinstall.is_installed(proj) is True
    assert (venv_dir / envinstall.READY_MARKER).exists()
    assert str(venv_dir) not in envinstall._VALIDATED


def test_the_probe_classifies_a_returncode_the_way_engine_probe_does(monkeypatch):
    """`_venv_runs` alone, with no `fused` and no venv — the classification IS the
    behaviour under test.

    `engine._probe` already carries this exact rule ("a child killed by a SIGNAL
    — `returncode < 0` — is inconclusive: it never got to answer"), which D277
    gave it while naming this function as the sibling it was not applying it to.
    Written without the `requires_fused` gate the sibling tests need, because
    the branch it covers is the one that decides whether a user's environment
    gets deleted, and it should not go unrun wherever that wheel is absent.
    """
    def _answer(code, **fields):
        monkeypatch.setattr(
            envinstall.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(args=["python"], returncode=code,
                                                        stdout="", stderr=""),
        )
        return envinstall._venv_runs("/nonexistent/venv")

    assert _answer(0) is True
    assert _answer(1) is False, "an interpreter that RAN and refused is definite"
    for signal_number in (11, 9, 15, 64):  # 64 has no name on macOS/Linux
        assert _answer(-signal_number) is None, f"signal {signal_number}"


@requires_fused
def test_a_probe_KILLED_BY_A_SIGNAL_destroys_nothing_and_reports_installed(
    tmp_path, monkeypatch
):
    """A child that was killed never answered, so it is not evidence either.

    This is the half of the three-valued probe D277 left open: the exceptions
    were classified, the RETURN CODE was not, so a negative one — a signal —
    fell through to `returncode != 0` and read as "this venv cannot run its own
    python", which unlinks the marker and buys a multi-GB re-download.

    The signal that motivated it is `-11`. This probe spawns from the SERVER
    process, which has PROJ resident, and until the sibling fix below it did so
    with `close_fds=True`: CPython forks, the child runs PROJ's `pthread_atfork`
    handler, and dies of SIGSEGV at ~1ms without ever reaching the interpreter.
    Both halves are needed — the flag stops the crash we know about, and this
    stops any other signal (an OOM SIGKILL, a shutdown SIGTERM) from being read
    as a verdict about the venv.
    """
    for number in (11, 9, 15):
        proj = _project(tmp_path, f"sig{number}")
        venv_dir = _marked_venv(proj, runnable=True)

        monkeypatch.setattr(
            envinstall.subprocess, "run",
            lambda *a, _n=number, **k: subprocess.CompletedProcess(
                args=["python"], returncode=-_n, stdout="", stderr=""),
        )
        assert envinstall.is_installed(proj) is True, f"signal {number}"
        assert (venv_dir / envinstall.READY_MARKER).exists(), (
            f"a probe killed by signal {number} must not cost the user a rebuild"
        )
        assert str(venv_dir) not in envinstall._VALIDATED
        monkeypatch.undo()


@requires_fused
def test_the_readiness_probe_spawns_off_the_fork_path(tmp_path, monkeypatch):
    """`close_fds=False`, so CPython uses posix_spawn rather than fork()+exec.

    The site D277 flagged and did not touch. It spawns from the server process,
    where `fused`/geopandas have pulled in libproj: `fork()` runs PROJ's atfork
    child handler, which closes a SQLite handle that is no longer valid, and the
    child takes SIGSEGV before exec. posix_spawn runs no atfork handlers.

    Asserted at the call rather than reproduced, exactly as `tests/test_engine.py`
    and `tests/test_worker_forksafe.py` do: the real crash needs a resident
    libproj holding a live handle, which a test cannot arrange.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    _marked_venv(proj, runnable=True)

    seen = {}
    real = envinstall.subprocess.run
    monkeypatch.setattr(
        envinstall.subprocess, "run",
        lambda *a, **k: (seen.update(k), real(*a, **k))[1],
    )
    envinstall.is_installed(proj)

    assert seen.get("close_fds") is False, (
        "the venv readiness probe must pass close_fds=False so the child is "
        "spawned via posix_spawn, not fork() (which runs PROJ's crashing atfork "
        "handler and makes a healthy venv look broken)"
    )


@requires_fused
def test_a_missing_or_unexecutable_interpreter_IS_definite_corruption(
    tmp_path, monkeypatch
):
    """The exe-specific OSErrors keep their old meaning.

    `FileNotFoundError`/`PermissionError`/`NotADirectoryError` out of the spawn
    are about the path we asked to execute, so they are evidence about the venv —
    and making the inconclusive case safe must not accidentally make the definite
    case a no-op, which would leave the DMG bug unfixed.
    """
    for exc in (FileNotFoundError(), PermissionError(), NotADirectoryError()):
        proj = _project(tmp_path, type(exc).__name__)
        venv_dir = _marked_venv(proj, runnable=True)

        def _raise(*a, _exc=exc, **k):
            raise _exc

        monkeypatch.setattr(envinstall.subprocess, "run", _raise)
        assert envinstall.is_installed(proj) is False
        assert not (venv_dir / envinstall.READY_MARKER).exists()
        monkeypatch.undo()  # restore subprocess.run (and venvs_path) per iteration


@requires_fused
def test_only_ONE_rebuild_is_attempted_per_venv_per_process(tmp_path, monkeypatch):
    """The bound that stops a futile download loop across runs.

    For a user on a pre-symlink `.app` the rebuild reproduces the identical
    breakage — the property that failed is the interpreter's own base prefix,
    which is invariant under rebuild, and the venv key folds in only that
    interpreter's path and version, both constants inside the bundle. Without a
    bound, every page reload, every `watchPath` auto-reload and every param change
    would pay another multi-hundred-MB download, guaranteed futile; before D212
    that cohort got one instant permanent error, so an unbounded rebuild would be
    a regression.

    So the SECOND definite failure for the same venv dir leaves the marker alone
    and reports installed: the run goes ahead and the user sees the interpreter's
    own stderr instead of another download. And no further probe is spawned —
    `is_installed` is called on every run of every PEP 723 script.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj)  # no interpreter at all
    marker = venv_dir / envinstall.READY_MARKER

    probes = []
    real = envinstall._venv_runs
    monkeypatch.setattr(
        envinstall, "_venv_runs", lambda d: (probes.append(d), real(d))[1]
    )

    assert envinstall.is_installed(proj) is False
    assert not marker.exists(), "the first failure earns one rebuild"

    # What the install worker leaves behind on a bundle that cannot be fixed by
    # rebuilding: the same broken venv, re-marked ready.
    _mark(venv_dir, proj)
    assert envinstall.is_installed(proj) is True, "no second rebuild"
    assert marker.exists(), "the marker must survive, or the loader downloads again"

    for _ in range(3):
        assert envinstall.is_installed(proj) is True
    assert len(probes) == 2, probes


@requires_fused
def test_reset_venv_validation_cache_clears_the_rebuild_bound(tmp_path, monkeypatch):
    """A fresh process gets a fresh rebuild — that is what heals a fixed DMG.

    The bound is per PROCESS on purpose: a user who installs a build with the
    `Contents/lib` symlink starts a new server, hence an empty set, hence exactly
    one rebuild, which this time works. `reset_venv_validation_cache` is the test
    seam for that lifecycle, so it has to clear the bound as well as the verdicts —
    otherwise it would only half-reset and every test after it would inherit the
    other half.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj)
    marker = venv_dir / envinstall.READY_MARKER

    assert envinstall.is_installed(proj) is False
    _mark(venv_dir, proj)
    assert envinstall.is_installed(proj) is True  # bound engaged

    envinstall.reset_venv_validation_cache()
    assert envinstall.is_installed(proj) is False, "a new process retries the repair"
    assert not marker.exists()


@requires_fused
def test_a_caller_that_LOSES_the_unmark_race_reports_not_installed(
    tmp_path, monkeypatch
):
    """The bound must be consulted against a marker that is still THERE.

    "stat the marker -> probe -> consult the bound -> unlink" is not atomic, and
    the endpoints run in FastAPI's threadpool, so two pre-flights genuinely
    interleave: A passes the marker check, probes, records the attempt and unlinks;
    B passed the marker check BEFORE A's unlink but reaches the bound AFTER A's
    add. B would then see `already_tried`, announce that a rebuild had already been
    tried (it had not — A's rebuild has not even been requested yet) and return
    True, executing a venv known to be broken instead of joining the install A just
    asked for.

    Driven deterministically rather than with threads and sleeps: the probe unlinks
    the marker as its side effect, which is exactly A's unlink landing inside B's
    window. With the marker re-checked inside the critical section, B answers False
    and joins the install.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj)  # no interpreter at all
    marker = venv_dir / envinstall.READY_MARKER

    # "A, earlier in this process": one definite failure, so the venv dir is in the
    # rebuild set — the precondition the racing branch needs.
    assert envinstall.is_installed(proj) is False
    marker.write_text("{}")  # what the install worker leaves behind

    def _probe_that_loses_the_race(venv):
        os.unlink(os.path.join(venv, envinstall.READY_MARKER))  # A's unlink lands
        return False

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_that_loses_the_race)
    assert envinstall.is_installed(proj) is False, "join the install, do not run"
    assert not marker.exists()


@requires_fused
def test_the_bound_warns_ONCE_per_venv_however_many_requests_arrive(
    tmp_path, monkeypatch, caplog
):
    """The log has to stay readable for the incident it exists to diagnose.

    Once the bound engages, `is_installed` answers from the cached verdict on every
    subsequent call — every page reload, every `watchPath` auto-reload, every param
    change — so warning each time would repeat the same multi-line message forever
    and bury the one occurrence that matters. Warn on the transition, debug after.
    The user-facing behaviour is unchanged: still True, still marker in place.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj)
    marker = venv_dir / envinstall.READY_MARKER

    assert envinstall.is_installed(proj) is False  # the one rebuild it gets
    marker.write_text("{}")  # the rebuild reproduced the same breakage

    needle = "still cannot run its own python after a rebuild"
    with caplog.at_level(logging.WARNING, logger="fused_render.envinstall"):
        caplog.clear()
        for _ in range(4):
            assert envinstall.is_installed(proj) is True
    warnings = [r for r in caplog.records if needle in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert marker.exists()


@requires_fused
def test_concurrent_callers_still_pay_no_probe_per_request(tmp_path, monkeypatch):
    """The ceiling, under threads: probes are per venv, not per call.

    The endpoints are sync `def` and run in FastAPI's threadpool, so this is the
    real shape of the traffic. A handful of duplicate probes in the initial race is
    accepted by design (see `_venv_is_usable`: the probe runs OUTSIDE the lock now,
    and a duplicated read-only `-c ""` is a far better trade than a global stall) —
    what must never happen is a probe per call, which is what an unmemoized or
    wrongly-invalidated cache would give.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj, runnable=True)

    probes = []
    probe_lock = threading.Lock()
    real = envinstall._venv_runs

    def _counting(d):
        with probe_lock:
            probes.append(d)
        return real(d)

    monkeypatch.setattr(envinstall, "_venv_runs", _counting)

    results = []
    def _hammer():
        for _ in range(20):
            results.append(envinstall.is_installed(proj))

    threads = [threading.Thread(target=_hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(results) and len(results) == 160
    racing = len(probes)
    assert racing <= len(threads), f"{racing} probes for one venv across 8 threads"

    # Steady state: once a verdict is stored, further calls must add nothing.
    for _ in range(10):
        assert envinstall.is_installed(proj) is True
    assert len(probes) == racing, "a probe per request in the steady state"


@requires_fused
def test_a_verdict_probed_ACROSS_a_discard_is_never_cached(tmp_path, monkeypatch):
    """The invariant: a verdict is only ever cached against the generation it judged.

    The probe runs outside the lock, so a concurrent `is_installed` can discard this
    venv — end its generation — while we are still probing it. Storing our verdict
    then would attach an answer about a DESTROYED venv to whatever replaces it under
    the same key, which is the same defect class as caching an inconclusive probe:
    the cache answers a question nobody asked it, and nothing ever re-asks.

    So a generation change means: return our answer to our own caller (it is what we
    genuinely measured) and store NOTHING. The next call re-probes. Deliberately no
    cleverness about which verdict is "fresher" — there is no evidence either way.

    Driven by making the probe itself perform the discard, which is exactly what a
    racing caller's discard looks like from here: no threads, no sleeps.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj, runnable=True)

    probes = []
    real = envinstall._venv_runs

    def _probe_then_someone_else_discards(d):
        verdict = real(d)
        with envinstall._validated_lock:
            envinstall._discard_verdict(d)  # what a concurrent discard does
        probes.append(d)
        return verdict

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_then_someone_else_discards)
    assert envinstall.is_installed(proj) is True, "our own caller still gets an answer"
    assert str(venv_dir) not in envinstall._VALIDATED, "stored against a dead generation"
    assert envinstall.is_installed(proj) is True
    assert len(probes) == 2, "the next call must re-probe, not trust the stale verdict"


@requires_fused
def test_a_marker_REPLACED_during_the_probe_is_not_unlinked(tmp_path, monkeypatch):
    """Identity, not existence: the marker we answer about must be the one we judged.

    Inside the probe window the install worker can finish a rebuild and write a
    FRESH marker. A boolean `exists()` re-check reads True for that, so the bound
    would be consulted against a verdict describing the venv that was destroyed —
    and the first-failure branch would unlink the marker of a freshly rebuilt,
    possibly healthy venv and force another download. Comparing
    `(st_ino, st_mtime_ns)` sees the swap that `exists()` cannot.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj)  # no interpreter -> False
    marker = venv_dir / envinstall.READY_MARKER
    before = envinstall._marker_stamp(str(marker))

    def _probe_while_the_worker_re_marks(d):
        m = os.path.join(d, envinstall.READY_MARKER)
        os.unlink(m)  # the rebuild starts
        with open(m + ".new", "w") as fh:  # ... and finishes, marker and all
            fh.write("{}")
        os.replace(m + ".new", m)
        return False

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_while_the_worker_re_marks)
    assert envinstall._marker_stamp(str(marker)) == before, "setup: stamp not captured"

    assert envinstall.is_installed(proj) is False, "answer about the venv we judged"
    assert marker.exists(), "the REBUILT venv's marker must survive"
    assert envinstall._marker_stamp(str(marker)) != before, "setup: stamp unchanged"


@requires_fused
def test_a_TRUE_verdict_is_not_returned_once_the_marker_has_VANISHED(
    tmp_path, monkeypatch
):
    """A "ready" answer requires the marker to still be there when we answer.

    `is_installed` stamps the marker, probes, then answers. If the worker un-marks
    and starts rebuilding inside that window, answering True hands `/api/run` a
    directory being rebuilt underneath it — a confusing mid-rebuild failure instead
    of the install loader. So the answer is False: the caller reports
    `needs_install` and joins the install already in flight (`start()` joins rather
    than duplicating), which is the same conclusion the replaced-marker branch
    reaches, through the same code path.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj, runnable=True)
    marker = venv_dir / envinstall.READY_MARKER
    real = envinstall._venv_runs

    def _probe_while_the_worker_unmarks(d):
        verdict = real(d)
        os.unlink(os.path.join(d, envinstall.READY_MARKER))
        return verdict

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_while_the_worker_unmarks)
    assert envinstall.is_installed(proj) is False, "do not run against a rebuild"
    assert str(venv_dir) not in envinstall._VALIDATED, "and do not keep the verdict"
    assert not marker.exists()


@requires_fused
def test_reset_ends_every_generation_so_an_inflight_probe_cannot_store(
    tmp_path, monkeypatch
):
    """`reset_venv_validation_cache` has to END generations, not just empty the dict.

    A probe already in flight when the reset lands measured the world before it. If
    the reset merely cleared `_VALIDATED`, that probe would find the key absent
    afterwards and happily insert a pre-reset verdict — the reset would be undone by
    the very call it was meant to invalidate.
    """
    proj = _project(tmp_path, deps=["some-dist"])
    venv_dir = _marked_venv(proj, runnable=True)
    real = envinstall._venv_runs

    def _probe_then_reset(d):
        verdict = real(d)
        envinstall.reset_venv_validation_cache()
        return verdict

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_then_reset)
    assert envinstall.is_installed(proj) is True
    assert str(venv_dir) not in envinstall._VALIDATED


@requires_fused
def test_a_stalled_probe_of_one_venv_does_not_block_another(tmp_path, monkeypatch):
    """One wedged venv must not freeze `is_installed` for every other venv.

    The probe budget is 5s and a venv can live on a wedged network mount — a
    failure mode this repo fights elsewhere. With the module-global lock held
    ACROSS the probe, that one stalled probe blocked EVERY `is_installed`,
    including cached hits for venvs that are perfectly fine; combined with the
    pre-flight running on the event loop, one stuck venv froze the whole app.

    Driven by an `Event` rather than by timing, so there is no race to lose: the
    probe of A parks inside the critical region for as long as this test says, and
    the answer for B has to arrive anyway.
    """
    proj_a = _project(tmp_path, "a", deps=["dist-a"])
    proj_b = _project(tmp_path, "b", deps=["dist-b"])
    _marked_venv(proj_a, runnable=True)
    _marked_venv(proj_b, runnable=True)
    assert envinstall.is_installed(proj_b) is True  # B's verdict is now cached

    in_probe, release = threading.Event(), threading.Event()

    def _stall(venv_dir):
        in_probe.set()
        release.wait(timeout=30)
        return True

    monkeypatch.setattr(envinstall, "_venv_runs", _stall)

    stalled = threading.Thread(target=envinstall.is_installed, args=(proj_a,))
    stalled.start()
    try:
        assert in_probe.wait(timeout=10), "the probe of A never started"
        answered = []
        b = threading.Thread(target=lambda: answered.append(
            envinstall.is_installed(proj_b)))
        b.start()
        b.join(timeout=10)
        assert answered == [True], "B waited on A's stalled probe"
    finally:
        release.set()
        stalled.join(timeout=30)


# --- /api/run's pre-flight ----------------------------------------------------


@requires_fused
def test_a_declared_project_with_no_venv_asks_for_an_install(tmp_path, monkeypatch):
    """The pre-flight answers instead of blocking on a download."""
    proj = _project(tmp_path, "needy", deps=["imagecodecs", "pyproj"])
    target = Path(proj) / "needs.py"
    target.write_text("def main():\n    return 1\n")
    import asyncio

    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    need = out["needs_install"]
    assert need["requirements"] == ["imagecodecs", "pyproj"]
    assert need["key"] == envinstall.venv_key_for(proj)
    assert need["project"] == proj
    # The error object is still populated: a client that knows nothing about
    # needs_install shows a real message rather than "undefined".
    assert out["error"]["type"] == "EnvNotInstalled"
    assert "imagecodecs" in out["error"]["message"]


@requires_fused
def test_a_project_whose_venv_exists_just_runs(tmp_path, monkeypatch, warm_fused_backend_venv):
    """No pre-flight interference once the venv is there."""
    import asyncio

    import conftest

    # This module's autouse fixture redirects FUSED_RENDER_HOME per test, and the
    # venv store hangs off it — so without restoring the home the warm fixture
    # built under, this would look for the venv in a directory nobody filled.
    if conftest.WARM_HOME is None:
        monkeypatch.delenv("FUSED_RENDER_HOME", raising=False)
    else:
        monkeypatch.setenv("FUSED_RENDER_HOME", conftest.WARM_HOME)
    monkeypatch.setattr(engine, "_backend", None)
    target = Path(warm_fused_backend_venv) / "ready.py"
    target.write_text("def main():\n    return 42\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is True, out
    assert out["result"] == 42
    assert "needs_install" not in out


@pytest.mark.parametrize("boom", [
    # `is_installed` reaches `fused.agent_core...` unguarded — no fused, no import.
    ImportError("No module named 'fused'"),
    # `_backend_attr` raises this BY DESIGN when an upstream private attribute
    # disappears: guessing would build the environment on the wrong interpreter.
    # routers/env.py already catches (ImportError, RuntimeError) for exactly this pair.
    RuntimeError("this fused build's Backend has no '_python_executable'"),
])
def test_a_preflight_that_cannot_answer_returns_the_house_error_shape(
    tmp_path, monkeypatch, boom
):
    """The pre-flight must fail like every other failure in `run_python`.

    It used to sit ABOVE the try/except that the function's own comment says
    catches "every other failure", so an `is_installed` that raised escaped as an
    unhandled exception. /api/run's handler turns that into a 500 whose body is
    `{"error": "<string>"}`, and runtime.js reads `data.error.message` off it —
    the user is shown the literal text `undefined` for a diagnostic that was
    written to be read.
    """
    import asyncio

    def _raise(*a, **kw):
        raise boom

    monkeypatch.setattr(envinstall, "is_installed", _raise)
    proj = _project(tmp_path, "needy", deps=["imagecodecs"])
    target = Path(proj) / "needs.py"
    target.write_text("def main():\n    return 1\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert isinstance(out["error"], dict), out
    assert set(out["error"]) >= {"type", "message", "traceback"}
    assert str(boom) in out["error"]["traceback"]


def test_a_headerless_script_never_asks_for_an_install(tmp_path, monkeypatch):
    """Nothing to install: it runs on the app's interpreter (PY-17)."""
    import asyncio

    class _R:
        error = None
        stdout = stderr = ""
        duration_ms = 1
        return_value = "1"
        response = None

    class _B:
        def _execute_sync(self, **kw):
            return _R()

        async def execute(self, **kw):
            return _R()

    monkeypatch.setattr(engine, "get_backend", lambda: _B())
    target = tmp_path / "plain.py"
    target.write_text("def main():\n    return 1\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert "needs_install" not in out


# --- the worker ---------------------------------------------------------------


@requires_fused
def test_the_worker_builds_the_venv_and_reports_done(tmp_path, monkeypatch):
    """End to end through the real worker: a venv appears, progress says done.

    `pip` because the dev-env recipe already seeds it into this interpreter, so
    uv resolves it from cache — this test is about the loader, not the network.
    """
    proj = _project(tmp_path, deps=["pip"])
    key = envinstall.venv_key_for(proj)
    envinstall.start(proj)
    prog = _wait_done(key, timeout=300)
    assert prog["error"] is None, prog
    assert prog["done"] is True
    assert prog["stage"] == "done"
    assert prog["pct"] == 100
    assert envinstall.is_installed(proj)


@requires_fused
def test_a_resolver_failure_reaches_the_user_verbatim(tmp_path, monkeypatch):
    """The whole point of making this visible.

    A distribution that cannot resolve must surface uv's/pip's own words, not an
    `EngineError` about "an internal error while running <path>". The assertion
    is deliberately on the resolver's text, not on a message we wrote.
    """
    # A name PyPI cannot have: no index lookup can succeed, and the failure is
    # the resolver's, which is exactly the class of error being surfaced.
    proj = _project(tmp_path, deps=["fused-render-no-such-distribution-9e3f1c"])
    key = envinstall.venv_key_for(proj)
    envinstall.start(proj)
    prog = _wait_done(key, timeout=300)
    assert prog["done"] is True
    assert prog["error"], prog
    assert "fused-render-no-such-distribution-9e3f1c" in prog["error"]
    assert not envinstall.is_installed(proj)


@requires_fused
def test_a_worker_that_died_unreaped_is_not_reported_alive(tmp_path, monkeypatch):
    """The zombie trap, which the pid-2**31-1 test below cannot see.

    That test uses an impossible pid, so it only proves the "pid does not exist"
    branch. A REAL worker is different: `start_new_session=True` does not reparent
    it — it stays our child until someone waits on it — and a ZOMBIE answers
    `os.kill(pid, 0)` successfully. So a worker that exited before writing `done`
    (a bad import, a kill) read as "still running" indefinitely: `progress()`
    never reaped it into an error, the page polled a corpse, and any bounded
    waiter burned its whole timeout. Found while investigating a slow CI job —
    which turned out to be legitimately slow, not hung, but the bug is real.

    The pid is registered in `_SPAWNED` because that is what `_spawn` does with a
    real worker's pid, and only registered pids may be reaped (see
    `test_a_pid_this_module_did_not_spawn_is_never_reaped` for the other half of
    that rule). Standing the process up here rather than through `_spawn` is what
    makes the zombie reachable at all.
    """
    dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"],
                            start_new_session=True)
    # A copy, so the pid does not leak into the module's real set past this test.
    monkeypatch.setattr(envinstall, "_SPAWNED", {dead.pid})
    # A plain sleep, NOT `dead.poll()` / `dead.wait()`: those call waitpid and
    # REAP the child, so polling for its exit destroys the very zombie this test
    # needs. (First version of this test did exactly that and skipped itself even
    # with the bug reintroduced — a test that cannot fail.)
    time.sleep(1.5)
    try:
        os.kill(dead.pid, 0)
    except ProcessLookupError:
        pytest.skip("the child was reaped already; no zombie to model here")

    key = "0123456789abcdef"
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "progress.json"), "w", encoding="utf-8") as f:
        json.dump({"stage": "spawn", "pct": 0, "detail": "", "done": False,
                   "error": None, "pid": dead.pid, "ts": time.time()}, f)

    assert envinstall._pid_alive(dead.pid) is False, "a zombie is not alive"
    prog = envinstall.progress(key)
    assert prog["done"] is True, "a dead worker must end the poll"
    assert "without finishing" in prog["error"]


@pytest.mark.skipif(os.name == "nt", reason="waitpid reaping is POSIX-only")
def test_a_pid_this_module_did_not_spawn_is_never_reaped():
    """`_pid_alive` must not steal another part of the server's child.

    The pid comes out of `progress.json`, and a not-`done` record survives a
    server crash mid-install — so it can name a pid that has since been recycled
    onto a child of the CURRENT server (an rclone rcd, a template tile daemon, a
    pyramid build worker). Reaping that child makes its owner's later
    `poll()`/`wait()` fail with `ECHILD`, which subprocess reports as **exit
    status 0**: a process that crashed, or one that is still needed, read as
    "finished successfully". Every one of those owners branches on that status.

    So the reap is gated on "we spawned this pid", tracked in-process. Modelled
    with a child that exits non-zero and is left unreaped, exactly as a recycled
    pid would appear.
    """
    other = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    # A plain sleep, NOT `poll()`: polling reaps, which destroys the very
    # unreaped-child state this test needs.
    time.sleep(1.5)
    envinstall._pid_alive(other.pid)
    assert other.wait(timeout=30) == 3, (
        "_pid_alive reaped a child it did not spawn, so its owner now reads the "
        "exit status as 0"
    )


@requires_fused
def test_a_dead_worker_is_reported_as_finished_not_pending(tmp_path, monkeypatch):
    """A killed installer must not leave the page polling forever.

    Same liveness check as docs.py's `_install_progress`: a not-done record
    whose pid is gone is a crash, and the poller has to be told so.
    """
    key = "deadbeefdeadbeef"
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "progress.json"), "w", encoding="utf-8") as f:
        # A pid that cannot be running: 2**31-1 is above every platform's pid_max.
        json.dump({"stage": "install", "pct": 25, "detail": "", "done": False,
                   "error": None, "pid": 2 ** 31 - 1, "ts": time.time()}, f)
    prog = envinstall.progress(key)
    assert prog["done"] is True
    assert "without finishing" in prog["error"]


@requires_fused
@pytest.mark.parametrize("final", ["success", "real error"])
def test_a_worker_that_finished_during_the_liveness_check_is_not_called_a_crash(
    tmp_path, monkeypatch, final
):
    """The record is re-read before a dead pid is reported as a crash.

    `progress()` reads progress.json, THEN asks whether the pid is alive — and
    that read is stale by construction, because `_pid_alive` reaps and so answers
    "dead" only once the worker is already gone. A worker writes its final record
    and then exits, so "the record said not-done" + "the pid is gone" is equally
    what SUCCESS looks like through a stale read.

    Modelled by having the liveness check itself write the final record, which is
    exactly the ordering the real worker produces. Without the re-read this
    returns "the installer exited unexpectedly" for a completed install, and
    runtime.js renders that as a hard failure over a venv that is ready.
    """
    key = "beefbeefbeefbeef"
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "progress.json")
    pending = {"stage": "install", "pct": 50, "detail": "", "done": False,
               "error": None, "pid": 4242, "ts": time.time()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pending, f)

    err = None if final == "success" else "could not resolve nosuchpkg"

    def _dead_and_finished(pid):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**pending, "stage": "done", "pct": 100, "done": True,
                       "error": err}, fh)
        return False

    monkeypatch.setattr(envinstall, "_pid_alive", _dead_and_finished)
    prog = envinstall.progress(key)
    assert prog["done"] is True
    assert prog["error"] == err, (
        "the worker's own final record must win over the synthesised crash error"
    )
    if final == "success":
        assert prog["pct"] == 100


@requires_fused
@pytest.mark.parametrize("detached", [True, False], ids=["group-leader", "same-group"])
def test_cancellation_kills_the_recorded_pid(tmp_path, monkeypatch, detached):
    """Cancel by the pid the worker recorded, and say the install was cancelled.

    Both cases, because `cancel` signals the process GROUP — it has to, or the
    uv download the worker is waiting on survives. The real worker is spawned
    `start_new_session`, so it leads its own group and `killpg` is safe. The
    `same-group` case is the hazard: the pid comes out of a file, and a stale or
    recycled one inside the SERVER's group would make an unguarded `killpg` take
    the server down with it. It killed a pytest session while this was being
    written, which is why the case is parametrized rather than assumed.
    """
    # A child that will not finish on its own, standing in for a slow download.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=detached,
    )
    key = "ca9ce11ed0000001"  # 16 hex: keys are validated now
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "progress.json"), "w", encoding="utf-8") as f:
        json.dump({"stage": "install", "pct": 25, "detail": "", "done": False,
                   "error": None, "pid": child.pid, "ts": time.time()}, f)
    try:
        assert envinstall.cancel(key) is True
        deadline = time.time() + 30
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert child.poll() is not None, "the recorded pid should have been killed"
        prog = envinstall.progress(key)
        assert prog["done"] is True
        assert "cancel" in (prog["error"] or "").lower()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


@requires_fused
def test_cancelling_a_pid_in_our_own_group_does_not_kill_us(tmp_path, monkeypatch):
    """The guard, asserted directly rather than only via the parametrized case.

    Our own pid is in our own group and is not its leader under pytest, so a
    naive `killpg(getpgid(pid))` would SIGTERM this process. `_kill` must reach
    for the single-pid path instead.
    """
    sent = []
    monkeypatch.setattr(envinstall.os, "killpg",
                        lambda *a: pytest.fail("must not signal our own group"))
    monkeypatch.setattr(envinstall.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    if os.getpgid(os.getpid()) == os.getpid():
        pytest.skip("this process leads its own group, so there is no hazard to model")
    assert envinstall._kill(os.getpid()) is True
    assert sent == [(os.getpid(), signal.SIGTERM)]


@requires_fused
def test_the_worker_is_told_the_venv_directory_rather_than_deriving_it(
    tmp_path, monkeypatch
):
    """The venv directory travels in argv, and it is `venv_dir_for`'s.

    The worker cannot import `fused_render` (D152), so it cannot call
    `projectenv` — and a second derivation of a cache key is exactly how a loader
    ends up filling a directory no run ever reads: the page installs, retries, is
    told `needs_install` again, and runtime.js turns that into a permanent
    "not installed yet" with a fully built venv sitting on disk. So the server
    computes it once and hands it over.
    """
    proj = _project(tmp_path, deps=["pip"])
    key = envinstall.venv_key_for(proj)

    argv = []

    class _Proc:
        pid = os.getpid()

    monkeypatch.setattr(envinstall.subprocess, "Popen",
                        lambda cmd, **kw: (argv.extend(cmd), _Proc())[1])
    envinstall._spawn(key, proj)

    # argv is [python, worker.py, key, progress_dir, project, venv, cache, py, acq]
    assert argv[2] == key
    assert argv[4] == os.path.abspath(proj)
    assert argv[5] == envinstall.venv_dir_for(proj), (
        "the worker was pointed at a different directory than the server looks in"
    )
    assert argv[6] == projectenv.uv_cache_dir()


@requires_fused
def test_the_worker_syncs_the_project_into_the_named_venv(tmp_path, monkeypatch):
    """`uv sync`, in the project dir, with the venv redirected out of it.

    UV_PROJECT_ENVIRONMENT is what keeps the user\'s folder free of a `.venv`, and
    UV_CACHE_DIR is what keeps cache and target on one filesystem so uv hardlinks
    instead of silently copying. Both are environment, not flags, because uv reads
    them itself.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    cache = str(tmp_path / "home" / "uv-cache")
    worker = _worker_module("_env_install_worker_sync")

    seen = {}

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        seen["env"] = kw.get("env")
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    worker._build(proj, venv_dir, cache, "3.12")

    assert seen["cmd"] == [
        "/usr/bin/uv", "sync", "--no-default-groups", "--python", "3.12",
    ]
    assert "--frozen" not in seen["cmd"], "no lock yet, so uv must resolve and write one"
    assert seen["cwd"] == proj
    assert seen["env"]["UV_PROJECT_ENVIRONMENT"] == venv_dir
    assert seen["env"]["UV_CACHE_DIR"] == cache
    assert not os.path.exists(os.path.join(proj, ".venv"))


@requires_fused
def test_the_sync_leaves_the_users_link_mode_alone(tmp_path, monkeypatch):
    """`UV_LINK_MODE` is inherited, never set and never stripped.

    uv prefers hardlinks and degrades on its own, so there is nothing to pin —
    but stripping an inherited value is the same override in the other
    direction. Someone who exported `UV_LINK_MODE=copy` had a reason (a cache
    and a venv on different mounts they cannot co-locate, an overlayfs where
    hardlinks fail), and silently dropping it breaks every project sync in a way
    that surfaces as an unexplained uv error.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_linkmode")
    seen = {}

    def _fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setenv("UV_LINK_MODE", "copy")

    worker._build(proj, venv_dir, str(tmp_path / "home" / "uv-cache"), "3.12")

    assert seen["env"]["UV_LINK_MODE"] == "copy"


@requires_fused
def test_the_sync_skips_default_dependency_groups(tmp_path, monkeypatch):
    """PY-16 makes `[project].dependencies` the WHOLE declaration.

    Without `--no-default-groups`, `uv sync` also installs `[dependency-groups]
    dev` — which `uv init` and `uv add --dev` write — so the venv would contain
    packages `applicable_dependencies_of` never reported. The loader's "not
    installed yet" list, the `app_satisfies` fast path and the environment
    actually built would then be describing three different sets.
    """
    proj = _project(tmp_path, deps=["pip"])
    with open(os.path.join(proj, "pyproject.toml"), "a", encoding="utf-8") as f:
        f.write('\n[dependency-groups]\ndev = ["pytest"]\n')
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_groups")
    seen = {}

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    worker._build(proj, venv_dir, str(tmp_path / "home" / "uv-cache"), "3.12")

    assert "--no-default-groups" in seen["cmd"]


@requires_fused
def test_a_locked_project_is_never_synced_frozen(tmp_path, monkeypatch):
    """`--frozen` turns a manifest edit into an error instead of reconciling it.

    Bare `uv sync` already honours a current lock and re-resolves only what a
    manifest edit moved — which IS the required behaviour, because a user must
    never have to run `uv sync` by hand (that would create an in-folder `.venv`
    and diverge from the home-dir store).
    """
    proj = _project(tmp_path, deps=["pip"])
    open(os.path.join(proj, "uv.lock"), "w").write("version = 1\n")
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_frozen")

    seen = {}

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")
    assert "--frozen" not in seen["cmd"], (
        "a locked project synced --frozen fails on a manifest edit instead of "
        "picking it up"
    )


@requires_fused
def test_the_worker_writes_the_sidecar_before_the_ready_marker(tmp_path, monkeypatch):
    """Order matters: a venv must never read as ready before it can say what it holds.

    Marking first would leave a window in which `is_installed` sees the marker,
    finds no sidecar, calls the venv stale and asks for an immediate rebuild.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = envinstall.venv_dir_for(proj)
    worker = _worker_module("_env_install_worker_sidecar")

    marker_present_when_sidecar_landed = []
    real_replace, real_open = os.replace, open

    def _fake_run(cmd, **kw):
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()
        # `uv sync` writes the lock for an unlocked project; the digest has to be
        # taken from THAT, not from the pre-sync state.
        with real_open(os.path.join(proj, "uv.lock"), "w") as fh:
            fh.write("version = 1\n")

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        worker.os, "replace",
        lambda a, b: (
            marker_present_when_sidecar_landed.append(
                os.path.exists(os.path.join(venv_dir, envinstall.READY_MARKER))
            ),
            real_replace(a, b),
        )[1],
    )

    worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")

    assert marker_present_when_sidecar_landed == [False], (
        "the venv was marked ready before it could say what it holds"
    )
    assert os.path.exists(os.path.join(venv_dir, envinstall.READY_MARKER))
    # The digest is the POST-sync one — `uv sync` wrote the lock — so the fresh
    # venv is not instantly stale.
    assert projectenv.read_sidecar(venv_dir) == {
        "path": proj, "digest": projectenv.state_digest(proj)
    }
    assert projectenv.sidecar_matches(venv_dir, proj)


@requires_fused
def test_an_unmarked_venv_directory_is_removed_before_syncing(tmp_path, monkeypatch):
    """D212\'s repair has to be a replacement, not a reconcile.

    The failure it exists for is a venv whose recorded base prefix is gone, which
    `uv sync` would happily leave in place because the packages inside it are
    already correct. The marker\'s absence is the only signal the directory is not
    to be trusted.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = envinstall.venv_dir_for(proj)
    os.makedirs(venv_dir)
    open(os.path.join(venv_dir, "leftover"), "w").close()
    worker = _worker_module("_env_install_worker_rmtree")

    def _fake_run(cmd, **kw):
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")
    assert not os.path.exists(os.path.join(venv_dir, "leftover"))


@requires_fused
def test_a_read_only_project_syncs_in_a_mirror_beside_the_venv(tmp_path, monkeypatch):
    """`uv sync` WRITES `uv.lock` into the directory it runs in.

    A project folder that cannot be written to therefore failed the sync outright
    — `failed to write to file .../uv.lock: Read-only file system (os error 30)`,
    no environment built. Which is every AI model download on the packaged Linux
    and Windows builds: the runner folders ship inside the app, the AppImage runs
    from a read-only squashfs mount, and a `Program Files` install is not
    user-writable. The venv, cache and interpreter are unchanged; only the
    directory uv is allowed to litter moves.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_ro")
    seen = {}

    def _fake_run(cmd, **kw):
        seen["cwd"] = kw.get("cwd")
        seen["env"] = kw.get("env")
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")
    finally:
        os.chmod(proj, 0o755)

    mirror = venv_dir + ".src"
    assert seen["cwd"] == mirror, "the sync ran where uv cannot write its lock"
    with open(os.path.join(mirror, "pyproject.toml"), encoding="utf-8") as fh:
        mirrored = fh.read()
    with open(os.path.join(proj, "pyproject.toml"), encoding="utf-8") as fh:
        assert mirrored == fh.read(), "the mirror must declare what the project does"
    # The environment is the same one either way.
    assert seen["env"]["UV_PROJECT_ENVIRONMENT"] == venv_dir


@requires_fused
def test_a_writable_project_gets_no_mirror(tmp_path, monkeypatch):
    """The mirror is a fallback, not a layer: a user's folder syncs in itself.

    The lock uv writes there is source and belongs in the user's tree (MD-7 puts
    only DERIVED state in the home dir), so a mirror for a writable folder would
    quietly stop that folder ever gaining a lock to commit.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_rw")
    seen = {}

    def _fake_run(cmd, **kw):
        seen["cwd"] = kw.get("cwd")
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")

    assert seen["cwd"] == proj
    assert not os.path.exists(venv_dir + ".src")


def _seed_mirror(proj, mirror, *, lock, manifest="same"):
    """A mirror left behind by an earlier build: its lock, and its record of the
    manifest that lock was resolved against.

    `manifest="same"` copies the project's current declaration, which is the state
    after any successful build; `"stale"` writes something else, standing in for a
    release that edited the manifest since. The mirror's manifest copy IS that
    record — `_sync_root` compares it byte-for-byte and expires the lock on a
    difference — so a test that seeds a lock without one is not describing any
    state the worker can actually produce.
    """
    os.makedirs(mirror, exist_ok=True)
    with open(os.path.join(mirror, "uv.lock"), "w", encoding="utf-8") as fh:
        fh.write(lock)
    with open(os.path.join(proj, "pyproject.toml"), encoding="utf-8") as fh:
        current = fh.read()
    with open(os.path.join(mirror, "pyproject.toml"), "w", encoding="utf-8") as fh:
        fh.write(current if manifest == "same" else manifest)


@requires_fused
def test_the_mirror_keeps_the_lock_it_RESOLVED_last_time(tmp_path, monkeypatch):
    """Which is the reason the mirror is a directory that persists at all.

    A read-only folder ships no lock and can never gain one, so without this every
    rebuild of a bundled runner re-resolves ctranslate2/torch from PyPI and can
    pick up versions no release ever tested. Kept here, the first build's
    resolution is what later builds reconcile against.

    The manifest is UNCHANGED here, which is the condition: this is the
    rebuild-after-repair case (D212 removed a venv whose base interpreter had
    gone), where nothing about the declaration moved and re-resolving would only
    risk picking different versions for the same request.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    mirror = venv_dir + ".src"
    _seed_mirror(proj, mirror, lock="version = 1  # what the first build resolved\n")
    worker = _worker_module("_env_install_worker_mirror_lock")

    def _fake_run(cmd, **kw):
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")
    finally:
        os.chmod(proj, 0o755)

    with open(os.path.join(mirror, "uv.lock"), encoding="utf-8") as fh:
        assert "the first build resolved" in fh.read()


@requires_fused
def test_a_lock_the_project_SHIPS_wins_over_the_mirrors(tmp_path, monkeypatch):
    """Source beats derived, the same way the manifest does.

    A runner folder that commits a `uv.lock` is stating the versions it was tested
    at, and a lock left over from an earlier release's own resolution must not
    outrank it.
    """
    proj = _project(tmp_path, deps=["pip"])
    with open(os.path.join(proj, "uv.lock"), "w", encoding="utf-8") as fh:
        fh.write("version = 1  # shipped with the app\n")
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    mirror = venv_dir + ".src"
    # Manifest unchanged, so the mirror's lock is one the expiry rule would KEEP:
    # what overwrites it here is the shipped lock and nothing else.
    _seed_mirror(proj, mirror, lock="version = 1  # left over from a previous release\n")
    worker = _worker_module("_env_install_worker_shipped_lock")

    def _fake_run(cmd, **kw):
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")
    finally:
        os.chmod(proj, 0o755)

    with open(os.path.join(mirror, "uv.lock"), encoding="utf-8") as fh:
        assert "shipped with the app" in fh.read()


@requires_fused
def test_the_error_for_a_read_only_project_names_the_PROJECT(tmp_path, monkeypatch):
    """Not the mirror, which is an implementation detail of the home dir.

    uv's text is passed through verbatim because it names the real problem, and
    the folder the user can act on is the runner/project folder — a message about
    `~/.fused-render/venvs/<hash>.src` sends them to a directory they have never
    heard of.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_ro_error")

    def _fake_run(cmd, **kw):
        class _P:
            returncode = 1
            stdout = ""
            stderr = "error: no wheels available"

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        with pytest.raises(RuntimeError) as excinfo:
            worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")
    finally:
        os.chmod(proj, 0o755)

    assert proj in str(excinfo.value)
    assert ".src" not in str(excinfo.value)


def test_writability_is_a_probe_and_not_os_access(tmp_path, monkeypatch):
    """`os.access(dir, os.W_OK)` is the wrong question, on the platform that matters.

    On Windows it reports the read-only ATTRIBUTE, which says nothing about a
    directory: an ACL-protected `C:\\Program Files\\FusedRender\\...` answers
    "writable", so the sync would run in place and uv would still die — `Access is
    denied. (os error 5)` instead of `Read-only file system (os error 30)`, the
    same install failing for the same reason on the platform the mirror was added
    for. POSIX ACLs and SELinux have the smaller version of the same hole.

    `os.access` is forced to lie here, which is the only way to describe that from
    a POSIX test box: the directory really cannot be written to, and the mirror has
    to happen anyway.
    """
    proj = _project(tmp_path, deps=["pip"])
    worker = _worker_module("_env_install_worker_probe")
    monkeypatch.setattr(worker.os, "access", lambda *a, **kw: True)

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        root = worker._sync_root(proj, str(tmp_path / "home" / "venvs" / "abc"))
    finally:
        os.chmod(proj, 0o755)

    assert root == str(tmp_path / "home" / "venvs" / "abc") + ".src", (
        "os.access said writable and the sync went to a directory uv cannot write"
    )


def test_the_write_probe_leaves_nothing_behind(tmp_path):
    """It runs in the user's own project folder on every single build.

    A probe file that survived would be a file this app creates in a tree it
    promised to touch only through the user's own edits (MD-7) — and one the user
    would then see in `git status`.
    """
    proj = _project(tmp_path, deps=["pip"])
    worker = _worker_module("_env_install_worker_probe_clean")
    before = sorted(os.listdir(proj))

    assert worker._writable_dir(proj) is True
    assert sorted(os.listdir(proj)) == before


@requires_fused
def test_a_CHANGED_manifest_expires_the_mirrors_lock(tmp_path, monkeypatch):
    """Otherwise a widened ceiling in a runner manifest never actually widens.

    Bare `uv sync` re-resolves only what the manifest invalidates, and a WIDENED
    range invalidates nothing: the runner manifests pin a pre-1.0 ceiling
    (`mlx-lm>=0.31,<0.32`) and a release that moves it to `<0.33` leaves the
    locked 0.31.x still satisfying the range. The rebuild would reinstall the
    identical versions — packaged builds behaving as though a lock had been
    committed and never refreshed, while dev checkouts (writable folder, no lock)
    picked the new version up. Exactly inverted from what those manifests say they
    rely on.

    The mirror's own copy of the manifest is the record of what its lock was
    resolved against, so a difference in those bytes is what expires it.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    mirror = venv_dir + ".src"
    _seed_mirror(proj, mirror, lock="version = 1  # resolved against the old ceiling\n",
                 manifest="[project]\nname = 'proj'\ndependencies = ['pip<1']\n")
    worker = _worker_module("_env_install_worker_expire_lock")

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        worker._sync_root(proj, venv_dir)
    finally:
        os.chmod(proj, 0o755)

    assert not os.path.exists(os.path.join(mirror, "uv.lock")), (
        "the mirror kept a lock resolved against a manifest that has since moved"
    )
    with open(os.path.join(mirror, "pyproject.toml"), encoding="utf-8") as fh:
        mirrored = fh.read()
    with open(os.path.join(proj, "pyproject.toml"), encoding="utf-8") as fh:
        assert mirrored == fh.read(), "the record must be updated to what it now holds"


@requires_fused
def test_a_MISSING_project_folder_still_reports_itself_and_builds_no_mirror(tmp_path):
    """"Not there" must not be diagnosed as "read-only".

    The probe answers the same False for both — nothing can be created in a folder
    that does not exist either — so without a separate question a vanished runner
    folder would be mirrored, uv would run in an empty directory, and the verbatim
    error PY-18 shows the user would complain about a missing `pyproject.toml`
    instead of naming the path that is gone. The direct sync root puts the real
    path on `cwd` and lets the spawn say so.
    """
    missing = str(tmp_path / "gone")
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_missing_project")

    assert worker._sync_root(missing, venv_dir) == missing
    assert not os.path.exists(venv_dir + ".src"), (
        "a folder that does not exist was mirrored"
    )


@requires_fused
def test_an_UNREADABLE_source_manifest_does_not_leave_a_vouched_for_lock(tmp_path, monkeypatch):
    """The gate must not read "I could not read either file" as "they agree".

    `_read_bytes` answers `None` for a file that is absent and for one it could not
    read, and conflating those on the KEEPING side is the dangerous direction: an
    unreadable source manifest compared against a mirror holding no record at all
    would come out equal, and the lock nothing had ever been compared against would
    be carried forward.

    The build fails either way — the copy loop cannot read the manifest either — so
    what this pins is that the failure does not leave a blessed lock behind for
    whatever runs next. An absent record fails the gate on its own, before any
    comparison, so the lock is gone before the copy is attempted.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    mirror = venv_dir + ".src"
    os.makedirs(mirror, exist_ok=True)
    lock = os.path.join(mirror, "uv.lock")
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write("version = 1  # resolved against nobody knows what\n")
    worker = _worker_module("_env_install_worker_unreadable_manifest")

    manifest = os.path.join(proj, "pyproject.toml")
    os.chmod(manifest, 0o000)
    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj) or worker._read_bytes(manifest) is not None:
            pytest.skip("running as root: read-only and unreadable are still readable")
        with pytest.raises(OSError):
            worker._sync_root(proj, venv_dir)
    finally:
        os.chmod(proj, 0o755)
        os.chmod(manifest, 0o644)

    assert not os.path.exists(lock), (
        "an unreadable manifest was taken as agreement and the lock survived"
    )


@requires_fused
def test_the_mirror_drops_a_file_the_source_STOPPED_shipping(tmp_path, monkeypatch):
    """A withdrawn `.python-version` must not govern every later build forever.

    The mirror only ever copied names that exist, so a release that removes a
    runner's `.python-version` (or withdraws a `uv.lock` it used to commit) left
    the old copy in place and the environment kept being built against a
    declaration no source tree contains. The folder is read-only, so there is no
    edit that could undo it, and the file sits in the home dir where no user will
    ever look for it.
    """
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    mirror = venv_dir + ".src"
    _seed_mirror(proj, mirror, lock="version = 1  # uv's own output\n")
    with open(os.path.join(mirror, ".python-version"), "w", encoding="utf-8") as fh:
        fh.write("3.10\n")  # shipped by the previous release, gone from this one
    worker = _worker_module("_env_install_worker_drop_stale")

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        worker._sync_root(proj, venv_dir)
    finally:
        os.chmod(proj, 0o755)

    assert not os.path.exists(os.path.join(mirror, ".python-version"))
    # `uv.lock` is the exemption and it is the whole point of the mirror: there the
    # mirror holds uv's own output, which the source never has. Rule: a changed
    # manifest expires it, an absent source file does not.
    assert os.path.exists(os.path.join(mirror, "uv.lock"))


@requires_fused
def test_the_mirror_carries_uv_s_own_config_beside_the_manifest(tmp_path, monkeypatch):
    """`uv.toml` is a resolution input, so leaving it out changes the answer.

    A private index, an `exclude-newer`, build settings — configured beside the
    manifest and simply not applying in the mirror, with nothing anywhere saying
    why the resolution differs from the same folder's on a writable machine.
    """
    proj = _project(tmp_path, deps=["pip"])
    with open(os.path.join(proj, "uv.toml"), "w", encoding="utf-8") as fh:
        fh.write('index-url = "https://internal.example/simple"\n')
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")
    worker = _worker_module("_env_install_worker_uv_toml")

    os.chmod(proj, 0o555)
    try:
        if worker._writable_dir(proj):
            pytest.skip("running as root: a read-only directory is still writable")
        mirror = worker._sync_root(proj, venv_dir)
    finally:
        os.chmod(proj, 0o755)

    with open(os.path.join(mirror, "uv.toml"), encoding="utf-8") as fh:
        assert "internal.example" in fh.read()


@requires_fused
def test_a_bundled_venvs_sidecar_records_its_place_in_the_PACKAGE(tmp_path, monkeypatch):
    """Not this launch's mount path, which no later launch can resolve.

    `gc()` keeps a venv whose source is merely unreachable (an unplugged drive
    looks the same), so an absolute path recorded for a folder inside an AppImage
    reads as unreachable forever — a runner folder a release removes or renames
    would strand a multi-gigabyte environment nothing could ever collect.
    """
    worker = _worker_module("_env_install_worker_sidecar_identity")
    pkg = tmp_path / ".mount_FusedRaaaaaa" / "fused_render"
    runner = _project(pkg / "ai" / "runners", name="faster_whisper", deps=["pip"])
    monkeypatch.setattr(worker, "_PACKAGE_DIR", str(pkg))
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")

    def _fake_run(cmd, **kw):
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    worker._build(runner, venv_dir, str(tmp_path / "cache"), "3.12")

    with open(os.path.join(venv_dir, ".fused-source.json"), encoding="utf-8") as fh:
        recorded = json.load(fh)["path"]
    assert recorded == "<fused_render>/ai/runners/faster_whisper"


@requires_fused
def test_a_users_folder_still_gets_its_absolute_path_in_the_sidecar(tmp_path, monkeypatch):
    """The identity only relativises what is genuinely inside the package.

    A user's folder recorded as anything but its own path could not be checked for
    deletion at all, and moving a folder is meant to orphan its venv (that is what
    `gc` reclaims).
    """
    worker = _worker_module("_env_install_worker_sidecar_abspath")
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = str(tmp_path / "home" / "venvs" / "abc")

    def _fake_run(cmd, **kw):
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(worker.subprocess, "run", _fake_run)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    worker._build(proj, venv_dir, str(tmp_path / "cache"), "3.12")

    with open(os.path.join(venv_dir, ".fused-source.json"), encoding="utf-8") as fh:
        assert json.load(fh)["path"] == proj


@requires_fused
def test_an_empty_interpreter_slot_means_the_workers_OWN_python(tmp_path, monkeypatch):
    """None has always meant "the backend's own interpreter", never a version.

    `_resolve_script_python` answers `(None, True)` whenever the server is
    already on the pinned version — the common path for the DMG, the AppImage,
    the Windows installer and scripts/dev.sh — and `_spawn` carries that None
    across argv as "". Mapping it to the literal "3.12" makes `uv sync --python
    3.12` resolve against PATH and uv's managed registry instead of the bundled
    app interpreter, and with uv's default download behaviour it fetches a
    managed CPython the app never uses as its base. The venv would then be built
    on a different interpreter than the one running the code, which is exactly
    what both docstrings promise cannot happen.
    """
    worker = _worker_module("_env_install_worker_default_py")
    seen = []
    monkeypatch.setattr(
        worker, "_build",
        lambda project_dir, venv_dir, uv_cache_dir, python_executable: (
            seen.append(python_executable) or "/x/bin/python"
        ),
    )
    d = str(tmp_path / "prog")
    worker.main(["k", d, str(tmp_path / "proj"), str(tmp_path / "venv"),
                 str(tmp_path / "cache"), "", ""])

    assert seen == [sys.executable], (
        "an empty interpreter slot must mean this worker's own python — the one "
        f"the server spawned it with — not {seen}"
    )


@requires_fused
def test_the_worker_spawns_with_the_interpreter_that_will_run_the_code(
    tmp_path, monkeypatch
):
    """End to end: the server's own `sys.executable` reaches `uv sync --python`.

    `_spawn` launches the worker with `sys.executable`, so the worker's own
    interpreter IS the server's — which is what makes the empty-slot translation
    faithful rather than merely convenient.
    """
    proj = _project(tmp_path, deps=["pip"])
    monkeypatch.setattr(envinstall, "_python_executable", lambda: None)
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: True)

    argv = []
    monkeypatch.setattr(envinstall.subprocess, "Popen",
                        lambda cmd, **kw: argv.append(cmd) or _FakePopen())
    envinstall.start(proj)

    assert argv, "no worker was spawned"
    assert argv[0][0] == sys.executable, "the worker must run on the server's python"
    assert argv[0][-2] == "", "None must travel as the empty string"


@requires_fused
@pytest.mark.skipif(os.name == "nt", reason="POSIX detachment; Windows uses flags")
def test_the_installer_is_spawned_off_the_fork_path(tmp_path, monkeypatch):
    """`start_new_session=True` here is a crash, not a detail.

    CPython's fast path requires `not close_fds and not start_new_session`
    (`subprocess.Popen._execute_child`), so asking for a new session forces
    `fork()+exec` — and this spawn happens in the SERVER process, where PROJ is
    resident. The forked child runs PROJ's `pthread_atfork` handler, closes a
    stale SQLite handle and dies of SIGSEGV before exec.

    What the user sees is deliberately hard to read, which is why this is pinned
    at the call: the worker never runs, so `worker.log` is EMPTY and
    `progress.json` still holds the parent's `spawn` record — reported as an
    installer that "was killed rather than failing". Every retry repeats it for
    as long as the server process lives, so a runner environment (and the model
    that needs it) can never be built.

    The detachment is not given up, it MOVES: `_env_install_worker._detach`
    calls `os.setsid()` in the child. See the test below.
    """
    proj = _project(tmp_path, deps=["pip"])
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: True)

    seen = {}
    monkeypatch.setattr(envinstall.subprocess, "Popen",
                        lambda cmd, **kw: (seen.update(kw), _FakePopen())[1])
    envinstall.start(proj)

    assert seen, "no worker was spawned"
    assert "start_new_session" not in seen, (
        "start_new_session forces fork()+exec; the worker detaches itself with "
        "os.setsid() instead"
    )
    assert seen.get("close_fds") is False, "close_fds=False is what selects posix_spawn"
    assert "preexec_fn" not in seen, "preexec_fn would force the fork path too"
    assert "cwd" not in seen, "a cwd would force the fork path too"


@pytest.mark.skipif(os.name == "nt", reason="POSIX detachment; Windows uses flags")
def test_the_installer_worker_leads_its_own_session(tmp_path, monkeypatch):
    """The other half: the child takes the session the spawner stopped asking for.

    Not cosmetic — `envinstall._kill` signals the process GROUP, and only when
    the pid leads it, because a stale pid living in the server's group once made
    `killpg` shut down a pytest session. Without this call an installer's ✕
    would leave the multi-GB `uv sync` it started running.

    Asserted as "before anything else it does", because the point is to be a
    group leader before uv exists to be killed with it.
    """
    from fused_render import _env_install_worker as worker

    order = []
    monkeypatch.setattr(worker.os, "setsid", lambda: order.append("setsid"))
    monkeypatch.setattr(worker, "install",
                        lambda *a, **k: order.append("install"))
    worker.main(["k", str(tmp_path / "prog"), str(tmp_path / "proj"),
                 str(tmp_path / "venv"), str(tmp_path / "cache"), "", ""])

    assert order == ["setsid", "install"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX detachment; Windows uses flags")
def test_an_already_leading_worker_still_installs(monkeypatch):
    """EPERM out of `setsid` means we are ALREADY a group leader — the same end
    state — so it must not take the install down with it."""
    from fused_render import _env_install_worker as worker

    def _eperm():
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(worker.os, "setsid", _eperm)
    worker._detach()  # must not raise


@requires_fused
def test_no_uv_is_reported_as_a_lost_capability_not_a_crash(tmp_path, monkeypatch):
    """uv IS the builder now, so without it a project venv is impossible (D231).

    The old builder fell back to `<python> -m venv` + pip. That is deliberately
    not restored: it cannot honour a `uv.lock`, so it would quietly produce a
    DIFFERENT environment than the one the user committed — breaking the
    reproducibility PY-16 promises, silently, on exactly the machines least able
    to notice. A clear refusal beats a wrong environment.

    So the error has to say which half is lost and how to get it back, rather
    than reading as a crash.
    """
    proj = _project(tmp_path, deps=["pip"])
    worker = _worker_module("_env_install_worker_no_uv")
    monkeypatch.setattr(worker.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError) as exc:
        worker._build(proj, str(tmp_path / "venv"), str(tmp_path / "cache"),
                      sys.executable)

    message = str(exc.value)
    assert "uv" in message
    assert "install" in message.lower(), "it must say how to fix it"
    assert "pyproject.toml" in message, (
        "it must say that folders WITHOUT one are unaffected — otherwise this "
        "reads as the whole app being broken"
    )


@requires_fused
def test_a_machine_with_no_uv_still_serves_the_app_interpreter_path(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The other half of D231: no uv must not take PY-17 down with it.

    `_resolve_script_python` still answers READY without uv, on purpose —
    readiness is about the interpreter, and a folder that declares no
    dependencies needs nothing built. Refusing there would break every ordinary
    script to report a capability most runs never use.
    """
    _uv_stub(tmp_path, monkeypatch, finds=None)
    monkeypatch.setattr(envinstall, "uv_bin", lambda: None)
    monkeypatch.setattr(envinstall, "_running_version", lambda: (3, 11))

    assert envinstall.script_python_ready() is True
    assert envinstall.script_python() is None

    # And a script in a folder with no manifest is simply not this flow's problem.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    from fused_render import projectenv

    assert projectenv.project_env_for(str(plain / "a.py")) is None


@requires_fused
def test_the_worker_imports_neither_fused_render_nor_fused(tmp_path):
    """A fresh process must reach `_build` with neither package imported.

    Asserted in a SUBPROCESS because this test session has both (and pandas)
    imported already — in-process the absence could never be observed, which is
    precisely why the ~500ms `fused` import went unnoticed for so long. Since the
    switch to `uv sync` the worker needs no `fused` at all, and `fused_render`
    was never allowed (D152).
    """
    worker_path = os.path.join(os.path.dirname(envinstall.__file__),
                               "_env_install_worker.py")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('_w', {worker_path!r})\n"
        "w = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(w)\n"
        "print(json.dumps({\n"
        "    'has_build': hasattr(w, '_build'),\n"
        "    'fused': 'fused' in sys.modules,\n"
        "    'fused_render': 'fused_render' in sys.modules,\n"
        "    'pandas': 'pandas' in sys.modules,\n"
        "}))\n",
        encoding="utf-8",
    )
    proc = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["has_build"], "the loaded module is not the worker"
    assert got["fused"] is False, "the worker imported fused to run `uv sync`"
    assert got["fused_render"] is False, "the worker imported its own package (D152)"
    assert got["pandas"] is False, "pandas was imported to install a package"



# --- the worker's heartbeat (D213) --------------------------------------------
#
# `ensure_requirements_venv` runs uv behind `capture_output=True`, so between the
# `install` record and the terminal one NOTHING was emitted — minutes on a cold
# cache with imagecodecs/pyproj. The client polled every 500ms and repainted an
# identical record, which is the whole "stuck for a long time" report. The
# heartbeat proves liveness; these tests protect the ORDERING it introduces,
# because a heartbeat landing after the terminal record puts `done: false` back on
# the wire and the page then polls forever — the same symptom, made permanent.


def _worker_module(name="_env_install_worker_hb"):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(envinstall.__file__), "_env_install_worker.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(progress_dir):
    with open(os.path.join(progress_dir, "progress.json"), encoding="utf-8") as fh:
        return json.load(fh)


# A heartbeat record, told apart from the FIRST `install` record by its elapsed
# suffix — "package(s)" means a bare "(" in the detail cannot be the signal.
_BEAT_RE = re.compile(r"\((?:\d+m)?\d+s\)$")


def _is_beat(stage, detail):
    return stage == "install" and _BEAT_RE.search(detail or "") is not None


def test_the_heartbeat_refreshes_ts_and_detail_without_faking_progress(
    tmp_path, monkeypatch
):
    """Liveness, not advancement.

    `pct` stays at 25 for the whole download because there IS no computable
    percentage — upstream captures uv's output, so nothing here can see per-package
    progress. Animating the number upward would be inventing data, and the number
    is what a user trusts most. What changes instead is elapsed time and `ts`: the
    record proves the install is alive without claiming to know how far along it is.
    """
    worker = _worker_module()
    monkeypatch.setattr(worker, "_HEARTBEAT_S", 0.02)
    d = str(tmp_path / "prog")
    beats = threading.Event()
    seen = []

    real_write = worker._write

    def _watch(progress_dir, stage, pct, detail="", done=False, error=None):
        out = real_write(progress_dir, stage, pct, detail, done, error)
        if _is_beat(stage, detail):
            # `ts` is read back off DISK, because that is what the client polls.
            seen.append((pct, detail, _record(progress_dir)["ts"]))
            if len(seen) >= 2:
                beats.set()
        return out

    monkeypatch.setattr(worker, "_write", _watch)
    monkeypatch.setattr(
        worker, "_build",
        lambda project_dir, venv_dir, uv_cache_dir, python_executable: (
            (beats.wait(10), "/x/venv/bin/python")[1]
        ),
    )
    worker.install("k", d, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"))

    assert len(seen) >= 2, f"the heartbeat did not repeat: {seen}"
    assert all(pct == worker._INSTALL_PCT for pct, _, _ in seen), seen
    stamps = [ts for _, _, ts in seen]
    assert stamps == sorted(stamps) and stamps[0] < stamps[-1], (
        f"ts never moved, so nothing on the wire proves the install is alive: {stamps}"
    )
    assert all("proj" in detail for _, detail, _ in seen), seen
    # An elapsed suffix, which is the only thing that changes between beats.
    assert _BEAT_RE.search(seen[0][1]), seen[0][1]
    assert _record(d)["done"] is True


def test_the_terminal_record_is_written_last_even_with_a_heartbeat_running(
    tmp_path, monkeypatch
):
    """The ordering the whole feature stands on.

    Heartbeat and terminal write both `os.replace` onto `progress.json`, so if a
    beat lands afterwards the client sees `done: false` again and polls a finished
    install forever. Pinned on both the success and the failure path, since the
    error record is written from an exception handler and is the easier one to get
    wrong.
    """
    worker = _worker_module()
    monkeypatch.setattr(worker, "_HEARTBEAT_S", 0.02)
    calls = []
    real_write = worker._write

    def _log(progress_dir, stage, pct, detail="", done=False, error=None):
        out = real_write(progress_dir, stage, pct, detail, done, error)
        calls.append((stage, done))
        return out

    monkeypatch.setattr(worker, "_write", _log)

    beats = threading.Event()
    monkeypatch.setattr(
        worker, "_build",
        lambda project_dir, venv_dir, uv_cache_dir, python_executable: (
            (beats.wait(10), "/x/venv/bin/python")[1]
        ),
    )

    def _wait_for_beats(progress_dir, stage, pct, detail="", done=False, error=None):
        if _is_beat(stage, detail) and len(
                [c for c in calls if c == ("install", False)]) >= 2:
            beats.set()
        return _log(progress_dir, stage, pct, detail, done, error)

    monkeypatch.setattr(worker, "_write", _wait_for_beats)
    ok_dir = str(tmp_path / "ok")
    worker.install("k", ok_dir, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"))
    assert calls[-1] == ("done", True), calls
    assert _record(ok_dir)["done"] is True

    calls.clear()
    beats.clear()

    def _boom(project_dir, venv_dir, uv_cache_dir, python_executable):
        beats.wait(10)
        raise RuntimeError("Failed to install: no wheels for imagecodecs")

    monkeypatch.setattr(worker, "_build", _boom)
    bad_dir = str(tmp_path / "bad")
    with pytest.raises(RuntimeError):
        worker.install("k", bad_dir, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"))
    assert calls[-1] == ("error", True), calls
    assert [c for c in calls if c[0] == "error"] == [("error", True)], (
        "more than one terminal error record"
    )
    rec = _record(bad_dir)
    assert rec["done"] is True
    assert "no wheels for imagecodecs" in rec["error"]


def test_a_late_heartbeat_cannot_undo_the_terminal_record(tmp_path, monkeypatch):
    """The guarantee, not the hope.

    `join(timeout=…)` can return with a beat still inside its own write (a slow
    filesystem, a suspended thread), so ordering cannot rest on the join alone.
    Writes therefore go through one lock and a latch: once a terminal record is
    written, nothing else may write. Driven by blocking a beat INSIDE `_write`
    until after the build has finished, which is exactly the interleaving the join
    cannot prevent.
    """
    worker = _worker_module()
    monkeypatch.setattr(worker, "_HEARTBEAT_S", 0.01)
    d = str(tmp_path / "prog")
    held = threading.Event()
    first_beat = threading.Event()
    real_write = worker._write

    def _hold_the_first_beat(progress_dir, stage, pct, detail="", done=False, error=None):
        if _is_beat(stage, detail) and not first_beat.is_set():
            first_beat.set()
            held.wait(10)  # the build finishes while this beat is parked here
        return real_write(progress_dir, stage, pct, detail, done, error)

    monkeypatch.setattr(worker, "_write", _hold_the_first_beat)
    monkeypatch.setattr(
        worker, "_build",
        lambda project_dir, venv_dir, uv_cache_dir, python_executable: (
            (first_beat.wait(10), "/x/venv/bin/python")[1]
        ),
    )
    # Released from a timer: the main thread is inside `install()`, which is where
    # the contention this test creates has to be resolved.
    threading.Timer(0.2, held.set).start()
    worker.install("k", d, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"))
    assert _record(d)["done"] is True, "a late heartbeat put done:false back on the wire"
    assert _record(d)["stage"] == "done"


def test_the_python_stage_is_in_STAGES_and_before_create():
    """Order is the contract the client renders against, not decoration.

    The interpreter download happens BEFORE anything can be created, and
    `runtime.js` reads position in `STAGES` to decide what is behind and what is
    ahead. A `python` stage appended after `install` would render as progress going
    backwards.
    """
    assert "python" in envinstall.STAGES
    assert envinstall.STAGES.index("python") < envinstall.STAGES.index("create")
    assert envinstall.STAGE_PCT["python"] < envinstall.STAGE_PCT["create"]
    assert set(envinstall.STAGE_PCT) == set(envinstall.STAGES)


def test_the_worker_acquires_the_interpreter_and_builds_NO_venv(tmp_path, monkeypatch):
    """Bootstrap mode is one job, not a prelude to the other.

    It cannot do both in one run: the venv it would go on to build belongs under a
    key folding in the interpreter it has only just fetched, which is not the key it
    was spawned under. Building anyway would fill a directory `is_installed` never
    looks at, and the page would install, retry and be told to install again.
    """
    worker = _worker_module("_env_install_worker_py")
    d = str(tmp_path / "prog")
    ran = []
    monkeypatch.setattr(worker, "_acquire_python", lambda v: ran.append(v))
    monkeypatch.setattr(worker, "_build", lambda *a, **k: pytest.fail("built a venv"))

    worker.install("k", d, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"),
                   acquire_python="3.12")
    assert ran == ["3.12"]
    rec = _record(d)
    assert rec["stage"] == "done" and rec["done"] is True
    assert "3.12" in rec["detail"]


def test_the_python_stage_reports_LIVENESS_not_an_invented_percentage(
    tmp_path, monkeypatch
):
    """Same rule as the install stage (D213), and the same reason.

    uv's download progress is not observable from here, so the only honest thing to
    refresh is the elapsed time and `ts`. A bar creeping upward on made-up numbers is
    worse than one that does not move — the number is what a waiting user trusts most.
    """
    worker = _worker_module("_env_install_worker_py2")
    d = str(tmp_path / "prog")
    seen = []
    real_write = worker._write

    def record_every(progress_dir, stage, pct, detail="", done=False, error=None):
        seen.append((stage, pct, detail))
        return real_write(progress_dir, stage, pct, detail, done, error)

    monkeypatch.setattr(worker, "_write", record_every)
    monkeypatch.setattr(worker, "_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(worker, "_acquire_python", lambda v: time.sleep(0.3))

    worker.install("k", d, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"),
                   acquire_python="3.12")

    beats = [s for s in seen if s[0] == "python"]
    assert len(beats) >= 2, f"the python stage never beat: {seen}"
    assert len({pct for _, pct, _ in beats}) == 1, (
        f"the python stage invented a percentage: {beats}"
    )
    assert any(_BEAT_RE.search(detail or "") for _, _, detail in beats), (
        f"no elapsed time on any python beat: {beats}"
    )


def test_an_interpreter_download_that_FAILS_reports_the_reason(tmp_path, monkeypatch):
    """Verbatim, like every other install error in this flow — a proxy refusing
    uv's download is the actual answer the user needs, not "install failed"."""
    worker = _worker_module("_env_install_worker_py3")
    d = str(tmp_path / "prog")

    def boom(version):
        raise RuntimeError("Failed to download Python 3.12: 403 Forbidden")

    monkeypatch.setattr(worker, "_acquire_python", boom)
    with pytest.raises(RuntimeError):
        worker.install("k", d, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"),
                       acquire_python="3.12")
    rec = _record(d)
    assert rec["done"] is True and rec["stage"] == "error"
    assert "403 Forbidden" in rec["error"]


def test_the_worker_maps_an_EMPTY_acquire_slot_back_to_no_acquisition(tmp_path, monkeypatch):
    """argv cannot carry None, so "" means "nothing to acquire" — the same idiom
    slot 4 already uses for the interpreter, translated in `main` and nowhere else.
    A slot read as the literal string "" would try to install a Python called
    nothing."""
    worker = _worker_module("_env_install_worker_py4")
    seen = {}
    monkeypatch.setattr(
        worker, "install",
        lambda *a, **kw: seen.update(args=a, kwargs=kw),
    )
    worker.main(["k", str(tmp_path), str(tmp_path / "proj"), str(tmp_path / "venv"),
                 str(tmp_path / "cache"), "", ""])
    assert seen["kwargs"]["acquire_python"] is None

    seen.clear()
    worker.main(["k", str(tmp_path), str(tmp_path / "proj"), str(tmp_path / "venv"),
                 str(tmp_path / "cache"), "", "3.12"])
    assert seen["kwargs"]["acquire_python"] == "3.12"


def test_a_failed_terminal_write_does_not_latch_out_the_error_record(tmp_path, monkeypatch):
    """The latch closes on evidence the record landed, not on the attempt.

    If it engaged before `_write` returned, a terminal `done` write that FAILED
    would still shut the file, and the `except` path's error record — the only one
    carrying the reason — would no-op. The wire would keep the last heartbeat's
    `done: false` forever: the same stuck poll the latch exists to prevent, reached
    from the other side.
    """
    worker = _worker_module()
    d = str(tmp_path / "prog")
    real_write = worker._write

    def _fail_the_done_record(progress_dir, stage, pct, detail="", done=False, error=None):
        if stage == "done":
            raise OSError(28, "No space left on device")
        return real_write(progress_dir, stage, pct, detail, done, error)

    monkeypatch.setattr(worker, "_write", _fail_the_done_record)
    monkeypatch.setattr(
        worker, "_build",
        lambda project_dir, venv_dir, uv_cache_dir, python_executable: "/x/venv/bin/python",
    )
    with pytest.raises(OSError):
        worker.install("k", d, str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"))

    rec = _record(d)
    assert rec["done"] is True, "the failed done-write latched the error record out"
    assert rec["stage"] == "error"
    assert "No space left on device" in rec["error"], (
        "the error record must name the real failure, not the install it was reporting"
    )


def test_the_heartbeat_thread_does_not_outlive_the_install(tmp_path, monkeypatch):
    """Stopped and joined on BOTH paths, and a daemon so a wedged one cannot keep
    the worker process alive after the record says done."""
    worker = _worker_module()
    monkeypatch.setattr(worker, "_HEARTBEAT_S", 0.01)
    threads = []
    real_thread = worker.threading.Thread

    def _capture(*a, **kw):
        t = real_thread(*a, **kw)
        threads.append(t)
        return t

    monkeypatch.setattr(worker.threading, "Thread", _capture)
    monkeypatch.setattr(
        worker, "_build",
        lambda project_dir, venv_dir, uv_cache_dir, python_executable: "/x/venv/bin/python",
    )
    worker.install("k", str(tmp_path / "a"), str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"))
    assert threads and all(t.daemon for t in threads)
    assert not any(t.is_alive() for t in threads), "the heartbeat outlived the install"

    def _boom(project_dir, venv_dir, uv_cache_dir, python_executable):
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "_build", _boom)
    with pytest.raises(RuntimeError):
        worker.install("k", str(tmp_path / "b"), str(tmp_path / "proj"), str(tmp_path / "venv"), str(tmp_path / "cache"))
    assert not any(t.is_alive() for t in threads), (
        "the heartbeat outlived a FAILED install"
    )


def test_the_workers_temp_file_is_unique_per_writer_not_just_per_process(
    tmp_path, monkeypatch
):
    """Two writers now live in ONE process (heartbeat + main), so a pid-unique temp
    name is no longer unique: the first `os.replace` consumes the other's file and
    its own replace fails with FileNotFoundError — a crashed installer whose venv
    was fine. Thread id, matching `envinstall._write`.
    """
    worker = _worker_module()
    d = tmp_path / "prog"
    d.mkdir()
    names = []
    real_replace = worker.os.replace
    monkeypatch.setattr(worker.os, "replace",
                        lambda src, dst: (names.append(src), real_replace(src, dst))[1])

    def _write_from_a_thread():
        worker._write(str(d), "install", 25, "x")

    t = threading.Thread(target=_write_from_a_thread)
    t.start()
    t.join(10)
    worker._write(str(d), "install", 25, "y")
    assert len(names) == 2 and names[0] != names[1], names


def test_the_worker_reads_an_empty_interpreter_argument_as_none(tmp_path, monkeypatch):
    """"" is how "the backend's default interpreter" crosses argv.

    argv cannot carry None, so the empty string stands for it — explicitly, and
    only here, so nothing downstream has to guess.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_env_install_worker_argv",
        os.path.join(os.path.dirname(envinstall.__file__), "_env_install_worker.py"),
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    seen = []
    monkeypatch.setattr(
        worker, "_build",
        lambda project_dir, venv_dir, uv_cache_dir, python_executable: (
            seen.append(python_executable) or "/x/bin/python"
        ),
    )
    d = str(tmp_path / "prog")
    worker.main(["k", d, str(tmp_path / "proj"), str(tmp_path / "venv"),
                str(tmp_path / "cache"), "", ""])
    assert seen == [sys.executable]
    worker.main(["k", d, str(tmp_path / "proj"), str(tmp_path / "venv"),
                str(tmp_path / "cache"), "/usr/bin/python3", ""])
    assert seen == [sys.executable, "/usr/bin/python3"]


@requires_fused
def test_starting_twice_does_not_spawn_a_second_worker(tmp_path, monkeypatch):
    """Two pages (or a double-click) must share one install, not race it.

    Two workers building the same directory is the race `fused`'s in-process
    lock cannot cover — the loser dies on a half-built `<venv>/bin/python`.
    """
    spawned = []
    # Our own pid, because it is provably alive: a made-up one would be reaped by
    # the liveness check in `progress()` and the second start would legitimately
    # re-spawn, which would pass this test for the wrong reason.
    monkeypatch.setattr(envinstall, "_spawn",
                        lambda *a, **kw: spawned.append(a) or os.getpid())
    proj = _project(tmp_path, deps=["pip"])
    envinstall.start(proj)
    envinstall.start(proj)
    assert len(spawned) == 1


@requires_fused
def test_concurrent_starts_spawn_exactly_one_worker(tmp_path, monkeypatch):
    """The race the sequential test cannot see.

    `progress()` then `_spawn()` is a check-then-act, and the endpoints are sync
    `def` — FastAPI runs those in a threadpool, so two POSTs really are
    concurrent. Two workers building one venv dir is precisely what `fused`'s
    in-process lock does not cover: the loser dies on a half-built
    `<venv>/bin/python`. A barrier makes every thread arrive inside the window at
    once, which is what the unsynchronised version could not survive.
    """
    workers = 16
    barrier = threading.Barrier(workers)
    spawned = []
    lock = threading.Lock()

    def fake_spawn(key, proj, **kw):
        with lock:
            spawned.append(key)
        return os.getpid()  # provably alive, so `_in_flight` stays true

    monkeypatch.setattr(envinstall, "_spawn", fake_spawn)
    proj = _project(tmp_path, deps=["pip"])
    errors = []

    def go():
        try:
            barrier.wait(timeout=30)
            envinstall.start(proj)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], errors
    assert len(spawned) == 1, f"{len(spawned)} workers spawned for one venv"


@requires_fused
def test_two_scripts_in_one_project_still_spawn_exactly_one_worker(
    tmp_path, monkeypatch
):
    """The claim holds under the NEW key: the project, not a requirement set.

    This is the server-side half of "one install per project". The client dedups
    too (`installEnv`'s registry, tests/test_server_env_install.py), but that is
    about not issuing N POSTs — this is what makes N POSTs harmless if they
    arrive anyway, which they will from two pages, a reload mid-install, or any
    direct API caller.

    Two DIFFERENT .py files, deliberately: under the old per-file key they would
    have been two keys and two workers by construction, so this is exactly the
    case the folder rule changed.
    """
    proj = _project(tmp_path, deps=["pip"])
    for name in ("one.py", "two.py"):
        with open(os.path.join(proj, name), "w", encoding="utf-8") as fh:
            fh.write("def main():\n    return 1\n")

    from fused_render import projectenv

    resolved = {projectenv.project_env_for(os.path.join(proj, n))
                for n in ("one.py", "two.py")}
    assert resolved == {proj}, "the two files must resolve to one project"

    barrier = threading.Barrier(2)
    spawned = []
    lock = threading.Lock()

    def fake_spawn(key, p, **kw):
        with lock:
            spawned.append(key)
        return os.getpid()  # provably alive, so `_in_flight` stays true

    monkeypatch.setattr(envinstall, "_spawn", fake_spawn)
    errors = []

    def go(py):
        try:
            barrier.wait(timeout=30)
            envinstall.start(projectenv.project_env_for(py))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=go, args=(os.path.join(proj, n),))
        for n in ("one.py", "two.py")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], errors
    assert len(spawned) == 1, (
        f"{len(spawned)} workers spawned for one project — the loser must JOIN"
    )
    assert spawned == [envinstall.venv_key_for(proj)]


@requires_fused
def test_a_stale_claim_from_a_dead_installer_is_taken_over(tmp_path, monkeypatch):
    """A crashed installer must not wedge the key forever.

    The claim file outlives the process that made it, so "claim exists" cannot
    mean "give up" — otherwise one crash makes a template permanently
    un-installable with no way back short of deleting a cache directory by hand.
    """
    proj = _project(tmp_path, deps=["pip"])
    key = envinstall.venv_key_for(proj)
    spawned = []
    monkeypatch.setattr(
        envinstall, "_spawn", lambda k, r, **kw: spawned.append(k) or (2 ** 31 - 1)
    )
    envinstall.start(proj)
    assert len(spawned) == 1
    assert os.path.exists(os.path.join(envinstall.progress_dir(key), "claim"))

    # The recorded pid cannot be running, so this install reads as crashed.
    assert envinstall.progress(key)["done"] is True
    envinstall.start(proj)
    assert len(spawned) == 2, "a dead installer's claim should be taken over"


def test_a_fresh_claim_with_no_record_reads_as_an_install_in_flight(monkeypatch):
    """The claim IS the install, from the instant it exists.

    A claim is written before `_spawn`, and the parent's first `_write` only
    happens after `Popen` returns — a fork/exec of a Python interpreter. For that
    whole window "claim present, no record" is the truth of a perfectly healthy
    install, and `progress()` used to answer None for it: never started. runtime.js
    turns a null record into a hard failure ("the installer left no progress
    record"), so the first open of any PEP 723 template could fail while the
    install it was waiting on ran to completion. Whoever polls — the caller that
    lost the claim, or a page that reloaded and never POSTed at all — must get a
    pollable record, which is why this is fixed in `progress()` and not in the
    response body `start()` happens to return.
    """
    key = "0c1a1f00000000e1"
    assert envinstall._claim(key) is True, "nothing else holds this key"
    assert envinstall._read_record(key) is None, "mid-spawn: no record written yet"
    prog = envinstall.progress(key)
    assert prog is not None, "a claimed install is in flight, not 'never started'"
    assert prog["done"] is False
    assert prog["error"] is None
    assert envinstall._in_flight(key) is True


def test_a_stale_claim_with_no_record_resolves_instead_of_polling_forever(monkeypatch):
    """The other side of treating a claim as evidence: it has to expire.

    A server killed between claiming and its first `_write` leaves a claim no
    process is behind. Reading that as "starting" forever would wedge the key —
    the poller would never stop and the page would never say anything. So past
    `_CLAIM_GRACE_S` the answer is done-with-an-error: the installer never got off
    the ground, which is an answer the caller can act on (show it, offer a retry —
    and the retry's `_claim` takes the stale claim over).
    """
    key = "0c1a15000000d0e1"
    assert envinstall._claim(key) is True
    claim = os.path.join(envinstall.progress_dir(key), "claim")
    old = time.time() - envinstall._CLAIM_GRACE_S - 60
    os.utime(claim, (old, old))
    prog = envinstall.progress(key)
    assert prog is not None
    assert prog["done"] is True, "an abandoned claim must end the poll"
    assert "never started" in prog["error"]
    assert envinstall._in_flight(key) is False


def test_claim_takeover_still_turns_on_claim_age_not_on_progress(monkeypatch):
    """`_claim_is_stale` must not be able to read the claim as its own alibi.

    It asks `progress()` whether the install is in flight, and `progress()` now
    reports a fresh claim as in flight — so a careless fix makes the claim the
    evidence for itself and no claim is ever stealable again, which is exactly the
    "one crash wedges the key forever" failure `_claim_is_stale` was written to
    prevent. Both directions are pinned here: fresh is not stealable, aged is.
    """
    key = "0c1a1a6ed0000001"
    assert envinstall._claim(key) is True
    assert envinstall._claim(key) is False, "a fresh claim is not stealable"

    claim = os.path.join(envinstall.progress_dir(key), "claim")
    old = time.time() - envinstall._CLAIM_GRACE_S - 60
    os.utime(claim, (old, old))
    assert envinstall._claim_is_stale(key, claim) is True
    assert envinstall._claim(key) is True, "an abandoned claim must be takeable"


@requires_fused
def test_joining_an_install_mid_spawn_yields_a_pollable_record(tmp_path, monkeypatch):
    """The user-visible bug, at the layer that produced it.

    The docs template fires `warmup` and awaits `import` for the same file, so one
    of the two always loses the claim and takes `start()`'s join branch. That
    branch's synthetic record only ever protected the POST's own response body;
    the loser's very next act is a SEPARATE GET /api/env/progress, which called
    `progress()` fresh and got null — "Cannot open sample.docx: the installer left
    no progress record" while the install was running fine. Distinct from
    `test_the_loader_polls_the_key_the_installer_actually_returned` in
    test_server_env_install.py, which reaches the same message via the WRONG key;
    here the key is right and the record simply had not been written yet.
    """
    proj = _project(tmp_path, deps=["pip"])
    key = envinstall.venv_key_for(proj)
    monkeypatch.setattr(
        envinstall, "_spawn", lambda *a: pytest.fail("the loser must not spawn")
    )
    # The winner, still inside `Popen`: claim taken, nothing written yet.
    assert envinstall._claim(key) is True
    record = envinstall.start(proj)
    assert record is not None and record["done"] is False
    # ...and the loser's next act is a fresh poll, not a re-read of that body.
    polled = envinstall.progress(key)
    assert polled is not None, "the poll after the join must not read null"
    assert polled["done"] is False


@requires_fused
def test_the_spawn_record_never_overwrites_a_record_the_worker_already_wrote(
    tmp_path, monkeypatch
):
    """The parent's `spawn` record must not be able to lose a worker's record.

    `_spawn` returns as soon as `Popen` does, and the worker is already running by
    then — a resolver that fails on its first import can write its `done` record
    before the parent gets its own line in. The parent's write used to be
    unconditional, so it replaced that record with `done: False` plus a pid that
    has already exited; `_recorded_progress` then synthesises "the installer
    exited unexpectedly" and runtime.js renders it as a hard install failure for
    an install that had already reported its real outcome. Asserting the parent
    wins the race is the same reasoning that produced the D180 bug, so the
    ordering is guaranteed here instead: the worker's record always wins.

    Modelled by having `_spawn` itself write the worker's record, which is exactly
    the interleaving a fast worker produces.
    """
    proj = _project(tmp_path, deps=["pip"])
    key = envinstall.venv_key_for(proj)
    worker_record = {
        "stage": "error", "pct": 100, "detail": "", "done": True,
        "error": "RuntimeError: Failed to install: no such distribution",
        # A pid that cannot be running: 2**31-1 is above every platform's pid_max,
        # so an unconditional parent write also loses the liveness argument.
        "pid": 2 ** 31 - 1, "ts": time.time(),
    }

    def _spawn_then_report(k, r, **kw):
        envinstall._write(k, worker_record)
        return 2 ** 31 - 1

    monkeypatch.setattr(envinstall, "_spawn", _spawn_then_report)
    record = envinstall.start(proj)
    assert record["error"] == worker_record["error"], record
    prog = envinstall.progress(key)
    assert prog["error"] == worker_record["error"], (
        "the worker's own outcome must survive the parent's spawn record"
    )


@requires_fused
def test_a_retry_does_not_inherit_the_previous_attempt_s_record(tmp_path, monkeypatch):
    """A taken-over claim starts from no record, not from the old one.

    The parent's spawn record only fills the gap before the worker's first write,
    so it must never displace a record the worker wrote — but that also means a
    FAILED attempt's record is still sitting there when the user retries. Left in
    place it becomes this attempt's answer: the loader would show the previous
    resolver failure the instant it opened, while the new worker was downloading
    perfectly well behind it.
    """
    proj = _project(tmp_path, deps=["pip"])
    key = envinstall.venv_key_for(proj)
    monkeypatch.setattr(envinstall, "_spawn", lambda k, r, **kw: 2 ** 31 - 1)
    envinstall.start(proj)
    assert envinstall.progress(key)["error"], "the first attempt reads as crashed"

    # Age the claim so the retry may take it over, exactly as a real retry does.
    claim = os.path.join(envinstall.progress_dir(key), "claim")
    old = time.time() - envinstall._CLAIM_GRACE_S - 60
    os.utime(claim, (old, old))
    live = []
    monkeypatch.setattr(envinstall, "_spawn",
                        lambda k, r, **kw: live.append(k) or os.getpid())
    record = envinstall.start(proj)
    assert live == [key], "the retry must spawn"
    assert record["error"] is None, record
    assert envinstall.progress(key)["error"] is None


@requires_fused
def test_start_is_a_no_op_once_the_venv_is_installed(tmp_path, monkeypatch):
    proj = _project(tmp_path, deps=["pip"])
    venv_dir = envinstall.venv_dir_for(proj)
    os.makedirs(venv_dir, exist_ok=True)
    # A runnable interpreter, not just the marker: since D212 `is_installed`
    # verifies the claim once, and a marker over an empty directory now reads
    # (correctly) as "not installed" — which is a different test than this one.
    _runnable_venv_python(venv_dir)
    _mark(venv_dir, proj)
    spawned = []
    monkeypatch.setattr(envinstall, "_spawn", lambda *a, **kw: spawned.append(a) or 1)
    envinstall.start(proj)
    assert spawned == []


# --- honesty about granularity ------------------------------------------------


def test_progress_stages_are_the_ones_we_can_actually_observe():
    """`venvs._run_step` uses capture_output=True, so pip's per-package output
    is unavailable without changing `fused`. The stage list is therefore coarse
    ON PURPOSE, and named here so a future "62%" that implies per-package
    resolution has to argue with a test first. `python` (D214) is coarse for the
    same reason: our own `uv python install` captures its output too.
    """
    assert envinstall.STAGES == ("spawn", "python", "create", "install", "done")


def _wait_done(key, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        prog = envinstall.progress(key)
        if prog and prog.get("done"):
            return prog
        time.sleep(0.1)
    pytest.fail(f"installer for {key} did not finish within {timeout}s: "
                f"{envinstall.progress(key)}")

def test_a_crash_message_says_how_far_it_got_and_whether_the_log_is_empty(tmp_path, monkeypatch):
    """The old message said only "see worker.log", and that was a dead end.

    A worker puts its diagnostics in the RECORD (uv's stderr verbatim), so
    worker.log holds raw child output only — and a worker that was KILLED never
    wrote any. Users followed the message to an empty file and had nothing left
    to go on, so the message now carries the stage the record already knew and
    states the emptiness as the diagnosis it is.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    key = "abcdefabcdef0123"
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    # 2**31-1 is above every platform's pid_max, so it cannot collide with a
    # live process and skip the crash path (the sibling tests' convention).
    record = {"stage": "install", "pct": 25, "detail": "resolving mlx-lm", "done": False,
              "error": None, "pid": 2 ** 31 - 1, "ts": time.time()}
    with open(os.path.join(d, "progress.json"), "w") as f:
        json.dump(record, f)

    # No worker.log at all (the spawn never got to create it).
    err = envinstall.progress(key)["error"]
    assert "last stage: install" in err and "resolving mlx-lm" in err
    assert "missing" in err

    # Present but empty — the case the user actually hit.
    open(os.path.join(d, "worker.log"), "wb").close()
    err = envinstall.progress(key)["error"]
    assert "is empty" in err
    assert "killed rather than failing" in err
    assert "out-of-memory" in err  # names the causes worth checking

    # With real output, it points at the log and says nothing about killing.
    with open(os.path.join(d, "worker.log"), "wb") as f:
        f.write(b"error: no wheels with a matching platform tag\n")
    err = envinstall.progress(key)["error"]
    assert "see worker.log" in err
    assert "killed rather than failing" not in err


@pytest.mark.skipif(os.name == "nt", reason="waitpid statuses are POSIX")
def test_a_worker_we_reaped_ourselves_is_diagnosed_by_its_SIGNAL(tmp_path, monkeypatch):
    """The guess becomes a fact when the status is ours to read.

    `_pid_alive` is the only `waitpid` on an installer, and it was throwing the
    status away — so a user whose install was killed got a list of four things it
    might have been (an OOM kill, a cancel, a quit, a sleeping machine) when the
    kernel had already said which. A SIGKILL is the first of those, a SIGTERM the
    middle two, and a SIGSEGV is none of them: it is a spawn that crashed before
    it ran, which is what this module's own fork-path bug looked like.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    key = "abcdefabcdef0123"
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "worker.log"), "wb").close()  # killed, so it logged nothing

    def _died_of(number, pid):
        """One reaped worker, exactly as `_pid_alive` would have buried it."""
        record = {"stage": "spawn", "pct": 0, "detail": "starting installer for mlx_text",
                  "done": False, "error": None, "pid": pid, "ts": time.time()}
        with open(os.path.join(d, "progress.json"), "w") as f:
            json.dump(record, f)
        envinstall._SPAWNED.add(pid)
        monkeypatch.setattr(
            envinstall.os, "waitpid",
            lambda p, flags, _n=number: (p, _n),  # WIFSIGNALED: low byte is the signal
        )
        return envinstall.progress(key)["error"]

    err = _died_of(signal.SIGKILL, 424201)
    assert "SIGKILL" in err and "out-of-memory" in err
    assert "the app quitting" not in err, "the fact replaces the list of guesses"
    assert "last stage: spawn" in err, "how far it got is still the useful half"

    err = _died_of(signal.SIGTERM, 424202)
    assert "SIGTERM" in err and "unload" in err

    err = _died_of(signal.SIGSEGV, 424203)
    assert "SIGSEGV" in err and "fork()" in err

    # An ordinary non-zero exit is not a kill, and must not be described as one.
    monkeypatch.setattr(envinstall.os, "waitpid", lambda p, flags: (p, 3 << 8))
    envinstall._SPAWNED.add(424204)
    record = {"stage": "install", "pct": 25, "detail": "resolving mlx-lm", "done": False,
              "error": None, "pid": 424204, "ts": time.time()}
    with open(os.path.join(d, "progress.json"), "w") as f:
        json.dump(record, f)
    err = envinstall.progress(key)["error"]
    assert "exited with status 3" in err and "killed" not in err


def _has_no_signal_name(number):
    try:
        signal.Signals(number)
    except ValueError:
        return True
    return False


@pytest.mark.skipif(os.name == "nt", reason="waitpid statuses are POSIX")
def test_an_unreaped_or_nameless_ending_degrades_instead_of_failing():
    """The message must survive not knowing. Three ways it can not know: a pid we
    never buried (a record left by a previous server), a pid that is not a number
    at all (`progress.json` is a file on disk and may say anything), and a signal
    this platform has no name for — which must not print "signal 64 (signal 64)".
    """
    assert envinstall._how_it_ended(2 ** 31 - 1) == ""
    assert envinstall._how_it_ended("nonsense") == ""
    assert envinstall._how_it_ended(None) == ""

    # Found rather than hardcoded: which numbers have names is the PLATFORM's
    # answer (Linux names every realtime signal up to SIGRTMAX, macOS does not),
    # and a literal here would test the wrong thing on one of them.
    nameless = next((n for n in range(1, 128) if _has_no_signal_name(n)), None)

    kept = dict(envinstall._ENDINGS)
    try:
        if nameless is not None:
            envinstall._remember_ending(4242, nameless)  # WIFSIGNALED
            assert envinstall._how_it_ended(4242) == (
                f"it was killed by signal {nameless}"), "never 'signal N (signal N)'"
        envinstall._remember_ending(4243, 7 << 8)  # a plain exit, not a kill
        assert envinstall._how_it_ended(4243) == (
            "it exited with status 7 without finishing")
    finally:
        envinstall._ENDINGS.clear()
        envinstall._ENDINGS.update(kept)


def test_the_endings_table_survives_concurrent_reaps():
    """Two threadpooled `/api/env/progress` calls can reap two workers at once.

    The eviction is read-then-write (`next(iter(...))` then `pop`), and neither
    half being atomic on its own is enough: the loser pops a key the winner
    already took (`KeyError`), or iterates a dict that changed size
    (`RuntimeError`), and either escapes `_pid_alive` into a 500 on a poll. A
    lock is the whole fix; this pins that the cap and the table hold under
    threads rather than trying to reproduce a timing window.
    """
    kept = dict(envinstall._ENDINGS)
    errors = []
    try:
        envinstall._ENDINGS.clear()

        def _hammer(base):
            try:
                for pid in range(base, base + 400):
                    envinstall._remember_ending(pid, 9)
            except BaseException as e:  # noqa: BLE001 - the point of the test
                errors.append(e)

        threads = [threading.Thread(target=_hammer, args=(n * 10_000,))
                   for n in range(1, 9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert len(envinstall._ENDINGS) == envinstall._ENDINGS_KEPT
    finally:
        envinstall._ENDINGS.clear()
        envinstall._ENDINGS.update(kept)


def test_the_endings_table_cannot_grow_without_bound():
    """One entry per installer this server has ever buried would be a slow leak in
    a process that runs for days; the record is read once, immediately after the
    reap, and is worthless after that."""
    kept = dict(envinstall._ENDINGS)
    try:
        envinstall._ENDINGS.clear()
        for pid in range(1000, 1000 + envinstall._ENDINGS_KEPT * 3):
            envinstall._remember_ending(pid, 9)
        assert len(envinstall._ENDINGS) == envinstall._ENDINGS_KEPT
        newest = 1000 + envinstall._ENDINGS_KEPT * 3 - 1
        assert newest in envinstall._ENDINGS, "the oldest goes, not the newest"
    finally:
        envinstall._ENDINGS.clear()
        envinstall._ENDINGS.update(kept)
