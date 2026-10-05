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

#: A timed-out `open` is not by itself proof the host is dead (the first
#: WebKitGTK view on a cold cache can outrun the caller's budget) — only this
#: many IN A ROW, with the process still alive and the socket still
#: connecting, means something.
_MAX_CONSECUTIVE_TIMEOUTS = 3

#: After `quit`'s reply, how long to let the host exit on its own (it saves
#: window frames on the way out) before `stop()` falls back to `_close_job`'s
#: SIGTERM/SIGKILL, which would otherwise race that save.
_QUIT_EXIT_WAIT_S = 2.0


def preference_enabled(state_dir: Path) -> bool:
    """`native_windows_enabled` from prefs.json (default ON, opt-out), read
    without importing the server's prefs module (it pulls in fastapi)."""
    try:
        data = json.loads((Path(state_dir) / "prefs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("native_windows_enabled") is False)


def wanted() -> bool:
    """Whether a host could even make sense in this process's environment —
    module-level so a caller (core.py's server-environment hook) can ask
    without building a `WindowHost` just to reach this check."""
    # NO_BROWSER is the supervisor tests' (and headless installs') "never
    # show anything" switch; a session without a display cannot host GTK.
    if "FUSED_RENDER_SUPERVISOR_NO_BROWSER" in os.environ:
        return False
    if os.environ.get("FUSED_RENDER_NO_NATIVE_WINDOWS"):
        return False
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def server_environment(paths: DesktopPaths) -> dict[str, str]:
    """Env for the SERVER process: where to reach the host. Empty when no
    host will run, so the server installs no hooks and behaves as before."""
    if not wanted():
        return {}
    return {ipc.ENV_SOCKET: str(ipc.socket_path(paths.runtime))}


class WindowHost:
    def __init__(self, paths: DesktopPaths, port: int, *, job_factory=Job,
                 start_timeout: float = _START_TIMEOUT_S) -> None:
        self._paths = paths
        self._port = port
        self._job_factory = job_factory
        self._timeout = start_timeout
        self._job = None
        self._process = None
        self._logged = False
        self._consecutive_timeouts = 0
        self.available = False
        self.log = paths.log
        self.socket = ipc.socket_path(paths.runtime)

    def _log_once(self, message: str) -> None:
        if not self._logged:
            self._logged = True
            self.log(f"native windows unavailable, using browser tabs: {message}")

    def start(self) -> bool:
        if not wanted():
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
        self._process = process
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if ipc.ping(self.socket):
                self.available = True
                self._consecutive_timeouts = 0
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
        self._process = None
        if job is not None:
            try:
                job.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass

    def open(self, url: str, activation_token: str | None = None) -> bool:
        """True if the host showed ``url``; False means "open it in a browser".
        ``activation_token`` (an `XDG_ACTIVATION_TOKEN`/`DESKTOP_STARTUP_ID`
        from the launch that triggered this open, when there was one) rides
        along so the host can hand focus-stealing prevention over to the
        compositor instead of just popping up unfocused; omitted when there
        is none, so the IPC payload is unchanged for every other open."""
        if not self.available:
            return False
        payload = {"cmd": "open", "url": url}
        if activation_token:
            payload["activation_token"] = activation_token
        try:
            reply = ipc.request(self.socket, payload, _OPEN_TIMEOUT_S)
        except ipc.HostUnavailable as error:
            self._open_failed(error)
            return False
        self._consecutive_timeouts = 0
        return bool(reply.get("ok"))

    def _open_failed(self, error: "ipc.HostUnavailable") -> None:
        """One `open` could not be answered. Only give up the whole host when
        there is real evidence it is gone: the process exited, or the socket
        itself refused the connection (ECONNREFUSED/ENOENT — `request`'s
        `from error` keeps that original OSError as `__cause__`). A bare
        timeout with the process still running (the first WebKitGTK view on a
        cold cache can outrun the caller's budget) just falls back to a
        browser tab for this one open — unless it keeps happening."""
        if self._process is not None and self._process.wait(0):
            self._give_up(f"the window host process exited: {error}")
            return
        if isinstance(error.__cause__, (ConnectionRefusedError, FileNotFoundError)):
            self._give_up(f"the window host socket is gone: {error}")
            return
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts >= _MAX_CONSECUTIVE_TIMEOUTS:
            self._give_up(
                f"the window host timed out {self._consecutive_timeouts} times in a row "
                f"({error})"
            )
            return
        self._log_once(f"the window host timed out answering open ({error}); "
                        "opening a browser tab for this one")

    def stop(self) -> None:
        if self._job is None:
            self.available = False
            return
        if self.available:
            try:
                ipc.request(self.socket, {"cmd": "quit"}, 1.0)
            except ipc.HostUnavailable:
                pass
            else:
                # Give the host a bounded window to exit on its own — it
                # saves window frames on the way out — before falling through
                # to _close_job's SIGTERM/SIGKILL, which would otherwise race
                # that save.
                if self._process is not None:
                    self._process.wait(int(_QUIT_EXIT_WAIT_S * 1000))
        self.available = False
        self._close_job()


def _last_line(path: Path) -> str:
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return ""
    lines = [ln for ln in lines if ln]
    return lines[-1] if lines else ""
