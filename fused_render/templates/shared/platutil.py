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


def kill_tree(pid: int, sig: int | None = None) -> None:
    """Signal `pid` and its descendants: the whole process group on POSIX
    (default SIGKILL), `taskkill /T /F` on Windows. A process that is already
    gone is not an error."""
    if not IS_WINDOWS:
        try:
            os.killpg(os.getpgid(pid), sig if sig is not None else signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


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
