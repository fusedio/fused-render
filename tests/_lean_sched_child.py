"""Child-process entry points for tests/test_lean_scheduling.py.

Run as `python tests/_lean_sched_child.py <command> <args...>`. Kept as a file
(not `python -c`) so no payload ever rides on argv. Every command reads its
home from FUSED_RENDER_HOME, set by the parent."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def lockrmw(lock_path, counter, n):
    from fused_render import tasks_store
    for _ in range(int(n)):
        with tasks_store.locked_path(lock_path):
            with open(counter) as f:
                v = int(f.read() or 0)
            time.sleep(0.002)  # widen the lost-update window
            with open(counter, "w") as f:
                f.write(str(v + 1))


def _no_wake():
    """Never install a real wake stub from a test child."""
    from fused_render import schedule_wake
    schedule_wake.sync = lambda due: False


def sched_hold_watch(entry_id):
    """Stamp this process as the live watcher of `entry_id`, then stay alive
    until killed — what a lean process mid-turn looks like to the leader."""
    _no_wake()
    from fused_render import schedule
    schedule._update(entry_id, **schedule._watcher_stamp())
    print("ready", flush=True)
    time.sleep(600)


def sched_create(target, n):
    """Create n far-future entries; print their ids, one per line, to stdout."""
    _no_wake()
    from fused_render import schedule
    for i in range(int(n)):
        entry = schedule.create(target, f"created {i}", "2099-01-01T00:00:00+00:00")
        print(entry["id"], flush=True)


def sched_cancel(ids_file):
    _no_wake()
    from fused_render import schedule
    for entry_id in open(ids_file).read().split():
        assert schedule.cancel(entry_id), entry_id


COMMANDS = {"lockrmw": lockrmw, "sched_create": sched_create,
            "sched_cancel": sched_cancel,
            "sched_hold_watch": sched_hold_watch}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]](*sys.argv[2:])
