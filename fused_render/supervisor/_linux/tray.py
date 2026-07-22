"""Linux tray backend — StatusNotifierItem + com.canonical.dbusmenu over D-Bus.

The Linux counterpart to `_win32/tray.py`. Where Windows drives Shell_NotifyIcon
through pystray, Linux exports the freedesktop/KDE StatusNotifierItem interface
(plus its dbusmenu) so any StatusNotifier host — waybar, KDE's panel, GNOME's
AppIndicator extension — renders the icon. No X11/XEmbed, so it works on Wayland.

This file is split into two layers so the logic is testable without a bus:

- Pure helpers (this section): `_icon_pixmap` (PNG/ICO → ARGB32 pixmap),
  `_menu_layout` (the dbusmenu tree), and `_dispatch_event` (menu id → the same
  `TrayAction` queue the Windows backend feeds, or the inline login toggle).
- The D-Bus glue (`run()` and the ServiceInterface classes) is thin and calls
  into the helpers above.

The shared `TrayAction`/`_State`/`TrayHandle` types come from
`fused_render.supervisor.tray`, imported at call time (no import cycle).
"""
from __future__ import annotations

from pathlib import Path

from fused_render.supervisor.paths import DesktopPaths
from fused_render.supervisor.tray import TrayAction, TrayHandle, _State

_ICON_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "fused-render.ico"

# Panel-tray render size. Hosts scale as needed; a single 22px pixmap is the
# conventional freedesktop tray size and keeps the ARGB payload small.
_ICON_SIZE = 22

# Stable dbusmenu item ids. The host echoes these back in Event, so they must be
# fixed. 0 is the (invisible) root the host asks for in GetLayout.
_ROOT_ID = 0
_ID_OPEN = 1
_ID_OPEN_FILE = 2
_ID_SEP1 = 3
_ID_RUNNING = 4
_ID_OPEN_LOGS = 5
_ID_LOGIN = 6
_ID_SEP2 = 7
_ID_EXIT = 8

# Menu ids that map straight to a queued TrayAction. The login id is handled
# inline (see _dispatch_event), and "Default apps..." is Windows-only so it is
# never built into the Linux menu — TrayAction.DEFAULT_APPS is simply unused
# here, so core.py needs no change.
_ACTION_BY_ID: dict[int, TrayAction] = {
    _ID_OPEN: TrayAction.OPEN,
    _ID_OPEN_FILE: TrayAction.OPEN_FILE,
    _ID_OPEN_LOGS: TrayAction.OPEN_LOGS,
    _ID_EXIT: TrayAction.EXIT,
}


def _icon_pixmap(path) -> tuple[int, int, bytes]:
    """Load `path`, scale to `_ICON_SIZE`, and repack to the SNI IconPixmap
    payload: ARGB32 in **network byte order** (big-endian), i.e. each pixel is
    the four bytes A, R, G, B. Pillow gives RGBA, so we reorder per pixel."""
    from PIL import Image

    image = Image.open(path).convert("RGBA").resize((_ICON_SIZE, _ICON_SIZE))
    rgba = image.tobytes()  # R, G, B, A per pixel
    argb = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        argb[i] = a
        argb[i + 1] = r
        argb[i + 2] = g
        argb[i + 3] = b
    return _ICON_SIZE, _ICON_SIZE, bytes(argb)


def _item(item_id: int, properties: dict) -> dict:
    """One dbusmenu layout node. `children` is always present (empty for leaves)
    so the D-Bus GetLayout serializer has a uniform shape to walk."""
    return {"id": item_id, "properties": properties, "children": []}


def _menu_layout(login_enabled: bool, port: int) -> tuple[int, list]:
    """Build the com.canonical.dbusmenu layout: the root id plus its children,
    mirroring the Windows menu minus the Windows-only "Default apps..." item.
    `toggle-state` tracks `login_enabled` so the host draws the live checkmark.
    """
    children = [
        _item(_ID_OPEN, {"label": "Open FusedRender", "enabled": True, "visible": True}),
        _item(_ID_OPEN_FILE, {"label": "Open file...", "enabled": True, "visible": True}),
        _item(_ID_SEP1, {"type": "separator", "visible": True}),
        _item(
            _ID_RUNNING,
            {"label": f"Running on port {port}", "enabled": False, "visible": True},
        ),
        _item(_ID_OPEN_LOGS, {"label": "Open logs", "enabled": True, "visible": True}),
        _item(
            _ID_LOGIN,
            {
                "label": "Start at sign in",
                "enabled": True,
                "visible": True,
                "toggle-type": "checkmark",
                "toggle-state": 1 if login_enabled else 0,
            },
        ),
        _item(_ID_SEP2, {"type": "separator", "visible": True}),
        _item(_ID_EXIT, {"label": "Exit", "enabled": True, "visible": True}),
    ]
    return _ROOT_ID, children


def _dispatch_event(item_id, state: _State, handle: TrayHandle, paths: DesktopPaths, set_enabled) -> None:
    """Handle a menu click. Plain items enqueue their `TrayAction` on the same
    queue the supervisor's run loop drains. The login id flips
    `state.login_enabled` via the injected `set_enabled` (so this is testable
    without `_backend`); an OSError leaves the state unchanged and is logged,
    matching the Windows backend's "revert the checkbox and log" behavior."""
    if item_id == _ID_LOGIN:
        want = not state.login_enabled
        try:
            set_enabled(want)
            state.login_enabled = want
        except OSError as error:
            paths.log(f"could not update sign-in setting: {error}")
        return

    action = _ACTION_BY_ID.get(item_id)
    if action is None:
        return
    try:
        handle.actions.put_nowait(action)
    except Exception:  # noqa: BLE001
        pass
