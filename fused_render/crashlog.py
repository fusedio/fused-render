"""Crash hooks for the server and every long-lived child (SPEC §50, D4).

Nothing in the app used to install `faulthandler`, `sys.excepthook` or
`threading.excepthook`. A native crash (duckdb, GDAL, pyarrow, torch, a
pyobjc callback) or an uncaught exception on a thread printed to stderr —
`/dev/null` under a Finder launch — and left only a macOS crash dialog.

`install(kind)` gives a process three things:

* `faulthandler.enable(fd, all_threads=True)` on a dedicated file
  `<log home>/crash/<kind>-<pid>.log`. faulthandler needs a real fd and
  writes raw C-level text, so it gets its own file rather than the rotating
  app log. The file is created empty and `release()` removes it on a clean
  exit, so **a leftover non-empty file holds a native stack, and a leftover
  empty file means the process did not exit cleanly** (killed outright, or
  SIGKILL from memory pressure, which no handler can observe).
* `sys.excepthook` / `threading.excepthook` → the root logger, full
  traceback, so an uncaught exception on any thread lands in the app log.
* `faulthandler.register(SIGTERM, chain=True)`: an external kill leaves the
  stack of every thread before the default action runs.

`describe_exit(returncode)` is the one shape every parent uses to say how a
child ended (D5), modelled on `envinstall._crash_diagnosis`.
"""
from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
import sys
import threading
import traceback

from fused_render.logs import log_dir

logger = logging.getLogger("fused_render.crash")

_STATE: dict = {"path": None, "fh": None, "kind": None}


def crash_dir() -> str:
    return os.path.join(log_dir(), "crash")


def crash_path() -> str | None:
    """This process's crash file, once `install()` ran."""
    return _STATE["path"]


def install(kind: str) -> str | None:
    """Install the hooks; return the crash file path (None if it could not be
    opened — the hooks for Python exceptions are installed regardless)."""
    _STATE["kind"] = kind
    _install_excepthooks(kind)
    path = None
    try:
        os.makedirs(crash_dir(), exist_ok=True)
        path = os.path.join(crash_dir(), f"{kind}-{os.getpid()}.log")
        fh = open(path, "w", encoding="utf-8")  # noqa: SIM115 — must stay open
        faulthandler.enable(fh, all_threads=True)
        if hasattr(signal, "SIGTERM"):
            try:
                faulthandler.register(signal.SIGTERM, file=fh, all_threads=True, chain=True)
            except (RuntimeError, ValueError, AttributeError):
                pass
        _STATE["path"] = path
        _STATE["fh"] = fh
        atexit.register(release)
    except OSError:
        logger.warning("crash log: could not open crash file for %s", kind, exc_info=True)
        path = None
    return path


def release() -> None:
    """Clean-exit hook: drop the crash file if nothing was written to it.

    Idempotent. The macOS app exits through `os._exit`, which skips atexit,
    so `app.quit_teardown` calls this explicitly."""
    fh = _STATE.get("fh")
    path = _STATE.get("path")
    if fh is None or path is None:
        return
    _STATE["fh"] = None
    _STATE["path"] = None
    try:
        faulthandler.disable()
    except Exception:  # noqa: BLE001
        pass
    try:
        fh.close()
    except OSError:
        pass
    try:
        if os.path.getsize(path) == 0:
            os.unlink(path)
    except OSError:
        pass


def _install_excepthooks(kind: str) -> None:
    def sys_hook(exc_type, exc, tb):
        logger.critical("uncaught exception in %s main thread:\n%s", kind,
                        "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            _prev_sys(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        logger.critical("uncaught exception in %s thread %r:\n%s", kind,
                        getattr(args.thread, "name", "?"),
                        "".join(traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback)))
        try:
            _prev_thread(args)
        except Exception:  # noqa: BLE001
            pass

    _prev_sys = sys.excepthook
    _prev_thread = threading.excepthook
    if getattr(_prev_sys, "_fused_crashlog", False):
        return
    sys_hook._fused_crashlog = True  # type: ignore[attr-defined]
    thread_hook._fused_crashlog = True  # type: ignore[attr-defined]
    sys.excepthook = sys_hook
    threading.excepthook = thread_hook


# ---- how a child ended ------------------------------------------------------

_SIGNAL_HINTS = {
    "SIGKILL": "killed outright — on macOS most often memory pressure (jetsam) or a kill -9",
    "SIGSEGV": "crashed in native code (segmentation fault)",
    "SIGBUS": "crashed in native code (bus error)",
    "SIGABRT": "aborted — an assertion or an uncaught C++ exception",
    "SIGTERM": "asked to stop (SIGTERM)",
    "SIGINT": "interrupted (SIGINT)",
    "SIGHUP": "hung up on (SIGHUP) — the session that started it went away",
    "SIGPIPE": "wrote to a closed pipe (SIGPIPE)",
    "SIGXCPU": "exceeded its CPU limit",
    "SIGXFSZ": "exceeded a file size limit",
}


def describe_exit(returncode: int | None) -> str:
    """One sentence for a child's `returncode`: `None` is still running, a
    negative code is the signal that killed it, anything else is an exit
    status. Signal names come from the platform's own table."""
    if returncode is None:
        return "still running"
    if returncode == 0:
        return "exited cleanly (code 0)"
    if returncode < 0:
        num = -returncode
        try:
            name = signal.Signals(num).name
        except ValueError:
            name = f"signal {num}"
        hint = _SIGNAL_HINTS.get(name)
        return f"killed by {name}" + (f": {hint}" if hint else "")
    if returncode > 128 and returncode - 128 in signal.Signals.__members__.values():
        # A shell wrapper reporting a signal as 128+N.
        name = signal.Signals(returncode - 128).name
        return f"exited {returncode} (shell-reported {name})"
    return f"exited with code {returncode}"


def tail(path: str, chars: int = 2000) -> str:
    """The last `chars` characters of a text file, '' when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - chars))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def report_child_exit(kind: str, pid: int | None, returncode: int | None,
                      log_path: str | None = None, *, level: int = logging.WARNING) -> str:
    """Log `"<kind> pid N exited: <description>"` plus the child's stderr tail.
    Returns the description so callers can put it in an error row."""
    desc = describe_exit(returncode)
    extra = tail(log_path) if log_path else ""
    extra = extra.strip()
    if extra:
        logger.log(level, "%s pid %s %s; last stderr:\n%s", kind, pid, desc, extra)
    else:
        logger.log(level, "%s pid %s %s; no stderr captured", kind, pid, desc)
    return desc
