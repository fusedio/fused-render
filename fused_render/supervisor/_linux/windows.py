"""Supervisor-side owner of the Linux native-window host (`window_host.py`).

`core.py` asks this for a window and gets True (shown) or False (use the
browser): the host being absent, declining, crashed or never startable all
collapse into False, so the app always has a way to show its UI. The reason is
logged once per session, never per click.

Stdlib only; no GTK is imported here — the host is a separate process.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from fused_render import window_host_ipc as ipc
from fused_render.supervisor._linux.tree import Job
from fused_render.supervisor.paths import DesktopPaths

_START_TIMEOUT_S = 10.0
_OPEN_TIMEOUT_S = ipc.CALLER_TIMEOUT_S
_HOST_MODULE = "fused_render.supervisor._linux.window_host"


def preference_enabled(state_dir: Path) -> bool:
    """`native_windows_enabled` from prefs.json (default ON, opt-out), read
    without importing the server's prefs module (it pulls in fastapi)."""
    try:
        data = json.loads((Path(state_dir) / "prefs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("native_windows_enabled") is False)


class WindowHost:
    def __init__(self, paths: DesktopPaths, port: int, *, job_factory=Job,
                 start_timeout: float = _START_TIMEOUT_S) -> None:
        self._paths = paths
        self._port = port
        self._job_factory = job_factory
        self._timeout = start_timeout
        self._job = None
        self._logged = False
        self.available = False
        self.log = paths.log
        self.socket = ipc.socket_path(paths.runtime)

    def _wanted(self) -> bool:
        # NO_BROWSER is the supervisor tests' (and headless installs') "never
        # show anything" switch; a session without a display cannot host GTK.
        if "FUSED_RENDER_SUPERVISOR_NO_BROWSER" in os.environ:
            return False
        if os.environ.get("FUSED_RENDER_NO_NATIVE_WINDOWS"):
            return False
        return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))

    def server_environment(self) -> dict[str, str]:
        """Env for the SERVER process: where to reach the host. Empty when no
        host will run, so the server installs no hooks and behaves as before."""
        return {ipc.ENV_SOCKET: str(self.socket)} if self._wanted() else {}

    def _log_once(self, message: str) -> None:
        if not self._logged:
            self._logged = True
            self.log(f"native windows unavailable, using browser tabs: {message}")

    def start(self) -> bool:
        if not self._wanted():
            return False
        output = self._paths.logs / "window-host.log"
        try:
            output.unlink()  # so a startup failure reads only its own reason
        except OSError:
            pass
        arguments = ["-I", "-m", _HOST_MODULE,
                     "--port", str(self._port),
                     "--socket", str(self.socket),
                     "--state", str(self._paths.state)]
        if not preference_enabled(self._paths.state):
            arguments.append("--disabled")
        job = self._job_factory()
        self._job = job
        try:
            process = job.spawn(Path(sys.executable), arguments, output=output)
        except Exception as error:  # noqa: BLE001 - any spawn failure means "no host"
            self._give_up(f"could not start the window host: {error}")
            return False
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if ipc.ping(self.socket):
                self.available = True
                return True
            if process.wait(0):
                self._give_up(_last_line(output) or "the window host exited at startup")
                return False
            time.sleep(0.1)
        self._give_up("the window host did not become ready in time")
        return False

    def _give_up(self, reason: str) -> None:
        self.available = False
        self._log_once(reason)
        self._close_job()

    def _close_job(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            try:
                job.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass

    def open(self, url: str) -> bool:
        """True if the host showed ``url``; False means "open it in a browser"."""
        if not self.available:
            return False
        try:
            reply = ipc.request(self.socket, {"cmd": "open", "url": url}, _OPEN_TIMEOUT_S)
        except ipc.HostUnavailable as error:
            self._give_up(f"the window host stopped answering ({error})")
            return False
        return bool(reply.get("ok"))

    def stop(self) -> None:
        if self._job is None:
            self.available = False
            return
        if self.available:
            try:
                ipc.request(self.socket, {"cmd": "quit"}, 1.0)
            except ipc.HostUnavailable:
                pass
        self.available = False
        self._close_job()


def _last_line(path: Path) -> str:
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return ""
    lines = [ln for ln in lines if ln]
    return lines[-1] if lines else ""
