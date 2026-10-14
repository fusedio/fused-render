"""Process helpers shared by template backends.

Not a package: each backend loads this by path (the templates tree is always
staged as one unit, so ../shared/ resolves from any template folder).
"""
import contextlib
import fcntl
import os
import time


def clean_env():
    """os.environ minus PYTHONHOME/PYTHONPATH, for spawning a venv interpreter:
    a bundle-pointing PYTHONPATH/PYTHONHOME (set when the packaged app launched)
    would otherwise leak the app's stdlib/site into the child and shadow the
    venv's own packages. The venv python resolves its stdlib/site from its own
    location (pyvenv.cfg), so dropping these is safe."""
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}


def _take(fh):
    """Non-blocking exclusive lock on one byte of `fh`; raises OSError if another
    handle holds it. Kernel-owned, so it is released the instant the holder's
    process exits — there is no stale lock to detect or steal."""
    fh.seek(0)
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextlib.contextmanager
def file_lock(path, timeout=600):
    """Cross-process exclusive lock, held for the life of the `with` block and
    released by the OS if this process dies mid-hold. Polls the non-blocking
    kernel lock until it is free, giving up with TimeoutError after `timeout`s —
    a replacement for mtime-based "steal a stale lock" schemes, which race a
    still-running holder."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _take(fh)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring {path} after {timeout}s")
                time.sleep(0.1)
        yield
    finally:
        fh.close()   # closing the handle drops the kernel lock


def pid_alive(pid):
    # os.kill(pid, 0) is the no-op liveness check.
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
