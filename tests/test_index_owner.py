"""The low-impact watcher ownership lease (server/index_owner.py).

One watcher per Mac: a non-blocking flock beside the duties lease, an owner
record, and a preempt request the preferred (installed :1777) app writes
when someone else holds the lease. `OwnerLoop.tick()` is driven one step at
a time; a rival process is a second open file description on the same lock
file (tests/_lease_rival.py), which conflicts with ours like a real one.
"""
import json
import os

import pytest

from fused_render import tasks_store
from fused_render.server import index_owner
from fused_render.server.index_owner import (
    HANDOVER_PAUSE_S,
    HOLD_POLL_S,
    LEASE_NAME,
    OWNER_FILE,
    PREEMPT_FILE,
    PREEMPT_STALE_S,
    RETRY_MAX_S,
    RETRY_S,
    OwnerLoop,
)
from tests import _lease_rival

ME = 4242
OTHER = 5151


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-sessions"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    yield d
    index_owner.reset_for_tests()
    tasks_store.reset_leases_for_tests()


class Calls:
    def __init__(self):
        self.log = []

    def acquire(self):
        self.log.append("acquire")

    def release(self):
        self.log.append("release")


def _loop(calls, *, preferred, alive=(OTHER,), pid=ME):
    return OwnerLoop(calls.acquire, calls.release, preferred=preferred,
                     now=lambda: 1000.0, sleep=lambda s: None,
                     pid_alive=lambda p: p in alive or p == pid, pid=pid)


def _read(state_dir, name):
    with open(os.path.join(state_dir, name)) as f:
        return json.load(f)


def _write(state_dir, name, data):
    with open(os.path.join(state_dir, name), "w") as f:
        json.dump(data, f)


def test_free_lease_is_acquired_and_recorded(state_dir):
    c = Calls()
    loop = _loop(c, preferred=False)
    assert loop.tick() == HOLD_POLL_S
    assert loop.owned and c.log == ["acquire"]
    assert LEASE_NAME in tasks_store._lease_handles
    assert _read(state_dir, OWNER_FILE) == {"pid": ME, "preferred": False,
                                            "since": 1000.0}
    # Holding: nothing more happens on later polls.
    assert loop.tick() == HOLD_POLL_S
    assert c.log == ["acquire"]


def test_preferred_writes_a_preempt_request_while_a_rival_holds(state_dir):
    rival = _lease_rival.hold(os.path.join(state_dir, LEASE_NAME))
    try:
        c = Calls()
        loop = _loop(c, preferred=True)
        assert loop.tick() == RETRY_S
        assert not loop.owned and c.log == []
        assert _read(state_dir, PREEMPT_FILE) == {"pid": ME, "since": 1000.0}
        # Backoff doubles up to the cap.
        delays = [loop.tick() for _ in range(4)]
        assert delays == [2 * RETRY_S, 4 * RETRY_S, RETRY_MAX_S, RETRY_MAX_S]
    finally:
        _lease_rival.release(rival)
    # Once free, the preferred process wins and withdraws its request.
    assert loop.tick() == HOLD_POLL_S
    assert loop.owned and c.log == ["acquire"]
    assert not os.path.exists(os.path.join(state_dir, PREEMPT_FILE))


def test_non_preferred_does_not_write_a_preempt_request(state_dir):
    rival = _lease_rival.hold(os.path.join(state_dir, LEASE_NAME))
    try:
        loop = _loop(Calls(), preferred=False)
        loop.tick()
        assert not os.path.exists(os.path.join(state_dir, PREEMPT_FILE))
    finally:
        _lease_rival.release(rival)


def test_non_preferred_holder_hands_over_to_a_live_preempt_request(state_dir):
    c = Calls()
    loop = _loop(c, preferred=False)
    loop.tick()
    assert loop.owned
    _write(state_dir, PREEMPT_FILE, {"pid": OTHER, "since": 990.0})
    assert loop.tick() == HANDOVER_PAUSE_S
    assert c.log == ["acquire", "release"]
    assert not loop.owned
    assert LEASE_NAME not in tasks_store._lease_handles
    assert not os.path.exists(os.path.join(state_dir, OWNER_FILE))
    # The rival can now take it...
    rival = _lease_rival.hold(os.path.join(state_dir, LEASE_NAME))
    _lease_rival.release(rival)
    # ...and we do not race back for it while the request stands, even with
    # the lease free.
    loop.tick()
    assert not loop.owned and c.log == ["acquire", "release"]


def test_a_preempt_request_from_a_dead_pid_is_ignored_and_cleaned(state_dir):
    c = Calls()
    loop = _loop(c, preferred=False, alive=())
    loop.tick()
    _write(state_dir, PREEMPT_FILE, {"pid": OTHER, "since": 990.0})
    assert loop.tick() == HOLD_POLL_S
    assert loop.owned and c.log == ["acquire"]
    assert not os.path.exists(os.path.join(state_dir, PREEMPT_FILE))


def test_an_old_request_from_a_live_pid_is_ignored(state_dir):
    """A live pid that stopped refreshing its request (a wedged preferred
    app) cannot park the lease forever."""
    c = Calls()
    loop = _loop(c, preferred=False)  # OTHER is alive
    _write(state_dir, PREEMPT_FILE,
           {"pid": OTHER, "since": 1000.0 - PREEMPT_STALE_S - 1})
    assert loop.tick() == HOLD_POLL_S and loop.owned
    assert loop.tick() == HOLD_POLL_S and c.log == ["acquire"]


def test_the_preferred_waiter_refreshes_its_request_every_tick(state_dir):
    rival = _lease_rival.hold(os.path.join(state_dir, LEASE_NAME))
    try:
        t = [1000.0]
        loop = OwnerLoop(lambda: None, lambda: None, preferred=True,
                         now=lambda: t[0], sleep=lambda s: None,
                         pid_alive=lambda p: True, pid=ME)
        loop.tick()
        t[0] = 1050.0
        loop.tick()
        assert _read(state_dir, PREEMPT_FILE) == {"pid": ME, "since": 1050.0}
    finally:
        _lease_rival.release(rival)


def test_stop_during_a_blocking_on_acquire_does_not_deadlock(state_dir):
    """Callbacks run outside the lock: `stop()` from another thread returns
    once the callback finishes, and the lease is given back."""
    import threading

    entered, unblock = threading.Event(), threading.Event()

    def slow_acquire():
        entered.set()
        unblock.wait(5)

    loop = OwnerLoop(slow_acquire, lambda: None, preferred=False,
                     sleep=lambda s: None, pid_alive=lambda p: True, pid=ME)
    ticker = threading.Thread(target=loop.tick)
    ticker.start()
    assert entered.wait(5)
    stopper = threading.Thread(target=loop.stop)
    stopper.start()
    stopper.join(0.2)
    assert stopper.is_alive()  # waits for the in-flight callback...
    assert not loop.owned  # ...but already gave back the lease
    unblock.set()
    stopper.join(5)
    ticker.join(5)
    assert not stopper.is_alive() and not ticker.is_alive()
    assert not loop.owned
    assert LEASE_NAME not in tasks_store._lease_handles
    assert not os.path.exists(os.path.join(state_dir, OWNER_FILE))


def test_a_stale_request_does_not_block_acquiring(state_dir):
    _write(state_dir, PREEMPT_FILE, {"pid": OTHER, "since": 990.0})
    loop = _loop(Calls(), preferred=False, alive=())
    assert loop.tick() == HOLD_POLL_S and loop.owned


def test_a_stale_owner_record_is_overwritten(state_dir):
    _write(state_dir, OWNER_FILE, {"pid": OTHER, "preferred": True, "since": 1})
    loop = _loop(Calls(), preferred=False, alive=())
    loop.tick()
    assert _read(state_dir, OWNER_FILE)["pid"] == ME


def test_preferred_holder_ignores_preempt_requests(state_dir):
    c = Calls()
    loop = _loop(c, preferred=True)
    loop.tick()
    _write(state_dir, PREEMPT_FILE, {"pid": OTHER, "since": 990.0})
    assert loop.tick() == HOLD_POLL_S
    assert loop.owned and c.log == ["acquire"]


def test_stop_releases_the_lease_and_our_files(state_dir):
    c = Calls()
    loop = _loop(c, preferred=False)
    loop.tick()
    loop.stop()
    assert not loop.owned
    assert LEASE_NAME not in tasks_store._lease_handles
    assert not os.path.exists(os.path.join(state_dir, OWNER_FILE))
    assert c.log == ["acquire"]  # stop does not call on_release
    assert loop.tick() == 0.0  # a stopped loop does nothing
    rival = _lease_rival.hold(os.path.join(state_dir, LEASE_NAME))
    _lease_rival.release(rival)


def test_stop_withdraws_a_pending_preempt_request(state_dir):
    rival = _lease_rival.hold(os.path.join(state_dir, LEASE_NAME))
    try:
        loop = _loop(Calls(), preferred=True)
        loop.tick()
        assert os.path.exists(os.path.join(state_dir, PREEMPT_FILE))
        loop.stop()
        assert not os.path.exists(os.path.join(state_dir, PREEMPT_FILE))
    finally:
        _lease_rival.release(rival)


def test_a_failing_callback_does_not_kill_the_loop(state_dir):
    def boom():
        raise RuntimeError("x")

    loop = OwnerLoop(boom, boom, preferred=False, sleep=lambda s: None,
                     pid_alive=lambda p: True, pid=ME)
    # The loop survives the raise; a failed on_acquire hands the lease back
    # (see test_a_raising_on_acquire_gives_the_lease_back) so a later tick
    # can retry instead of holding a lease that watches nothing.
    assert loop.tick() == HOLD_POLL_S and not loop.owned
    assert loop.tick() > 0  # still ticking


def test_ensure_owner_waiter_is_idempotent_and_stop_waiter_releases(
        state_dir, monkeypatch):
    import threading

    monkeypatch.setattr(index_owner, "is_preferred_owner", lambda: False)
    acquired = threading.Event()
    w1 = index_owner.ensure_owner_waiter(acquired.set, lambda: None)
    w2 = index_owner.ensure_owner_waiter(acquired.set, lambda: None)
    assert w1 is w2
    assert acquired.wait(2)
    assert index_owner.owns()
    index_owner.stop_waiter()
    assert not index_owner.owns() and not index_owner.waiter_running()
    assert LEASE_NAME not in tasks_store._lease_handles
    index_owner.stop_waiter()  # idempotent


def test_is_preferred_owner_is_the_baseline_render_app(monkeypatch):
    from fused_render import _branch, _flavor

    monkeypatch.setattr(_branch, "branch_ref", lambda ref=None: "")
    monkeypatch.setattr(_flavor, "is_bot", lambda: False)
    assert index_owner.is_preferred_owner()
    monkeypatch.setattr(_branch, "branch_ref", lambda ref=None: "feature-x")
    assert not index_owner.is_preferred_owner()
    monkeypatch.setattr(_branch, "branch_ref", lambda ref=None: "")
    monkeypatch.setattr(_flavor, "is_bot", lambda: True)
    assert not index_owner.is_preferred_owner()


# ------------------------------------------------------------ flag toggle

class FakeWatch:
    def __init__(self, armed=True):
        self.armed = armed
        self.log = []

    def is_armed(self):
        return self.armed

    def start(self):
        self.armed = True
        self.log.append("start")

    def stop(self):
        self.armed = False
        self.log.append("stop")


@pytest.fixture()
def fake_watch(monkeypatch):
    from fused_render.server import index_watch

    fw = FakeWatch()
    for name in ("is_armed", "start", "stop"):
        monkeypatch.setattr(index_watch, name, getattr(fw, name))
    started = []

    def fake_ensure(on_acquire, on_release):
        started.append((on_acquire, on_release))
        monkeypatch.setattr(index_owner, "waiter_running", lambda: True)

    monkeypatch.setattr(index_owner, "ensure_owner_waiter", fake_ensure)
    fw.started = started
    return fw


def test_flag_on_stops_the_free_watcher_and_starts_the_waiter(fake_watch):
    index_owner.on_flag_changed(True)
    assert fake_watch.log == ["stop"]
    assert len(fake_watch.started) == 1
    index_owner.on_flag_changed(True)  # idempotent
    assert fake_watch.log == ["stop"] and len(fake_watch.started) == 1


def test_flag_on_does_nothing_without_a_running_watcher(fake_watch):
    fake_watch.armed = False
    index_owner.on_flag_changed(True)
    assert fake_watch.log == [] and fake_watch.started == []


def test_flag_off_stops_the_waiter_and_restarts_the_watcher(
        fake_watch, monkeypatch):
    stopped = []
    monkeypatch.setattr(index_owner, "waiter_running", lambda: True)
    monkeypatch.setattr(index_owner, "stop_waiter", lambda: stopped.append(1))
    index_owner.on_flag_changed(False)
    assert stopped == [1]
    assert fake_watch.log == ["stop", "start"]


def test_flag_off_without_a_waiter_is_a_noop(fake_watch):
    index_owner.on_flag_changed(False)
    assert fake_watch.log == []


def test_the_watcher_lease_stays_machine_wide_on_a_branch(state_dir, monkeypatch):
    """main nests the scheduler's duties lease per branch ref (#1697); the
    watcher lease must NOT follow, or every dev server would own its own
    watcher and the whole 'one watcher per Mac' point is lost."""
    from fused_render import _branch

    monkeypatch.setattr(_branch, "_CACHED_REF", "agent-202610")
    assert tasks_store.duties_dir() != str(state_dir)  # nesting is in effect
    assert index_owner._default_try_acquire(LEASE_NAME) is True
    try:
        assert os.path.exists(os.path.join(str(state_dir), LEASE_NAME))
        assert not os.path.exists(os.path.join(tasks_store.duties_dir(), LEASE_NAME))
    finally:
        tasks_store.release_lease(LEASE_NAME)


def test_a_failed_on_acquire_gives_the_lease_back(state_dir):
    """CodeRabbit on #1700: index_watch.start() can find no root to watch;
    a holder that watches nothing must not keep the lease for life."""
    calls = Calls()
    loop = OwnerLoop(lambda: False, calls.release, preferred=False,
                     sleep=lambda s: None, pid_alive=lambda p: True, pid=ME)
    loop.tick()
    assert loop.owned is False
    assert not os.path.exists(os.path.join(str(state_dir), index_owner.OWNER_FILE))
    # The lease is free again: a rival can take it.
    rival = _lease_rival.hold(os.path.join(str(state_dir), LEASE_NAME))
    try:
        assert rival is not None
    finally:
        _lease_rival.release(rival)


def test_a_raising_on_acquire_gives_the_lease_back(state_dir):
    def boom():
        raise RuntimeError("no roots")

    loop = OwnerLoop(boom, lambda: None, preferred=False,
                     sleep=lambda s: None, pid_alive=lambda p: True, pid=ME)
    loop.tick()
    assert loop.owned is False
