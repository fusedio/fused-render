"""Process helpers shared by template backends.

Not a package: each backend loads this by path (the templates tree is always
staged as one unit, so ../shared/ resolves from any template folder).
"""
import os
import sys


def spawn_python():
    """Interpreter to launch DETACHED background children with. On Windows,
    prefer ``pythonw.exe`` (no console) over ``python.exe`` so a detached worker
    or daemon never flashes a terminal window. ``DETACHED_PROCESS`` already
    suppresses the console; this is belt-and-braces — and note that
    ``CREATE_NO_WINDOW`` must NOT be added to a detached spawn, since the two
    combined fail to launch on Windows. Falls back to ``sys.executable``."""
    exe = sys.executable
    if os.name == "nt" and exe:
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def pid_alive(pid):
    # os.kill(pid, 0) is the POSIX no-op liveness check, but on Windows signal 0
    # aliases CTRL_C_EVENT and doesn't reliably error on a dead pid — check the
    # process's exit code via the Win32 API instead.
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
