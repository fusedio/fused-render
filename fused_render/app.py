"""Menu-bar entry point for the packaged macOS app (SPEC DM-3/DM-5/DM-7).

Wraps the existing `create_app()` server with a `rumps` NSStatusItem whose
single surface is the pinned-view popover (menubar_pin.py, SPEC §25 D98):
header row of app actions + a WKWebView of the pinned file. The rumps menu is
only a fallback if the popover controller fails (PV-8). The CLI (`cli.py`,
`fused-render`) is unaffected and remains the dev entry point.

`rumps` is macOS-only and is not a core dependency (see the `app` extra in
pyproject.toml) — it is imported lazily, inside `main()`, so that
`import fused_render.app` never fails on another platform or in CI.
"""
import importlib.util
import json
import logging
import os
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from fused_render import desktop_probe
from fused_render._branch import branch_dir, branch_port
from fused_render.logs import log_path, setup_logging
from fused_render.server import create_app, export_app_env, set_server_origin_env
# The two teardown budgets the quit deadline is derived from (see
# QUIT_HARD_DEADLINE_S). Imported eagerly — `create_app` above already pulls the
# mounts package in, so this costs nothing — because a deadline that has to
# outlast them must be computed FROM them, not restated.
from fused_render.shell.mounts import _QUIT_UNMOUNT_BUDGET_S, RCD_REAP_WORST_CASE_S
from fused_render.shell.seed import ensure_fused_dir

logger = logging.getLogger("fused_render")

_APP_SUPPORT_BASE = os.path.expanduser("~/Library/Application Support/fused-render")
APP_SUPPORT_DIR = branch_dir(_APP_SUPPORT_BASE)
PIDFILE = os.path.join(APP_SUPPORT_DIR, "server.pid")
PORTFILE = os.path.join(APP_SUPPORT_DIR, "server.port")

DEFAULT_PORT = branch_port()
MAX_PORT = DEFAULT_PORT + 10


def view_url_path(fs_path: str) -> str:
    """Shell URL path for a Finder-opened file (SB-9, D99).

    A `.bookmark` file is not previewed — it routes to the `_bookmark`
    sentinel, which reads the file server-side and redirects to the view it
    describes (the frontend resolves its relative paths against the file's
    own directory). Everything else opens as a plain `/view/<path>`.
    Module-level (not a closure) so it is testable without AppKit.

    Delegates to the shared `_view_url_codec` (the single body, now 4/4
    consumers with winopen/deeplink/shell.seed) so macOS encodes paths exactly
    as the frontend router and the Windows supervisor do — per-segment with the
    `!*'()` safe set of `encodeURIComponent`.
    """
    from fused_render._view_url_codec import view_url_path as _shared

    return _shared(fs_path)


def clone_url_path(raw_url: str) -> str:
    """Shell URL path for an OS-delivered `fused-render://` deep link (SPEC
    §26, D110): the /clone confirm page with the raw link as ?src=. Parsing
    and validation happen server-side (deeplink.py); this only ferries the
    string. Module-level (not a closure) so it is testable without AppKit.

    Delegates to the shared `_view_url_codec.open_target_path` so all three
    platforms (macOS here, the Windows/Linux supervisor) ferry a deep link
    identically."""
    from fused_render._view_url_codec import open_target_path

    return open_target_path(raw_url)


def openurls_target_path(raw_url: str) -> str:
    """Shell URL path for an `application:openURLs:` event (SPEC §26, D110).

    AppKit delivers both `fused-render://` deep links AND plain document
    opens (e.g. a Finder double-click on a registered `.bookmark` file, as
    a `file://` URL) through this one selector — unlike `openFiles:`, which
    only ever gets plain paths. Only a `fused-render:` URL is a deep link;
    anything else is a file open and must resolve the same way
    `application_openFiles_` does, via `view_url_path`. Module-level (not a
    closure) so it is testable without AppKit.

    Delegates to the shared `_view_url_codec.open_target_path` — the single
    implementation shared with the Windows/Linux supervisor.
    """
    from fused_render._view_url_codec import open_target_path

    return open_target_path(raw_url)


# ---- quit-time close of the duckdb reader's cached connection ---------------
# The duckdb parquet reader is an in-process helper (executor.INPROCESS_HELPERS),
# so on macOS — where the server runs inside THIS rumps process — the HTTP
# connection it stashes on the duckdb module (templates/duckdb/reader.py's
# _http_connection) lives here and nothing ever closes it. AppKit's exit() then
# destructs it without the GIL and the process aborts (INCIDENT 2026-07-29; see
# close_http_connection for the full mechanism). The close logic lives with the
# stash, in reader.py; this side only has to reach it.
_DUCKDB_READER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "templates", "duckdb", "reader.py")


def _load_duckdb_reader():
    """The duckdb reader module, loaded by path — `templates/` is deliberately
    not an importable package (executor._run_inprocess loads its helpers the
    same way). Which COPY we load is immaterial: the stash lives on the shared
    `duckdb` module, not on the reader, so the bundled original next to this
    file closes the connection a staged copy created."""
    spec = importlib.util.spec_from_file_location(
        "__fused_duckdb_reader__", _DUCKDB_READER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _close_duckdb_default_connection() -> None:
    """Close duckdb's own DEFAULT connection — the handle INCIDENT 2026-07-29's
    fix could not reach.

    Closing the reader's stash was necessary and not sufficient. duckdb 1.5.5
    (what the bundle ships) creates its default connection EAGERLY AT IMPORT and
    holds it in a C++ global that `__cxa_finalize` destructs during exit(), so a
    process that merely `import duckdb`s — which the in-process server does for
    /api/index, parquet, search, git_repos, h3, excel and tableau, none of which
    ever touch the reader's stash — aborts exactly like one that left the stash
    open.

    HONEST ABOUT WHAT THIS BUYS, because it is less than it looks: measured on
    the shipped interpreter (Contents/MacOS/python, duckdb 1.5.5), `import
    duckdb` + `exit()` from a non-main thread aborts, AND SO DOES THE SAME RUN
    WITH THIS CLOSE FIRST. The Python-level `close()` does not take the C++
    global with it. So this is correct shutdown hygiene — release the handle
    while the interpreter is healthy and holding the GIL, which is what any
    later exit path would want — and NOT the fix for the crash. `hard_exit` is
    the fix: the quit never reaches `__cxa_finalize` at all.

    `default_connection` is a builtin FUNCTION in 1.5.5, but the attribute has
    changed shape across duckdb releases (and may be gone in a future one), so
    both a callable and a bare connection object are accepted rather than
    assuming: it costs one `callable()`."""
    duckdb = sys.modules.get("duckdb")
    default = getattr(duckdb, "default_connection", None)
    if default is None:
        return
    con = default() if callable(default) else default
    close = getattr(con, "close", None)
    if callable(close):
        close()


def _close_duckdb_stash() -> None:
    """Best-effort quit-time close of every DuckDB connection this process can
    still be holding: the reader's cached HTTP connection and duckdb's own
    default connection.

    Skips both entirely when `duckdb` was never imported: no import means no
    connection can exist, and quit shouldn't pay a multi-hundred-ms duckdb
    import to discover that. Each half is guarded on its own — either one alone
    is enough to abort the process, so a failure in one must not skip the other
    — and everything is swallowed (duckdb missing, unreadable reader, a raising
    close): a failure here must not block the quit.

    Neither close is what stops the abort any more — `hard_exit` is, by never
    reaching `__cxa_finalize` (and measurement says the default-connection close
    alone would NOT have been enough anyway; see there). They stay because
    releasing a connection while the interpreter is healthy and holding the GIL
    is the correct shutdown: the reader's HTTP connection holds a socket against
    an rclone serve that the very next teardown step reaps, and any future path
    that does reach a normal exit() starts from a cleaner state."""
    if "duckdb" not in sys.modules:
        return
    try:
        _load_duckdb_reader().close_http_connection()
    except Exception:
        logger.warning("closing the duckdb http connection on quit failed",
                       exc_info=True)
    try:
        _close_duckdb_default_connection()
    except Exception:
        logger.warning("closing duckdb's default connection on quit failed",
                       exc_info=True)


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def find_running_server() -> tuple[int, int] | None:
    """Return (pid, port) of an already-live fused-render instance, or None.

    "Live" means: the recorded pid is running AND it serves the shell page
    on the recorded port. Probing "/" (not /api/config) matters: "/" reads
    shell.html from disk, so a zombie whose bundle files were deleted or
    replaced (e.g. a build-dir instance clobbered by a rebuild) fails the
    probe and a fresh healthy instance gets started instead.
    """
    pid = _read_int(PIDFILE)
    port = _read_int(PORTFILE)
    if pid is None or port is None or not _is_process_alive(pid):
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
            if resp.status == 200:
                return pid, port
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pick_port(start: int = DEFAULT_PORT, end: int = MAX_PORT) -> int:
    for port in range(start, end + 1):
        if not _port_in_use(port):
            return port
    raise RuntimeError(f"no free port between {start} and {end}; is something hogging the whole range?")


def configure_desktop_instance() -> tuple[str, str]:
    """Publish this launch's desktop instance id + a fresh 256-bit token into
    the environment. The in-process server reads them lazily per request via
    `fused_render.paths.desktop_instance()`, so `/api/config` echoes the id
    (and the token, only to a caller that already knows it) and the token-gated
    `POST /api/desktop/shutdown` endpoint becomes available — parity with the
    Windows supervisor's child server. Readiness is then verified against this
    exact token (shared `desktop_probe`), so a decoy server on the port cannot
    fool startup. Module-level (AppKit-free) so it is testable."""
    token = secrets.token_hex(32)  # 32 bytes == 256 bits, 64 hex chars
    os.environ["FUSED_RENDER_DESKTOP_INSTANCE_ID"] = desktop_probe.DESKTOP_INSTANCE_ID
    os.environ["FUSED_RENDER_DESKTOP_INSTANCE_TOKEN"] = token
    return desktop_probe.DESKTOP_INSTANCE_ID, token


def _write_pidfile(port: int) -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    with open(PORTFILE, "w") as f:
        f.write(str(port))


def _remove_pidfile() -> None:
    for path in (PIDFILE, PORTFILE):
        try:
            os.remove(path)
        except OSError:
            pass


def _start_server_thread(port: int) -> tuple[uvicorn.Server, threading.Thread]:
    """Start uvicorn serving create_app(start_dir=Fused dir) on a daemon thread.
    Returns the server and its thread (quit drains it — `should_exit` alone is
    fire-and-forget, and uvicorn never resets `started`, so the thread ending is
    the only observable "it has stopped serving")."""
    # The D337 workspace migration does NOT run here: it runs in `main()`,
    # before the run loop and before anything reads state (see the call site).
    # This thread starts long after it, so onboarding below is still strictly
    # after the migration — the ordering cli._run_serve records.
    # First-run onboarding (D81): create ~/Fused.
    start_dir = ensure_fused_dir()
    # Showcase apps: clone/sync the community repo into <workspace>/showcase in
    # the background — the apps grid lists it as an ordinary tag dir once done.
    from fused_render import community

    community.refresh_in_background()
    # One-time migration: stamp `<meta name="fused-app">` into pre-existing
    # workspace apps (meta_migration's docstring carries the rules).
    from fused_render import meta_migration

    meta_migration.run_once_in_background(start_dir)
    # Probe Claude Code (found / version / signed-in) off the request path, so
    # the first-run strip's GET is a disk read. Mirrors cli._run_serve — an entry
    # point, never create_app.
    from fused_render import claude_health

    claude_health.warm_in_background()
    app = create_app(start_dir=start_dir)
    # Publish the real bound origin so runPython children (e.g. the zarr_aoi
    # tile daemon) read store bytes from THIS port, not the branch default.
    set_server_origin_env(port, host="127.0.0.1")
    # Same lifecycle point, same reason: templates read the shell dirs + the
    # read-only mount list from the env (they can't import fused_render).
    export_app_env()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


# ---- how the process actually dies (SPEC DM-9; INCIDENT 2026-08-19) ---------
# EVERY quit surface ends here, and none of them returns control to C `exit()`.
#
# The mechanism, measured twice now: `-[NSApplication terminate:]` ends in
# `exit()`, which runs `__cxa_finalize` over the static destructors of every
# loaded dylib. pyobjc releases the GIL for the duration of an ObjC call, so
# those destructors run on the main thread WITHOUT the GIL — and a native
# extension's C++ global that touches the Python C-API on the way out (duckdb's
# `~DuckDBPyConnection` -> `PyEval_SaveThread`) hits `_Py_FatalErrorFunc` and
# `abort()`s. The 2026-07-29 fix closed the one connection we knew about; on
# 2026-08-19 the app aborted 20ms after a teardown that had FULLY succeeded,
# because duckdb 1.5.5 also holds a default connection created at import — and
# measured on the shipped interpreter, closing THAT one does not help either:
# `import duckdb` + exit() off-thread aborts with or without the close. Every
# native extension the server loads (GDAL/rasterio, pyarrow, torch) is the same
# hazard, so chasing handles is whack-a-mole against a game we cannot win. The
# durable fix is to never reach `__cxa_finalize`. Same measurement, dying via
# os._exit instead: rc 0.
#
# `os._exit` skips atexit handlers, Python finalization (interpreter shutdown,
# gc, module teardown) and `__cxa_finalize`. That is safe here, and ONLY because
# quit already does its shutdown explicitly and in order: `quit_teardown` drains
# the server, closes duckdb, detaches every mount and reaps rcd BEFORE this runs,
# and `begin_quit` removes the pidfile on the calling thread. What is given up is
# work we either do not have or do not want: the tree registers no `atexit`
# handler (logging's flush is the one that matters, and it is done here by hand);
# Python finalization would only free memory the kernel reclaims anyway; and
# AppKit's own termination is precisely the step that aborts.


# Budget for the log flush below. Paid on every quit, so it may not be a visible
# stall; and it is a backstop against a wedged filesystem, not an I/O allowance —
# a flush that is going to work at all takes microseconds.
QUIT_LOG_FLUSH_S = 0.5


def _flush_logs() -> None:
    try:
        logging.shutdown()
    except Exception:
        pass


def hard_exit(code: int = 0, *, exit_process=os._exit,
              flush_budget_s: float = QUIT_LOG_FLUSH_S) -> None:
    """Kill this process immediately, skipping every finalizer (see above).

    Never returns. `exit_process` is injectable so tests can pin the behavior
    without killing the pytest worker.

    os._exit runs no atexit handler, and logging's flush IS one — without it the
    tail of the quit log, exactly what a crash report gets read against, can be
    lost. But the flush is BOUNDED and off-thread, because a try/except catches
    raises and not hangs, and the hang is the case that matters:
    `logging.shutdown()` acquires every handler's lock, and the one scenario the
    AppKit backstop exists for — a teardown thread wedged somewhere unbudgeted —
    is precisely the scenario where that thread may be wedged mid-emit holding
    the RotatingFileHandler lock (a rollover or write against a wedged
    FUSED_RENDER_LOG_DIR). Waiting on that acquire() would make the app
    unquittable: the exact outcome QUIT_HARD_DEADLINE_S and
    QUIT_APPKIT_REPLY_WAIT_S exist to rule out. A daemon flusher we stop waiting
    for costs at worst the last few log lines; blocking here costs the quit."""
    flusher = threading.Thread(target=_flush_logs, daemon=True,
                               name="quit-log-flush")
    flusher.start()
    flusher.join(flush_budget_s)
    exit_process(code)


# ---- quit teardown (SPEC DM-7; INCIDENT 2026-07-29) -------------------------
# Quit used to be four blocking statements inside the menu-item action. It hung
# for seconds (the reap runs synchronously on the AppKit main thread), orphaned
# the kernel NFS mounts (rcd killed while they were still attached), and then
# aborted in exit()'s static destructors. The teardown is a module-level,
# injectable function so the ORDER is testable (tests/test_app_quit.py) instead
# of being an accident of statement order in a closure.

QUIT_SERVER_DRAIN_S = 2.0

# Ceiling on the whole teardown, after which the app terminates regardless. It
# has to exist: a wedged `umount -f` blocks in the kernel and cannot be
# cancelled, and an app that can never be quit is worse than one that quits with
# a mount still attached.
#
# DERIVED from the bounds of the steps it waits on, never a hand-picked number:
# a first cut hardcoded 15s while the steps summed to 21s, so the deadline fired
# DURING the rcd SIGTERM wait — skipping the SIGKILL escalation, and on macOS a
# surviving rcd reparents to launchd, leaving a live daemon under mounts whose
# teardown may not have finished. That is the exact failure this branch exists to
# stop, reintroduced by arithmetic. Every inner budget is imported (rcd exports
# its own worst case rather than having 3+3+5+5 restated here), so tightening any
# of them moves this with it; tests/test_app_quit.py asserts the inequality.
# The margin covers the unbudgeted interstitials (thread starts, the duckdb close,
# a `_rcd_lock` handoff).
QUIT_DEADLINE_MARGIN_S = 2.0

QUIT_HARD_DEADLINE_S = (
    QUIT_SERVER_DRAIN_S
    + _QUIT_UNMOUNT_BUDGET_S
    + RCD_REAP_WORST_CASE_S
    + QUIT_DEADLINE_MARGIN_S
)


def quit_teardown(server, *, server_thread=None, drain_s: float = QUIT_SERVER_DRAIN_S,
                  close_duckdb=None, unmount_mounts=None, stop_rcd=None) -> list[str]:
    """Run the ordered quit teardown; returns the steps attempted, in order.

    The order is the point, and each rung is a precondition of the next:

      1. "server" — stop accepting requests and drain in-flight ones, bounded by
         `drain_s`. A live /api/fs/raw read holds files open under a mount, which
         is a measured cause of a busy-mount unmount failure (see
         detach_mount/_quit_tile_daemons), so this comes before the unmounts.
      2. "duckdb" — close the reader's cached DuckDB connection while Python is
         healthy and the GIL is held. Anything still alive at
         `NSApplication.terminate:` destructs without the GIL and aborts.
      3. "unmount" — detach every mount through the rc-unmount -> force-unmount
         ladder, BEFORE its NFS server is signalled.
      4. "rcd" — reap the daemon. Only now is it safe: nothing is mounted on it.

    Every step is best-effort and independently guarded — a failure in one must
    not skip the ones after it (a mount store we cannot read must still let the
    daemon be reaped, and vice versa). The step callables are injectable for
    tests; the defaults are the real ladder."""
    steps: list[str] = []
    if close_duckdb is None:
        close_duckdb = _close_duckdb_stash
    if unmount_mounts is None:
        def unmount_mounts():
            from fused_render.shell.mounts import unmount_all_for_quit

            unmount_all_for_quit()
    if stop_rcd is None:
        def stop_rcd():
            from fused_render.shell.mounts import stop_local_rcd

            stop_local_rcd()

    started = time.monotonic()
    if server is not None:
        steps.append("server")
        try:
            server.should_exit = True
            if server_thread is not None:
                # Bounded: uvicorn's graceful shutdown waits on open connections,
                # and a hung handler must not become a hung quit.
                server_thread.join(drain_s)
                if server_thread.is_alive():
                    logger.warning("server did not drain within %.1fs; "
                                   "continuing teardown", drain_s)
        except Exception:
            logger.warning("stopping the server on quit failed", exc_info=True)
    for name, step in (("duckdb", close_duckdb), ("unmount", unmount_mounts),
                       ("rcd", stop_rcd)):
        steps.append(name)
        try:
            step()
        except Exception:
            logger.warning("quit teardown step %r failed", name, exc_info=True)
    logger.info("quit teardown finished in %.1fs (steps: %s)",
                time.monotonic() - started, ", ".join(steps))
    return steps


def start_quit(server, *, terminate, server_thread=None, teardown=None,
               deadline_s: float = QUIT_HARD_DEADLINE_S) -> threading.Thread:
    """Begin quitting WITHOUT blocking the caller, and terminate when done.

    Called from a menu-item action, i.e. on the AppKit main thread with the run
    loop blocked for as long as we stay in it — so every blocking step (the
    unmount ladder, rcd's SIGTERM/SIGKILL polls: ~13s worst case) runs on a
    worker and this returns immediately. `terminate` is then called from the
    watchdog thread once teardown finishes OR `deadline_s` elapses, whichever
    comes first: teardown gets a real, bounded chance to complete, and a wedged
    step still cannot leave an app that refuses to quit. Exactly one call to
    `terminate` either way — only the watchdog ever calls it.

    Returns the watchdog thread (tests join it; nothing in the app does — the
    process is gone by then)."""
    if teardown is None:
        def teardown():
            quit_teardown(server, server_thread=server_thread)

    done = threading.Event()

    def _teardown() -> None:
        try:
            teardown()
        except Exception:
            logger.warning("quit teardown failed", exc_info=True)
        finally:
            done.set()

    threading.Thread(target=_teardown, daemon=True, name="quit-teardown").start()

    def _terminate_when_done() -> None:
        if not done.wait(deadline_s):
            logger.warning("quit teardown exceeded %.1fs; terminating anyway",
                           deadline_s)
        terminate()

    watchdog = threading.Thread(target=_terminate_when_done, daemon=True,
                                name="quit-terminate")
    watchdog.start()
    return watchdog


# Guards the quit bookkeeping on `state` — the lazy `quit_ready` event and
# begin_quit's check-then-set of `quitting`. NOT because the surfaces are exotic:
# the tray item and the delegate hook are both AppKit callbacks on the main thread,
# but `_bootstrap_server`'s readiness-failure abort calls the same quit action from
# the BOOTSTRAP thread, so a Dock/⌘Q quit can genuinely interleave with it. Unlocked,
# both callers saw `quitting` False and ran two unmount fan-outs and two reaps (the
# second raising "did not exit"), and two lazily-created events meant one surface
# waiting on a signal the other never set — an app AppKit never gets a reply from.
_quit_lock = threading.Lock()


def _quit_ready_event(state: dict) -> threading.Event:
    """The one "teardown is finished (or its deadline fired) — dying is now
    correct" signal, shared by every quit surface. Lazily created on `state` so a
    surface that arrives while another's teardown is mid-flight observes the SAME
    event instead of inventing a second answer.

    Callers already holding `_quit_lock` must use `_quit_ready_event_locked`."""
    with _quit_lock:
        return _quit_ready_event_locked(state)


def _quit_ready_event_locked(state: dict) -> threading.Event:
    event = state.get("quit_ready")
    if event is None:
        event = state["quit_ready"] = threading.Event()
    return event


def begin_quit(state: dict, *, terminate=None, start=None,
               remove_pidfile=None, on_claim=None) -> bool:
    """Start THE teardown unless one is already running; True if this call
    started it.

    Every quit surface funnels through here — the tray menu item, the popover's
    `quitApp_`, and AppKit's own `terminate:` (Dock menu Quit, ⌘Q,
    logout/restart) — because they must converge on ONE teardown: the app stays
    alive and clickable while it runs, so a second Quit from any surface has to
    join the one in flight rather than race a second unmount + reap against it.

    `terminate` (optional) runs after `state["quit_ready"]` is set, so a surface
    that owes AppKit an action at the end can hang it there while every surface
    still observes the same event. The pidfile is removed on the calling (main)
    thread: it costs microseconds, and a relaunch during a slow teardown must not
    find this dying instance and hand the user a browser tab on a closing
    server.

    `on_claim` (optional) runs INSIDE the claim — after the pidfile is gone,
    before the teardown is started — for work that must be finished before this
    process can die. That ordering has to be enforced here now: since D355 the
    quit ends in `os._exit` on the watchdog thread with no main-thread hop, so
    "after begin_quit returns" is no longer safely before the exit. An instance
    with nothing mounted and a server that drains on its first poll can complete
    the whole teardown and exit while the caller is still executing its next
    statement — which for `begin_relaunch` is the `Popen` that parks its
    successor (a fork+exec of a large process under `start_new_session`), i.e.
    the app quits and nothing comes back. The old
    `AppHelper.callAfter(rumps.quit_application)` made that impossible by
    construction, and this hook is what replaces that guarantee."""
    if start is None:
        start = start_quit
    if remove_pidfile is None:
        remove_pidfile = _remove_pidfile
    # One critical section for the event and the flag: claiming the teardown has to
    # be atomic against another surface doing the same (see _quit_lock). The lock is
    # released before `start`, which spawns threads — nothing under it blocks.
    with _quit_lock:
        ready = _quit_ready_event_locked(state)
        if state.get("quitting"):
            logger.info("quit already in progress; joining it")
            return False
        state["quitting"] = True
    remove_pidfile()
    if on_claim is not None:
        try:
            on_claim()
        except Exception:
            # Best-effort like the teardown steps, and for a sharper reason: a
            # quit that is CLAIMED but never torn down is the worst state
            # available — quit_ready is never set, so every later surface waits
            # out its backstop before the app can die at all.
            logger.warning("quit: the on-claim hook failed", exc_info=True)

    def _finished() -> None:
        # Set BEFORE the surface's own action, because that action is typically
        # what re-enters AppKit's terminate: — and the delegate hook reads this
        # event to answer NSTerminateNow instead of waiting on a teardown that
        # has already finished.
        ready.set()
        if terminate is not None:
            terminate()

    start(state.get("server"), terminate=_finished,
          server_thread=state.get("server_thread"))
    return True


def bundle_path() -> str | None:
    """The .app bundle root when running packaged, None otherwise.

    sys.executable is …/FusedRender.app/Contents/MacOS/python under py2app
    (same anatomy fusedcli.setup_cli_hint and installed.installed_version
    rely on).
    """
    if getattr(sys, "frozen", None) != "macosx_app":
        return None
    contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(contents)


# The relauncher's poll cadence while it waits for this process to die. Fast
# enough that the relaunch feels immediate after the teardown (bounded by
# QUIT_HARD_DEADLINE_S), slow enough to cost nothing.
RELAUNCH_POLL_S = 0.2


def spawn_relauncher(bundle: str, pid: int, *, popen=subprocess.Popen):
    """Detached shell child that waits for `pid` to exit, then `open`s the
    bundle. A dying app cannot start its own successor — `open` on a bundle
    that is still running only foregrounds it — so the wait has to happen in a
    process that survives us: its own session, no inherited pipes.

    The poll loop has no timeout of its own: the pid it waits on is guaranteed
    to die within QUIT_HARD_DEADLINE_S (start_quit's watchdog terminates the
    app past it, teardown finished or not), so a bounded wait here would only
    duplicate that guarantee."""
    # `open -a <bundle> fused-render://launch`, not a plain `open <bundle>`:
    # a plain open is a normal launch, which boots onto a fresh home tab and
    # steals focus from the page that asked for the restart. Delivering the
    # launch action instead makes the successor's handler set state["docs"]
    # and open nothing (D128); -a pins WHICH copy launches, so the deep link
    # can't resolve to some other registered install.
    quoted = shlex.quote(bundle)
    script = (
        f"while /bin/kill -0 {int(pid)} 2>/dev/null; do sleep {RELAUNCH_POLL_S}; done; "
        f"exec /usr/bin/open -a {quoted} fused-render://launch"
    )
    return popen(
        ["/bin/sh", "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def begin_relaunch(*, quit_action, bundle=None, spawn=None,
                   running=None, installed=None) -> bool:
    """fused-render://relaunch: quit through the normal teardown and park a
    relauncher on our pid. True if the relaunch was started.

    Acts ONLY when this process is provably stale — the disk version is known
    and differs from the running one. The OS may LAUNCH a fresh instance just
    to deliver this link (app not running, or a second click landing after the
    old pid died): that instance IS the disk version, and quitting it to boot
    itself again would be a pointless extra cycle that can even race a second
    instance past the pidfile begin_quit already removed.

    Also no-ops when unpackaged (no bundle to respawn — the app keeps running)
    and when a quit is already in flight — respawning an app the user is
    quitting would be worse than ignoring the link. The in-flight case rides
    `quit_action`'s return (begin_quit's claim bool) rather than reading
    state["quitting"] here: that flag's check-then-set is only atomic under
    _quit_lock, and begin_quit already owns that critical section.

    The spawn is handed to the quit as its `on_claim` hook rather than run after
    it returns, and that is load-bearing, not tidiness: since D355 the quit ends
    in `os._exit` off a watchdog thread, so an instance with nothing to unmount
    can be dead before a statement after `quit_action()` finishes — and a
    relauncher that was never spawned means the app quits with no successor.
    "The teardown drains for seconds" used to make this safe by accident; the
    hook makes it safe by construction."""
    if bundle is None:
        bundle = bundle_path()
    if bundle is None:
        logger.info("relaunch deep link ignored: not running from a bundle")
        return False
    if running is None:
        from fused_render import __version__ as running
    if installed is None:
        from fused_render.installed import installed_version

        installed = installed_version()
    if installed is None or installed == running:
        logger.info("relaunch deep link ignored: running %s, disk %s — nothing to swap",
                    running, installed)
        return False
    if spawn is None:
        spawn = spawn_relauncher
    if not quit_action(on_claim=lambda: spawn(bundle, os.getpid())):
        logger.info("relaunch deep link ignored: quit already in progress")
        return False
    return True


def make_quit_action(state: dict, *, terminate, start=None, remove_pidfile=None):
    """The Quit action for the two surfaces WE own — the rumps menu item and the
    popover's `quitApp_`, which receives it through the controller's actions
    dict. Module-level (not a `main()` closure) so it is testable without AppKit;
    it takes `main()`'s `state` dict because the server and its thread only exist
    once the bootstrap thread has published them."""
    def _do_quit(on_claim=None) -> bool:
        # The claim bool matters to one caller — begin_relaunch only parks a
        # relauncher behind a quit THIS call started, and hands its spawn in as
        # `on_claim` so it happens before anything can exit. The menu/popover
        # surfaces pass nothing and ignore the bool.
        return begin_quit(state, terminate=terminate, start=start,
                          remove_pidfile=remove_pidfile, on_claim=on_claim)

    return _do_quit


# AppKit's NSApplicationTerminateReply values, spelled out because AppKit is
# macOS-only and this module must import everywhere (see the module docstring).
# Verified against the real framework, and ABI-stable: NSTerminateCancel 0,
# NSTerminateNow 1, NSTerminateLater 2.
NS_TERMINATE_NOW = 1
NS_TERMINATE_LATER = 2

# Backstop on the wait for `quit_ready` in the AppKit quit thread. An app AppKit
# is still waiting on an answer from cannot be quit at all, so a teardown that
# somehow never signals must not strand it: die anyway. Past the quit deadline,
# since a teardown that hits the deadline DOES signal — so reaching this backstop
# means the teardown thread is wedged somewhere unbudgeted, and it has already
# had more than its bounded chance. A mount left attached beats an unquittable
# app (the same trade QUIT_HARD_DEADLINE_S makes).
QUIT_APPKIT_REPLY_WAIT_S = QUIT_HARD_DEADLINE_S + 5.0


def make_appkit_terminate_hook(state: dict, *, reply, start=None,
                               remove_pidfile=None, exit_now=None):
    """`applicationShouldTerminate:` for the quit surfaces AppKit owns itself.

    The app is a REGULAR app — `scripts/setup_py2app.py` deliberately sets no
    LSUIElement (D34: Dock icon AND menu bar item) — so the Dock icon's
    right-click Quit, ⌘Q and logout/restart all go straight to
    `-[NSApplication terminate:]` and, without this hook, straight on to C
    `exit()`: no drain, no duckdb close, no unmount, no rcd reap. Every defect
    the teardown exists to fix was fully live on those surfaces, and none of them
    passes through the tray action.

    Half of the canonical Cocoa answer applies: return NSTerminateLater so the
    teardown runs off the main thread while AppKit waits. The other half —
    `replyToApplicationShouldTerminate:`, which resumes `terminate:` and lands in
    `exit()` — is exactly the step that aborts (see `hard_exit`), and so is the
    NSTerminateNow that AppKit would act on the same way. Once dying is correct
    we therefore `exit_now` (hard_exit) instead of answering. AppKit is not owed
    a tidy shutdown: the ordered teardown has already done everything AppKit's
    termination would have given us, and more.

    `reply` survives as the LAST RESORT only, on the one path where the process
    is somehow still alive after `exit_now` returned or raised. `os._exit` cannot
    fail, so this is unreachable in practice, but an app AppKit never hears back
    from cannot be quit at all — that property is worth a two-line fallback
    rather than a comment claiming it can't happen.

    Three cases, all of which must end in the process dying exactly once:
      * teardown already finished — a second Quit after one completed: die on the
        spot, nothing to wait for (answering Later here would hang the quit).
      * a teardown in flight (a tray/popover Quit first) — do NOT start a second;
        wait for the shared event, then die. Its own end-of-teardown hard exit
        may win the race; whichever does, the other is moot.
      * nothing started yet — AppKit is the first surface: start the same
        teardown, with the hard exit as its ending.
    """
    if exit_now is None:
        exit_now = hard_exit

    def _exit() -> None:
        """Die. Guarded only so a broken exit falls through to the caller's
        fallback instead of raising into an AppKit callback or a daemon thread."""
        try:
            exit_now(0)
        except Exception:
            logger.warning("quit: the hard exit failed", exc_info=True)

    def _exit_when_ready(ready: threading.Event) -> None:
        if not ready.wait(QUIT_APPKIT_REPLY_WAIT_S):
            logger.warning("quit: teardown never signalled; exiting anyway "
                           "rather than leaving the app unquittable")
        _exit()
        # Unreachable while exit_now is os._exit-backed. If we do get here the
        # process is still alive and AppKit is still waiting on the reply we owe
        # it for the NSTerminateLater above, so give it one — a quit that aborts
        # in exit() beats an app that cannot be quit at all.
        try:
            reply(True)
        except Exception:
            logger.warning("quit: replying to AppKit failed", exc_info=True)

    def _should_terminate() -> int:
        ready = _quit_ready_event(state)
        if ready.is_set():
            # No reply is owed on this path — we never answered Later — so there
            # is no fallback to make here. The return is unreachable in the app,
            # and is the honest answer if a stubbed or broken exit hands control
            # back: NSTerminateNow is what this branch meant before the hard exit.
            _exit()
            return NS_TERMINATE_NOW
        begin_quit(state, start=start, remove_pidfile=remove_pidfile)
        threading.Thread(target=_exit_when_ready, args=(ready,), daemon=True,
                         name="quit-appkit-exit").start()
        return NS_TERMINATE_LATER

    return _should_terminate


def install_terminate_hook(delegate_class, hook) -> bool:
    """Attach `applicationShouldTerminate:` to rumps' delegate class; True on
    success.

    rumps builds ONE delegate (`rumps.rumps.NSApp`, a pyobjc NSObject subclass)
    and sets it as the NSApplication delegate in `App.run()`. It defines no
    `applicationShouldTerminate_`, so adding the method to the class is enough —
    pyobjc registers the selector automatically and resolves its real signature
    from AppKit's protocol metadata (verified: `I@:@`, an unsigned-int return, so
    returning NSTerminateLater actually reaches AppKit as 2). This is the same
    mechanism the openFiles/openURLs/reopen patches above already rely on, which
    is also why it survives a rumps upgrade: it adds a method rumps has no
    opinion about instead of wrapping one.

    Never raises (PV-8 shape): if a future rumps rejects the patch, log it and
    keep today's behavior — an app that won't launch is worse than one whose
    AppKit-initiated quit skips the teardown."""
    def applicationShouldTerminate_(self, _app):
        return hook()

    try:
        delegate_class.applicationShouldTerminate_ = applicationShouldTerminate_
    except Exception:
        logger.exception("could not install applicationShouldTerminate_; Dock/⌘Q "
                         "quits will bypass the teardown")
        return False
    return True


def main() -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    setup_logging()  # first: everything after this can crash-report to the file
    logger.info("app starting (pid %s)", os.getpid())

    existing = find_running_server()
    if existing is not None:
        pid, port = existing
        # Another instance already owns the menu bar and the pidfile; don't
        # start a second server, just point the browser at it and exit.
        logger.info("found live server (pid %s, port %s); reusing it", pid, port)
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return

    # One-shot relocation of the workspace out of iCloud-synced ~/Documents
    # (D337) — HERE, before the run loop, not in the server thread. The
    # migration rewrites on-disk state, so it is a precondition of every
    # component that READS that state, and the menu-bar pin reads its
    # (workspace-absolute) path in PinController.__init__, which the boot timer
    # runs before the server thread has even started: migrating later left the
    # popover pointing at the pre-move path for the whole upgrade session.
    # Cost is a same-filesystem os.rename (metadata only — the workspace holds
    # git trees but no bytes move) plus a few small JSON reads, so the run loop
    # starts imperceptibly later. Still strictly BEFORE ensure_fused_dir(),
    # which only `_start_server_thread` calls, on a thread started below.
    # Placed after the reuse-a-running-instance return above: that instance
    # already migrated, and we are about to exit.
    from fused_render import workspace_migration

    workspace_migration.run()

    port = pick_port()
    url = f"http://127.0.0.1:{port}/"

    # Publish this launch's instance id + token before the server thread starts
    # so the in-process server echoes them from /api/config; readiness is then
    # verified against this token (a decoy server on the port can't satisfy it).
    _, desktop_token = configure_desktop_instance()

    import rumps  # macOS-only; see module docstring

    icon_path = os.path.join(os.path.dirname(__file__), "assets", "menubar-template.png")

    # Startup ordering matters (learned the hard way): the AppKit run loop
    # starts FIRST and the server boots in the background AFTER it. Document
    # open events (Finder double-click) are delivered once the run loop is
    # up; the server takes seconds to become ready. Deciding home-vs-file
    # AFTER server readiness therefore happens long after any launch document
    # event has arrived — no timing race, unlike every timer-window variant.
    state = {
        "ready": False,      # server answers; safe to open browser tabs
        "docs": False,       # at least one document open event arrived
        "pending": [],       # file views requested before the server was ready
        "server": None,      # uvicorn.Server, set by the bootstrap thread
        "server_thread": None,  # its thread, so quit can drain it (bounded)
        "quitting": False,   # a teardown is in flight; later Quits join it
        "quit_ready": threading.Event(),  # teardown done/deadline hit: die now
        "pin": None,         # menubar_pin.PinController, built after run loop start
    }

    def open_file_view(fs_path: str) -> None:
        target = f"http://127.0.0.1:{port}" + view_url_path(fs_path)
        if state["ready"]:
            logger.info("opening file view: %s", target)
            webbrowser.open(target)
        else:
            logger.info("queuing file view until server is ready: %s", target)
            state["pending"].append(target)

    # ---- Finder "Open with FusedRender" -------------------------------------
    # AppKit delivers double-clicked documents to the app delegate's
    # application:openFiles:. rumps's delegate (rumps.rumps.NSApp, a pyobjc
    # NSObject subclass) doesn't implement it — adding the method to the class
    # is all that's needed; pyobjc registers the selector automatically.
    def application_openFiles_(self, _app, filenames):
        # This is the "Right-Click open" path: Finder "Open with FusedRender".
        # Log the raw filenames the OS handed us — if a view later 500s, the
        # log ties the failing URL back to the file the user actually clicked.
        names = [str(n) for n in filenames]
        logger.info("Finder open-files event: %s", names)
        state["docs"] = True
        for name in names:
            open_file_view(name)

    rumps.rumps.NSApp.application_openFiles_ = application_openFiles_

    # ---- fused-render:// deep links (SPEC §26, D110) -------------------------
    # AppKit delivers URL-scheme opens (CFBundleURLTypes in the py2app plist)
    # to application:openURLs:. Same delegate-patch mechanism as openFiles
    # above; the /clone confirm page does all parsing and asks before any
    # clone, so this handler only ferries the raw URL to the server.
    #
    # AppKit also routes plain document opens (Finder double-click on a
    # registered file type, e.g. .bookmark) through this same selector as a
    # file:// URL on some launches, not through application:openFiles:.
    # openurls_target_path tells the two apart (mirrors the scheme check in
    # winopen.py's _open()).
    def application_openURLs_(self, _app, urls):
        from fused_render.deeplink import is_launch_url, is_relaunch_url

        raws = [str(u.absoluteString()) for u in urls]
        logger.info("deep-link open-URLs event: %s", raws)
        state["docs"] = True  # a deep-link launch shouldn't also open the home tab
        for raw in raws:
            if is_relaunch_url(raw):
                # fused-render://relaunch (the update-restart banner's button):
                # park a relauncher on our pid and quit through the normal
                # teardown — the successor boots from the bundle on disk, and
                # the page that linked here reconnects + reloads on its own
                # (server-status.ts). `_do_quit` is assigned later in main(),
                # before the run loop starts delivering URL events.
                logger.info("relaunch deep link: quitting to respawn from disk")
                begin_relaunch(quit_action=_do_quit)
                continue
            if is_launch_url(raw):
                # fused-render://launch (D128): the OS launching/foregrounding
                # this app IS the whole action — the server boot is already in
                # flight and the page that linked here reconnects on its own
                # (D126 banner), so no tab is opened now and nothing is queued
                # to open later. state["docs"] above also keeps the bootstrap
                # from auto-opening the home tab on a fresh launch.
                logger.info("launch deep link: ensuring app/server only, no tab")
                continue
            try:
                target = f"http://127.0.0.1:{port}" + openurls_target_path(raw)
            except OSError as error:
                # A host-bearing file:// URL or a foreign scheme:// — there is
                # no local file behind it (see open_target_path). Surface the
                # failure like the supervisor's _safe_open does (log + skip)
                # instead of opening a garbage /view tab.
                logger.error("cannot open delivered URL %s: %s", raw, error)
                continue
            if state["ready"]:
                logger.info("opening open-URLs target: %s", target)
                webbrowser.open(target)
            else:
                logger.info("queuing open-URLs target until server is ready: %s", target)
                state["pending"].append(target)

    rumps.rumps.NSApp.application_openURLs_ = application_openURLs_

    # ---- Dock icon click on the running app ---------------------------------
    # AppKit sends applicationShouldHandleReopen:hasVisibleWindows: when the
    # user clicks the Dock icon (or double-clicks the app in Finder) while the
    # app is already running. rumps's delegate doesn't implement it, so without
    # this patch a Dock click does nothing. Open the home tab; if the server is
    # still booting, queue it on the same pending list the bootstrap flushes.
    # Must return a BOOL — returning None here breaks the pyobjc bridge.
    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):
        logger.info("dock reopen event (server ready=%s)", state["ready"])
        if state["ready"]:
            webbrowser.open(url)
        else:
            state["pending"].append(url)
        return True

    rumps.rumps.NSApp.applicationShouldHandleReopen_hasVisibleWindows_ = (
        applicationShouldHandleReopen_hasVisibleWindows_
    )

    # ---- AppKit-initiated quit (Dock menu Quit, ⌘Q, logout/restart) ----------
    # Same delegate-patch mechanism as the three handlers above, for the quit
    # surfaces that never touch our menu item — see make_appkit_terminate_hook for
    # why they would otherwise reach exit() with no teardown at all.
    def _reply_to_appkit(should_terminate: bool) -> None:
        # The hook's LAST RESORT only — it hard-exits instead of replying, and
        # this runs solely if that somehow did not kill us (see
        # make_appkit_terminate_hook). replyToApplicationShouldTerminate: is
        # AppKit, so main-thread only, and we are on the quit thread. callAfter
        # is delivered in the run loop's common modes, which includes the mode
        # AppKit runs while it waits for this reply.
        from AppKit import NSApplication
        from PyObjCTools import AppHelper

        AppHelper.callAfter(
            lambda: NSApplication.sharedApplication()
            .replyToApplicationShouldTerminate_(should_terminate))

    install_terminate_hook(
        rumps.rumps.NSApp,
        make_appkit_terminate_hook(state, reply=_reply_to_appkit))

    def _bootstrap_server() -> None:
        logger.info("starting server on port %s", port)
        server, server_thread = _start_server_thread(port)
        state["server"] = server
        state["server_thread"] = server_thread
        if not desktop_probe.wait_until_ready(port, desktop_token, 15.0, poll_interval=0.2):
            # Log file, not print: Finder-launched apps have no visible stderr.
            logger.error("server did not become ready on port %s", port)
            # Through the quit ACTION, not straight to quit_application: by now
            # the server has been up for as long as 15s, so run_automount has had
            # ample time to spawn rcd and attach mounts — aborting past the
            # teardown would strand exactly what the teardown exists to detach.
            _do_quit()
            return
        _write_pidfile(port)
        state["ready"] = True
        logger.info("server ready on port %s", port)
        # Self-update checks (update/mac.py): a background loop that only
        # flips /api/config's `update` field — the shell shows the badge and
        # drives install from there. Never on the startup critical path.
        try:
            from fused_render.update import mac as mac_update

            mac_update.start()
        except Exception:
            logger.exception("update manager failed to start")
        if state["pin"] is not None:
            # AppKit is main-thread-only; this bootstrap runs on a worker.
            from PyObjCTools import AppHelper

            AppHelper.callAfter(state["pin"].server_ready)
        pending, state["pending"] = state["pending"], []
        for target in pending:
            webbrowser.open(target)
        # Home tab only when this launch wasn't a document double-click.
        if not state["docs"] and not os.environ.get("FUSED_RENDER_NO_BROWSER"):
            webbrowser.open(url)

    class FusedRenderStatusApp(rumps.App):
        def __init__(self):
            # Template icon (black+alpha) — macOS recolors it for menu bar
            # appearance. Icon beats a text title: recognizable and compact
            # in a crowded (notched) menu bar.
            # This menu is normally never seen: the popover controller strips
            # it from the status item and carries these actions in its header
            # row (SPEC §25 PV-3, D98). It stays built as the fallback surface
            # if the controller fails to construct (PV-8) — the app must never
            # be left unquittable.
            super().__init__("fused-render", icon=icon_path, template=True, quit_button=None)
            self.menu = ["Open in browser", "Copy URL", "Open app logs", "Quit"]

        @rumps.clicked("Open in browser")
        def open_browser(self, _sender):
            _open_browser()

        @rumps.clicked("Copy URL")
        def copy_url(self, _sender):
            _copy_url()

        @rumps.clicked("Open app logs")
        def open_logs(self, _sender):
            _open_logs()

        @rumps.clicked("Quit")
        def quit(self, _sender):
            _do_quit()

    def _open_browser():
        webbrowser.open(url)

    def _copy_url():
        subprocess.run(["pbcopy"], input=url.encode(), check=False)

    def _open_logs():
        # Reveal in Finder rather than opening the file: users are asked to
        # zip/attach it, and Console.app (the .log default handler) confuses
        # more than it helps.
        subprocess.run(["open", "-R", log_path()], check=False)

    def _terminate():
        # NOT rumps.quit_application() -> NSApplication.terminate: -> exit(),
        # which aborts in __cxa_finalize after a teardown that already succeeded
        # (see hard_exit). By the time this runs, begin_quit has set quit_ready
        # and quit_teardown has drained the server, closed duckdb, detached the
        # mounts and reaped rcd — there is nothing AppKit's termination would
        # still do for us. os._exit is thread-safe and needs no main thread, so
        # the callAfter hop this used to need is gone with it.
        hard_exit()

    # Returns immediately — the AppKit run loop must not block here — and lets
    # quit_teardown do the blocking work off-thread under a hard deadline.
    _do_quit = make_quit_action(state, terminate=_terminate)

    status_app = FusedRenderStatusApp()

    def _kickoff(timer):
        # One-shot, fired right after the run loop starts — the status item
        # (status_app._nsapp.nsstatusitem) exists only from this point on.
        timer.stop()
        try:
            # Lazy + guarded: pyobjc-framework-WebKit may be missing in an
            # older [app] env; on failure the rumps menu stays attached and
            # the app runs menu-only (PV-8).
            from fused_render.menubar_pin import PinController

            state["pin"] = PinController(
                status_app._nsapp.nsstatusitem,
                port,
                APP_SUPPORT_DIR,
                actions={
                    "open_browser": _open_browser,
                    "copy_url": _copy_url,
                    "open_logs": _open_logs,
                    "quit": _do_quit,
                },
            )
        except Exception:
            logger.exception("popover unavailable; falling back to the status-item menu")
        threading.Thread(target=_bootstrap_server, daemon=True).start()

    boot_timer = rumps.Timer(_kickoff, 0.1)
    boot_timer.start()

    status_app.run()


if __name__ == "__main__":
    main()
