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

# Escape hatch honoring the "written to /tmp or something" ask: point the log
# anywhere with FUSED_RENDER_LOG_DIR (e.g. `/tmp`). Unset, the default below is
# the stable per-user dir the app already owns — persistent across reboots and
# co-located with the pid/port files, so it's one folder to hand over, unlike
# /tmp which the OS may sweep out from under a running app.
LOG_DIR_ENV = "FUSED_RENDER_LOG_DIR"
LOG_FILENAME = "fused-render.log"


def log_dir() -> str:
    """Directory holding the log file.

    FUSED_RENDER_LOG_DIR wins if set. Otherwise, macOS: the app-support dir the
    menu-bar app already owns (pid/port files live there — one folder to zip).
    Elsewhere: ~/.fused-render, which the template registry already establishes
    as ours.
    """
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return os.path.expanduser(override)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/fused-render")
    return os.path.expanduser("~/.fused-render")


def log_path() -> str:
    return os.path.join(log_dir(), LOG_FILENAME)


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
