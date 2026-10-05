"""Cross-process behaviour behind task scheduling in lean `fused-render open`
processes. Concurrency here is REAL (subprocesses), never a faked lock."""
import os
import subprocess
import sys

import pytest

from fused_render import tasks_store

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lean_sched_child.py")


def _spawn(*args, home):
    env = dict(os.environ, FUSED_RENDER_HOME=str(home))
    return subprocess.Popen([sys.executable, CHILD, *map(str, args)], env=env)


def _wait_all(procs, timeout=120):
    for p in procs:
        assert p.wait(timeout=timeout) == 0


# ------------------------------------------------------------------ H7


@pytest.mark.skipif(tasks_store.fcntl is None, reason="needs real flock")
def test_locked_path_serialises_real_processes(tmp_path):
    counter = tmp_path / "counter"
    counter.write_text("0")
    lock = tmp_path / "counter.lock"
    procs = [_spawn("lockrmw", lock, counter, 25, home=tmp_path) for _ in range(4)]
    _wait_all(procs)
    assert counter.read_text() == "100"


class _LockingMsvcrt:
    """The msvcrt surface `locked_path` uses: one byte, held until unlocked."""
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, busy_for=0):
        self.busy = busy_for
        self.log = []

    def locking(self, fd, mode, nbytes):
        self.log.append(mode)
        if mode == self.LK_NBLCK and self.busy > 0:
            self.busy -= 1
            raise OSError("rival holds byte 0")


def test_locked_on_windows_really_locks_retries_and_unlocks(tmp_path, monkeypatch):
    fake = _LockingMsvcrt(busy_for=3)
    monkeypatch.setattr(tasks_store, "fcntl", None)
    monkeypatch.setattr(tasks_store, "msvcrt", fake)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tasks_store.time, "sleep", lambda _s: None)
    with tasks_store.locked("x.json"):
        assert fake.log == [2, 2, 2, 2]  # 3 refusals, then the winning try
    assert fake.log[-1] == 0  # LK_UNLCK on the way out
    # the lock file was stamped, never truncated away
    assert (tmp_path / "x.json.lock").read_bytes() == b"\0"


# ------------------------------------------------------------------ H1


def test_schedule_store_loses_no_edit_across_real_processes(tmp_path):
    """One process creates entries while another cancels earlier ones, in the
    same store at the same moment. With only an in-process lock, whichever
    wrote last carried the other's stale snapshot: a created entry vanished or
    a cancel was undone (and the cancelled message would then fire)."""
    import json

    from fused_render import schedule

    target = tmp_path / "proj"
    target.mkdir()
    env = dict(os.environ, FUSED_RENDER_HOME=str(tmp_path))
    ids = []
    out = subprocess.run(
        [sys.executable, CHILD, "sched_create", str(target), "30"], env=env,
        capture_output=True, text=True, check=True, timeout=120)
    ids = out.stdout.split()
    assert len(ids) == 30
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("\n".join(ids))

    creator = subprocess.Popen(
        [sys.executable, CHILD, "sched_create", str(target), "30"], env=env,
        stdout=subprocess.PIPE, text=True)
    canceller = _spawn("sched_cancel", ids_file, home=tmp_path)
    new_ids = creator.communicate(timeout=120)[0].split()
    assert creator.returncode == 0
    _wait_all([canceller])

    stored = {e["id"]: e for e in
              json.load(open(os.path.join(str(tmp_path), schedule._STORE_NAME)))["entries"]}
    assert len(new_ids) == 30
    missing = [i for i in new_ids if i not in stored]
    assert not missing, f"created entries lost: {missing}"
    undone = [i for i in ids if stored.get(i, {}).get("state") != schedule.CANCELLED]
    assert not undone, f"cancels undone: {undone}"


# ------------------------------------------------------------------ H2


@pytest.fixture()
def sched_home(tmp_path, monkeypatch):
    """A private home for the in-process half, no real wake stub."""
    from fused_render import schedule, schedule_wake

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    monkeypatch.setattr(schedule_wake, "sync", lambda due: False)
    schedule._watched.clear()
    yield tmp_path
    schedule._watched.clear()


def _sent_entry(entry_id="E1", run_id="r-gone"):
    from fused_render import schedule

    schedule._write([{
        "id": entry_id, "state": schedule.SENT, "turn": "", "run_id": run_id,
        "target": "/tmp", "message": "m", "due": "2020-01-01T00:00:00+00:00",
        "session_id": "", "error": ""}])


def _entry(entry_id="E1"):
    from fused_render import schedule

    return next(e for e in schedule._read() if e["id"] == entry_id)


def _hold_watch(entry_id, home):
    child = subprocess.Popen(
        [sys.executable, CHILD, "sched_hold_watch", entry_id],
        env=dict(os.environ, FUSED_RENDER_HOME=str(home)),
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    return child


def test_sweep_leaves_a_turn_another_live_process_is_watching(sched_home):
    """The leader is not always the process that sent the message. Another
    process's watch is real even though THIS process's `_watched` is empty."""
    from fused_render import schedule

    _sent_entry()
    child = _hold_watch("E1", sched_home)
    try:
        schedule._claim_due(schedule._now())
        stored = _entry()
        assert stored["turn"] == "", "a turn a live process watches was closed"
        assert not stored["error"]
    finally:
        child.kill()
        child.wait()


def test_sweep_closes_a_turn_once_its_watcher_process_is_dead(sched_home):
    from fused_render import schedule

    _sent_entry()
    child = _hold_watch("E1", sched_home)
    child.kill()
    child.wait()
    schedule._claim_due(schedule._now())
    stored = _entry()
    assert stored["turn"] == "unknown"
    assert "interrupted" in stored["error"]


def test_sweep_closes_a_turn_whose_watcher_heartbeat_went_stale(sched_home):
    """A live pid proves nothing alone (pid reuse); the heartbeat decides."""
    from fused_render import schedule
    from datetime import timedelta

    _sent_entry()
    child = _hold_watch("E1", sched_home)
    try:
        old = (schedule._now() - timedelta(seconds=schedule._WATCH_STALE_S + 60)).isoformat()
        schedule._update("E1", watcher_at=old)
        schedule._claim_due(schedule._now())
        assert _entry()["turn"] == "unknown"
    finally:
        child.kill()
        child.wait()


def test_sweep_adopts_the_watch_of_a_run_that_outlived_its_leader(sched_home, monkeypatch):
    """The previous leader died; its detached Claude child did not. The new
    leader follows the run to its real verdict rather than calling it
    interrupted."""
    from fused_render import schedule

    _sent_entry(run_id="r-live")
    adopted = []
    monkeypatch.setattr(schedule, "_followable", lambda run_id: True)
    monkeypatch.setattr(schedule, "_watch_turn",
                        lambda entry, run_id: adopted.append(run_id))
    schedule._claim_due(schedule._now())
    for _ in range(100):
        if adopted:
            break
        import time
        time.sleep(0.02)
    assert adopted == ["r-live"]
    stored = _entry()
    assert stored["turn"] == "" and not stored["error"]
    assert stored["watcher_pid"] == os.getpid()


def test_a_watch_refreshes_its_heartbeat_on_the_stored_entry(sched_home, monkeypatch):
    from fused_render import schedule

    _sent_entry()
    schedule._update("E1", watcher_pid=os.getpid(), watcher_at="2020-01-01T00:00:00+00:00")
    schedule._heartbeat("E1")
    first = _entry()["watcher_at"]
    assert first > "2020-01-01"
    schedule._update("E1", watcher_at="2020-01-01T00:00:00+00:00")
    schedule._heartbeat("E1")  # inside the beat window: no write
    assert _entry()["watcher_at"] == "2020-01-01T00:00:00+00:00"
    schedule._beats.clear()
    schedule._heartbeat("E1")
    assert _entry()["watcher_at"] > "2020-01-01T00:00:01"
