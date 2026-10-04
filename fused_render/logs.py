"""File logging for the server process and its helpers.

The packaged .app is launched by Finder, so stderr goes nowhere a user can
reach — an "Internal Server Error" traceback, or a failed right-click "Open
with FusedRender", used to exist for a moment in a hidden stream and then
vanish. `setup_logging()` gives every entry point (CLI and menu-bar app) a
rotating log file in a stable, user-findable location, so the diagnostics
bundle (`fused_render.diagnostics`) is a complete bug report.

THE LOG HOME IS PERSISTENT (SPEC §50). D68 put the log in the system temp
dir because nothing pruned it; the price was that a reboot — the first thing
anyone does after a crash dialog — erased the evidence. `prune_log_home()`
is that missing retention policy (newest `KEEP_SESSIONS` session files, at
most `KEEP_BYTES` in total, swept at every boot), so the home can be a real
directory: `~/Library/Logs/fused-render/` on macOS, where Console.app lists
it, and `<home>/logs/app/` elsewhere. `FUSED_RENDER_LOG_DIR` still overrides.

UVICORN LOGS PROPAGATE HERE. uvicorn's default logging config gives the
`uvicorn` logger its own stderr handler with `propagate: False`, so on a
Finder launch "Exception in ASGI application" tracebacks, bind errors and
lifespan failures were lost. `uvicorn_log_config()` hands every
`uvicorn.Config` a config that routes `uvicorn`/`uvicorn.error` to the root
(this file) and keeps `uvicorn.access` silent — the request middleware in
`server/common.py` already writes one line per request with its duration,
and a second access trail would double the log.

Not structured logging (still future work, SV-3) — text lines, so the grep
and awk recipes in the troubleshooting docs keep working. The formatter
carries pid and thread so lines from the server, the LAN listener and the
sampler threads can be told apart.
"""
import logging
import logging.handlers
import os
import platform
import sys
import tempfile
import time

# FUSED_RENDER_LOG_DIR relocates the log home.
LOG_DIR_ENV = "FUSED_RENDER_LOG_DIR"
LOG_NAME_PREFIX = "fused-render"

#: Per-file rotation. 10 MB × (1 + 2 backups) per session: a visible shell tab
#: writes one health-probe access line every 5 s (~1.5 MB/day), and the
#: diagnostics bundle wants a full 24 h window to survive in one session.
ROTATE_BYTES = 10_000_000
ROTATE_BACKUPS = 2

#: Retention for the whole home, swept at boot by `prune_log_home()`. The
#: byte cap must hold at least two FULL sessions (rotation above is 30 MB per
#: session): after a crash and relaunch, the crashed session's log is the
#: one that matters, and a cap under 2 × 30 MB would evict it at once.
KEEP_SESSIONS = 10
KEEP_BYTES = 150_000_000

FORMAT = "%(asctime)s %(levelname)s [%(process)d %(threadName)s] %(name)s: %(message)s"


def default_log_dir() -> str:
    """Where logs live when `FUSED_RENDER_LOG_DIR` is unset.

    macOS: `~/Library/Logs/fused-render` — the platform's per-user log
    folder, visible in Console.app's Reports sidebar. Elsewhere: an `app/`
    subdirectory of `<home>/logs`, beside (not inside) the call store's
    partitions, so SPEC CL-7's "the two never share a directory" holds on
    every platform. Tests set `FUSED_RENDER_HOME`, which this honours.
    """
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/fused-render")
    home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(home, "logs", "app")


def log_dir() -> str:
    """Directory holding the log files: `FUSED_RENDER_LOG_DIR` if set, else
    `default_log_dir()`."""
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return os.path.expanduser(override)
    return default_log_dir()


def temp_log_dir() -> str:
    """The pre-§50 location (system temp). Read by the diagnostics bundle so
    a machine upgraded mid-investigation still contributes its old logs."""
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
    writer always knows its own path via getpid(), so `Open app logs` and the CLI's
    startup print still point at the right file; `ls -t` orders sessions.
    """
    return os.path.join(log_dir(), f"{LOG_NAME_PREFIX}-{os.getpid()}.log")


def _session_files(directory: str) -> list[tuple[float, int, str]]:
    """(mtime, size, path) for every session log and its rotated backups."""
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        if not name.startswith(LOG_NAME_PREFIX + "-") or ".log" not in name:
            continue
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append((st.st_mtime, st.st_size, path))
    return out


def prune_log_home(directory: str | None = None, *, keep_sessions: int = KEEP_SESSIONS,
                   keep_bytes: int = KEEP_BYTES, live_pid: int | None = None) -> list[str]:
    """Delete old session logs so the home stays bounded. Returns removed paths.

    Groups files by the pid in their name (`fused-render-123.log`,
    `fused-render-123.log.1`, `fused-render-relaunch-123.log` all belong to
    session 123), keeps the newest `keep_sessions` sessions, then keeps
    dropping the oldest remaining session until the total is under
    `keep_bytes`. The live process's own session is never removed.
    Best-effort: an unlink that fails is skipped, never raised.
    """
    directory = directory or log_dir()
    live_pid = os.getpid() if live_pid is None else live_pid
    sessions: dict[str, list[tuple[float, int, str]]] = {}
    for mtime, size, path in _session_files(directory):
        stem = os.path.basename(path).split(".log", 1)[0]
        key = stem.rsplit("-", 1)[-1]
        sessions.setdefault(key, []).append((mtime, size, path))
    # Newest first, so index i is "the i-th most recent session".
    ordered = sorted(sessions.items(),
                     key=lambda kv: max(m for m, _, _ in kv[1]), reverse=True)
    removed: list[str] = []
    total = sum(s for files in sessions.values() for _, s, _ in files)

    def drop(files) -> None:
        nonlocal total
        for _, size, path in files:
            try:
                os.unlink(path)
                removed.append(path)
                total -= size
            except OSError:
                pass

    # Pass 1: the count cap takes the OLDEST sessions beyond keep_sessions.
    survivors = []
    for i, (key, files) in enumerate(ordered):
        if key != str(live_pid) and i >= keep_sessions:
            drop(files)
        else:
            survivors.append((key, files))
    # Pass 2: the byte cap also takes the OLDEST first — never the session
    # that just crashed, which is the newest non-live one and the evidence
    # this prune exists to keep (bugbot, PR #1399). Walk survivors from the
    # tail (oldest) and stop as soon as the total fits.
    for key, files in reversed(survivors):
        if total <= keep_bytes:
            break
        if key == str(live_pid):
            continue
        drop(files)
    removed.extend(_prune_crash_dir(os.path.join(directory, "crash")))
    return removed


#: A crash file (crashlog.py) older than this is no longer evidence anyone
#: will ask for: the diagnostics bundle's window is at most a few days.
CRASH_KEEP_S = 7 * 24 * 3600


def _prune_crash_dir(directory: str, *, keep_s: float = CRASH_KEEP_S) -> list[str]:
    """Drop crash files older than `keep_s`. Empty ones too — an empty file
    means "did not exit cleanly", which a week later nobody is still asking
    about, and a watcher the server terminates on every reload leaves one
    per restart."""
    removed: list[str] = []
    cutoff = time.time() - keep_s
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.stat(path).st_mtime < cutoff:
                os.unlink(path)
                removed.append(path)
        except OSError:
            pass
    return removed


def uvicorn_log_config() -> dict:
    """A `log_config` for `uvicorn.Config` that sends uvicorn's own records to
    the root logger (and so to this file) and keeps the access logger quiet.

    `disable_existing_loggers` is False so the root handler installed by
    `setup_logging()` survives uvicorn's `dictConfig` call.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
        },
    }


def setup_logging() -> str:
    """Attach a rotating file handler to the root logger; return the log path.

    Root logger (not a package logger) on purpose: library warnings and
    (via `uvicorn_log_config`) uvicorn's own records propagate there too, so
    the file captures everything the process would have said on a visible
    stderr. Idempotent: a second call (a CLI restart in tests, or both entry
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
        path, maxBytes=ROTATE_BYTES, backupCount=ROTATE_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    try:
        removed = prune_log_home()
    except Exception:  # noqa: BLE001 — retention must never block startup
        removed = []
    _log_boot_context()
    if removed:
        logging.getLogger("fused_render").info(
            "log home: pruned %d old file(s)", len(removed))
    return path


def _log_boot_context() -> None:
    """One block of environment facts per boot — the questions asked first when
    debugging a broken install, answered before anyone has to ask."""
    log = logging.getLogger("fused_render")
    try:
        from fused_render import __version__ as fr_version
        from fused_render.health import boot_id

        log.info(
            "boot: fused-render=%s boot_id=%s python=%s (%s) platform=%s sys.prefix=%s "
            "started=%s",
            fr_version,
            boot_id(),
            platform.python_version(),
            sys.executable,
            platform.platform(),
            sys.prefix,
            time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
    except Exception:
        # Boot context is best-effort; never let it block startup.
        log.exception("boot: failed to collect environment info")
