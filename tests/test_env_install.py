"""The explicit installation loader for scripts that keep a PEP 723 header
(SPEC PY-18, D173).

A header-less script runs on the app's own interpreter with nothing to install
(PY-17). The seven core templates that keep a header need a real download, and
`fused.runPython` has roughly a 30-second budget — so a first run used to hit
the timeout and surface as an opaque `EngineError` with a resolver failure
buried in it, or nothing at all.

So the venv build is moved out of the request: `/api/run` answers
`needs_install` instead of blocking, a detached worker builds the venv and
writes `progress.json`, and the page polls. The shape is
`templates/docs/install_worker.py`'s, which already does exactly this for the
typst download — one pattern in the repo, not two.

What these tests are really protecting:

  * the pre-flight's venv key must be **the same key the backend will use**, or
    the loader installs into one directory and the run builds another — a
    double download that looks like the loader did nothing;
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

import pytest

from fused_render import engine, envinstall

pytest.importorskip("tomllib", reason="PEP 723 parsing needs Python 3.11+")

requires_fused = pytest.mark.skipif(
    not engine.available(), reason="fused package not installed (engine falls back)"
)

HEADER = '# /// script\n# dependencies = ["pip"]\n# ///\n'


@pytest.fixture(autouse=True)
def _isolated_install_state(tmp_path, monkeypatch):
    """Give every test its own progress dir.

    `progress_dir` is keyed by the venv key alone and lives under the shell home,
    which conftest sets ONCE for the whole session — so two tests using the same
    requirement set (several here use `["pip"]`) would otherwise share one
    progress record and one claim file, and pass or fail depending on order.

    Also drops the per-process venv-validation memo (D212). That cache is keyed by
    venv DIRECTORY, and `venvs_path` is monkeypatched per test to a tmp dir, so
    real collisions are unlikely — but a memo that outlives the directory it
    describes is exactly the thing these tests are about, and a leaked verdict
    would make a later test pass or fail on ordering.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    envinstall.reset_venv_validation_cache()


# --- the venv key must be the backend's own -----------------------------------


@requires_fused
def test_the_preflight_key_is_the_key_the_backend_will_use():
    """Computed through `fused`'s own helpers, never re-derived.

    A local re-implementation of "sha256 of the sorted requirements" is the
    failure this test exists to prevent: it would agree with the backend right
    up until upstream changed the recipe, and then the loader would build a venv
    the run never looks at, forever, with no error anywhere.
    """
    from fused.agent_core.backends.local.venvs import requirements_venv_id, venv_key

    reqs = ["b-dist", "a-dist"]
    expected = venv_key(requirements_venv_id(reqs, None))
    assert envinstall.venv_key_for(reqs) == expected


@requires_fused
def test_the_key_ignores_requirement_order():
    assert envinstall.venv_key_for(["a", "b"]) == envinstall.venv_key_for(["b", "a"])


@requires_fused
def test_the_loader_and_the_backend_agree_on_the_venv_DIRECTORY():
    """Matching keys are not enough — the parent directory has to match too.

    `venv_dir_for` must be `<the backend's own venvs_path>/<key>`. A correct key
    under a different root is the same silent failure as a wrong key: the loader
    reports success, the run finds nothing there and asks to install again, and
    the user installs forever. (Seen for real while driving this end to end with
    `venvs_path` patched on only one side — the keys agreed perfectly and the two
    directories were still different.) Read off the live backend, not restated.
    """
    backend = engine.get_backend()
    reqs = ["pip"]
    expected = os.path.join(
        os.path.expanduser(backend._venvs_path), envinstall.venv_key_for(reqs)
    )
    assert envinstall.venv_dir_for(reqs) == expected


@requires_fused
def test_the_key_folds_in_the_backend_s_base_interpreter(monkeypatch):
    """`python_identity` keys on the interpreter, so the loader must use the
    backend's `python_executable` — not just its own `sys.executable`."""
    from fused.agent_core.backends.local.venvs import requirements_venv_id, venv_key

    monkeypatch.setattr(envinstall, "_python_executable", lambda: sys.executable)
    reqs = ["pip"]
    assert envinstall.venv_key_for(reqs) == venv_key(
        requirements_venv_id(reqs, sys.executable)
    )


@requires_fused
def test_the_backend_attributes_this_module_reads_still_exist():
    """Pin the private attributes the loader depends on.

    `_venvs_path` and `_python_executable` decide which directory the loader
    fills and which interpreter the key folds in. If upstream renames either, we
    want a red test naming it — not a loader that quietly fills a directory no
    run ever reads, which is the same silent failure as a wrong key.
    """
    backend = engine.get_backend()
    missing = [a for a in envinstall.BACKEND_ATTRS if not hasattr(backend, a)]
    assert not missing, (
        f"{type(backend).__name__} no longer has {missing}; envinstall reads them "
        "to stay in step with where script venvs live"
    )


@requires_fused
def test_a_renamed_backend_attribute_fails_loudly(monkeypatch):
    """And when it IS missing, the failure says so instead of guessing."""

    class Renamed:
        pass

    monkeypatch.setattr(engine, "get_backend", lambda: Renamed())
    with pytest.raises(RuntimeError, match="_venvs_path"):
        envinstall.venvs_path()
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
    reqs = ["pip"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)
    # The baseline is about the MARKER, so the interpreter must be pinned ready
    # rather than measured: `_fresh_script_python` drops conftest's pin, and on a
    # runner that is neither 3.12 nor holding a uv-managed 3.12 the real
    # resolution answers "not ready" — which is the very short-circuit under test,
    # so the baseline would assert the post-condition and pass for the wrong
    # reason on 3.12 while failing outright on 3.11 (which is what CI's
    # fused-engine job pins, and the only job where @requires_fused runs at all).
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: True)
    assert envinstall.is_installed(reqs) is True  # baseline: the marker is trusted

    envinstall.reset_venv_validation_cache()
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    probed = []
    monkeypatch.setattr(envinstall, "_venv_is_usable",
                        lambda d: probed.append(d) or True)
    assert envinstall.is_installed(reqs) is False
    assert not probed, "validated a venv that cannot even be named yet"
    assert os.path.exists(os.path.join(venv_dir, envinstall.READY_MARKER)), (
        "unlinked the marker of a venv the missing interpreter says nothing about"
    )


@requires_fused
def test_an_empty_requirement_set_never_needs_an_interpreter(
    monkeypatch, _fresh_script_python
):
    """No header means no venv, so the pin is irrelevant — and must not block."""
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    assert envinstall.is_installed([]) is True


@requires_fused
def test_the_bootstrap_reports_under_its_OWN_key_not_a_venv_key(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The one place a venv key genuinely cannot be computed.

    `progress.json` lives under the venv key, and the key folds in the base
    interpreter — so while that interpreter is what is missing, there is no key to
    report under. Deriving one from the 3.14 that happens to be running would name
    the directory of a venv nobody will ever build, and the real install (once 3.12
    lands) would report somewhere else entirely, leaving the page polling a file
    that never changes again.
    """
    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    spawned = []
    monkeypatch.setattr(envinstall, "_spawn",
                        lambda key, reqs, **kw: spawned.append((key, reqs, kw)) or 4242)

    rec = envinstall.start(["pip"])
    assert spawned, "no installer was started"
    key, _, kw = spawned[0]
    assert key == envinstall.PYTHON_BOOTSTRAP_KEY
    assert key != envinstall.venv_key_for(["pip"])
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
    monkeypatch.setattr(envinstall, "_spawn", lambda *a, **kw: os.getpid())

    monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)
    assert envinstall.start(["pip"])["key"] == envinstall.PYTHON_BOOTSTRAP_KEY

    monkeypatch.setattr(envinstall, "script_python_ready", lambda: True)
    envinstall.reset_venv_validation_cache()
    assert envinstall.start(["pip"])["key"] == envinstall.venv_key_for(["pip"])


@requires_fused
def test_the_venv_key_folds_in_the_RESOLVED_script_interpreter(
    tmp_path, monkeypatch, _fresh_script_python
):
    """The whole point of resolving one: the key the loader computes, and so the
    directory it fills, must be the key upstream will look in when built from that
    same interpreter. A resolution that did not reach the key would build 3.12
    venvs into the 3.14 venv's directory."""
    from fused.agent_core.backends.local.venvs import requirements_venv_id, venv_key

    exe = _py312_stub(tmp_path)
    monkeypatch.setattr(envinstall, "_python_executable", lambda: str(exe))
    reqs = ["pip"]
    assert envinstall.venv_key_for(reqs) == venv_key(
        requirements_venv_id(reqs, str(exe))
    )


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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    reqs = ["some-dist"]
    assert not envinstall.is_installed(reqs)

    venv_dir = tmp_path / envinstall.venv_key_for(reqs)
    venv_dir.mkdir()
    assert not envinstall.is_installed(reqs), "a marker-less dir is half-built"

    _runnable_venv_python(str(venv_dir))
    (venv_dir / ".openfused-ready").write_text("{}")
    assert envinstall.is_installed(reqs)


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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    reqs = ["some-dist"]
    venv_dir = tmp_path / envinstall.venv_key_for(reqs)
    marker = venv_dir / envinstall.READY_MARKER
    venv_dir.mkdir()
    marker.write_text("{}")
    # No interpreter at all is the cheapest unrunnable venv and the one shape that
    # behaves the same on every OS; the exits-nonzero shape is covered below.
    assert not envinstall.is_installed(reqs)
    assert not marker.exists(), "the marker must go, or upstream will not rebuild"


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX #! stub interpreter")
@requires_fused
def test_a_marked_venv_whose_python_FAILS_is_not_installed(tmp_path, monkeypatch):
    """Present but broken, which is what the real bug looked like.

    The DMG's venv python existed and was executable; it died on startup because
    its recorded base prefix was gone. So "the file is there" is not the question
    the probe asks — it has to actually run something.
    """
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    reqs = ["some-dist"]
    venv_dir = tmp_path / envinstall.venv_key_for(reqs)
    exe = envinstall._venv_python(str(venv_dir))
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"No module named 'encodings'\" >&2\nexit 1\n")
    os.chmod(exe, 0o755)
    (venv_dir / envinstall.READY_MARKER).write_text("{}")

    assert not envinstall.is_installed(reqs)
    assert not (venv_dir / envinstall.READY_MARKER).exists()


@requires_fused
def test_the_venv_probe_runs_at_most_once_per_venv_per_process(tmp_path, monkeypatch):
    """The cost ceiling. A probe per request would be a subprocess per request.

    `/api/run`'s pre-flight calls `is_installed` on every run of every PEP 723
    script, so the validation has to be memoized per venv directory per process —
    the same shape as `engine.app_interpreter()`'s one-probe-per-process cache.
    """
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    reqs = ["some-dist"]
    venv_dir = tmp_path / envinstall.venv_key_for(reqs)
    venv_dir.mkdir()
    _runnable_venv_python(str(venv_dir))
    (venv_dir / envinstall.READY_MARKER).write_text("{}")

    probes = []
    real = envinstall._venv_runs
    monkeypatch.setattr(
        envinstall, "_venv_runs", lambda d: (probes.append(d), real(d))[1]
    )
    for _ in range(5):
        assert envinstall.is_installed(reqs)
    assert probes == [str(venv_dir)]


@requires_fused
def test_a_missing_marker_never_probes(tmp_path, monkeypatch):
    """Nothing to validate: the marker's absence is already the whole answer.

    Also the common case by count — every first open of a PEP 723 script — so it
    must stay a single stat, not a spawn.
    """
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    probes = []
    monkeypatch.setattr(envinstall, "_venv_runs", lambda d: probes.append(d) or True)
    assert not envinstall.is_installed(["some-dist"])
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    reqs = ["some-dist"]
    venv_dir = tmp_path / envinstall.venv_key_for(reqs)
    marker = venv_dir / envinstall.READY_MARKER
    venv_dir.mkdir()
    marker.write_text("{}")
    assert not envinstall.is_installed(reqs)  # no interpreter -> marker removed

    _runnable_venv_python(str(venv_dir))  # what the rebuild leaves behind
    marker.write_text("{}")
    assert envinstall.is_installed(reqs)


# --- the probe has THREE answers, and the rebuild is bounded (D212) ------------
#
# Both of these came out of review, and both are about the same thing: the
# deletion at the end of `is_installed` destroys a venv the user paid a
# multi-hundred-MB download for, so it may only ever happen on *definite*
# evidence, and it may only ever happen once per venv per process.


def _marked_venv(tmp_path, monkeypatch, reqs, *, runnable=False):
    """A venv dir carrying a ready marker, with or without a working python."""
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    venv_dir = tmp_path / envinstall.venv_key_for(reqs)
    venv_dir.mkdir(exist_ok=True)
    if runnable:
        _runnable_venv_python(str(venv_dir))
    (venv_dir / envinstall.READY_MARKER).write_text("{}")
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="python", timeout=5)

    monkeypatch.setattr(envinstall.subprocess, "run", _timeout)
    assert envinstall.is_installed(reqs) is True
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)

    def _eagain(*a, **k):
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(envinstall.subprocess, "run", _eagain)
    assert envinstall.is_installed(reqs) is True
    assert (venv_dir / envinstall.READY_MARKER).exists()
    assert str(venv_dir) not in envinstall._VALIDATED


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
        reqs = [f"some-dist-{type(exc).__name__}"]
        venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)

        def _raise(*a, _exc=exc, **k):
            raise _exc

        monkeypatch.setattr(envinstall.subprocess, "run", _raise)
        assert envinstall.is_installed(reqs) is False
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs)  # no interpreter at all
    marker = venv_dir / envinstall.READY_MARKER

    probes = []
    real = envinstall._venv_runs
    monkeypatch.setattr(
        envinstall, "_venv_runs", lambda d: (probes.append(d), real(d))[1]
    )

    assert envinstall.is_installed(reqs) is False
    assert not marker.exists(), "the first failure earns one rebuild"

    # What the install worker leaves behind on a bundle that cannot be fixed by
    # rebuilding: the same broken venv, re-marked ready.
    marker.write_text("{}")
    assert envinstall.is_installed(reqs) is True, "no second rebuild"
    assert marker.exists(), "the marker must survive, or the loader downloads again"

    for _ in range(3):
        assert envinstall.is_installed(reqs) is True
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs)
    marker = venv_dir / envinstall.READY_MARKER

    assert envinstall.is_installed(reqs) is False
    marker.write_text("{}")
    assert envinstall.is_installed(reqs) is True  # bound engaged

    envinstall.reset_venv_validation_cache()
    assert envinstall.is_installed(reqs) is False, "a new process retries the repair"
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs)  # no interpreter at all
    marker = venv_dir / envinstall.READY_MARKER

    # "A, earlier in this process": one definite failure, so the venv dir is in the
    # rebuild set — the precondition the racing branch needs.
    assert envinstall.is_installed(reqs) is False
    marker.write_text("{}")  # what the install worker leaves behind

    def _probe_that_loses_the_race(venv):
        os.unlink(os.path.join(venv, envinstall.READY_MARKER))  # A's unlink lands
        return False

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_that_loses_the_race)
    assert envinstall.is_installed(reqs) is False, "join the install, do not run"
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs)
    marker = venv_dir / envinstall.READY_MARKER

    assert envinstall.is_installed(reqs) is False  # the one rebuild it gets
    marker.write_text("{}")  # the rebuild reproduced the same breakage

    needle = "still cannot run its own python after a rebuild"
    with caplog.at_level(logging.WARNING, logger="fused_render.envinstall"):
        caplog.clear()
        for _ in range(4):
            assert envinstall.is_installed(reqs) is True
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)

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
            results.append(envinstall.is_installed(reqs))

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
        assert envinstall.is_installed(reqs) is True
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)

    probes = []
    real = envinstall._venv_runs

    def _probe_then_someone_else_discards(d):
        verdict = real(d)
        with envinstall._validated_lock:
            envinstall._discard_verdict(d)  # what a concurrent discard does
        probes.append(d)
        return verdict

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_then_someone_else_discards)
    assert envinstall.is_installed(reqs) is True, "our own caller still gets an answer"
    assert str(venv_dir) not in envinstall._VALIDATED, "stored against a dead generation"
    assert envinstall.is_installed(reqs) is True
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs)  # no interpreter -> False
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

    assert envinstall.is_installed(reqs) is False, "answer about the venv we judged"
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)
    marker = venv_dir / envinstall.READY_MARKER
    real = envinstall._venv_runs

    def _probe_while_the_worker_unmarks(d):
        verdict = real(d)
        os.unlink(os.path.join(d, envinstall.READY_MARKER))
        return verdict

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_while_the_worker_unmarks)
    assert envinstall.is_installed(reqs) is False, "do not run against a rebuild"
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
    reqs = ["some-dist"]
    venv_dir = _marked_venv(tmp_path, monkeypatch, reqs, runnable=True)
    real = envinstall._venv_runs

    def _probe_then_reset(d):
        verdict = real(d)
        envinstall.reset_venv_validation_cache()
        return verdict

    monkeypatch.setattr(envinstall, "_venv_runs", _probe_then_reset)
    assert envinstall.is_installed(reqs) is True
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
    reqs_a, reqs_b = ["dist-a"], ["dist-b"]
    _marked_venv(tmp_path, monkeypatch, reqs_a, runnable=True)
    _marked_venv(tmp_path, monkeypatch, reqs_b, runnable=True)
    assert envinstall.is_installed(reqs_b) is True  # B's verdict is now cached

    in_probe, release = threading.Event(), threading.Event()

    def _stall(venv_dir):
        in_probe.set()
        release.wait(timeout=30)
        return True

    monkeypatch.setattr(envinstall, "_venv_runs", _stall)

    stalled = threading.Thread(target=envinstall.is_installed, args=(reqs_a,))
    stalled.start()
    try:
        assert in_probe.wait(timeout=10), "the probe of A never started"
        answered = []
        b = threading.Thread(target=lambda: answered.append(
            envinstall.is_installed(reqs_b)))
        b.start()
        b.join(timeout=10)
        assert answered == [True], "B waited on A's stalled probe"
    finally:
        release.set()
        stalled.join(timeout=30)


# --- /api/run's pre-flight ----------------------------------------------------


@requires_fused
def test_a_declared_header_with_no_venv_asks_for_an_install(tmp_path, monkeypatch):
    """The pre-flight answers instead of blocking on a download."""
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    target = tmp_path / "needs.py"
    target.write_text(
        '# /// script\n# dependencies = ["imagecodecs", "pyproj"]\n# ///\n'
        "def main():\n    return 1\n"
    )
    import asyncio

    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    need = out["needs_install"]
    assert need["requirements"] == ["imagecodecs", "pyproj"]
    assert need["key"] == envinstall.venv_key_for(["imagecodecs", "pyproj"])
    # The error object is still populated: a client that knows nothing about
    # needs_install shows a real message rather than "undefined".
    assert out["error"]["type"] == "EnvNotInstalled"
    assert "imagecodecs" in out["error"]["message"]


@requires_fused
def test_a_header_whose_venv_exists_just_runs(tmp_path, monkeypatch, warm_fused_backend_venv):
    """No pre-flight interference once the venv is there."""
    import asyncio

    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "ready.py"
    target.write_text(HEADER + "def main():\n    return 42\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is True, out
    assert out["result"] == 42
    assert "needs_install" not in out


@pytest.mark.parametrize("boom", [
    # `venv_key_for` imports `fused.agent_core...` unguarded — no fused, no import.
    ImportError("No module named 'fused'"),
    # `_backend_attr` raises this BY DESIGN when an upstream private attribute
    # disappears: guessing would fill a venv no run ever reads. routers/env.py
    # already catches (ImportError, RuntimeError) for exactly this pair.
    RuntimeError("this fused build's Backend has no '_venvs_path'"),
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
    target = tmp_path / "needs.py"
    target.write_text(
        '# /// script\n# dependencies = ["imagecodecs"]\n# ///\n'
        "def main():\n    return 1\n"
    )
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    reqs = ["pip"]
    key = envinstall.venv_key_for(reqs)
    envinstall.start(reqs)
    prog = _wait_done(key, timeout=300)
    assert prog["error"] is None, prog
    assert prog["done"] is True
    assert prog["stage"] == "done"
    assert prog["pct"] == 100
    assert envinstall.is_installed(reqs)


@requires_fused
def test_a_resolver_failure_reaches_the_user_verbatim(tmp_path, monkeypatch):
    """The whole point of making this visible.

    A distribution that cannot resolve must surface uv's/pip's own words, not an
    `EngineError` about "an internal error while running <path>". The assertion
    is deliberately on the resolver's text, not on a message we wrote.
    """
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    # A name PyPI cannot have: no index lookup can succeed, and the failure is
    # the resolver's, which is exactly the class of error being surfaced.
    reqs = ["fused-render-no-such-distribution-9e3f1c"]
    key = envinstall.venv_key_for(reqs)
    envinstall.start(reqs)
    prog = _wait_done(key, timeout=300)
    assert prog["done"] is True
    assert prog["error"], prog
    assert "fused-render-no-such-distribution-9e3f1c" in prog["error"]
    assert not envinstall.is_installed(reqs)


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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
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
    assert "unexpectedly" in prog["error"]


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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    key = "deadbeefdeadbeef"
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "progress.json"), "w", encoding="utf-8") as f:
        # A pid that cannot be running: 2**31-1 is above every platform's pid_max.
        json.dump({"stage": "install", "pct": 25, "detail": "", "done": False,
                   "error": None, "pid": 2 ** 31 - 1, "ts": time.time()}, f)
    prog = envinstall.progress(key)
    assert prog["done"] is True
    assert "unexpectedly" in prog["error"]


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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    sent = []
    monkeypatch.setattr(envinstall.os, "killpg",
                        lambda *a: pytest.fail("must not signal our own group"))
    monkeypatch.setattr(envinstall.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    if os.getpgid(os.getpid()) == os.getpid():
        pytest.skip("this process leads its own group, so there is no hazard to model")
    assert envinstall._kill(os.getpid()) is True
    assert sent == [(os.getpid(), signal.SIGTERM)]


@requires_fused
def test_the_worker_builds_the_venv_the_server_will_look_for(tmp_path, monkeypatch):
    """The worker's venv and `venv_dir_for`'s must be the SAME directory.

    Both are keyed on the backend's base interpreter — `venv_key_for` through
    `_backend_attr("_python_executable")`, the worker through whatever it hands
    `ensure_requirements_venv`. The worker used to hardcode `None` there, which
    agrees only while the backend's own value is None too. Let that attribute
    ever be set and `is_installed()` never turns true: the page installs, retries,
    is told `needs_install` again, and runtime.js turns that into a permanent
    "declares dependencies that are not installed yet" — with a fully built venv
    sitting on disk. So the executable travels through argv, and this test drives
    a non-None one end to end.
    """
    import importlib.util

    from fused.agent_core.backends.local.venvs import requirements_venv_id, venv_key

    # A REAL other interpreter, not `sys.executable`: `python_identity` folds
    # `python_executable or sys.executable` into the key, so None and our own
    # path produce the identical key and the drift this test is about would be
    # invisible (which is exactly why the hardcoded None was latent, not broken).
    other = next(
        (p for p in ("/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3")
         if os.access(p, os.X_OK) and os.path.realpath(p) != os.path.realpath(sys.executable)),
        None,
    )
    if other is None:
        pytest.skip("no second python interpreter to key a venv on")

    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    monkeypatch.setattr(envinstall, "_python_executable", lambda: other)
    reqs = ["pip"]
    key = envinstall.venv_key_for(reqs)

    argv = []

    class _Proc:
        pid = os.getpid()

    monkeypatch.setattr(envinstall.subprocess, "Popen",
                        lambda cmd, **kw: (argv.extend(cmd), _Proc())[1])
    envinstall._spawn(key, list(reqs))

    # Now run the worker's own entry logic over exactly that argv, with the
    # upstream builder replaced by its (documented) directory recipe — the real
    # one would download.
    spec = importlib.util.spec_from_file_location(
        "_env_install_worker_under_test",
        os.path.join(os.path.dirname(envinstall.__file__), "_env_install_worker.py"),
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    built = {}

    def _fake_ensure(venvs_path, requirements, python_executable):
        d = os.path.join(
            os.path.expanduser(venvs_path),
            venv_key(requirements_venv_id(list(requirements), python_executable)),
        )
        built["dir"] = d
        return os.path.join(d, "bin", "python")

    import fused.agent_core.backends.local.venvs as _venvs

    monkeypatch.setattr(_venvs, "ensure_requirements_venv", _fake_ensure)
    worker.main(argv[2:])

    assert built["dir"] == envinstall.venv_dir_for(reqs), (
        "the worker built a venv under a different key than the server looks for"
    )


# --- the worker's builder is loaded by FILE, not through the `fused` package ---
#
# `from fused.agent_core.backends.local.venvs import ...` imports every parent,
# so `fused/__init__` runs → `fused.api` → `fused._auth` → `fused._optional_deps`
# → pandas. Measured: ~520ms of the 543ms it took this worker to install one tiny
# pure-Python package was that import chain, to reach a module whose own import
# costs microseconds. `venvs.py` is stdlib-only, so it can be loaded straight off
# disk — and being the SAME FILE is what keeps `venv_key` identical to the key
# `envinstall.venv_key_for` computes. These two tests protect both halves.


@requires_fused
def test_the_worker_loads_venvs_without_importing_the_fused_package(tmp_path):
    """A fresh process must reach `ensure_requirements_venv` with no `fused` import.

    Asserted in a SUBPROCESS because this test session has `fused` (and pandas)
    imported already — in-process the absence could never be observed, which is
    precisely why the cost went unnoticed.
    """
    worker_path = os.path.join(os.path.dirname(envinstall.__file__),
                               "_env_install_worker.py")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('_w', {worker_path!r})\n"
        "w = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(w)\n"
        "mod = w._venvs_module()\n"
        "print(json.dumps({\n"
        "    'has_builder': hasattr(mod, 'ensure_requirements_venv'),\n"
        "    'has_key': hasattr(mod, 'venv_key'),\n"
        "    'fused': 'fused' in sys.modules,\n"
        "    'pandas': 'pandas' in sys.modules,\n"
        "}))\n",
        encoding="utf-8",
    )
    proc = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["has_builder"] and got["has_key"], "the loaded module is not venvs.py"
    assert got["fused"] is False, (
        "loading venvs.py executed fused/__init__ — the ~500ms this avoids is back"
    )
    assert got["pandas"] is False, "pandas was imported to install a package"


@requires_fused
def test_the_directly_loaded_venvs_keys_a_venv_exactly_like_the_server_does():
    """The invariant that makes the file load safe: same file, same key.

    `envinstall.venv_key_for` composes upstream's `requirements_venv_id`/`venv_key`
    through the package import; the worker now composes them from a module loaded
    off disk. If those two ever disagreed the worker would fill a directory
    `is_installed()` never looks in — the page would install, retry, and be told to
    install again forever, with a fully built venv on disk (the same failure the
    argv-borne `python_executable` exists to prevent).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_env_install_worker_keycheck",
        os.path.join(os.path.dirname(envinstall.__file__), "_env_install_worker.py"),
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    mod = worker._venvs_module()
    reqs = ["pandas>=2.0.0", "pyarrow>=14.0.0"]
    assert mod.venv_key(
        mod.requirements_venv_id(list(reqs), envinstall._python_executable())
    ) == envinstall.venv_key_for(reqs)


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
        lambda venvs_path, requirements, python_executable: (
            (beats.wait(10), "/x/venv/bin/python")[1]
        ),
    )
    worker.install("k", d, str(tmp_path / "venvs"), ["pandas", "pyproj"])

    assert len(seen) >= 2, f"the heartbeat did not repeat: {seen}"
    assert all(pct == worker._INSTALL_PCT for pct, _, _ in seen), seen
    stamps = [ts for _, _, ts in seen]
    assert stamps == sorted(stamps) and stamps[0] < stamps[-1], (
        f"ts never moved, so nothing on the wire proves the install is alive: {stamps}"
    )
    assert all("pandas, pyproj" in detail for _, detail, _ in seen), seen
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
        lambda venvs_path, requirements, python_executable: (
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
    worker.install("k", ok_dir, str(tmp_path / "venvs"), ["pandas"])
    assert calls[-1] == ("done", True), calls
    assert _record(ok_dir)["done"] is True

    calls.clear()
    beats.clear()

    def _boom(venvs_path, requirements, python_executable):
        beats.wait(10)
        raise RuntimeError("Failed to install: no wheels for imagecodecs")

    monkeypatch.setattr(worker, "_build", _boom)
    bad_dir = str(tmp_path / "bad")
    with pytest.raises(RuntimeError):
        worker.install("k", bad_dir, str(tmp_path / "venvs"), ["imagecodecs"])
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
        lambda venvs_path, requirements, python_executable: (
            (first_beat.wait(10), "/x/venv/bin/python")[1]
        ),
    )
    # Released from a timer: the main thread is inside `install()`, which is where
    # the contention this test creates has to be resolved.
    threading.Timer(0.2, held.set).start()
    worker.install("k", d, str(tmp_path / "venvs"), ["pandas"])
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

    worker.install("k", d, str(tmp_path / "venvs"), ["tensorflow"],
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

    worker.install("k", d, str(tmp_path / "venvs"), ["tensorflow"],
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
        worker.install("k", d, str(tmp_path / "venvs"), ["tensorflow"],
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
    worker.main(["k", str(tmp_path), str(tmp_path / "venvs"), "", "", "pandas"])
    assert seen["kwargs"]["acquire_python"] is None

    seen.clear()
    worker.main(["k", str(tmp_path), str(tmp_path / "venvs"), "", "3.12", "pandas"])
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
        lambda venvs_path, requirements, python_executable: "/x/venv/bin/python",
    )
    with pytest.raises(OSError):
        worker.install("k", d, str(tmp_path / "venvs"), ["pandas"])

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
        lambda venvs_path, requirements, python_executable: "/x/venv/bin/python",
    )
    worker.install("k", str(tmp_path / "a"), str(tmp_path / "venvs"), ["pandas"])
    assert threads and all(t.daemon for t in threads)
    assert not any(t.is_alive() for t in threads), "the heartbeat outlived the install"

    def _boom(venvs_path, requirements, python_executable):
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "_build", _boom)
    with pytest.raises(RuntimeError):
        worker.install("k", str(tmp_path / "b"), str(tmp_path / "venvs"), ["pandas"])
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
        lambda venvs_path, requirements, python_executable: (
            seen.append(python_executable) or "/x/bin/python"
        ),
    )
    d = str(tmp_path / "prog")
    worker.main(["k", d, str(tmp_path / "venvs"), "", "", "pip"])
    assert seen == [None]
    worker.main(["k", d, str(tmp_path / "venvs"), "/usr/bin/python3", "", "pip"])
    assert seen == [None, "/usr/bin/python3"]


@requires_fused
def test_starting_twice_does_not_spawn_a_second_worker(tmp_path, monkeypatch):
    """Two pages (or a double-click) must share one install, not race it.

    Two workers building the same directory is the race `fused`'s in-process
    lock cannot cover — the loser dies on a half-built `<venv>/bin/python`.
    """
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    spawned = []
    # Our own pid, because it is provably alive: a made-up one would be reaped by
    # the liveness check in `progress()` and the second start would legitimately
    # re-spawn, which would pass this test for the wrong reason.
    monkeypatch.setattr(envinstall, "_spawn",
                        lambda *a, **kw: spawned.append(a) or os.getpid())
    reqs = ["pip"]
    envinstall.start(reqs)
    envinstall.start(reqs)
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    workers = 16
    barrier = threading.Barrier(workers)
    spawned = []
    lock = threading.Lock()

    def fake_spawn(key, reqs, **kw):
        with lock:
            spawned.append(key)
        return os.getpid()  # provably alive, so `_in_flight` stays true

    monkeypatch.setattr(envinstall, "_spawn", fake_spawn)
    reqs = ["pip"]
    errors = []

    def go():
        try:
            barrier.wait(timeout=30)
            envinstall.start(reqs)
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
def test_a_stale_claim_from_a_dead_installer_is_taken_over(tmp_path, monkeypatch):
    """A crashed installer must not wedge the key forever.

    The claim file outlives the process that made it, so "claim exists" cannot
    mean "give up" — otherwise one crash makes a template permanently
    un-installable with no way back short of deleting a cache directory by hand.
    """
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    reqs = ["pip"]
    key = envinstall.venv_key_for(reqs)
    spawned = []
    monkeypatch.setattr(
        envinstall, "_spawn", lambda k, r, **kw: spawned.append(k) or (2 ** 31 - 1)
    )
    envinstall.start(reqs)
    assert len(spawned) == 1
    assert os.path.exists(os.path.join(envinstall.progress_dir(key), "claim"))

    # The recorded pid cannot be running, so this install reads as crashed.
    assert envinstall.progress(key)["done"] is True
    envinstall.start(reqs)
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    reqs = ["pip"]
    key = envinstall.venv_key_for(reqs)
    monkeypatch.setattr(
        envinstall, "_spawn", lambda *a: pytest.fail("the loser must not spawn")
    )
    # The winner, still inside `Popen`: claim taken, nothing written yet.
    assert envinstall._claim(key) is True
    record = envinstall.start(reqs)
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    reqs = ["pip"]
    key = envinstall.venv_key_for(reqs)
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
    record = envinstall.start(reqs)
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
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    reqs = ["pip"]
    key = envinstall.venv_key_for(reqs)
    monkeypatch.setattr(envinstall, "_spawn", lambda k, r, **kw: 2 ** 31 - 1)
    envinstall.start(reqs)
    assert envinstall.progress(key)["error"], "the first attempt reads as crashed"

    # Age the claim so the retry may take it over, exactly as a real retry does.
    claim = os.path.join(envinstall.progress_dir(key), "claim")
    old = time.time() - envinstall._CLAIM_GRACE_S - 60
    os.utime(claim, (old, old))
    live = []
    monkeypatch.setattr(envinstall, "_spawn",
                        lambda k, r, **kw: live.append(k) or os.getpid())
    record = envinstall.start(reqs)
    assert live == [key], "the retry must spawn"
    assert record["error"] is None, record
    assert envinstall.progress(key)["error"] is None


@requires_fused
def test_start_is_a_no_op_once_the_venv_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    reqs = ["pip"]
    venv_dir = os.path.join(str(tmp_path / "venvs"), envinstall.venv_key_for(reqs))
    os.makedirs(venv_dir, exist_ok=True)
    # A runnable interpreter, not just the marker: since D212 `is_installed`
    # verifies the claim once, and a marker over an empty directory now reads
    # (correctly) as "not installed" — which is a different test than this one.
    _runnable_venv_python(venv_dir)
    with open(os.path.join(venv_dir, ".openfused-ready"), "w") as f:
        f.write("{}")
    spawned = []
    monkeypatch.setattr(envinstall, "_spawn", lambda *a, **kw: spawned.append(a) or 1)
    envinstall.start(reqs)
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
