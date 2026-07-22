"""Tray icon — port of windows/supervisor/src/tray.rs (feat/windows-desktop-
foundation, PR #162), using pystray instead of the tray-icon crate.

Bugbot fixes carried over from the Rust review (PR #162):
- #1/#3 "Start at sign in" checkbox desync / registry failure kills the
  supervisor: pystray's `checked=` menu-item argument is a *callable*,
  re-evaluated every time the menu opens, so the checkbox is always derived
  from `_State.login_enabled` — there is no "created once, stale forever"
  state to desync. `_on_toggle_login` only flips `_State.login_enabled` on a
  *successful* registry write; a failure is caught, logged via
  `paths.log(...)`, and simply doesn't change state (which reads as the
  checkbox "reverting"). The supervisor process itself is never at risk:
  pystray dispatches each menu action on its own thread, and every handler
  here is also wrapped so an unexpected exception can't propagate.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import pystray
from PIL import Image

from fused_render.win_supervisor import startup
from fused_render.win_supervisor.paths import DesktopPaths

_RETRY_START = 0.5
_RETRY_CAP = 30.0
_LOG_AFTER_ATTEMPTS = 10

_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "fused-render.ico"


class TrayAction(Enum):
    OPEN = auto()
    OPEN_FILE = auto()
    OPEN_LOGS = auto()
    DEFAULT_APPS = auto()
    EXIT = auto()


@dataclass
class _State:
    login_enabled: bool


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
    _current_icon: list = field(default_factory=list)  # 0 or 1 pystray.Icon
    _stopped: threading.Event = field(default_factory=threading.Event)

    def stop(self) -> None:
        self._stopped.set()
        for icon in self._current_icon:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001 - best-effort, process is exiting anyway
                pass


def start(port: int, login_enabled: bool, paths: DesktopPaths) -> TrayHandle:
    """Spawns the tray on its own daemon thread and returns immediately — the
    Job/Python server lifecycle must never depend on tray success. If
    Explorer's notification-area infrastructure isn't up yet (launched from
    the sign-in Run key before Explorer's tray is ready), retry with backoff
    until it succeeds: the icon shows up late, never "not at all.\""""
    handle = TrayHandle(actions=queue.Queue())
    state = _State(login_enabled=login_enabled)

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
    actions = handle.actions
    image = Image.open(_ICON_PATH)

    def emit(action: TrayAction):
        try:
            actions.put_nowait(action)
        except Exception:  # noqa: BLE001
            pass

    def on_open(icon, item):
        emit(TrayAction.OPEN)

    def on_open_file(icon, item):
        emit(TrayAction.OPEN_FILE)

    def on_open_logs(icon, item):
        emit(TrayAction.OPEN_LOGS)

    def on_default_apps(icon, item):
        emit(TrayAction.DEFAULT_APPS)

    def on_exit(icon, item):
        emit(TrayAction.EXIT)

    def on_toggle_login(icon, item):
        want = not state.login_enabled
        try:
            startup.set_enabled(want)
            state.login_enabled = want
        except OSError as error:
            paths.log(f"could not update sign-in setting: {error}")
        icon.update_menu()

    menu = pystray.Menu(
        pystray.MenuItem("Open FusedRender", on_open, default=True),
        pystray.MenuItem("Open file...", on_open_file),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Running on port {port}", None, enabled=False),
        pystray.MenuItem("Open logs", on_open_logs),
        pystray.MenuItem("Default apps...", on_default_apps),
        pystray.MenuItem(
            "Start at sign in", on_toggle_login, checked=lambda item: state.login_enabled
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit),
    )
    icon = pystray.Icon("FusedRender", image, f"FusedRender (port {port})", menu)
    handle._current_icon.append(icon)
    try:
        if handle._stopped.is_set():
            return
        icon.run()
    finally:
        handle._current_icon.remove(icon)
