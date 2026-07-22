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

for _var, _prefix in (
    ("FUSED_RENDER_HOME", "fused-render-tests-"),
    ("FUSED_RENDER_DIR", "fused-render-tests-dir-"),
):
    if _var not in os.environ:
        _tmp = tempfile.mkdtemp(prefix=_prefix)
        os.environ[_var] = _tmp
        atexit.register(shutil.rmtree, _tmp, ignore_errors=True)


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

    def _tracking_write_rcd_state(port, pid, log_path=None):
        original(port, pid, log_path)
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
