"""Platform seams: advisory file locks, process tree kill and child-detach
flags. Stdlib only, and importing never raises on any platform.

Not a package: the server reaches it through `fused_render._platform`; the
template backends and `claude_agent/agent.py` (which never import
`fused_render`) load it by path from `templates/shared`."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any

IS_WINDOWS = sys.platform == "win32"

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

_WIN_LOCK_POLL_S = 0.05


def _fileno(fh: Any) -> int:
    return fh if isinstance(fh, int) else fh.fileno()


def flock(fh: Any, exclusive: bool = True, blocking: bool = True) -> bool:
    """Take an advisory lock on an open file or raw fd. True once held; False
    when `blocking=False` and another open file description holds it.

    POSIX uses `flock(2)`. Windows locks byte 0 with `msvcrt.locking`, which is
    exclusive-only, so `exclusive` is honoured on POSIX alone. The lock drops
    when the handle closes (or on `funlock`)."""
    fd = _fileno(fh)
    if fcntl is not None:
        op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            op |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, op)
        except BlockingIOError:
            return False
        return True
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
        # LK_LOCK gives up after ten tries, so a blocking wait polls LK_NBLCK.
        time.sleep(_WIN_LOCK_POLL_S)


def funlock(fh: Any) -> None:
    """Release a lock taken by `flock`."""
    fd = _fileno(fh)
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


#: `signal.SIGKILL` where it exists; Windows has none, and `kill_tree` there
#: ends the tree outright whatever signal is named.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def kill_tree(pid: int, sig: int | None = None) -> bool:
    """Signal `pid` and what it started (default SIGKILL). True once a signal
    was delivered, False when the process is gone or not ours to signal.

    POSIX signals the whole process group, but only when `pid` leads its own
    (a `start_new_session` child): a pid read from a file may be stale or
    recycled into the caller's own group, and `killpg` on that would take the
    caller down. Any other pid gets a plain single-pid signal. Windows runs
    `taskkill /T /F`, falling back to `os.kill`."""
    if sig is None:
        sig = SIGKILL
    if not IS_WINDOWS:
        try:
            leader = os.getpgid(pid) == pid
        except OSError:
            leader = False
        try:
            os.killpg(pid, sig) if leader else os.kill(pid, sig)
        except OSError:
            return False
        return True
    try:
        done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                              capture_output=True, timeout=15)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True


def detach_kwargs() -> dict[str, Any]:
    """`Popen` kwargs that put the child in its own session / process group, so
    `kill_tree` reaches it and its descendants without touching the parent."""
    if not IS_WINDOWS:
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def probe_pid(pid: int) -> None:
    """The liveness probe `os.kill(pid, 0)` is on POSIX: returns when the
    process exists (even one owned by someone else, as `PermissionError`
    allows), raises `ProcessLookupError` when it is gone. On Windows signal 0
    is `CTRL_C_EVENT`, not a probe, so the process is opened and its exit code
    read instead."""
    if not IS_WINDOWS:
        os.kill(pid, 0)
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if kernel32.GetLastError() == 5:  # ERROR_ACCESS_DENIED: exists, not ours
            return
        raise ProcessLookupError(pid)
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
            raise ProcessLookupError(pid)
    finally:
        kernel32.CloseHandle(handle)


def euid() -> int:
    """The effective uid, or 0 where the platform has none (Windows): callers
    use it to give each user a private directory name under the shared temp
    dir, and Windows' temp dir is already per-user."""
    return os.geteuid() if hasattr(os, "geteuid") else 0


def venv_python(venv_dir: str) -> str:
    """Where a venv keeps its own interpreter: `Scripts\\python.exe` on Windows,
    `bin/python` elsewhere."""
    if IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def relpath_within(path: str, start: str) -> str:
    """`os.path.relpath(path, start)`, except that a *path* with no relative
    spelling from *start* (a different drive on Windows, where relpath raises)
    comes back as ``..`` followed by the absolute path, so the usual "starts
    with `..`" outside-the-root test covers it."""
    try:
        return os.path.relpath(path, start)
    except ValueError:
        return os.pardir + os.sep + os.path.abspath(path)
