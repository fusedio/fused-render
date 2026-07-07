"""File logging for the server process.

The packaged .app is launched by Finder, so stderr goes nowhere a user can
reach — an "Internal Server Error" traceback, or a failed right-click "Open
with FusedRender", used to exist for a moment in a hidden stream and then
vanish. `setup_logging()` gives every entry point (CLI and menu-bar app) a
rotating log file in a stable, user-findable location, so "zip me that file"
is a complete bug report.

Not structured logging (still future work, see DECISIONS.md backlog) — just
tracebacks and boot context for remote debugging of DMG installs.
"""
import logging
import logging.handlers
import os
import platform
import sys
import tempfile

# FUSED_RENDER_LOG_DIR relocates the log; unset, it lands in the system temp
# directory.
LOG_DIR_ENV = "FUSED_RENDER_LOG_DIR"
LOG_NAME_PREFIX = "fused-render"


def log_dir() -> str:
    """Directory holding the log file: the system temp dir, or
    FUSED_RENDER_LOG_DIR if set.

    Temp by default because the log is disposable diagnostic output with no
    long-term retention policy: rotation bounds a single file's size, but
    nothing prunes the directory, so a permanent home (a user dotdir, app
    support) would silently accumulate stale logs forever. Temp storage is
    reclaimed by the OS and cleared on reboot — which also gives a fresh log
    per session, exactly the scope you want when diagnosing what went wrong
    this run. Point FUSED_RENDER_LOG_DIR at a persistent directory to keep
    logs across reboots.
    """
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return os.path.expanduser(override)
    return tempfile.gettempdir()


def log_path() -> str:
    """Path to THIS process's log file (`fused-render-<pid>.log`).

    Per-process, not a single fixed name, so concurrent instances (e.g. two
    `fused-render` on different ports) can't collide: a shared name would be
    safe for *sequential* launches — RotatingFileHandler opens append, so it
    never truncates — but two live writers would interleave their lines, and
    size rotation is not multi-process safe (when one process renames the file
    and opens a fresh one, the other's open fd keeps writing to the renamed
    file and the next rotation clobbers it). A per-pid file sidesteps both. The
    writer always knows its own path via getpid(), so `Open logs` and the CLI's
    startup print still point at the right file; `ls -t` orders sessions.
    """
    return os.path.join(log_dir(), f"{LOG_NAME_PREFIX}-{os.getpid()}.log")


def setup_logging() -> str:
    """Attach a rotating file handler to the root logger; return the log path.

    Root logger (not a package logger) on purpose: uvicorn's error logs and any
    library warnings propagate there too, so the file captures everything the
    process would have said on a visible stderr. Rotation keeps the worst case
    at ~4 MB — this must never become the disk-filling bug it exists to
    diagnose. Idempotent: a second call (a CLI restart in tests, or both entry
    points colliding) won't stack duplicate handlers.
    """
    path = log_path()
    os.makedirs(log_dir(), exist_ok=True)

    root = logging.getLogger()
    for h in root.handlers:
        if (
            isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(h, "baseFilename", None) == os.path.abspath(path)
        ):
            return path

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    _log_boot_context()
    return path


def _log_boot_context() -> None:
    """One block of environment facts per boot — the questions asked first when
    debugging a broken install, answered before anyone has to ask."""
    log = logging.getLogger("fused_render")
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            fr_version = version("fused-render")
        except PackageNotFoundError:
            fr_version = "not installed"

        log.info(
            "boot: fused-render=%s python=%s (%s) platform=%s sys.prefix=%s",
            fr_version,
            platform.python_version(),
            sys.executable,
            platform.platform(),
            sys.prefix,
        )
    except Exception:
        # Boot context is best-effort; never let it block startup.
        log.exception("boot: failed to collect environment info")
