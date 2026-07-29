"""Redirect the shell home dir + Fused workspace dir to throwaway tmp dirs for
the whole test run.

Importing fused_render.server / .executor now stages the core templates into
home_dir()/.core-templates on import (core_templates.ensure_core_templates).
This runs at collection time — before any fixture — so the redirect must be
set here at conftest import, ahead of the first test-module import, or the
copy would land in the real ~/.fused-render.

FUSED_RENDER_DIR is redirected for the same reason: /api/config reads it (D81)
and the seed tests write into it, so no test may see the real ~/Documents/Fused.

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
                       ("FUSED_RENDER_DIR", "fused-render-tests-dir-")):
    if _var not in os.environ:
        _tmp = tempfile.mkdtemp(prefix=_prefix)
        os.environ[_var] = _tmp
        atexit.register(shutil.rmtree, _tmp, ignore_errors=True)


# The PEP 723 header the warm fixture (and the tests that ask for it) declare.
# `pip` because the dev-env recipe seeds it into this venv already, so uv resolves
# it from cache and the real-backend venv tests stay offline-safe — the assertions
# are about WHICH interpreter ran, never about the package.
WARM_HEADER = '# /// script\n# dependencies = ["pip"]\n# ///\n'


@pytest.fixture(scope="session")
def warm_fused_backend_venv(tmp_path_factory):
    """Build the fused backend's script venv once, serialized across xdist workers.

    Every real-backend test that needs a venv at all declares `WARM_HEADER`, so
    they all want the same venv under ~/.openfused/venvs (a script with NO header
    runs on the app's own interpreter now — PY-17 — and builds nothing, which is
    why the header is what makes this fixture necessary). Creating it is guarded
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

    from fused_render import engine

    if not engine.available():
        return  # the tests that ask for this are skipped anyway

    lock = tmp_path_factory.getbasetemp().parent / "fused-bare-venv.lock"
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
        # One trivial run through our own API (not fused internals) is what
        # creates the venv; after this every worker's run hits the ready marker.
        probe_dir = tmp_path_factory.mktemp("warm-venv")
        probe = probe_dir / "warm.py"
        probe.write_text(WARM_HEADER + "def main():\n    return 1\n")
        out = asyncio.run(engine.run_python(str(probe), {}))
        if not out.get("ok"):
            error = out.get("error") or {}
            pytest.fail(
                "could not build the fused backend's script venv, so the "
                "real-backend tests would race on a half-built one: "
                f"{error.get('type')}: {error.get('message')}\n"
                f"{error.get('traceback', '')}"
            )
    finally:
        # Released as soon as the venv exists — the lock serializes *creation*,
        # not the tests that use it.
        os.close(fd)
        try:
            os.unlink(str(lock))
        except OSError:
            pass
# The template<->app env contract (SPEC PY-15): the server exports these before
# it serves, and `templates/shared/appenv.py` is the only reader. They are set
# with a plain os.environ assignment by design — every child process has to
# inherit them — which means a test that starts a server (or calls
# export_app_env / export_ro_mounts_env directly) leaves them behind for every
# later test in the same worker. That leak is invisible and one-directional: the
# next test's mount detection quietly answers against the previous test's home.
_APPENV_VARS = ("FUSED_RENDER_HOME_DIR", "FUSED_RENDER_MOUNTS_DIR",
                "FUSED_RENDER_RO_MOUNTS", "FUSED_RENDER_ORIGIN")


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
