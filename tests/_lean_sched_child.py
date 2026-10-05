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


def lean_serve(start_dir, marker_dir):
    """A lean `fused-render open` process, minus the socket: build the real
    lean app and run its real lifespan (so whatever lean starts at startup
    starts here), with the scheduler body replaced by a marker file so the test
    can see WHO became leader. The lease, the waiter thread and the takeover are
    all real. Never makes a request. Runs until killed."""
    import asyncio

    from fused_render import schedule, schedule_wake

    schedule_wake.sync = lambda due: False

    def fake_start():
        with open(os.path.join(marker_dir, f"leader-{os.getpid()}"), "w") as f:
            f.write("1")

    schedule.start = fake_start

    from fused_render.server.app import create_app

    app = create_app(start_dir=start_dir, lean=True)

    async def main():
        async with app.router.lifespan_context(app):
            print("ready", flush=True)
            await asyncio.sleep(600)

    asyncio.run(main())


def stage_core(go_file):
    """Wait for the starting gun, stage the core templates, and then keep
    checking the staged dir stays complete — a second process wiping the live
    dir mid-swap is exactly what this catches."""
    from fused_render import core_templates

    while not os.path.exists(go_file):
        time.sleep(0.001)
    core = core_templates.ensure_core_templates()
    for _ in range(100):
        assert os.path.isdir(os.path.join(core, "vendor")), "core templates vanished"
        time.sleep(0.01)


COMMANDS = {"stage_core": stage_core, "lean_serve": lean_serve, "lockrmw": lockrmw, "sched_create": sched_create,
            "sched_cancel": sched_cancel,
            "sched_hold_watch": sched_hold_watch}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]](*sys.argv[2:])
