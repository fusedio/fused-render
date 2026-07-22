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
from fused_render._view_url_codec import view_url
from fused_render.desktop_probe import DESKTOP_INSTANCE_ID as _INSTANCE_ID
from fused_render.supervisor import _backend, protocol, tray
from fused_render.supervisor.paths import DesktopPaths

# The single live backend (win32 today). Every platform-specific call in this
# module goes through these names; see supervisor/_backend.py for the contract.
Job = _backend.Job
instance = _backend.instance
startup = _backend.startup
ui = _backend.ui

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
        except instance.CommandRejected:
            # A healthy primary answered and rejected the specific command.
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
    token = _launch_token()
    job, process, port = _start_ready_server(paths, token)

    # The initial open is dispatched off-thread like every other open: a hung
    # Path.exists()/os.startfile (disconnected UNC path) must not stall run()
    # here, before the tray and pipe server exist — the supervisor would be
    # alive but undiscoverable and unable to answer ShutdownForUpgrade. A
    # failed open is logged by _safe_open inside the worker and deliberately
    # ignored: it must never tear down the Job-owned server (bugbot #7).
    _spawn_open(port, initial, paths)

    try:
        login_enabled = startup.enabled()
    except OSError as error:
        paths.log(f"could not read sign-in setting, defaulting to off: {error}")
        login_enabled = False

    tray_handle = tray.start(port, login_enabled, paths)
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
    session, then reports WHY it ended — and nothing else. All teardown
    lives in _teardown(), so every exit reason goes through the identical
    sequence instead of each hand-rolling its own. The second element is
    the installer's pending response queue for UPGRADE (it must be answered
    only after teardown finishes, with its true status), else None.

    Ordering per iteration is load-bearing: pipe requests are checked even
    while an exit-confirm dialog sits unanswered (confirmed is absent, not
    blocking), so ShutdownForUpgrade pre-empts the dialog — it returns
    immediately and the dangling daemon-thread dialog vanishes at exit.
    Forwarded opens are likewise dispatched via _spawn_open, never awaited
    here — their pipe response is put from the worker once the open
    actually finishes, so a hung Path.exists() (a disconnected UNC path)
    can't stall this loop's ability to answer a concurrent
    ShutdownForUpgrade inside the pipe server's 20s window."""
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
                _safe_call(paths, lambda: ui.open_path(paths.logs))
            elif action is tray.TrayAction.DEFAULT_APPS:
                _safe_call(paths, lambda: ui.open_uri("ms-settings:defaultapps"))
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
    retried each iteration because a single poke can race the gap between
    `_serve_pipe`'s stop check and its `CreateNamedPipe`. Requests drained
    here are genuine external traffic (the self-unblock never enters the
    queue — it's rejected with status 1 inside `_serve_pipe`): a real
    ShutdownForUpgrade gets 0 — teardown is in progress and completes right
    after this returns — everything else gets 1. At the deadline, give up:
    the pipe thread is a daemon, process exit reaps it, and blocking any
    longer would stall job.close() indefinitely (bugbot: an uncapped flood
    of forwarded commands during teardown used to hang this forever)."""
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
    """Open on a dedicated thread (same idiom as `_spawn_file_dialog`):
    `_open_command` can hang the caller — `Path.exists()` stalls on a
    disconnected UNC path, `os.startfile` on a stuck shell association —
    and the loop must keep servicing pipe_requests so a concurrent
    ShutdownForUpgrade is answered inside `_serve_pipe`'s 20s window. For a
    pipe-forwarded command the response is put from this worker once the
    open actually finishes: a hung open makes THAT client's pipe call time
    out client-side, not the whole loop."""

    def worker():
        ok = _safe_open(port, command, paths)
        if response is not None:
            response.put(0 if ok else 1)

    threading.Thread(target=worker, daemon=True, name="fused-render-open").start()


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
    """Exit confirmation on a dedicated thread (same idiom as
    `_spawn_file_dialog`): `MessageBoxW` is modal and would otherwise block
    the loop that services tray actions and pipe requests — if a
    ShutdownForUpgrade arrives while the dialog is up, it must still be
    answered within the pipe server's 20s window, or the upgrade fails even
    though the app is running fine (just waiting on an unanswered dialog).
    The upgrade wins that race: it gets serviced immediately and the process
    exits, so the still-open dialog (on its own daemon thread) simply
    vanishes — no attempt to dismiss it first. The lock drops a second Exit
    click while one confirmation is already open, same as the file dialog
    (kept as a separate lock: sharing `_dialog_lock` would make Exit
    silently unclickable whenever a file picker happens to be open)."""
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


def _absolute_command(command: protocol.Command) -> protocol.Command:
    if isinstance(command, protocol.Open) and not Path(command.path).is_absolute():
        return protocol.Open(str(Path.cwd() / command.path))
    return command


def _current_python_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _available_port() -> int:
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
            # _start_server (job.spawn) must be inside this try too: its
            # failure modes (pywintypes.error from AssignProcessToJobObject/
            # ResumeThread, FileNotFoundError if the runtime is missing) are
            # neither RuntimeError nor TimeoutError, so they used to escape
            # this loop entirely and skip job.close() below.
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
