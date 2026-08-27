"""Tests for the background GPU/VRAM-detection wiring (SPEC AI-18, D519).

`hw_detect.refresh_hardware()`/`detect_hardware()` had NO caller anywhere in
`fused_render/` or `frontend/` outside their own module and tests (code
review, 2026-08-27) — `hw_detect.cached_hardware()` therefore always answered
`None` in production, which meant `fit._select_pool` always took the
"hardware is None" branch (`runMode: "cpu-only"` on every non-Apple machine,
the VRAM ceiling never applied at all) and `speed._uncalibrated` always fell
back to its per-backend constant. `supervisor.start_hardware_refresh` is the
fix: a background daemon thread, wired from `server/app.py`'s startup event
exactly like `supervisor.start_reaper` already is.

Like the reaper (see `tests/conftest.py::_no_ai_idle_reaper_thread`'s own
docstring for why), no test here asserts the THREAD gets spawned — that
would mean spawning a real `nvidia-smi`/`rocm-smi`/`powershell` subprocess
per test. `_hardware_refresh_tick()` is the loop body split out specifically
so it can be driven directly, with `hw_detect.refresh_hardware` and
`fit.machine_ram_gb` monkeypatched, the same way `reap_idle(now)` is tested
without ever starting `start_reaper`'s thread.
"""
import pytest

from fused_render.ai import fit, hw_detect, supervisor

# Captured at COLLECTION time, before any test's autouse
# `_no_ai_hardware_refresh_thread` fixture (tests/conftest.py) monkeypatches
# `supervisor.start_hardware_refresh` to a no-op for the rest of the suite.
# The two tests below are the one place that needs the REAL implementation —
# everything else in the suite must not run it (see that fixture's own
# docstring for why) — so they call this captured reference rather than
# `supervisor.start_hardware_refresh` by name, which would silently be the
# patched no-op by the time a test body runs.
_real_start_hardware_refresh = supervisor.start_hardware_refresh


def test_a_tick_calls_refresh_hardware_with_this_machines_ram(monkeypatch):
    calls = []
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 32.0)
    monkeypatch.setattr(hw_detect, "refresh_hardware",
                        lambda ram_gb=None: calls.append(ram_gb))
    supervisor._hardware_refresh_tick()
    assert calls == [32.0]


def test_a_tick_survives_ram_being_unreadable(monkeypatch):
    """`fit.machine_ram_gb()` can answer None (an unreadable platform read) —
    the tick must still probe, not skip it: `hw_detect.detect_hardware`
    already handles a missing `ram_gb` (it just cannot answer the
    Apple-unified-pool / unified-APU-override cases), which is a strictly
    better outcome than never probing at all."""
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: None)
    calls = []
    monkeypatch.setattr(hw_detect, "refresh_hardware",
                        lambda ram_gb=None: calls.append(ram_gb))
    supervisor._hardware_refresh_tick()
    assert calls == [None]


def test_start_hardware_refresh_is_idempotent(monkeypatch):
    """A second call while the thread is still alive must not spawn a
    second one — `server/app.py`'s startup hook can fire more than once
    across the test suite's many `create_app` calls in one real (unpatched)
    invocation of this function, and `start_reaper` draws the identical
    module-level-handle guard for the identical reason."""
    started = []

    class _FakeThread:
        def __init__(self, target, name, daemon):
            self.target = target
            self.name = name
            self._alive = True
            started.append(self)

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(supervisor.threading, "Thread", _FakeThread)
    monkeypatch.setattr(supervisor, "_hardware_refresh_thread", None)

    _real_start_hardware_refresh()
    _real_start_hardware_refresh()

    assert len(started) == 1
    assert started[0].name == "ai-hardware-refresh"

    # Cleanup: leave no fake "alive" thread parked in the module global for
    # a later test that happens to import supervisor fresh in-process.
    monkeypatch.setattr(supervisor, "_hardware_refresh_thread", None)


def test_start_hardware_refresh_starts_a_new_thread_once_the_old_one_died(monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, target, name, daemon):
            started.append(self)
            self._alive = True

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(supervisor.threading, "Thread", _FakeThread)
    monkeypatch.setattr(supervisor, "_hardware_refresh_thread", None)

    _real_start_hardware_refresh()
    started[0]._alive = False
    _real_start_hardware_refresh()

    assert len(started) == 2

    monkeypatch.setattr(supervisor, "_hardware_refresh_thread", None)
