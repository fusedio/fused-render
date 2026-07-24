"""Tray icon — platform-neutral seam over the per-OS tray backends.

Port of windows/supervisor/src/tray.rs (feat/windows-desktop-foundation, PR
#162). This module owns everything that is *not* platform-specific — the
`TrayAction` queue contract, `_State`, `TrayHandle` (icon lifetime + stop
signal), `start()` and its retry-with-backoff loop. The one function that talks
to a concrete tray, `_run()`, is a thin dispatcher that lazily imports the
matching backend for `sys.platform` (`._win32.tray` on Windows via pystray,
`._linux.tray` on Linux via StatusNotifierItem over D-Bus) and forwards to its
`run(port, state, handle, paths)`.

The lazy import is deliberate: importing this module on Linux must never import
pystray (Windows-only), and the Windows backend must never load off-Windows.
The backend imports the shared types (`TrayAction`, `_State`, `TrayHandle`)
back from here at call time, when this module is fully initialized — no cycle.
Keeping the whole `TrayHandle`/`start()`/retry contract here means `core.py`
does not change when a new platform backend is added, mirroring `_backend`.
"""
from __future__ import annotations

import importlib
import queue
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum, auto

from fused_render.supervisor.paths import DesktopPaths

_RETRY_START = 0.5
_RETRY_CAP = 30.0
_LOG_AFTER_ATTEMPTS = 10


class TrayAction(Enum):
    OPEN = auto()
    OPEN_FILE = auto()
    OPEN_LOGS = auto()
    DEFAULT_APPS = auto()
    CHECK_UPDATES = auto()
    EXIT = auto()


@dataclass
class _State:
    login_enabled: bool
    # The version of a background-discovered update, or None. Read by the
    # backend's menu-label callable; set via TrayHandle.set_update_available.
    available_update: str | None = None


@dataclass
class TrayHandle:
    """Returned by `start()`. `actions` is the queue the supervisor's main
    loop drains; `stop()` removes the tray icon (Windows' NIM_DELETE) when
    the supervisor has decided to actually exit — without it, the icon
    lingers in the notification area (ghost icon) until Explorer next
    refreshes, since nothing else tells pystray to tear it down. `_stopped`
    also ends the retry loop in `start()`: if `stop()` lands while the loop
    is backing off between attempts (no icon exists yet), the event keeps a
    later retry from showing a brand-new icon after shutdown began."""

    actions: "queue.Queue[TrayAction]"
    # 0 or 1 backend icon handle — a pystray.Icon on Windows, a stop-shim on
    # Linux. `stop()` needs `.stop()` on it; `set_update_available` additionally
    # needs `.update_menu()` (only ever called on Windows, where `update` lives).
    _current_icon: list = field(default_factory=list)
    _stopped: threading.Event = field(default_factory=threading.Event)
    _state: "_State | None" = None

    def stop(self) -> None:
        self._stopped.set()
        for icon in self._current_icon:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001 - best-effort, process is exiting anyway
                pass

    def set_update_available(self, version: str) -> None:
        """Flag a background-discovered update so the tray item reads "Install
        update X" instead of "Check for updates". Idempotent — a re-check of the
        same version won't re-draw the menu."""
        if self._state.available_update == version:
            return
        self._state.available_update = version
        for icon in self._current_icon:
            icon.update_menu()


def start(port: int, login_enabled: bool, paths: DesktopPaths) -> TrayHandle:
    """Spawns the tray on its own daemon thread and returns immediately — the
    Job/Python server lifecycle must never depend on tray success. If
    Explorer's notification-area infrastructure isn't up yet (launched from
    the sign-in Run key before Explorer's tray is ready), retry with backoff
    until it succeeds: the icon shows up late, never "not at all.\""""
    state = _State(login_enabled=login_enabled)
    handle = TrayHandle(actions=queue.Queue(), _state=state)

    def loop():
        delay = _RETRY_START
        attempt = 0
        while not handle._stopped.is_set():
            attempt += 1
            try:
                _run(port, state, handle, paths)
                return  # icon.stop() was called deliberately (supervisor exiting)
            except Exception as error:  # noqa: BLE001 - must never kill the supervisor
                if attempt == _LOG_AFTER_ATTEMPTS:
                    paths.log(f"tray icon still not up after {attempt} attempts, retrying: {error}")
            if handle._stopped.wait(delay):
                return
            delay = min(delay * 2, _RETRY_CAP)

    threading.Thread(target=loop, daemon=True, name="fused-render-tray").start()
    return handle


def _run(port: int, state: _State, handle: TrayHandle, paths: DesktopPaths) -> None:
    """Dispatch to the per-OS tray backend, lazily. Kept a module attribute so
    the retry-loop test can monkeypatch `tray._run`. The backend's `run()` has
    the identical signature and the identical "returns on deliberate stop,
    raises to trigger a retry" contract the retry loop in `start()` expects."""
    if sys.platform == "win32":
        backend_name = "fused_render.supervisor._win32.tray"
    elif sys.platform.startswith("linux"):
        backend_name = "fused_render.supervisor._linux.tray"
    else:
        raise RuntimeError(f"no tray backend for {sys.platform!r}")
    backend = importlib.import_module(backend_name)
    backend.run(port, state, handle, paths)
