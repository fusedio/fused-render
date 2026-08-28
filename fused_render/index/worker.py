"""Detached scan worker entrypoint: `python -m fused_render.index.worker <run_dir>`.

A module rather than a script path (OpenIndex spawned `Popen([python,
__file__, "--worker", run_dir])`) because a py2app bundle has no source file to
point at — but it does have the package on `sys.path`.

Everything this process needs is in `<run_dir>/spec.json`, written by
`runner.start`: the root, the full/incremental flag, and the IndexConfig. It
re-derives nothing from the environment, so a home that moved between the
request and the spawn cannot make the worker compact into a directory nobody
reads.
"""
import ctypes
import os
import platform
import sys

from fused_render.index.scan import run_scan

# How far below the server this process (and every pool child and stat thread
# it goes on to spawn, since niceness is inherited) runs. The invariant it
# buys: an interactive `/api/index/rank` keystroke wins the scheduler against
# a scan, which otherwise puts up to ten processes x 16 stat threads plus an
# all-cores DuckDB compaction against the one thread the user is waiting on.
SCAN_NICE_INCREMENT = 10

# `<sys/resource.h>` constants for `setiopolicy_np(3)` (darwin). `nice()`
# only lowers CPU scheduling priority; nothing about it touches the disk I/O
# queue, so a scan's stat pool and compaction reads/writes could still queue
# ahead of an interactive `/api/index/rank`'s `read_parquet` at the disk
# layer even while niced. These three throttle the *process's* disk I/O
# scheduling class down to "background", the same class Spotlight/Time
# Machine use, so the kernel serves interactive I/O first.
IOPOL_TYPE_DISK = 0      # IOPOL_TYPE_DISK
IOPOL_SCOPE_PROCESS = 0  # IOPOL_SCOPE_PROCESS
IOPOL_THROTTLE = 3       # IOPOL_THROTTLE

# `ioprio_set(2)` constants (linux). No libc wrapper exists for this one —
# it has to go through the raw syscall table, and the number is arch-specific
# (there is no stable cross-arch symbol like the darwin/win32 calls have).
# The kernel only honors priority under I/O schedulers that implement
# classes (BFQ); under mq-deadline/none this is a harmless no-op, which is
# fine — best-effort by design, same as the darwin and win32 branches.
IOPRIO_WHO_PROCESS = 1
IOPRIO_CLASS_IDLE = 3
IOPRIO_CLASS_SHIFT = 13
_IOPRIO_SET_SYSCALL_NR = {"x86_64": 251, "aarch64": 30}

# `SetPriorityClass` constant (win32). `os.nice` does not exist on Windows at
# all, so this is Windows' FIRST scan mitigation, not just its I/O half:
# PROCESS_MODE_BACKGROUND_BEGIN lowers CPU, I/O, *and* memory priority
# together in one call.
PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000


def _renice_self() -> None:
    """Nice this process down, in the child. Never a `preexec_fn` at the
    spawn: that forces CPython off posix_spawn onto fork(), which is the
    SIGSEGV this file's other comment is about."""
    try:
        os.nice(SCAN_NICE_INCREMENT)
    except (AttributeError, OSError):
        pass  # no nice (Windows) or not permitted — scan at full priority


def _set_background_io_policy() -> bool:
    """Throttle this process's disk I/O scheduling class, in the child, same
    reasoning and same never-a-preexec_fn discipline as `_renice_self`.

    Per-platform, best-effort: this must never take a scan down. A missing
    symbol, an unrecognized arch, a nonzero return, or any other failure is
    swallowed and reported as `False`. Any platform not matched below is a
    silent no-op.
    """
    try:
        if sys.platform == "darwin":
            lib = ctypes.CDLL(None, use_errno=True)
            rc = lib.setiopolicy_np(IOPOL_TYPE_DISK, IOPOL_SCOPE_PROCESS,
                                    IOPOL_THROTTLE)
            return rc == 0
        if sys.platform.startswith("linux"):
            nr = _IOPRIO_SET_SYSCALL_NR.get(platform.machine())
            if nr is None:
                return False  # unrecognized arch — silent no-op
            lib = ctypes.CDLL(None, use_errno=True)
            prio = (IOPRIO_CLASS_IDLE << IOPRIO_CLASS_SHIFT) | 0
            rc = lib.syscall(nr, IOPRIO_WHO_PROCESS, 0, prio)
            return rc == 0
        if sys.platform == "win32":
            kernel32 = ctypes.windll.kernel32
            rc = kernel32.SetPriorityClass(kernel32.GetCurrentProcess(),
                                           PROCESS_MODE_BACKGROUND_BEGIN)
            return bool(rc)
        return False  # unmatched platform — documented no-op
    except (AttributeError, OSError):
        return False


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m fused_render.index.worker <run_dir>",
              file=sys.stderr)
        return 2
    # Detach from the server's session HERE, in the child, not via
    # `start_new_session=True` at the spawn: that kwarg forces CPython off
    # posix_spawn onto fork()+exec, and a fork of a server that has touched
    # pyproj/rasterio runs PROJ's atfork handler and dies with SIGSEGV before
    # this line is ever reached (see envinstall.py, same discipline). setsid
    # in the child after exec is the safe equivalent — the scan must outlive
    # the request and ignore the server's terminal signals.
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass  # already a session leader (spawned from a shell)
    _renice_self()
    _set_background_io_policy()
    run_scan(argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
