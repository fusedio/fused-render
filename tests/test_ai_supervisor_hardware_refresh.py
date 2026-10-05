"""Tests for the background GPU/VRAM-detection wiring (SPEC AI-18, D519).

`hw_detect.refresh_hardware()`/`detect_hardware()` had NO caller anywhere in
`fused_render/` or `frontend/` outside their own module and tests (code
review, 2026-08-27) — `hw_detect.cached_hardware()` therefore always answered
`None` in production, which meant `fit._select_pool` always took the
"hardware is None" branch (`runMode: "cpu-only"` on every non-Apple machine,
the VRAM ceiling never applied at all) and `speed._uncalibrated` always fell
back to its per-backend constant. `supervisor.start_hardware_refresh` is the
fix: a background daemon thread, started lazily from `hw_detect.cached_
hardware()`'s own cache-miss path (`hw_detect._probe_once_if_missing`) —
every reader, not a dedicated `server/app.py` startup hook, which would be
skipped in `lean` mode and redundant everywhere else once the cache-miss
path covers every caller including `serve`'s own first read. The identical
shape `supervisor._start_resident` already draws for `start_reaper`.

`supervisor._child_env`'s budget computation (`_await_hardware_cache`) is
the one caller that cannot settle for `cached_hardware()`'s own
immediate-`None`-on-a-miss contract: a worker spawn bakes whatever budget
it computes into `FUSED_AI_MEMORY_BUDGET_BYTES` for that worker's entire
life, so it waits briefly and boundedly (`hw_detect._PROBE_WAIT_S`) for
an in-flight probe to land before giving up.

Like the reaper (see `tests/conftest.py::_no_ai_idle_reaper_thread`'s own
docstring for why), no test here asserts the THREAD gets spawned — that
would mean spawning a real `nvidia-smi`/`rocm-smi`/`powershell` subprocess
per test. `_hardware_refresh_tick()` is the loop body split out specifically
so it can be driven directly, with `hw_detect.refresh_hardware` and
`fit.machine_ram_gb` monkeypatched, the same way `reap_idle(now)` is tested
without ever starting `start_reaper`'s thread.
"""
import threading
import time

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
    supervisor._hardware_refresh_starter.reset_for_tests()

    _real_start_hardware_refresh()
    _real_start_hardware_refresh()

    assert len(started) == 1
    assert started[0].name == "ai-hardware-refresh"

    # Cleanup: leave no fake "alive" thread parked in the module global for
    # a later test that happens to import supervisor fresh in-process.
    supervisor._hardware_refresh_starter.reset_for_tests()


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
    supervisor._hardware_refresh_starter.reset_for_tests()

    _real_start_hardware_refresh()
    started[0]._alive = False
    _real_start_hardware_refresh()

    assert len(started) == 2

    supervisor._hardware_refresh_starter.reset_for_tests()


def test_concurrent_first_calls_to_start_hardware_refresh_start_exactly_one_thread(
        monkeypatch):
    """`start_hardware_refresh()` fires its first probe inline, from
    whichever caller wins the race — a burst of concurrent request-path
    callers (every `hw_detect.cached_hardware()` reader hitting a cold
    cache at once — `cached_hardware()` calls this on every miss) can all
    reach it at once on a cold process. Two callers
    racing the `is_alive()` check before either has created a thread must
    not both create and start one, the identical race
    `test_concurrent_first_calls_to_start_reaper_start_exactly_one_thread`
    (`tests/test_ai_runtime.py`) pins for `start_reaper`/`_reaper_starter` —
    this is the same test, mirrored for `start_hardware_refresh`/
    `_hardware_refresh_starter`.

    `_hardware_refresh_tick` is patched to a harmless counter: the real tick
    spawns a subprocess probe immediately, on thread start, not on a delay,
    so leaving it real would spawn real `nvidia-smi`/`rocm-smi`/`powershell`
    processes here. Every spawned thread is joined before returning so
    nothing outlives the test — `run`'s only other work is `time.sleep`,
    which a daemon thread sitting in forever is fine to leave behind, but
    this test does not need to find out, since the tick itself returns
    immediately."""
    supervisor._hardware_refresh_starter.reset_for_tests()
    monkeypatch.setattr(supervisor, "_hardware_refresh_tick", lambda: None)
    real_thread_cls = threading.Thread
    created = []

    class CountingThread(real_thread_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if self.name == "ai-hardware-refresh":
                created.append(self)

    monkeypatch.setattr(supervisor.threading, "Thread", CountingThread)

    n = 8
    barrier = threading.Barrier(n)

    def call_start_hardware_refresh():
        barrier.wait(timeout=5)
        _real_start_hardware_refresh()

    callers = [real_thread_cls(target=call_start_hardware_refresh) for _ in range(n)]
    for t in callers:
        t.start()
    for t in callers:
        t.join(timeout=5)
        assert not t.is_alive(), "a start_hardware_refresh() caller never returned"

    assert len(created) == 1, (
        f"expected exactly one hardware-refresh thread to be created, got {len(created)}")
    refresh_thread = created[0]
    assert supervisor._hardware_refresh_starter._thread is refresh_thread
    try:
        assert refresh_thread.is_alive()
    finally:
        # The real `run` sleeps `_HARDWARE_REFRESH_INTERVAL_S` (6 hours)
        # between ticks and never exits; it's a daemon so there is nothing
        # to join. Reset the starter so later tests see a clean slate,
        # matching how every other test here gets `start_hardware_refresh`
        # no-op'd by the autouse conftest fixture.
        supervisor._hardware_refresh_starter.reset_for_tests()


# -- `_await_hardware_cache`: the bounded spawn-time wait --------------------


def test_await_hardware_cache_returns_immediately_on_a_warm_cache(monkeypatch):
    """A warm cache must cost nothing beyond the one read — no sleep, no
    poll loop — since this sits on the already-slow spawn path and must not
    add latency to the common case (a machine that has been up a while)."""
    sentinel = object()
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: sentinel)

    def _boom(*args, **kwargs):
        raise AssertionError("a warm cache must not sleep at all")

    monkeypatch.setattr(supervisor.time, "sleep", _boom)
    assert supervisor._await_hardware_cache() is sentinel


def test_await_hardware_cache_polls_until_the_probe_lands(monkeypatch):
    """A cache that lands partway through the bound is picked up — this is
    the whole point of waiting rather than reading once and giving up."""
    sentinel = object()
    calls = []

    def _cached_hardware():
        calls.append(1)
        return sentinel if len(calls) >= 3 else None

    monkeypatch.setattr(hw_detect, "cached_hardware", _cached_hardware)
    monkeypatch.setattr(hw_detect, "_PROBE_WAIT_S", 5.0)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)
    assert supervisor._await_hardware_cache() is sentinel
    assert len(calls) == 3


def test_await_hardware_cache_gives_up_after_the_bound(monkeypatch):
    """A probe that never lands (hung, or simply slower than the bound)
    must not hang the spawn path forever — `_await_hardware_cache` answers
    `None`, exactly what an unawaited `cached_hardware()` read would have
    answered, once `hw_detect._PROBE_WAIT_S` has elapsed."""
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: None)
    monkeypatch.setattr(hw_detect, "_PROBE_WAIT_S", 0.05)
    assert supervisor._await_hardware_cache() is None


def test_probe_wait_covers_several_sequential_tool_spawns():
    """`_PROBE_TIMEOUT_S` caps ONE vendor-tool spawn; `detect_hardware` runs
    nvidia, amd, windows and sysctl probes one after another. A spawn-time
    wait of a single per-tool timeout expires while a slow first tool is
    still running, baking a no-GPU budget into the worker for its whole
    life. The wait must span several sequential spawns."""
    assert hw_detect._PROBE_WAIT_S >= 3 * hw_detect._PROBE_TIMEOUT_S


def test_await_hardware_cache_is_bounded_by_the_probe_wait_constant(monkeypatch):
    """The bound is `hw_detect._PROBE_WAIT_S`, read at call time."""
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: None)
    monkeypatch.setattr(hw_detect, "_PROBE_WAIT_S", 0.05)
    deadlines = []
    real_monotonic = supervisor.time.monotonic

    def _recording_monotonic():
        now = real_monotonic()
        deadlines.append(now)
        return now

    monkeypatch.setattr(supervisor.time, "monotonic", _recording_monotonic)
    supervisor._await_hardware_cache()
    # The wait actually ran for roughly the patched bound, not an unrelated
    # fixed amount of time.
    assert deadlines[-1] - deadlines[0] < 1.0


def test_await_hardware_cache_ends_early_once_the_probe_has_finished(monkeypatch):
    """Detection that FAILS (probe exception, no vendor tools) never writes
    the cache. Waiting the full `_PROBE_WAIT_S` for it would stall every
    worker spawn that long; once the first probe has finished the cache will
    not change, so the wait ends at once with the no-GPU answer."""
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: None)
    monkeypatch.setattr(hw_detect, "_PROBE_WAIT_S", 30.0)
    supervisor._hardware_first_probe_done.set()
    try:
        started = time.monotonic()
        assert supervisor._await_hardware_cache() is None
        assert time.monotonic() - started < 2.0
    finally:
        supervisor._hardware_first_probe_done.clear()


def test_a_failing_first_probe_marks_it_finished(monkeypatch):
    """The refresh thread flags the first probe done even when the tick
    raises, so a spawn waiting on it is released."""
    def _boom(*a, **k):
        raise OSError("no vendor tools")

    monkeypatch.setattr(hw_detect, "refresh_hardware", _boom)
    monkeypatch.setattr(supervisor, "_HARDWARE_REFRESH_INTERVAL_S", 3600)
    supervisor._hardware_first_probe_done.clear()
    supervisor._hardware_refresh_starter.reset_for_tests()
    try:
        _real_start_hardware_refresh()
        assert supervisor._hardware_first_probe_done.wait(5.0)
    finally:
        supervisor._hardware_refresh_starter.reset_for_tests()
        supervisor._hardware_first_probe_done.clear()
