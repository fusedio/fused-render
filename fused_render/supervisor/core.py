"""Platform-neutral supervisor run loop.

Owns the lifecycle every desktop backend shares — single-instance election,
the supervised server process, the tray, forwarded opens, graceful shutdown
and teardown — and reaches all genuinely OS-specific behavior only through
`supervisor._backend` (job / instance / startup / ui). The Windows pieces this
was first ported from live in `supervisor/_win32/`; a new OS is a new backend,
not a new copy of this loop.
"""
from __future__ import annotations

import os
import queue
import secrets
import socket
import sys
import threading
import time
import urllib.request
from enum import Enum, auto
from pathlib import Path

from fused_render import desktop_probe
from fused_render._branch import branch_port
from fused_render._view_url_codec import is_launch_url, open_target_url, view_url
from fused_render.desktop_probe import DESKTOP_INSTANCE_ID as _INSTANCE_ID
from fused_render.supervisor import _backend, protocol, tray
from fused_render.supervisor.paths import DesktopPaths

# The single live backend (win32 today). Every platform-specific call in this
# module goes through these names; see supervisor/_backend.py for the contract.
Job = _backend.Job
instance = _backend.instance
startup = _backend.startup
ui = _backend.ui
update = getattr(_backend, "update", None)  # optional hook (Windows only)

_READY_TIMEOUT_S = 20.0
_SHUTDOWN_TIMEOUT_S = 5.0
_STOP_PIPE_TIMEOUT_S = 10.0

_dialog_lock = threading.Lock()
_exit_dialog_lock = threading.Lock()


class SupervisorStoppedError(RuntimeError):
    """run() got past startup — the server was ready and the tray was up —
    and then failed (the server died mid-session, or its process tree would
    not stop during teardown). Callers must not report this as "could not
    start": the app DID start; the accurate story is that it stopped."""


class _ExitReason(Enum):
    TRAY_EXIT = auto()      # user confirmed Exit in the tray dialog
    UPGRADE = auto()        # installer sent ShutdownForUpgrade over the pipe
    SERVER_DIED = auto()    # the Python server exited on its own


def run(initial: protocol.Command) -> None:
    initial = _absolute_command(initial)
    names = instance.InstanceNames.current_user()
    inst = instance.acquire(names)

    if isinstance(inst, instance.SecondaryInstance):
        try:
            inst.send(initial, 75.0)
        except (instance.CommandRejected, UnicodeEncodeError):
            # CommandRejected: a healthy primary answered and rejected the
            # specific command. UnicodeEncodeError: a non-UTF-8 (surrogateescape)
            # argv path cannot be encoded into the UTF-16-LE wire frame at all
            # (raised inside protocol.encode, past SecondaryInstance.send's
            # OSError guard) — a healthy primary IS running, only this path is
            # unusable. Both mean the app is up; only this command failed.
            # Only an Open is safe to swallow this way (e.g. a bad/missing
            # forwarded path — the app did start, just not that file): report
            # it accurately instead of the generic "could not start" dialog.
            # Anything else (crucially ShutdownForUpgrade) must re-raise —
            # the installer's upgrade/uninstall step execs us with
            # --shutdown-for-upgrade and requires a non-zero exit code on
            # failure to know teardown didn't actually happen; swallowing a
            # rejection there would report a clean shutdown that never
            # occurred and let the installer proceed over a still-running
            # supervisor.
            if isinstance(initial, protocol.Open):
                ui.report_open_rejected(initial.path)
                return
            raise
        if isinstance(initial, protocol.ShutdownForUpgrade):
            inst.wait_for_exit(20.0)
        return

    # Primary instance.
    if isinstance(initial, protocol.ShutdownForUpgrade):
        inst.release()
        return

    paths = DesktopPaths.discover()
    paths.create()
    _spawn_desktop_integration(paths)
    token = _launch_token()
    job, process, port = _start_ready_server(paths, token)

    # Dispatched off-thread like every other open: a hung Path.exists()/
    # os.startfile (disconnected UNC path) must not stall run() before the
    # tray and pipe server exist, or the supervisor is alive but can't answer
    # ShutdownForUpgrade. A failed open is logged and ignored, never fatal.
    _spawn_open(port, initial, paths)

    try:
        login_enabled = startup.enabled()
    except OSError as error:
        paths.log(f"could not read sign-in setting, defaulting to off: {error}")
        login_enabled = False

    tray_handle = tray.start(port, login_enabled, paths)
    if update is not None:
        update.start_auto_checks(paths)
    pipe_requests: "queue.Queue[instance.Request]" = queue.Queue()
    pipe_thread = inst.serve(pipe_requests, paths.log)

    reason, upgrade_response = _event_loop(
        port, process, paths, tray_handle.actions, pipe_requests
    )
    _teardown(
        reason,
        upgrade_response,
        port=port,
        token=token,
        paths=paths,
        job=job,
        process=process,
        inst=inst,
        pipe_requests=pipe_requests,
        pipe_thread=pipe_thread,
        tray_handle=tray_handle,
    )


def _event_loop(
    port: int,
    process,
    paths: DesktopPaths,
    tray_actions: "queue.Queue[tray.TrayAction]",
    pipe_requests: "queue.Queue[instance.Request]",
) -> "tuple[_ExitReason, queue.Queue[int] | None]":
    """Services tray actions and pipe requests until something ends the
    session, then returns why (teardown itself lives in _teardown, so every
    exit reason takes the same path). The second element is the installer's
    pending UPGRADE response queue, answered only after teardown, else None.

    Ordering is load-bearing: pipe requests are polled even while an exit
    dialog is unanswered, so a ShutdownForUpgrade pre-empts it; forwarded
    opens are dispatched via _spawn_open, never awaited, so a hung open can't
    stall answering a concurrent ShutdownForUpgrade in its 20s window."""
    exit_confirm: "queue.Queue[bool]" = queue.Queue()
    while True:
        while True:
            try:
                action = tray_actions.get_nowait()
            except queue.Empty:
                break
            if action is tray.TrayAction.OPEN:
                _spawn_open(port, protocol.OpenHome(), paths)
            elif action is tray.TrayAction.OPEN_FILE:
                _spawn_file_dialog(port, paths)
            elif action is tray.TrayAction.OPEN_LOGS:
                _spawn_call(paths, lambda: ui.open_path(paths.logs))
            elif action is tray.TrayAction.DEFAULT_APPS:
                _spawn_call(paths, ui.open_default_apps)
            elif action is tray.TrayAction.CHECK_UPDATES:
                _spawn_update_check(paths)
            elif action is tray.TrayAction.EXIT:
                _spawn_exit_confirm(exit_confirm)

        try:
            if exit_confirm.get_nowait():  # False (user clicked No) just resumes
                return _ExitReason.TRAY_EXIT, None
        except queue.Empty:
            pass

        try:
            request = pipe_requests.get(timeout=0.25)
        except queue.Empty:
            request = None

        if request is not None:
            if isinstance(request.command, protocol.ShutdownForUpgrade):
                return _ExitReason.UPGRADE, request.response
            _spawn_open(port, request.command, paths, request.response)
        elif process.wait(0):
            return _ExitReason.SERVER_DIED, None


def _teardown(
    reason: _ExitReason,
    upgrade_response: "queue.Queue[int] | None",
    *,
    port: int,
    token: str,
    paths: DesktopPaths,
    job: Job,
    process,
    inst: instance.PrimaryInstance,
    pipe_requests: "queue.Queue[instance.Request]",
    pipe_thread: threading.Thread,
    tray_handle: tray.TrayHandle,
) -> None:
    """The one teardown path. Invariants, identical for every reason: the
    tray stops first, then the pipe, then the server — and job.close() runs
    exactly once before this function returns or raises."""
    tray_handle.stop()

    if reason is not _ExitReason.UPGRADE:
        # UPGRADE: the pipe thread is parked in its ShutdownForUpgrade
        # handler waiting on upgrade_response and stops itself once that is
        # answered below; a self-sent stop now would just time out against
        # the busy pipe.
        _stop_pipe(inst, pipe_requests, pipe_thread)

    error: SupervisorStoppedError | None = None
    if reason is _ExitReason.SERVER_DIED:
        # The child itself already died, but any Job-assigned grandchildren
        # must not be left to PyHANDLE GC timing — close deterministically
        # before raising.
        job.close()
        error = SupervisorStoppedError("Python server exited unexpectedly")
    else:
        _safe_graceful_shutdown(port, token, paths)
        exited = process.wait(int(_SHUTDOWN_TIMEOUT_S * 1000))
        job.close()
        if not exited and not process.wait(int(_SHUTDOWN_TIMEOUT_S * 1000)):
            error = SupervisorStoppedError("Python process tree did not stop")

    if upgrade_response is not None:
        upgrade_response.put(1 if error else 0)
        pipe_thread.join(timeout=10)

    if error:
        raise error


def _stop_pipe(
    inst: instance.PrimaryInstance,
    pipe_requests: "queue.Queue[instance.Request]",
    pipe_thread: threading.Thread,
) -> None:
    """Stop the pipe server under one overall deadline. `stop_serving()` is
    retried each iteration because a single poke can race `_serve_pipe`'s stop
    check. Requests drained here are real external traffic: a genuine
    ShutdownForUpgrade gets status 0 (teardown completes right after), all
    else gets 1. At the deadline, give up — the daemon thread is reaped at
    process exit, and blocking longer would stall job.close()."""
    deadline = time.monotonic() + _STOP_PIPE_TIMEOUT_S
    while pipe_thread.is_alive() and time.monotonic() < deadline:
        inst.stop_serving()
        try:
            request = pipe_requests.get(timeout=0.25)
        except queue.Empty:
            continue
        is_shutdown = isinstance(request.command, protocol.ShutdownForUpgrade)
        request.response.put(0 if is_shutdown else 1)
    pipe_thread.join(timeout=max(0.0, deadline - time.monotonic()))


def _safe_open(port: int, command: protocol.Command, paths: DesktopPaths) -> bool:
    try:
        _open_command(port, command)
        return True
    except OSError as error:
        paths.log(f"open failed: {error}")
        return False


def _safe_call(paths: DesktopPaths, action) -> None:
    """Run a tray action (Open logs, Default apps, ...) without letting an
    `OSError` from `os.startfile` unwind `run()` — a routine tray click must
    never tear down the already-running Job-owned server (same rule as
    `_safe_open`, generalized to actions that aren't a browser open)."""
    try:
        action()
    except OSError as error:
        paths.log(f"tray action failed: {error}")


def _safe_graceful_shutdown(port: int, token: str, paths: DesktopPaths) -> None:
    try:
        _graceful_shutdown(port, token)
    except OSError as error:
        paths.log(f"graceful shutdown request failed: {error}")


def _spawn_open(
    port: int,
    command: protocol.Command,
    paths: DesktopPaths,
    response: "queue.Queue[int] | None" = None,
) -> None:
    """Open on a dedicated thread: `_open_command` can hang (Path.exists on a
    disconnected UNC path, os.startfile on a stuck association) and the loop
    must stay free to answer a concurrent ShutdownForUpgrade. A forwarded
    command's response is put from this worker once the open finishes, so a
    hung open times out that client, not the loop."""

    def worker():
        ok = _safe_open(port, command, paths)
        if response is not None:
            response.put(0 if ok else 1)

    threading.Thread(target=worker, daemon=True, name="fused-render-open").start()


def _spawn_update_check(paths: DesktopPaths) -> None:
    """Run the manual update check off the loop — it blocks on network I/O and
    its own dialogs. update.check is self-guarded, so a second click while a
    check is already running is a no-op."""
    threading.Thread(
        target=update.check, args=(paths,), daemon=True, name="fused-render-update"
    ).start()


def _spawn_call(paths: DesktopPaths, action) -> None:
    """Run a tray action (Open logs, Default apps, ...) on a dedicated thread
    (same idiom as `_spawn_open`). These land on the loop thread that services
    tray actions AND pipe_requests, so they must not block it: on Linux
    `ui.open_path` -> `_xdg_open` waits up to `_XDG_OPEN_WAIT_S` on the child, so
    a slow/foreground `xdg-open` would otherwise stall the loop — and a
    concurrent ShutdownForUpgrade must still be answered inside the pipe
    server's 20s window, or the upgrade fails even though the app is running
    fine (the same reasoning as `_spawn_exit_confirm`). `_safe_call` keeps an
    `OSError` from the action from unwinding this daemon worker."""

    def worker():
        _safe_call(paths, action)

    threading.Thread(
        target=worker, daemon=True, name="fused-render-tray-action"
    ).start()


def _spawn_file_dialog(port: int, paths: DesktopPaths) -> None:
    """Open-file dialog on a dedicated thread (bugbot #2): the supervisor's
    main loop has no message pump, but the backend's file dialog pumps its own
    internally — it just must not run on a thread another blocking call owns.
    The thread + lock idiom lives here (platform-neutral); the actual native
    dialog is `ui.pick_file()`. The lock drops a second click while one dialog
    is already open rather than stacking dialogs."""
    if not _dialog_lock.acquire(blocking=False):
        return

    def worker():
        try:
            path = ui.pick_file()
        finally:
            _dialog_lock.release()
        if path:
            _safe_open(port, protocol.Open(path), paths)

    threading.Thread(target=worker, daemon=True, name="fused-render-open-file").start()


def _spawn_exit_confirm(results: "queue.Queue[bool]") -> None:
    """Exit confirmation on a dedicated thread: the modal MessageBoxW must not
    block the loop, so a ShutdownForUpgrade arriving while it's up is still
    answered in the 20s window (the upgrade wins — the process exits and the
    dialog vanishes with it). A separate lock from the file dialog, so an open
    file picker doesn't make Exit unclickable."""
    if not _exit_dialog_lock.acquire(blocking=False):
        return

    def worker():
        try:
            results.put(ui.confirm_exit())
        finally:
            _exit_dialog_lock.release()

    threading.Thread(target=worker, daemon=True, name="fused-render-exit-confirm").start()


def _open_command(port: int, command: protocol.Command) -> None:
    if isinstance(command, protocol.Open):
        if command.path.lower().startswith("fused-render:"):
            # Lazy import: deeplink pulls in fastapi, which the supervisor must
            # not pay for on the common file-open path (only a fused-render:
            # link ever reaches here). deeplink.is_launch_url is the STRICT,
            # action-only `fused-render://launch` matcher (D128) — distinct from
            # _view_url_codec.is_launch_url below, which matches any URL.
            from fused_render import deeplink

            if deeplink.is_launch_url(command.path):
                # D128 launch: by this point in the primary the server is up
                # (and a forwarded Open reaches here only after startup). The
                # server-down banner just needs the app running — the page that
                # linked here reconnects on its own, so open NO tab (matching
                # macOS app.py and Windows winopen._open).
                return
        if is_launch_url(command.path):
            # A `fused-render:` deep link or a `file:`/scheme:// URL: there is
            # no file to stat — route through the shared helper (deep link ->
            # /clone?src=, file: -> /view). The file: decode here supersedes the
            # branch's old _normalize_target: open_target_path (deeplink.py's
            # sibling in _view_url_codec) decodes the URI to a filesystem path.
            url = open_target_url(port, command.path)
        else:
            url = _view_url(port, Path(command.path))
    elif isinstance(command, protocol.OpenHome):
        url = f"http://127.0.0.1:{port}/"
    else:
        return  # StartInBackground / ShutdownForUpgrade carry no browser action
    _open_browser(url)


def _view_url(port: int, path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    fs_path = str(path if path.is_absolute() else Path.cwd() / path)
    return view_url(port, fs_path)


def _open_browser(url: str) -> None:
    if "FUSED_RENDER_SUPERVISOR_NO_BROWSER" in os.environ:
        return
    ui.open_url(url)


def _launch_token() -> str:
    return secrets.token_hex(32)  # 32 bytes == 256 bits, 64 hex chars


def _spawn_desktop_integration(paths: DesktopPaths) -> None:
    """Kick off best-effort user-level desktop self-integration on a daemon
    thread (Linux only; a no-op backend hook elsewhere). It shells out to
    update-mime-database / update-desktop-database, which must never delay the
    server/tray coming up — and it must never break startup, so any failure is
    swallowed inside the backend (log-and-continue) with a final guard here."""
    integrate = getattr(_backend, "integrate", None)
    if integrate is None:
        return

    def worker():
        try:
            integrate(paths)
        except Exception as error:  # noqa: BLE001 - integration is never fatal to startup
            paths.log(f"desktop integration failed: {error}")

    threading.Thread(target=worker, daemon=True, name="fused-render-integrate").start()


def _absolute_command(command: protocol.Command) -> protocol.Command:
    # A URL payload (a `fused-render:` deep link, a `file:`/scheme:// URI) is
    # not a filesystem path — prepending cwd would mangle it. Only resolve a
    # genuine relative path. (A `file:` URI is left intact here and decoded
    # later by open_target_url in _open_command, superseding the branch's old
    # _normalize_target file:// handling.)
    if (
        isinstance(command, protocol.Open)
        and not is_launch_url(command.path)
        and not Path(command.path).is_absolute()
    ):
        return protocol.Open(str(Path.cwd() / command.path))
    return command


def _current_python_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _available_port() -> int:
    """Prefer the branch's stable base port (1777 for a shipped build) so the
    server keeps the same origin across restarts — open browser tabs stay valid
    and per-origin localStorage (e.g. the onboarding tour's "seen" flag) isn't
    wiped every launch. Scan a small range for the first free port; fall back to
    an OS-assigned ephemeral port only if the whole range is taken."""
    base = branch_port()
    for port in range(base, base + 11):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # SO_REUSEADDR so this probe agrees with uvicorn's own bind (see
            # cli._port_free): a base port merely lingering in TIME_WAIT after a
            # clean shutdown reads as free, so a quick relaunch keeps 1777
            # instead of drifting to 1778. A live listener still fails the bind,
            # so a real collision is still caught.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(job: Job, paths: DesktopPaths, port: int, token: str):
    python_dir = _current_python_dir()
    python = Path(sys.executable)
    if not python.is_file():
        raise FileNotFoundError(f"private Python runtime not found: {python}")
    arguments = ["-I", "-m", "fused_render.cli", "serve", "--no-browser", "--port", str(port)]
    return job.spawn(
        python,
        arguments,
        environment=paths.child_environment(_INSTANCE_ID, token, python_dir),
        output=paths.logs / "server-console.log",
    )


def _wait_until_ready(process, port: int, token: str, timeout_s: float) -> None:
    def _fail_fast_if_child_died() -> None:
        if process.wait(0):
            raise RuntimeError("Python server failed during startup")

    if not desktop_probe.wait_until_ready(
        port, token, timeout_s, instance_id=_INSTANCE_ID, on_poll=_fail_fast_if_child_died
    ):
        raise TimeoutError("Python server did not become ready")


def _start_ready_server(paths: DesktopPaths, token: str):
    last_error = None
    for _attempt in range(3):
        port = _available_port()
        job = Job()
        process = None
        try:
            # job.spawn must be inside this try too: its failure modes
            # (pywintypes.error, FileNotFoundError) would otherwise escape the
            # loop and skip job.close() below.
            process = _start_server(job, paths, port, token)
            _wait_until_ready(process, port, token, _READY_TIMEOUT_S)
            return job, process, port
        except (OSError, RuntimeError, TimeoutError, *_backend.SPAWN_ERRORS) as error:
            job.close()
            if process is not None:
                process.wait(5000)
            last_error = error
    raise last_error or RuntimeError("Python server failed to start")


def _graceful_shutdown(port: int, token: str) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/desktop/shutdown",
        method="POST",
        headers={"X-Fused-Desktop-Token": token, "Content-Length": "0"},
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        if resp.status != 200:
            raise OSError("Python server rejected graceful shutdown")
