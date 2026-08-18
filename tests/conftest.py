"""Redirect the shell home dir + Fused workspace dir to throwaway tmp dirs for
the whole test run.

Importing fused_render.server / .executor now stages the core templates into
home_dir()/.core-templates on import (core_templates.ensure_core_templates).
This runs at collection time — before any fixture — so the redirect must be
set here at conftest import, ahead of the first test-module import, or the
copy would land in the real ~/.fused-render.

FUSED_RENDER_DIR is redirected for the same reason: /api/config reads it (D81)
and the seed tests write into it, so no test may see the real ~/Documents/Fused.

CLAUDE_CONFIG_DIR likewise: the user-level skill sync (user_skills.py, D185)
writes into <config dir>/skills/ and POST /api/apps/new triggers it, so no
test may touch the real ~/.claude.

FUSED_RENDER_ENGINE is pinned to `builtin` for the same class of reason. D204
flipped the engine PREF's default to fused-when-available, so from then on the
executor an incidental `/api/run` test exercises depends on whether the optional
`fused` package happens to be importable in the environment — which is exactly
the install-order dependence D204 accepts for users and must not inherit into the
suite, where it would mean the call-log, template and mount tests silently cover
a different runner on a machine with the extra installed. The tests that are
ABOUT the engine or the pref clear or set this variable themselves (see
test_server_engine.py and test_shell_prefs.py::_client).

Only allocate + register cleanup when the var is unset, so a caller that set it
(CI pointing at a real dir) still wins and we don't eagerly leak a mkdtemp we
never use. The dirs we create are removed at process exit.
"""
import atexit
import os
import shutil
import signal
import tempfile

import pytest

for _var, _prefix in (("FUSED_RENDER_HOME", "fused-render-tests-"),
                       ("FUSED_RENDER_DIR", "fused-render-tests-dir-"),
                       ("CLAUDE_CONFIG_DIR", "fused-render-tests-claude-")):
    if _var not in os.environ:
        _tmp = tempfile.mkdtemp(prefix=_prefix)
        os.environ[_var] = _tmp
        atexit.register(shutil.rmtree, _tmp, ignore_errors=True)

# A stable default executor for every test that reaches /api/run without caring
# which engine answers — see the module docstring. Not a tmpdir, so it gets its
# own line rather than a fourth entry above.
os.environ.setdefault("FUSED_RENDER_ENGINE", "builtin")


# What the warm fixture's project folder declares (SPEC PY-16 — the environment
# is the FOLDER's, so the fixture hands back a directory rather than a header).
# `pip` because the dev-env recipe seeds it into this venv already, so uv resolves
# it from cache and the real-backend venv tests stay offline-safe — the assertions
# are about WHICH interpreter ran, never about the package.
WARM_DEPS = ["pip"]
WARM_PYPROJECT = (
    "[project]\nname = 'fused-render-warm'\nversion = '0.1.0'\n"
    'dependencies = ["pip"]\n'
)

# The FUSED_RENDER_HOME the warm venv was actually built under. The venv store is
# `<home_dir()>/venvs`, so a test whose own fixture redirects the home would look
# for the warm venv in a directory nobody built — this is what it restores.
WARM_HOME = None


@pytest.fixture(scope="session")
def warm_fused_backend_venv(tmp_path_factory):
    """Build one project venv once, serialized across xdist workers.

    Returns the PROJECT FOLDER it warmed: the environment belongs to the folder
    (SPEC PY-16), so a test that wants the warm venv puts its `.py` inside this
    directory. Every real-backend test that needs a venv at all uses this one
    folder, so they all want the same venv (a script in a folder with no
    `pyproject.toml` runs on the app's own interpreter — PY-17 — and builds
    nothing, which is why the declaration is what makes this fixture necessary). Creating it is guarded
    only by an in-process lock inside `fused`, which is no guard at all against
    `-n auto`: on a cold cache (a CI runner, always) N worker processes each
    find no ready-marker, each start building the same directory, and the losers
    die on `FileNotFoundError: <venv>/bin/python` mid-build. Cheap to reproduce —
    `rm -rf` the venv and run the engine tests — and it fails the *tests*, which
    reads as an engine bug rather than a test-harness race.

    So the first worker to arrive builds it while the others wait on a lock file
    in xdist's shared base temp dir (its `.parent` is common to all workers).
    O_CREAT|O_EXCL rather than `filelock`: no dependency, and the only thing
    needed is "exactly one process in here at a time".

    Two ways this fixture could look like it worked without working, both of
    which defeat its entire purpose by handing the race back:

      * breaking a lock a LIVE worker still holds. It writes its pid, and a
        waiter only steals the lock when that pid is gone (or the file is
        unreadable) AND it has not been touched for a while — a timeout alone is
        not evidence of a crash, since a cold `uv` build legitimately takes
        minutes. Two holders at once would be worse than no lock: whichever
        finished first would unlink the *other's* lock file, and every
        subsequent waiter would see a free lock.
      * ignoring the warm's outcome. `run_python` reports a venv-build failure
        as an `{ok: false}` dict rather than raising, so a discarded result
        means a failed warm releases the lock and lets every worker proceed into
        exactly the FileNotFoundError this exists to prevent — with a confusing
        downstream failure instead of the real error. The result is checked and
        the session fails loudly with the engine's own message.
    """
    import asyncio
    import time

    from fused_render import engine, envinstall

    if not engine.available():
        return None  # the tests that ask for this are skipped anyway

    # Build from THIS interpreter, whatever version it is (D214). Session-scoped, so
    # the function-scoped pin below has not run yet and the real resolution would
    # apply: on any runner that is not on the pinned 3.12 and has no uv-managed 3.12
    # — the `fused-engine` CI job is exactly that, deliberately pinned to 3.11 as the
    # extra's floor — `start()` correctly asks for the INTERPRETER first, under
    # `PYTHON_BOOTSTRAP_KEY`, and this fixture polls the venv key and sees nothing
    # ("the warm script venv was not built. last progress: None").
    #
    # Pinned rather than taught the two-round flow on purpose: what these tests need
    # is *a* working venv, not a particular Python, and making CI download 30MB of
    # CPython to satisfy a fixture buys nothing. The bootstrap flow has its own tests.
    envinstall._script_python = (None, True)

    # One shared project folder for every worker, in xdist's common base temp dir.
    # The venv key IS this path (projectenv), so all workers agree on the venv only
    # while they agree on the directory — which is why it is not per-worker tmp.
    global WARM_HOME
    WARM_HOME = os.environ.get("FUSED_RENDER_HOME")

    shared = tmp_path_factory.getbasetemp().parent
    project_dir = str(shared / "fused-render-warm-project")
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "pyproject.toml"), "w", encoding="utf-8") as fh:
        fh.write(WARM_PYPROJECT)

    lock = shared / "fused-bare-venv.lock"
    stale_after = 600  # a cold `uv venv` + install can legitimately take minutes
    give_up_at = time.monotonic() + 1800

    def _holder_gone() -> bool:
        """True when the lock's owner is provably not running any more."""
        try:
            pid = int(lock.read_text().strip() or 0)
        except (OSError, ValueError):
            return True  # unreadable/garbage: no owner to protect
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # EPERM: someone else's live process — do not touch it
        return False

    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > give_up_at:
                pytest.fail(
                    f"timed out waiting for another worker to build the fused "
                    f"backend venv (lock: {lock})"
                )
            try:
                idle = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # holder released it between the open and the stat
            if idle > stale_after and _holder_gone():
                try:
                    os.unlink(str(lock))
                except OSError:
                    pass
            time.sleep(0.2)

    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        # Drive the INSTALL LOADER, not /api/run. `run_python` deliberately no
        # longer builds a venv inline — a script whose header names something
        # uninstalled comes back as `needs_install` so the download happens off
        # the request path (SPEC PY-18). This fixture predates that and used to
        # rely on the inline build; when the contract changed it started failing
        # with `EnvNotInstalled`, which is the new behaviour working exactly as
        # designed. So it now does what a page does: ask for the install and wait.
        #
        # The point of the fixture is unchanged — serialize venv CREATION so N
        # xdist workers don't race on a half-built `<venv>/bin/python` — and it is
        # still done through our own API rather than fused internals.
        from fused_render import envinstall

        if not envinstall.is_installed(project_dir):
            envinstall.start(project_dir)
            key = envinstall.venv_key_for(project_dir)
            # Bounded and DIAGNOSTIC. `is_installed()` is the authority, not the
            # progress record: the venv existing is the thing the tests need, and
            # making bookkeeping the success condition is how a wait turns into a
            # hang. The budget is minutes rather than the quarter hour this first
            # had — a cold `uv venv` + install of one small package is seconds,
            # and anything slower is broken, not busy. On timeout the worker's own
            # log is printed, because "timed out" on its own says nothing.
            deadline = time.monotonic() + 300
            progress = None
            while time.monotonic() < deadline:
                if envinstall.is_installed(project_dir):
                    break
                progress = envinstall.progress(key)
                if progress and progress.get("done"):
                    break
                time.sleep(0.2)
            if progress and progress.get("error"):
                pytest.fail(
                    "could not build the fused backend's script venv, so the "
                    "real-backend tests would race on a half-built one: "
                    f"{progress['error']}"
                )
            if not envinstall.is_installed(project_dir):
                log = os.path.join(envinstall.progress_dir(key), "worker.log")
                tail = ""
                try:
                    with open(log, encoding="utf-8", errors="replace") as fh:
                        tail = fh.read()[-4000:]
                except OSError as e:
                    tail = f"(no worker log: {e})"
                pytest.fail(
                    f"the warm project venv ({project_dir}) was not built.\n"
                    f"last progress: {progress}\n"
                    f"venv dir: {envinstall.venv_dir_for(project_dir)}\n"
                    f"uv: {envinstall.uv_bin()}\n"
                    f"--- worker.log ---\n{tail}"
                )
    finally:
        # Released as soon as the venv exists — the lock serializes *creation*,
        # not the tests that use it.
        os.close(fd)
        try:
            os.unlink(str(lock))
        except OSError:
            pass
    return project_dir
# The template<->app env contract (SPEC PY-15): the server exports these before
# it serves, and `templates/shared/appenv.py` is the only reader. They are set
# with a plain os.environ assignment by design — every child process has to
# inherit them — which means a test that starts a server (or calls
# export_app_env / export_ro_mounts_env directly) leaves them behind for every
# later test in the same worker. That leak is invisible and one-directional: the
# next test's mount detection quietly answers against the previous test's home.
_APPENV_VARS = ("FUSED_RENDER_HOME_DIR", "FUSED_RENDER_MOUNTS_DIR",
                "FUSED_RENDER_RO_MOUNTS", "FUSED_RENDER_ORIGIN",
                # D216: the skill plugin root a spawned claude session is handed.
                # Leaks the same way — a test that calls export_app_env would
                # otherwise leave a previous test's plugin path on every later
                # spawn's argv in the same worker.
                "FUSED_RENDER_SKILL_PLUGIN_DIR")


@pytest.fixture(autouse=True)
def _isolate_appenv_contract_vars():
    """Every test starts with the contract vars UNSET and cannot leak them.

    Unset is the honest default for a test: it is the state before any server
    has started, so a template under test either sets what it needs or exercises
    the documented no-server fallback."""
    saved = {v: os.environ.pop(v, None) for v in _APPENV_VARS}
    try:
        yield
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


@pytest.fixture(autouse=True)
def _pin_the_script_interpreter_resolution():
    """Every test starts believing this machine's pinned Python is available.

    `envinstall.script_python()` (D214) resolves the interpreter script venvs are
    built from, and its answer depends on TWO things a test has no business
    depending on: the version of Python running the suite, and whether uv's managed
    registry happens to hold a 3.12 on this machine. Left alone, the whole suite
    would behave differently per interpreter — CI runs the matrix on 3.10/3.11/3.13,
    where the resolution goes to "no 3.12 yet", and `is_installed` then answers False
    for every requirement set, so tests about markers, probes and rebuild budgets
    would fail for a reason unrelated to what they assert.

    Pinned to `(None, True)` — "build from ours, and it is fine" — because that is
    what the resolution is for the interpreters this project ships on, and it is the
    pre-D214 behaviour, so tests written before the pin keep testing what they meant
    to. Tests that are ABOUT the resolution reset it (see `_fresh_script_python` in
    tests/test_env_install.py) and drive it explicitly.
    """
    from fused_render import envinstall

    envinstall._script_python = (None, True)
    try:
        yield
    finally:
        envinstall.reset_script_python_cache()


@pytest.fixture(autouse=True)
def _no_background_mount_threads(monkeypatch):
    """`create_app` starts two daemon threads that reach for a real rclone;
    neither may run in a test.

    `shell_mounts.startup()` runs run_automount -> attach_mount -> ensure_rcd,
    and `start_health_monitor()` re-attaches a mount it finds disconnected the
    same way. Both are started from the create_app BODY, so every
    `TestClient(create_app(...))` in the suite starts them — on CI, which
    installs rclone, ensure_rcd then genuinely execs a daemon. Two consequences,
    both observed:

      - the thread outlives the test that made the app (daemon, never joined),
        so `write_rcd_state` lands in whatever tmp home is current when it gets
        there. A LATER test then reads an rcd.json pointing at a real live
        daemon it never started: test_ensure_rcd_reuses_live_daemon got that
        daemon's port back instead of its stub's (`assert 59165 == 42993`,
        CI 2026-08-07) — with no spawn inside its own window to explain it.
      - each spawn leaks a real daemon until session teardown reaps it
        (_reap_test_rcd_daemons below, which exists because of this).

    No test asserts either function spawns anything; the tests that are ABOUT
    automount call `run_automount()` directly against the stub rcd."""
    from fused_render.shell import mounts

    monkeypatch.setattr(mounts, "startup", lambda: None)
    monkeypatch.setattr(mounts, "start_health_monitor", lambda: None)


@pytest.fixture(autouse=True)
def _no_startup_index_scan(monkeypatch):
    """`create_app` schedules a background scan of the user's HOME dir.

    Same hazard as the mount threads above, one step worse: a test that runs
    the lifespan (`with TestClient(create_app(...))`) would spawn a detached
    worker that walks the developer's real home directory and writes an index
    — outliving the test, and pointed at whatever FUSED_RENDER_HOME happened
    to be current when the spawn landed.

    The tests that are ABOUT the scheduler call `run_startup_scan()` directly
    with their own config (tests/test_index_api.py)."""
    from fused_render.server.routers import index as index_routes

    async def _skip(start_dir=None):
        return None

    monkeypatch.setattr(index_routes, "startup_scan", _skip)
    # The same hook also warms the home page's search corpus on a detached
    # thread. Harmless to the index, but it sweeps `git check-ignore` over the
    # developer's real home and outlives the test that spawned it — so it goes
    # too. Its own tests call `run_startup_warm()` directly.
    monkeypatch.setattr(index_routes, "startup_warm", lambda: None)
    # Same hazard by a second door: GET /api/fs/list notes the folder for the
    # index freshness check (scan-incremental.md §5), which runs on a background
    # thread and can call runner.start. A test listing any path under the
    # developer's real home would therefore spawn a detached whole-home scan —
    # a leaked thread that outlives the test and reads whatever
    # FUSED_RENDER_HOME has moved on to, exactly as above.
    #
    # The tests that are ABOUT the check call `_run_freshness_check` /
    # `freshness.note_folder_opened` directly with their own config
    # (tests/test_index_api.py, tests/test_index_freshness.py).
    monkeypatch.setattr(index_routes, "note_folder_opened", lambda path: False)
    # ...and by a third: every /api/fs mutation tells the index which folder it
    # changed (server/index_touch.py), which arms a timer thread that calls
    # runner.start. A test that renames a file anywhere would therefore spawn a
    # detached scan of that folder a second and a half later — after the test
    # has finished and its tmp home has gone.
    #
    # The tests that are ABOUT it drive `RescanQueue` directly with injected
    # deps (tests/test_index_touch.py).
    from fused_render.server import fs_mutate

    monkeypatch.setattr(fs_mutate, "note_index_mutation", lambda *paths: None)


@pytest.fixture(autouse=True)
def _no_startup_engine_warm(monkeypatch):
    """Neutralize create_app's fused-engine warm daemon in tests (same leaked-thread
    hazard as the mounts/index fixtures above; it leaks os.scandir into test_index_scan)."""
    from fused_render import engine

    monkeypatch.setattr(engine, "warm_in_background", lambda: None)
    engine._available_cached = None
    yield
    engine._available_cached = None


@pytest.fixture(scope="session", autouse=True)
def _no_real_rcd_spawn():
    """Make spawning a REAL rclone rcd from the suite impossible, loudly.

    The flake this kills (PR #407's own CI run proved the mechanism): a
    background mount thread leaked by an earlier test finishes its ensure_rcd
    spawn-wait AFTER the FUSED_RENDER_HOME env var has moved on to a later
    test's home, so write_rcd_state (rcd.py, post-liveness) lands a real
    daemon's {port, pid} in THAT test's rcd.json — and its ensure_rcd then
    "reuses" a foreign daemon (assert 55455 == 37291). Per-test patches can't
    stop it: env vars and monkeypatches are process-global, so a leaked thread
    always sees whatever the current test sees. The only sound boundary is the
    spawn itself: no real daemon can ever exist, so no foreign state write can
    ever land — and the leaked thread now raises, which pytest surfaces as an
    unhandled-thread-exception warning NAMING the leaking thread.

    Session-scoped and applied to rcd's module namespace only, so:
      * tests that fake the spawn by patching `mounts_mod.subprocess.Popen`
        (the stdlib module object — test_mounts_rcd_persist/_auth,
        test_mount_nfs_handle_cache) still work: the shim delegates whenever
        global Popen is not the real one;
      * every other user of subprocess (StubRcd helpers, node runners, the
        reaper's `ps` via subprocess.run) is untouched.
    """
    import subprocess as _sp

    from fused_render.shell.mounts import rcd as _rcd

    real_popen = _sp.Popen
    real_module = _rcd.subprocess

    class _NoRealSpawn:
        def __getattr__(self, name):
            return getattr(_sp, name)

        @staticmethod
        def Popen(*args, **kwargs):
            popen = _sp.Popen
            if popen is real_popen:
                raise AssertionError(
                    "test attempted to spawn a real rclone rcd daemon "
                    "(patch subprocess.Popen or rclone_bin, or fix the "
                    "leaked background thread this raised in)")
            return popen(*args, **kwargs)

    _rcd.subprocess = _NoRealSpawn()
    try:
        yield
    finally:
        _rcd.subprocess = real_module


@pytest.fixture(scope="session", autouse=True)
def _reap_test_rcd_daemons():
    """Kill any REAL rclone rcd daemon a test spawned, on session teardown.

    The rcd daemon is spawned detached and "outlives the server on purpose"
    (mounts.ensure_rcd) — nothing in the app kills it. A test that drives a real
    spawn (not the StubRcd stand-in) therefore leaks a daemon that survives the
    pytest process, which is exactly how days-old orphaned rcd daemons pile up.

    We wrap mounts.write_rcd_state — the one call every spawn makes to record its
    {port, pid} — to track every (pid, home) recorded during the session, then
    on teardown SIGTERM the ones that are ALL of:
      (a) recorded under a throwaway temp home (never a user's real
          ~/.fused-render daemon — that's the strict provenance guard), AND
      (b) still alive, AND
      (c) provably an rclone rcd (mounts._pid_looks_like_rcd).
    The StubRcd fixture records a FAKE pid (4242); guard (c) means we never
    signal it, nor whatever unrelated process happens to hold a recycled pid."""
    import fused_render.shell.mounts as mounts

    tracked = []  # (pid, home) recorded this session
    original = mounts.write_rcd_state

    def _tracking_write_rcd_state(port, pid, log_path=None, auth=None):
        original(port, pid, log_path, auth)
        try:
            tracked.append((pid, mounts.storage.home_dir()))
        except Exception:
            pass

    mounts.write_rcd_state = _tracking_write_rcd_state
    try:
        yield
    finally:
        mounts.write_rcd_state = original
        tmp_root = os.path.realpath(tempfile.gettempdir())
        for pid, home in tracked:
            try:
                if not os.path.realpath(str(home)).startswith(tmp_root):
                    continue  # provenance guard: only temp-home test daemons
                if mounts._pid_alive(pid) and mounts._pid_looks_like_rcd(pid):
                    os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
