"""A rcd that never comes up must not be left running.

`_ensure_rcd_locked` spawns `rclone rcd` and polls `core/pid` until a deadline.
Until this test existed the Popen handle was DISCARDED, so the timeout path
raised `RuntimeError("rclone rcd did not come up within 10s")` and walked away
from the child it had just started. In dev that child is also `setsid`-detached
(FUSED_RENDER_RCLONE_PERSIST), so nothing ever reaped it.

That leak is not cosmetic. The mount health loop retries automount on a timer,
once per mount record, so a store with a dozen mounts leaks a dozen abandoned
rclone daemons per cycle, indefinitely — measured on a real machine as 14
orphaned `rclone rcd` processes spanning eight days. The resulting process/fd
pressure inside the long-lived server process is what starves OTHER
`subprocess.run` calls in the same process, and every git call site in the app
fails CLOSED and silently when its subprocess cannot be spawned or overruns its
timeout (the git gate, `_is_repo_root`, the check-ignore oracle) — which is how
"the Git side panel is disabled for every repository" presents.

No real rclone is exec'd: rclone_bin, log rotation, the state write and the
`core/pid` probe are all monkeypatched, and the startup deadline is shortened
so the failure path is reached immediately.
"""
import pytest

import fused_render.shell.mounts as mounts_mod


class _FakeChild:
    """Stands in for the rclone Popen handle and records how it was disposed of."""

    def __init__(self):
        self.terminated = 0
        self.killed = 0
        self.waited = 0
        self._alive = True

    def terminate(self):
        self.terminated += 1
        self._alive = False

    def kill(self):
        self.killed += 1
        self._alive = False

    def wait(self, timeout: float | None = None):
        self.waited += 1
        if self._alive:
            # TimeoutExpired wants a real number; a bare `wait()` (no timeout)
            # still has to raise, so 0 stands in for "no budget given".
            raise mounts_mod.subprocess.TimeoutExpired("rclone", timeout or 0)
        return 0

    def poll(self):
        return None if self._alive else 0


@pytest.fixture
def spawn_never_ready(monkeypatch):
    """Force the spawn path, make the daemon never answer, and hand back the child."""
    child = _FakeChild()

    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda *a, **k: None)
    monkeypatch.setattr(mounts_mod, "reap_stale_rcd", lambda: None)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/fake/rclone")
    monkeypatch.setattr(mounts_mod, "_rotate_rcd_log", lambda: "/fake/rcd.log")
    monkeypatch.setattr(mounts_mod, "write_rcd_state", lambda *a, **k: None)

    def never(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mounts_mod, "_rc", never)
    monkeypatch.setattr(mounts_mod.subprocess, "Popen", lambda args, **kw: child)
    # Reach the give-up path without waiting out the real startup budget.
    monkeypatch.setattr(mounts_mod.rcd, "_RCD_STARTUP_TIMEOUT_S", 0.0)
    return child


def test_rcd_that_never_comes_up_is_not_orphaned(spawn_never_ready):
    with pytest.raises(RuntimeError, match="did not come up"):
        mounts_mod._ensure_rcd_locked()
    child = spawn_never_ready
    assert child.terminated == 1, "the abandoned rclone rcd child was never terminated"


def test_rcd_that_ignores_terminate_is_killed(spawn_never_ready):
    child = spawn_never_ready
    # A child that refuses SIGTERM (terminate() leaves it alive) must be SIGKILLed
    # rather than left behind, otherwise the leak survives the fix.
    child.terminate = lambda: (setattr(child, "terminated", child.terminated + 1))
    with pytest.raises(RuntimeError, match="did not come up"):
        mounts_mod._ensure_rcd_locked()
    assert child.terminated == 1
    assert child.killed == 1, "a rcd child that ignored terminate() was left running"


def test_healthy_rcd_child_is_left_alone(monkeypatch):
    """The success path must not touch the daemon it just started."""
    child = _FakeChild()
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda *a, **k: None)
    monkeypatch.setattr(mounts_mod, "reap_stale_rcd", lambda: None)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/fake/rclone")
    monkeypatch.setattr(mounts_mod, "_rotate_rcd_log", lambda: "/fake/rcd.log")
    monkeypatch.setattr(mounts_mod, "write_rcd_state", lambda *a, **k: None)
    monkeypatch.setattr(mounts_mod, "_rc", lambda *a, **k: {"pid": 4321})
    monkeypatch.setattr(mounts_mod.subprocess, "Popen", lambda args, **kw: child)

    assert isinstance(mounts_mod._ensure_rcd_locked(), int)
    assert child.terminated == 0
    assert child.killed == 0
