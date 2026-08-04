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
import json
import os
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

    Also drops the per-process venv-validation memo (D206). That cache is keyed by
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
    still final — but since D206 it is a *claim* that is verified once per process
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


# --- a marker is a claim, and the claim is verified once (D206) ----------------
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
    worker.main(["k", d, str(tmp_path / "venvs"), "", "pip"])
    assert seen == [None]
    worker.main(["k", d, str(tmp_path / "venvs"), "/usr/bin/python3", "pip"])
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
    monkeypatch.setattr(envinstall, "_spawn", lambda *a: spawned.append(a) or os.getpid())
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

    def fake_spawn(key, reqs):
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
        envinstall, "_spawn", lambda k, r: spawned.append(k) or (2 ** 31 - 1)
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

    def _spawn_then_report(k, r):
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
    monkeypatch.setattr(envinstall, "_spawn", lambda k, r: 2 ** 31 - 1)
    envinstall.start(reqs)
    assert envinstall.progress(key)["error"], "the first attempt reads as crashed"

    # Age the claim so the retry may take it over, exactly as a real retry does.
    claim = os.path.join(envinstall.progress_dir(key), "claim")
    old = time.time() - envinstall._CLAIM_GRACE_S - 60
    os.utime(claim, (old, old))
    live = []
    monkeypatch.setattr(envinstall, "_spawn", lambda k, r: live.append(k) or os.getpid())
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
    # A runnable interpreter, not just the marker: since D206 `is_installed`
    # verifies the claim once, and a marker over an empty directory now reads
    # (correctly) as "not installed" — which is a different test than this one.
    _runnable_venv_python(venv_dir)
    with open(os.path.join(venv_dir, ".openfused-ready"), "w") as f:
        f.write("{}")
    spawned = []
    monkeypatch.setattr(envinstall, "_spawn", lambda *a: spawned.append(a) or 1)
    envinstall.start(reqs)
    assert spawned == []


# --- honesty about granularity ------------------------------------------------


def test_progress_stages_are_the_ones_we_can_actually_observe():
    """`venvs._run_step` uses capture_output=True, so pip's per-package output
    is unavailable without changing `fused`. The stage list is therefore coarse
    ON PURPOSE, and named here so a future "62%" that implies per-package
    resolution has to argue with a test first.
    """
    assert envinstall.STAGES == ("spawn", "create", "install", "done")


def _wait_done(key, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        prog = envinstall.progress(key)
        if prog and prog.get("done"):
            return prog
        time.sleep(0.1)
    pytest.fail(f"installer for {key} did not finish within {timeout}s: "
                f"{envinstall.progress(key)}")
