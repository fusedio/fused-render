"""Linux tray pure helpers: icon → ARGB32 big-endian pixmap, com.canonical.
dbusmenu layout, and event → TrayAction dispatch. All bus-free.

Plus a DBUS_SESSION_BUS_ADDRESS-gated integration test of `run()`'s bring-up
and teardown against a real session bus (headless CI without a bus skips it)."""
import os
import queue
import threading
import time

import pytest

pytest.importorskip("PIL")

from PIL import Image

from fused_render.supervisor import tray
from fused_render.supervisor._linux import tray as linux_tray


def _stub_handle():
    return tray.TrayHandle(actions=queue.Queue())


class _Paths:
    def __init__(self):
        self.messages = []

    def log(self, message):
        self.messages.append(message)


# --- _icon_pixmap ------------------------------------------------------------


def test_icon_pixmap_dimensions_and_length():
    w, h, data = linux_tray._icon_pixmap(linux_tray._ICON_PATH)
    assert w == linux_tray._ICON_SIZE
    assert h == linux_tray._ICON_SIZE
    assert len(data) == w * h * 4


def test_pack_argb32_is_big_endian():
    # The pure packer does ONLY the RGBA→ARGB32 (network byte order) repack: each
    # source pixel R,G,B,A becomes the four bytes A,R,G,B, no recolor.
    size = linux_tray._ICON_SIZE
    image = Image.new("RGBA", (size, size), (10, 20, 30, 40))

    w, h, data = linux_tray._pack_argb32(image)

    assert (w, h) == (size, size)
    assert data[:4] == bytes([40, 10, 20, 30])  # A, R, G, B
    assert data == bytes([40, 10, 20, 30]) * (w * h)


# --- _tint_white -------------------------------------------------------------


def test_tint_white_maps_glyph_to_white_preserving_alpha():
    # opaque black glyph → opaque white; transparent bg → stays transparent;
    # a mid-alpha pixel keeps its exact alpha. Only the alpha channel survives.
    image = Image.new("RGBA", (1, 3))
    image.putpixel((0, 0), (0, 0, 0, 255))    # opaque glyph pixel
    image.putpixel((0, 1), (0, 0, 0, 0))      # transparent background
    image.putpixel((0, 2), (12, 34, 56, 128))  # mid-alpha pixel

    tinted = linux_tray._tint_white(image)

    assert tinted.getpixel((0, 0)) == (255, 255, 255, 255)
    assert tinted.getpixel((0, 1))[3] == 0
    assert tinted.getpixel((0, 2)) == (255, 255, 255, 128)


def test_icon_pixmap_emits_white_on_transparent():
    # End-to-end on the real menubar-template.png: the tinted pixmap must have at
    # least one fully-transparent pixel and at least one fully-opaque WHITE pixel.
    w, h, data = linux_tray._icon_pixmap(linux_tray._ICON_PATH)
    assert (w, h) == (linux_tray._ICON_SIZE, linux_tray._ICON_SIZE)
    assert len(data) == w * h * 4

    pixels = [tuple(data[i : i + 4]) for i in range(0, len(data), 4)]  # A, R, G, B
    assert any(pixel[0] == 0 for pixel in pixels)  # a fully-transparent pixel
    assert (255, 255, 255, 255) in pixels  # a fully-opaque white pixel (A,R,G,B)


# --- _menu_layout ------------------------------------------------------------


def _labels(children):
    return [c["properties"].get("label") for c in children]


@pytest.mark.parametrize("login_enabled", [False, True])
def test_menu_layout_structure(login_enabled):
    root_id, children = linux_tray._menu_layout(login_enabled, port=1777)
    assert isinstance(root_id, int)

    labels = _labels(children)
    assert "Open FusedRender" in labels
    assert "Open file..." in labels
    assert "Open logs" in labels
    assert "Exit" in labels
    # Windows-only item is dropped on Linux.
    assert "Default apps..." not in labels

    by_label = {c["properties"].get("label"): c for c in children}

    running = by_label[f"Running on port {1777}"]
    assert running["properties"]["enabled"] is False

    toggle = by_label["Start at sign in"]
    assert toggle["properties"]["toggle-type"] == "checkmark"
    assert toggle["properties"]["toggle-state"] == (1 if login_enabled else 0)

    separators = [c for c in children if c["properties"].get("type") == "separator"]
    assert len(separators) == 2


# --- root node children-display ----------------------------------------------


def test_root_props_advertise_submenu():
    # waybar only renders the root's children as a menu when the root node
    # advertises children-display == "submenu"; without it the menu is empty.
    assert linux_tray._ROOT_PROPS["children-display"] == "submenu"
    assert "children-display" in linux_tray._MENU_PROP_TYPES


def test_root_props_encode_to_string_variant():
    pytest.importorskip("dbus_fast")  # _props_to_variants imports Variant lazily
    variants = linux_tray._props_to_variants(linux_tray._ROOT_PROPS)
    variant = variants["children-display"]
    assert variant.signature == "s"
    assert variant.value == "submenu"


# --- _dispatch_event ---------------------------------------------------------


@pytest.mark.parametrize(
    "item_id, action",
    [
        (linux_tray._ID_OPEN, tray.TrayAction.OPEN),
        (linux_tray._ID_OPEN_FILE, tray.TrayAction.OPEN_FILE),
        (linux_tray._ID_OPEN_LOGS, tray.TrayAction.OPEN_LOGS),
        (linux_tray._ID_EXIT, tray.TrayAction.EXIT),
    ],
)
def test_dispatch_event_enqueues_action(item_id, action):
    handle = _stub_handle()
    state = tray._State(login_enabled=False)
    paths = _Paths()
    calls = []

    linux_tray._dispatch_event(item_id, state, handle, paths, calls.append)

    assert handle.actions.get_nowait() is action
    assert calls == []  # no login write for plain actions


def test_dispatch_event_login_toggle_flips_on_success():
    handle = _stub_handle()
    state = tray._State(login_enabled=False)
    paths = _Paths()
    calls = []

    linux_tray._dispatch_event(linux_tray._ID_LOGIN, state, handle, paths, calls.append)

    assert calls == [True]  # set_enabled(not state.login_enabled)
    assert state.login_enabled is True
    assert handle.actions.empty()  # inline toggle, not a queued action


def test_dispatch_event_login_toggle_keeps_state_on_oserror():
    handle = _stub_handle()
    state = tray._State(login_enabled=False)
    paths = _Paths()

    def failing(_value):
        raise OSError("cannot write autostart entry")

    linux_tray._dispatch_event(linux_tray._ID_LOGIN, state, handle, paths, failing)

    assert state.login_enabled is False  # unchanged on failure
    assert paths.messages  # logged


# --- D-Bus bring-up / teardown (integration, needs a session bus) ------------


def _name_has_owner(name: str) -> bool:
    import asyncio

    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    async def check():
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        try:
            introspection = await bus.introspect(
                "org.freedesktop.DBus", "/org/freedesktop/DBus"
            )
            obj = bus.get_proxy_object(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", introspection
            )
            dbus_iface = obj.get_interface("org.freedesktop.DBus")
            return await dbus_iface.call_name_has_owner(name)
        finally:
            bus.disconnect()

    return asyncio.run(check())


def _wait(predicate, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(
    not os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
    reason="no D-Bus session bus (headless CI)",
)
def test_run_brings_up_and_tears_down_sni():
    pytest.importorskip("dbus_fast")

    handle = tray.TrayHandle(actions=queue.Queue())
    state = tray._State(login_enabled=False)
    paths = _Paths()
    name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    thread = threading.Thread(
        target=linux_tray.run, args=(1777, state, handle, paths), daemon=True
    )
    thread.start()
    try:
        assert _wait(lambda: _name_has_owner(name)), "SNI bus name never appeared"
    finally:
        handle.stop()

    assert _wait(lambda: not _name_has_owner(name)), "SNI bus name never released"
    thread.join(timeout=5)
    assert not thread.is_alive()
