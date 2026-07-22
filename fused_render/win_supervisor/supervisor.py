"""Supervisor main run loop — port of windows/supervisor/src/supervisor.rs
(feat/windows-desktop-foundation, PR #162).
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from enum import Enum, auto
from pathlib import Path

import pythoncom
import pywintypes
import win32con
import win32gui

from fused_render._view_url_codec import view_url
from fused_render.win_supervisor import instance, protocol, startup, tray, update
from fused_render.win_supervisor.job import Job
from fused_render.win_supervisor.paths import DesktopPaths

_INSTANCE_ID = "desktop-v1"
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
            # A healthy primary rejected the specific command. Swallow it only
            # for an Open (the app started, just not that file); anything else
            # — crucially ShutdownForUpgrade — must re-raise so the installer
            # sees the non-zero exit and knows teardown never happened.
            if isinstance(initial, protocol.Open):
                _report_open_rejected(initial.path)
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
                _safe_call(paths, lambda: _open_path(paths.logs))
            elif action is tray.TrayAction.DEFAULT_APPS:
                _safe_call(paths, lambda: _open_uri("ms-settings:defaultapps"))
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


def _spawn_file_dialog(port: int, paths: DesktopPaths) -> None:
    """Open-file common dialog on a dedicated thread: GetOpenFileNameW pumps
    its own messages but needs its own thread and an STA COM apartment for
    shell extensions. The lock drops a second click while one dialog is
    already open."""
    if not _dialog_lock.acquire(blocking=False):
        return

    def worker():
        path = None
        try:
            pythoncom.CoInitialize()
            try:
                path, _filter_index, _flags = win32gui.GetOpenFileNameW(
                    Filter="All files\0*.*\0\0",
                    Flags=win32con.OFN_FILEMUSTEXIST | win32con.OFN_PATHMUSTEXIST,
                )
            finally:
                pythoncom.CoUninitialize()
        except pywintypes.error:
            path = None
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
            results.put(_confirm_exit())
        finally:
            _exit_dialog_lock.release()

    threading.Thread(target=worker, daemon=True, name="fused-render-exit-confirm").start()


def _confirm_exit() -> bool:
    MB_YESNO = 0x4
    MB_ICONQUESTION = 0x20
    IDYES = 6
    result = ctypes.windll.user32.MessageBoxW(
        0,
        "Stop FusedRender and all running render processes?",
        "Exit FusedRender",
        MB_YESNO | MB_ICONQUESTION,
    )
    return result == IDYES


def _report_open_rejected(path: str) -> None:
    # The primary already logged the underlying reason (paths.log via its
    # own _safe_open) — this is just accurate user-facing feedback for a
    # forwarded open that failed, not a launch failure.
    MB_OK = 0x0
    MB_ICONWARNING = 0x30
    ctypes.windll.user32.MessageBoxW(
        0, f"FusedRender could not open:\n\n{path}", "FusedRender", MB_OK | MB_ICONWARNING
    )


def _open_path(path: Path) -> None:
    os.startfile(str(path))  # noqa: S606 - local admin-installed path, not user input


def _open_uri(uri: str) -> None:
    os.startfile(uri)


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
    os.startfile(url)


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


def _matching_server(port: int, token: str) -> bool:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/config",
        headers={"X-Fused-Desktop-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    instance_info = payload.get("desktop_instance") or {}
    return instance_info.get("id") == _INSTANCE_ID and instance_info.get("token") == token


def _wait_until_ready(process, port: int, token: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.wait(0):
            raise RuntimeError("Python server failed during startup")
        if _matching_server(port, token):
            return
        time.sleep(0.1)
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
        except (OSError, RuntimeError, TimeoutError, pywintypes.error) as error:
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
