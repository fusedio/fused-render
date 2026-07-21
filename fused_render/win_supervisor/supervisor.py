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
from pathlib import Path

import pythoncom
import pywintypes
import win32con
import win32gui

from fused_render.win_supervisor import instance, protocol, startup, tray
from fused_render.win_supervisor.job import Job
from fused_render.win_supervisor.paths import DesktopPaths

_INSTANCE_ID = "desktop-v1"
_READY_TIMEOUT_S = 20.0
_SHUTDOWN_TIMEOUT_S = 5.0

_dialog_lock = threading.Lock()


def run(initial: protocol.Command) -> None:
    initial = _absolute_command(initial)
    names = instance.InstanceNames.current_user()
    inst = instance.acquire(names)

    if isinstance(inst, instance.SecondaryInstance):
        inst.send(initial, 75.0)
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

    if not _safe_open(port, initial, paths):
        pass  # already logged by _safe_open — a failed initial open must
        # never tear down the already-running Job-owned server (bugbot #7).

    try:
        login_enabled = startup.enabled()
    except OSError as error:
        paths.log(f"could not read sign-in setting, defaulting to off: {error}")
        login_enabled = False

    tray_handle = tray.start(port, login_enabled, paths)
    tray_actions = tray_handle.actions
    pipe_requests: "queue.Queue[instance.Request]" = queue.Queue()
    pipe_thread = inst.serve(pipe_requests)

    stop_pipe_locally = False
    shutdown_response = None
    server_died = False

    running = True
    while running:
        while True:
            try:
                action = tray_actions.get_nowait()
            except queue.Empty:
                break
            if action is tray.TrayAction.OPEN:
                _safe_open(port, protocol.OpenHome(), paths)
            elif action is tray.TrayAction.OPEN_FILE:
                _spawn_file_dialog(port, paths)
            elif action is tray.TrayAction.OPEN_LOGS:
                _open_path(paths.logs)
            elif action is tray.TrayAction.DEFAULT_APPS:
                _open_uri("ms-settings:defaultapps")
            elif action is tray.TrayAction.EXIT:
                if _confirm_exit():
                    tray_handle.stop()
                    _safe_graceful_shutdown(port, token, paths)
                    stop_pipe_locally = True
                    running = False
                    break
        if not running:
            break

        try:
            request = pipe_requests.get(timeout=0.25)
        except queue.Empty:
            request = None

        if request is not None:
            if isinstance(request.command, protocol.ShutdownForUpgrade):
                tray_handle.stop()
                _safe_graceful_shutdown(port, token, paths)
                shutdown_response = request.response
                running = False
            else:
                ok = _safe_open(port, request.command, paths)
                request.response.put(0 if ok else 1)
        elif process.wait(0):
            server_died = True
            break

    if server_died:
        _stop_pipe(inst, pipe_requests, pipe_thread)
        raise RuntimeError("Python server exited unexpectedly")

    if stop_pipe_locally:
        _stop_pipe(inst, pipe_requests, pipe_thread)

    teardown_error = None
    if not process.wait(int(_SHUTDOWN_TIMEOUT_S * 1000)):
        job.close()
        if not process.wait(int(_SHUTDOWN_TIMEOUT_S * 1000)):
            teardown_error = RuntimeError("Python process tree did not stop")
    else:
        job.close()

    if shutdown_response is not None:
        shutdown_response.put(1 if teardown_error else 0)
        pipe_thread.join(timeout=10)

    if teardown_error:
        raise teardown_error


def _stop_pipe(
    inst: instance.PrimaryInstance,
    pipe_requests: "queue.Queue[instance.Request]",
    pipe_thread: threading.Thread,
) -> None:
    client = inst.client()

    def send_stop():
        try:
            client.send(protocol.ShutdownForUpgrade(), 5.0)
        except (OSError, TimeoutError):
            pass

    sender = threading.Thread(target=send_stop, daemon=True)
    sender.start()
    while True:
        try:
            request = pipe_requests.get(timeout=5)
        except queue.Empty:
            # The self-sent ShutdownForUpgrade never arrived (pipe server
            # stuck/gone) — teardown must proceed regardless, or job.close()
            # never runs and __main__ surfaces a false start-failure dialog.
            break
        is_shutdown = isinstance(request.command, protocol.ShutdownForUpgrade)
        request.response.put(0 if is_shutdown else 1)
        if is_shutdown:
            break
    sender.join(timeout=5)
    pipe_thread.join(timeout=5)


def _safe_open(port: int, command: protocol.Command, paths: DesktopPaths) -> bool:
    try:
        _open_command(port, command)
        return True
    except OSError as error:
        paths.log(f"open failed: {error}")
        return False


def _safe_graceful_shutdown(port: int, token: str, paths: DesktopPaths) -> None:
    try:
        _graceful_shutdown(port, token)
    except OSError as error:
        paths.log(f"graceful shutdown request failed: {error}")


def _spawn_file_dialog(port: int, paths: DesktopPaths) -> None:
    """Open-file common dialog on a dedicated thread (bugbot #2): the
    supervisor's main loop has no Win32 message pump, but
    `GetOpenFileNameW` pumps its own internally — it just must not run on
    a thread another blocking call owns, and needs an STA COM apartment
    for shell extensions. The lock drops a second click while one dialog
    is already open rather than stacking dialogs."""
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
    absolute = path if path.is_absolute() else Path.cwd() / path
    raw = str(absolute)
    normalized = raw.replace("\\", "/") if len(raw) > 1 and raw[1] == ":" else raw
    segments = "/".join(
        _percent_encode(segment)
        for segment in normalized.strip("/").split("/")
        if segment
    )
    return f"http://127.0.0.1:{port}/view/{segments}"


def _percent_encode(value: str) -> str:
    encoded = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch.isalnum() and ch.isascii() or ch in "-._~":
            encoded.append(ch)
        else:
            encoded.append(f"%{byte:02X}")
    return "".join(encoded)


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
        process = _start_server(job, paths, port, token)
        try:
            _wait_until_ready(process, port, token, _READY_TIMEOUT_S)
            return job, process, port
        except (RuntimeError, TimeoutError) as error:
            job.close()
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
