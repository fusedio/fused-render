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
