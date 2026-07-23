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

# The macOS "template" diamond: a black glyph on a transparent background. macOS
# tints it white in the menu bar; waybar/StatusNotifier has no such template
# auto-tinting, so _icon_pixmap recolors it to white itself (see _tint_white).
_ICON_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "menubar-template.png"

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


def _pack_argb32(image) -> tuple[int, int, bytes]:
    """Repack an RGBA Pillow image to the SNI IconPixmap payload: ARGB32 in
    **network byte order** (big-endian), i.e. each pixel is the four bytes
    A, R, G, B. Pillow gives RGBA, so we reorder per pixel. No recolor."""
    rgba = image.tobytes()  # R, G, B, A per pixel
    argb = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        argb[i] = a
        argb[i + 1] = r
        argb[i + 2] = g
        argb[i + 3] = b
    return image.width, image.height, bytes(argb)


def _tint_white(image):
    """Map every pixel of an RGBA image to white (255, 255, 255) while keeping
    its original alpha. Turns the black-on-transparent macOS template glyph into
    a white-on-transparent one. White (255,255,255) is symmetric under any R/B
    swap, so the emitted pixmap is correct regardless of channel order."""
    from PIL import Image

    alpha = image.getchannel("A")
    white = Image.new("RGBA", image.size, (255, 255, 255, 0))
    white.putalpha(alpha)
    return white


def _icon_pixmap(path) -> tuple[int, int, bytes]:
    """Load `path`, tint it white, scale to `_ICON_SIZE`, and repack to the SNI
    IconPixmap ARGB32 payload. The white glyph assumes a dark bar: waybar/SNI has
    no template auto-tinting like macOS, so an untinted glyph would be invisible
    (or a colored blob) on a typically dark panel — we emit white ourselves."""
    from PIL import Image

    image = Image.open(path).convert("RGBA")
    image = _tint_white(image).resize((_ICON_SIZE, _ICON_SIZE))
    return _pack_argb32(image)


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
        _item(
            _ID_RUNNING,
            {"label": f"Running on port {port}", "enabled": False, "visible": True},
        ),
        _item(_ID_SEP1, {"type": "separator", "visible": True}),
        _item(_ID_OPEN, {"label": "Open FusedRender", "enabled": True, "visible": True}),
        _item(_ID_OPEN_FILE, {"label": "Open file...", "enabled": True, "visible": True}),
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


# --- D-Bus layer -------------------------------------------------------------
#
# Thin glue over the pure helpers above. Everything below is imported/used only
# on the tray daemon thread inside run(); the dbus_fast import is deferred so
# `import fused_render.supervisor._linux.tray` stays cheap and dependency-light
# for the pure-helper tests.

_SNI_INTERFACE = "org.kde.StatusNotifierItem"
_MENU_INTERFACE = "com.canonical.dbusmenu"
_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_SNI_PATH = "/StatusNotifierItem"
_MENU_PATH = "/MenuBar"

# dbusmenu property → D-Bus variant type. Only the properties _menu_layout emits.
_MENU_PROP_TYPES = {
    "label": "s",
    "enabled": "b",
    "visible": "b",
    "type": "s",
    "toggle-type": "s",
    "toggle-state": "i",
    "children-display": "s",
}

# Root-node properties returned by GetLayout. `children-display: submenu` is the
# signal StatusNotifier hosts (waybar, KDE) require before they render the root's
# children as a menu; without it the tray shows an empty context menu.
_ROOT_PROPS = {"children-display": "submenu"}


def _props_to_variants(properties: dict) -> dict:
    from dbus_fast import Variant

    return {key: Variant(_MENU_PROP_TYPES[key], value) for key, value in properties.items()}


def _make_interfaces(port, state, handle, paths, set_enabled, revision_ref):
    """Build the two ServiceInterface instances. Defined inside a function so the
    dbus_fast import (and the ServiceInterface subclassing it drives) happens
    only on the tray thread, never at module import."""
    from dbus_fast import Variant
    from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method, signal

    icon_pixmap = _icon_pixmap(_ICON_PATH)

    class StatusNotifierItem(ServiceInterface):
        def __init__(self):
            super().__init__(_SNI_INTERFACE)

        @dbus_property(access=PropertyAccess.READ)
        def Category(self) -> "s":  # noqa: N802,F821
            return "ApplicationStatus"

        @dbus_property(access=PropertyAccess.READ)
        def Id(self) -> "s":  # noqa: N802,F821
            return "FusedRender"

        @dbus_property(access=PropertyAccess.READ)
        def Title(self) -> "s":  # noqa: N802,F821
            return "FusedRender"

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":  # noqa: N802,F821
            return "Active"

        @dbus_property(access=PropertyAccess.READ)
        def IconName(self) -> "s":  # noqa: N802,F821
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def IconPixmap(self) -> "a(iiay)":  # noqa: N802,F821
            width, height, data = icon_pixmap
            return [[width, height, data]]

        @dbus_property(access=PropertyAccess.READ)
        def ToolTip(self) -> "(sa(iiay)ss)":  # noqa: N802,F821
            return ["", [], "FusedRender", f"Running on port {port}"]

        @dbus_property(access=PropertyAccess.READ)
        def ItemIsMenu(self) -> "b":  # noqa: N802,F821
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Menu(self) -> "o":  # noqa: N802,F821
            return _MENU_PATH

        @method()
        def Activate(self, x: "i", y: "i"):  # noqa: N802,F821
            _dispatch_event(_ID_OPEN, state, handle, paths, set_enabled)

        @method()
        def SecondaryActivate(self, x: "i", y: "i"):  # noqa: N802,F821
            _dispatch_event(_ID_OPEN, state, handle, paths, set_enabled)

    class DBusMenu(ServiceInterface):
        def __init__(self):
            super().__init__(_MENU_INTERFACE)

        @dbus_property(access=PropertyAccess.READ)
        def Version(self) -> "u":  # noqa: N802,F821
            return 3

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":  # noqa: N802,F821
            return "normal"

        @method()
        def GetLayout(
            self, parentId: "i", recursionDepth: "i", propertyNames: "as"  # noqa: N803,F821
        ) -> "u(ia{sv}av)":  # noqa: F821,N802
            # Spec: GetLayout has TWO out-arguments — revision (u) and the layout
            # node (ia{sv}av). Emitting them as one wrapping struct makes strict
            # clients (libdbusmenu-gtk / waybar) reject the reply and draw an
            # empty menu, so the out-signature stays unwrapped and the body
            # returns the two values as a flat list.
            _root_id, children = _menu_layout(state.login_enabled, port)
            child_nodes = [
                Variant(
                    "(ia{sv}av)",
                    [child["id"], _props_to_variants(child["properties"]), []],
                )
                for child in children
            ]
            if parentId == _ROOT_ID:
                layout = [_ROOT_ID, _props_to_variants(_ROOT_PROPS), child_nodes]
            else:
                # Flat menu: a non-root parent is one of the leaf children (no
                # grandchildren). Return that node, or an empty node for an id
                # the layout does not know.
                match = next((c for c in children if c["id"] == parentId), None)
                if match is None:
                    layout = [parentId, {}, []]
                else:
                    layout = [parentId, _props_to_variants(match["properties"]), []]
            return [revision_ref[0], layout]

        @method()
        def GetGroupProperties(
            self, ids: "ai", propertyNames: "as"  # noqa: N803,F821
        ) -> "a(ia{sv})":  # noqa: F821,N802
            _root_id, children = _menu_layout(state.login_enabled, port)
            return [
                [child["id"], _props_to_variants(child["properties"])]
                for child in children
                if not ids or child["id"] in ids
            ]

        @method()
        def GetProperty(self, id: "i", name: "s") -> "v":  # noqa: A002,N802,N803,F821
            _root_id, children = _menu_layout(state.login_enabled, port)
            for child in children:
                if child["id"] == id and name in child["properties"]:
                    return _props_to_variants(child["properties"])[name]
            return Variant("s", "")

        def _apply_event(self, item_id, event_id):
            # Shared body for the singular Event and the batched EventGroup: only
            # a "clicked" activates the item; a login click also bumps the
            # revision and emits LayoutUpdated so the host redraws the checkmark.
            if event_id != "clicked":
                return
            _dispatch_event(item_id, state, handle, paths, set_enabled)
            if item_id == _ID_LOGIN:
                revision_ref[0] += 1
                self.LayoutUpdated(revision_ref[0], _ROOT_ID)

        @method()
        def Event(
            self, id: "i", eventId: "s", data: "v", timestamp: "u"  # noqa: A002,N803,F821
        ):  # noqa: N802
            self._apply_event(id, eventId)

        @method()
        def EventGroup(
            self, events: "a(isvu)"  # noqa: N803,F821
        ) -> "ai":  # noqa: N802
            # libdbusmenu delivers ALL clicks here (never the singular Event)
            # once the server advertises Version >= 3. Each (id, eventId, data,
            # timestamp) is applied exactly as Event would (data is a Variant and
            # is ignored, same as Event). Ids not in the menu are collected and
            # returned as idErrors; [] means every id was valid.
            _root_id, children = _menu_layout(state.login_enabled, port)
            known = {_root_id, *(child["id"] for child in children)}
            id_errors = []
            for item_id, event_id, _data, _timestamp in events:
                if item_id not in known:
                    id_errors.append(item_id)
                    continue
                self._apply_event(item_id, event_id)
            return id_errors

        @method()
        def AboutToShow(self, id: "i") -> "b":  # noqa: A002,N802,F821
            return False

        @method()
        def AboutToShowGroup(
            self, ids: "ai"  # noqa: N803,F821
        ) -> "aiai":  # noqa: N802
            # Batched counterpart to AboutToShow, used by Version >= 3 clients.
            # First array: ids whose layout needs updating before showing (none
            # for us, matching AboutToShow's False). Second array: unknown ids.
            _root_id, children = _menu_layout(state.login_enabled, port)
            known = {_root_id, *(child["id"] for child in children)}
            id_errors = [item_id for item_id in ids if item_id not in known]
            return [[], id_errors]

        @signal()
        def LayoutUpdated(self, revision: "u", parent: "i") -> "ui":  # noqa: N802,F821
            return [revision, parent]

    return StatusNotifierItem(), DBusMenu()


def _should_reregister(name: str, new_owner: str) -> bool:
    """Whether a NameOwnerChanged signal means the StatusNotifierWatcher just
    (re)appeared under a new owner. Registration is with the watcher's CURRENT
    owner, so a restarted host (waybar, the panel) has never heard of us — the
    icon would be gone forever without re-registering. An empty new_owner is
    the watcher vanishing; nothing to register with, and the eventual gain
    fires its own signal."""
    return name == _WATCHER_NAME and bool(new_owner)


async def _register_with_watcher(connection) -> None:
    """Introspect the current StatusNotifierWatcher owner and register our SNI
    with it. Raises when no watcher owns the name (stock GNOME) — initial
    bring-up lets that propagate to tray.start()'s retry loop."""
    introspection = await connection.introspect(_WATCHER_NAME, _WATCHER_PATH)
    watcher = connection.get_proxy_object(
        _WATCHER_NAME, _WATCHER_PATH, introspection
    ).get_interface(_WATCHER_NAME)
    await watcher.call_register_status_notifier_item(_SNI_PATH)


async def _subscribe_watcher_restarts(connection, loop, paths: DesktopPaths) -> None:
    """Subscribe to org.freedesktop.DBus NameOwnerChanged and re-register with
    the StatusNotifierWatcher whenever its name gains a new owner (host
    restart). Failures are logged and ignored — the host announcing itself
    again fires another signal, and the exported services themselves are
    untouched either way."""
    introspection = await connection.introspect(
        "org.freedesktop.DBus", "/org/freedesktop/DBus"
    )
    dbus_iface = connection.get_proxy_object(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", introspection
    ).get_interface("org.freedesktop.DBus")

    async def _reregister():
        try:
            await _register_with_watcher(connection)
        except Exception as error:  # noqa: BLE001 - logged; never tears down the loop
            paths.log(f"tray: re-register with restarted watcher failed: {error}")

    def _on_name_owner_changed(name, old_owner, new_owner):
        # Called on the loop thread by dbus-fast's message handling, so the
        # task can be scheduled directly.
        if _should_reregister(name, new_owner):
            loop.create_task(_reregister())

    dbus_iface.on_name_owner_changed(_on_name_owner_changed)


def run(port: int, state: _State, handle: TrayHandle, paths: DesktopPaths) -> None:
    """Export the SNI + dbusmenu services on a fresh asyncio loop bound to this
    (tray daemon) thread, register with the StatusNotifierWatcher, then run the
    loop until stopped. `call_register_status_notifier_item` raises when no
    watcher owns the name (stock GNOME) — that propagates out to the retry loop
    in `tray.start()`, which backs off and retries so a late waybar is still
    picked up. Teardown is the same `_current_icon` / `stop()` contract the
    Windows backend uses: a stop-shim calls `loop.stop()` cross-thread, the loop
    unwinds, the bus name drops and the icon vanishes."""
    import asyncio
    import os

    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    from fused_render.supervisor import _backend

    set_enabled = _backend.startup.set_enabled
    revision_ref = [0]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    class _StopShim:
        # Mirrors pystray's Icon.stop() shape so TrayHandle.stop() (unchanged)
        # can iterate _current_icon and call .stop() cross-thread.
        def stop(self) -> None:
            loop.call_soon_threadsafe(loop.stop)

    shim = _StopShim()
    bus = None

    async def _bringup() -> "MessageBus":
        connection = await MessageBus(bus_type=BusType.SESSION).connect()
        # From here on the connection is live but run()'s local `bus` is still
        # None, so its finally-block disconnect can't fire — any failure below
        # (no watcher on the bus is the COMMON case on stock GNOME, retried
        # forever by tray.start()) must disconnect here or every retry leaks a
        # bus connection.
        try:
            sni, menu = _make_interfaces(port, state, handle, paths, set_enabled, revision_ref)
            connection.export(_SNI_PATH, sni)
            connection.export(_MENU_PATH, menu)
            await connection.request_name(f"org.kde.StatusNotifierItem-{os.getpid()}-1")
            # Raises if no StatusNotifier host is running → out to the retry loop.
            await _register_with_watcher(connection)
            # Registration binds to the watcher's CURRENT owner; a restarted
            # host needs a fresh RegisterStatusNotifierItem or the icon is
            # gone forever. Watch for the name changing owners.
            await _subscribe_watcher_restarts(connection, loop, paths)
        except BaseException:
            try:
                connection.disconnect()
            except Exception:  # noqa: BLE001 - best-effort; the bring-up error wins
                pass
            raise
        return connection

    try:
        bus = loop.run_until_complete(_bringup())
        handle._current_icon.append(shim)
        if handle._stopped.is_set():  # stop() raced bring-up: don't start the loop
            return
        loop.run_forever()
    finally:
        if shim in handle._current_icon:
            handle._current_icon.remove(shim)
        if bus is not None:
            try:
                bus.disconnect()  # drops the bus name → icon vanishes
            except Exception:  # noqa: BLE001 - best-effort, thread is ending
                pass
        loop.close()
