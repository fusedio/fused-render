"""Tray retry-loop stop-signal contract: stop() during backoff must end the
retry loop, or a later retry can show a tray icon after shutdown began."""
import sys
import time
import types

import pytest

from fused_render.supervisor import tray


def test_run_dispatches_by_platform(monkeypatch):
    # _run is a thin per-OS dispatcher: it lazily imports the matching backend
    # module and forwards the same 4 args, verbatim. Pure — stubbing the
    # backend module in sys.modules means no bus and no pystray import here.
    for platform, modname in (
        ("win32", "fused_render.supervisor._win32.tray"),
        ("linux", "fused_render.supervisor._linux.tray"),
    ):
        calls = []
        stub = types.ModuleType(modname)

        def run(port, state, handle, paths, _calls=calls):
            _calls.append((port, state, handle, paths))

        stub.run = run
        monkeypatch.setitem(sys.modules, modname, stub)
        monkeypatch.setattr(sys, "platform", platform)

        state, handle, paths = object(), object(), object()
        tray._run(1777, state, handle, paths)

        assert calls == [(1777, state, handle, paths)]


def test_tray_retry_loop_honors_stop(monkeypatch):
    pytest.importorskip("pystray")
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
