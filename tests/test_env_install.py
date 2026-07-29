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
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


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
def test_readiness_follows_the_ready_marker_not_the_directory(tmp_path, monkeypatch):
    """A half-built venv (no marker) must read as NOT ready.

    `ensure_requirements_venv` deletes and rebuilds a marker-less directory, so
    treating "the directory exists" as installed would skip the loader and hand
    the request the very build it was meant to move off the request path.
    """
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path))
    reqs = ["some-dist"]
    assert not envinstall.is_installed(reqs)

    venv_dir = tmp_path / envinstall.venv_key_for(reqs)
    venv_dir.mkdir()
    assert not envinstall.is_installed(reqs), "a marker-less dir is half-built"

    (venv_dir / ".openfused-ready").write_text("{}")
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


@requires_fused
def test_start_is_a_no_op_once_the_venv_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(envinstall, "venvs_path", lambda: str(tmp_path / "venvs"))
    reqs = ["pip"]
    venv_dir = os.path.join(str(tmp_path / "venvs"), envinstall.venv_key_for(reqs))
    os.makedirs(venv_dir, exist_ok=True)
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
