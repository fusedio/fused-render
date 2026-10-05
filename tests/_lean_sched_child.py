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


COMMANDS = {"lockrmw": lockrmw}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]](*sys.argv[2:])
