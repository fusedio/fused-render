"""A second, independent holder of a lease/lock file: stands in for another
process in the lease tests, on either platform.

POSIX takes an `flock` on a fresh open file description; Windows takes the
same one-byte `msvcrt.locking` byte lock `tasks_store` itself uses (a byte
lock is per handle there, so a second handle in this process conflicts just
as a real rival process would). Tests use this instead of importing `fcntl`,
which does not exist on Windows."""
import os

from fused_render import tasks_store

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None


def hold(path: str):
    """Open `path` (never truncating) and take its lock without waiting.
    Raises OSError if someone else holds it."""
    handle = tasks_store._open_lockfile(path)
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise
    return handle


def release(handle) -> None:
    """Drop the lock and close the handle (closing alone also drops it)."""
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()
