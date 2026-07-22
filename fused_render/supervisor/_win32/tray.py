"""Windows tray backend — pystray over Shell_NotifyIcon. The concrete `run()`
lifted verbatim from the old `tray._run` (feat/windows-desktop-foundation, PR
#162); the platform-neutral `TrayAction`/`TrayHandle`/`start()`/retry contract
lives in `fused_render.supervisor.tray`, imported here at call time.

Bugbot fixes carried over from the Rust review (PR #162):
- #1/#3 "Start at sign in" checkbox desync / registry failure kills the
  supervisor: pystray's `checked=` menu-item argument is a *callable*,
  re-evaluated every time the menu opens, so the checkbox is always derived
  from `_State.login_enabled` — there is no "created once, stale forever"
  state to desync. `on_toggle_login` only flips `_State.login_enabled` on a
  *successful* registry write; a failure is caught, logged via
  `paths.log(...)`, and simply doesn't change state (which reads as the
  checkbox "reverting"). The supervisor process itself is never at risk:
  pystray dispatches each menu action on its own thread, and every handler
  here is also wrapped so an unexpected exception can't propagate.
"""
from __future__ import annotations

from pathlib import Path

import pystray
from PIL import Image

from fused_render.supervisor import _backend
from fused_render.supervisor.paths import DesktopPaths
from fused_render.supervisor.tray import TrayAction, TrayHandle, _State

_ICON_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "fused-render.ico"


def run(port: int, state: _State, handle: TrayHandle, paths: DesktopPaths) -> None:
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
            _backend.startup.set_enabled(want)
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
