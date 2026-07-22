"""Platform-neutral supervisor loop contract: tray actions dispatched via
`_safe_call` must run OFF the main loop, never on the thread that services
pipe_requests.

This imports `fused_render.supervisor.core` directly (no win32 gate): the
`_spawn_call`/`_safe_call` helpers under test are platform-neutral, so on Linux
CI — where a supervisor backend exists — core imports cleanly and these tests
run. Where no backend exists for the running OS (e.g. darwin) the import raises
at module load; we skip rather than ERROR at collection, matching the sibling
convention in tests/test_supervisor_linux_instance.py.
"""
import queue
import threading

import pytest

try:
    from fused_render.supervisor import core
except Exception:  # noqa: BLE001 - no supervisor backend on this OS (e.g. darwin)
    core = None

pytestmark = pytest.mark.skipif(core is None, reason="no supervisor backend on this OS")


class _Paths:
    """Minimal stand-in for DesktopPaths: `_safe_call` only ever touches
    `.log(msg)`."""

    def __init__(self):
        self.messages = []

    def log(self, message):
        self.messages.append(message)


def test_spawn_call_never_blocks_the_loop_thread():
    # Bugbot: OPEN_LOGS / DEFAULT_APPS called `_safe_call` DIRECTLY on the loop
    # thread. On Linux, `ui.open_path` -> `_xdg_open` now waits up to 5s on the
    # child, so a slow/foreground xdg-open blocked the loop that services
    # pipe_requests — a concurrent ShutdownForUpgrade could then time out inside
    # the IPC server's 20s window. `_spawn_call` must return immediately and run
    # the action off-loop, exactly like `_spawn_exit_confirm`.
    started = threading.Event()
    release = threading.Event()
    ran = queue.Queue()

    def blocking_action():
        started.set()
        release.wait(5)
        ran.put(True)

    paths = _Paths()
    core._spawn_call(paths, blocking_action)  # must return without blocking

    assert started.wait(5)  # the action began on its own thread
    assert ran.empty()      # ...but the loop thread is free while it blocks

    release.set()
    assert ran.get(timeout=5) is True
