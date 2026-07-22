"""Tray retry-loop stop-signal contract: stop() during backoff must end the
retry loop, or a later retry can show a tray icon after shutdown began."""
import time

import pytest

pytest.importorskip("pystray")

from fused_render.supervisor import tray


def test_tray_retry_loop_honors_stop(monkeypatch):
    # Bugbot: stop() during the retry backoff was a no-op (no icon yet) and
    # the next retry showed a tray icon after shutdown had already started.
    calls = []

    def failing_run(port, state, handle, paths):
        calls.append(port)
        raise RuntimeError("explorer not ready")

    monkeypatch.setattr(tray, "_run", failing_run)
    monkeypatch.setattr(tray, "_RETRY_START", 30.0)  # park the loop in backoff

    class _Paths:
        def log(self, message):
            pass

    handle = tray.start(1777, False, _Paths())
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls  # first attempt failed, loop is now in its 30s backoff

    handle.stop()  # must wake the backoff and end the loop
    time.sleep(0.5)
    assert len(calls) == 1  # no post-stop retry, so no ghost icon
