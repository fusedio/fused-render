"""A second, independent holder of a lease/lock file: stands in for another
process in the lease tests by taking an `flock` on a fresh open file
description."""
import fcntl

from fused_render import tasks_store


def hold(path: str):
    """Open `path` (never truncating) and take its lock without waiting.
    Raises OSError if someone else holds it."""
    handle = tasks_store._open_lockfile(path)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


def release(handle) -> None:
    """Drop the lock and close the handle (closing alone also drops it)."""
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()
