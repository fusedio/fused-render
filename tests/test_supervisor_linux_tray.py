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


# --- desktop colour scheme → glyph tint (D135) -------------------------------
#
# The glyph was tinted white unconditionally, which is invisible on a LIGHT
# panel. The tint now follows the XDG portal's org.freedesktop.appearance /
# color-scheme setting: 0 = no preference, 1 = prefer dark, 2 = prefer light.
# Anything that isn't a clear "prefer light" keeps today's white glyph, because
# many desktops expose no portal at all and must see no regression.


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, linux_tray._TINT_WHITE),  # no preference → today's behavior
        (1, linux_tray._TINT_WHITE),  # prefer dark panel → white glyph
        (2, linux_tray._TINT_DARK),  # prefer light panel → dark glyph
        (7, linux_tray._TINT_WHITE),  # value outside the spec → fall back
        (None, linux_tray._TINT_WHITE),  # portal absent / read failed
    ],
)
def test_tint_for_color_scheme(value, expected):
    assert linux_tray._tint_for_color_scheme(value) == expected


@pytest.mark.parametrize(
    "reply, expected",
    [
        (2, 2),
        (None, 0),  # a None reply (nothing read) → no preference
        ("light", 0),  # malformed: a string where an int belongs
        (True, 0),  # a bool is not a colour-scheme value
        (9, 0),  # out of the spec's 0..2 range
    ],
)
def test_color_scheme_from_reply_is_defensive(reply, expected):
    assert linux_tray._color_scheme_from_reply(reply) == expected


def test_color_scheme_from_reply_unwraps_nested_variants():
    # dbus-fast hands back a Variant; some portal builds nest a second one
    # inside it. Both shapes must yield the plain int.
    class FakeVariant:
        def __init__(self, value):
            self.value = value

    assert linux_tray._color_scheme_from_reply(FakeVariant(2)) == 2
    assert linux_tray._color_scheme_from_reply(FakeVariant(FakeVariant(1))) == 1


def test_tint_dark_maps_glyph_to_the_dark_tint_preserving_alpha():
    image = Image.new("RGBA", (1, 3))
    image.putpixel((0, 0), (0, 0, 0, 255))
    image.putpixel((0, 1), (0, 0, 0, 0))
    image.putpixel((0, 2), (12, 34, 56, 128))

    tinted = linux_tray._tint(image, linux_tray._TINT_DARK)

    assert tinted.getpixel((0, 0)) == (*linux_tray._TINT_DARK, 255)
    assert tinted.getpixel((0, 1))[3] == 0
    assert tinted.getpixel((0, 2)) == (*linux_tray._TINT_DARK, 128)


def test_icon_pixmap_dark_panel_variant_is_unchanged():
    # The dark-panel appearance must stay byte-identical to today's: the default
    # tint IS white, and asking for it explicitly gives the same payload.
    assert linux_tray._icon_pixmap(linux_tray._ICON_PATH) == linux_tray._icon_pixmap(
        linux_tray._ICON_PATH, linux_tray._TINT_WHITE
    )


def test_icon_pixmap_light_panel_variant_differs_and_is_dark():
    w, h, data = linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_DARK)
    assert (w, h) == (linux_tray._ICON_SIZE, linux_tray._ICON_SIZE)
    assert data != linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_WHITE)[2]

    pixels = [tuple(data[i : i + 4]) for i in range(0, len(data), 4)]  # A, R, G, B
    assert any(pixel[0] == 0 for pixel in pixels)  # still transparent where it was
    assert (255, *linux_tray._TINT_DARK) in pixels  # a fully-opaque dark pixel


def test_icon_pixmap_caches_each_tint_separately():
    # Bring-up retries forever on a watcher-less session, so the decode must
    # stay cached; it just can't key on the path alone any more.
    linux_tray._icon_pixmap.cache_clear()
    linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_WHITE)
    linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_WHITE)
    linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_DARK)
    info = linux_tray._icon_pixmap.cache_info()
    assert info.misses == 2, "one decode per tint"
    assert info.hits == 1
    assert info.currsize == 2, "both variants must fit — no eviction thrash"


def test_apply_tint_reemits_the_icon_only_when_it_changed():
    # SNI hosts redraw on NewIcon; emitting it needlessly makes the panel
    # churn, and not emitting it at all leaves a stale glyph.
    class FakeItem:
        def __init__(self):
            self.emitted = 0

        def NewIcon(self):  # noqa: N802 - mirrors the D-Bus signal name
            self.emitted += 1

    item = FakeItem()
    pixmap_ref = [linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_WHITE)]

    linux_tray._apply_tint(item, pixmap_ref, linux_tray._TINT_WHITE)
    assert item.emitted == 0, "same tint — nothing to redraw"

    linux_tray._apply_tint(item, pixmap_ref, linux_tray._TINT_DARK)
    assert item.emitted == 1
    assert pixmap_ref[0] == linux_tray._icon_pixmap(
        linux_tray._ICON_PATH, linux_tray._TINT_DARK
    )

    linux_tray._apply_tint(item, pixmap_ref, linux_tray._TINT_WHITE)
    assert item.emitted == 2


def test_apply_tint_never_raises_when_the_signal_fails():
    # Icon tinting is cosmetic: a bus error mid-emit must not propagate into the
    # portal callback and take the tray's event loop with it.
    class ExplodingItem:
        def NewIcon(self):  # noqa: N802
            raise RuntimeError("bus went away")

    pixmap_ref = [linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_WHITE)]
    linux_tray._apply_tint(ExplodingItem(), pixmap_ref, linux_tray._TINT_DARK)
    # The pixmap still advanced — only the redraw notification was lost.
    assert pixmap_ref[0] == linux_tray._icon_pixmap(
        linux_tray._ICON_PATH, linux_tray._TINT_DARK
    )


def test_appearance_setting_changed_only_fires_for_the_colour_scheme_key():
    assert linux_tray._is_color_scheme_change(
        linux_tray._APPEARANCE_NAMESPACE, linux_tray._COLOR_SCHEME_KEY
    )
    assert not linux_tray._is_color_scheme_change(
        linux_tray._APPEARANCE_NAMESPACE, "accent-color"
    )
    assert not linux_tray._is_color_scheme_change("org.gnome.desktop.interface", "color-scheme")


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


def test_menu_layout_includes_uninstall_above_exit():
    # The "Uninstall FusedRender..." item sits just above Exit, set off from the
    # login toggle by the second separator: ..., login, _SEP2, uninstall, exit.
    _root_id, children = linux_tray._menu_layout(False, port=1777)
    labels = _labels(children)
    assert "Uninstall FusedRender…" in labels or "Uninstall FusedRender..." in labels

    uninstall_label = next(l for l in labels if l and l.startswith("Uninstall FusedRender"))
    assert labels.index(uninstall_label) == labels.index("Exit") - 1

    by_id = {c["id"]: c for c in children}
    assert by_id[linux_tray._ID_UNINSTALL]["properties"]["label"] == uninstall_label


def test_menu_layout_status_line_is_first():
    # The greyed status line sits at the very top, set off by a separator.
    _root_id, children = linux_tray._menu_layout(False, port=1777)
    first, second = children[0], children[1]
    assert first["properties"]["label"] == "Running on port 1777"
    assert first["properties"]["enabled"] is False
    assert second["properties"].get("type") == "separator"


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
        (linux_tray._ID_UNINSTALL, tray.TrayAction.UNINSTALL),
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


# --- dbusmenu GetLayout signature / parentId --------------------------------


def _make_menu(port=1777, login_enabled=False):
    """Build the DBusMenu ServiceInterface with bus-free stubs so we can inspect
    its dbus-fast method metadata and call its methods directly."""
    pytest.importorskip("dbus_fast")
    handle = _stub_handle()
    state = tray._State(login_enabled=login_enabled)
    paths = _Paths()
    _sni, menu = linux_tray._make_interfaces(
        port, state, handle, paths, lambda _v: None, [0]
    )
    return menu


def _get_layout_descriptor(menu):
    from dbus_fast.service import ServiceInterface

    for descriptor in ServiceInterface._get_methods(menu):
        if descriptor.name == "GetLayout":
            return descriptor
    raise AssertionError("GetLayout method not found on DBusMenu interface")


def _get_layout_out_signature(menu):
    return _get_layout_descriptor(menu).out_signature


def _call_get_layout(menu, parent_id):
    # dbus_fast's @method() wraps the handler so the bound attribute returns None;
    # the real implementation lives on the descriptor's .fn.
    return _get_layout_descriptor(menu).fn(menu, parent_id, -1, [])


def test_get_layout_declares_two_out_args_not_one_struct():
    # The com.canonical.dbusmenu spec declares GetLayout with TWO out-arguments:
    #   revision: u, layout: (ia{sv}av)
    # i.e. out-signature "u(ia{sv}av)". Wrapping both in a single top-level struct
    # ("(u(ia{sv}av))") makes strict clients (libdbusmenu-gtk / waybar) reject the
    # reply and render an empty menu. The body already returns [revision, layout].
    menu = _make_menu()
    assert _get_layout_out_signature(menu) == "u(ia{sv}av)"


def test_get_layout_root_returns_revision_and_children():
    menu = _make_menu()
    revision, layout = _call_get_layout(menu, linux_tray._ROOT_ID)
    assert isinstance(revision, int)
    node_id, _props, children = layout
    assert node_id == linux_tray._ROOT_ID
    assert len(children) == 9  # the nine menu items under the root


def test_get_layout_nonzero_parent_returns_matching_node():
    # A non-zero parentId must return that node, not the root. The menu is flat,
    # so the matched leaf has no children of its own.
    menu = _make_menu()
    _revision, layout = _call_get_layout(menu, linux_tray._ID_EXIT)
    node_id, props, children = layout
    assert node_id == linux_tray._ID_EXIT
    assert children == []
    assert props["label"].value == "Exit"


def test_get_layout_unknown_parent_returns_empty_node():
    menu = _make_menu()
    unknown = 999
    _revision, layout = _call_get_layout(menu, unknown)
    node_id, props, children = layout
    assert node_id == unknown
    assert props == {}
    assert children == []


# --- dbusmenu EventGroup / AboutToShowGroup (v3 batched methods) -------------
#
# libdbusmenu clients set group_events=TRUE when the server advertises
# Version >= 3 and then deliver EVERY click via EventGroup (never the singular
# Event) and every show hook via AboutToShowGroup. Without these two methods the
# menu items are inert (each real click dies with UnknownMethod).


def _make_menu_ctx(port=1777, login_enabled=False):
    """Like `_make_menu` but exposes the stub state/handle/set_enabled recorder
    and revision_ref so dispatch side effects can be asserted bus-free."""
    pytest.importorskip("dbus_fast")
    handle = _stub_handle()
    state = tray._State(login_enabled=login_enabled)
    paths = _Paths()
    calls = []
    revision_ref = [0]
    _sni, menu = linux_tray._make_interfaces(
        port, state, handle, paths, calls.append, revision_ref
    )
    return menu, state, handle, paths, calls, revision_ref


def _descriptor(menu, name):
    from dbus_fast.service import ServiceInterface

    for descriptor in ServiceInterface._get_methods(menu):
        if descriptor.name == name:
            return descriptor
    raise AssertionError(f"{name} method not found on DBusMenu interface")


def _clicked(item_id):
    from dbus_fast import Variant

    # One (id:i, eventId:s, data:v, timestamp:u) tuple, a "clicked" event.
    return [item_id, "clicked", Variant("s", ""), 0]


def _event(item_id, event_id):
    from dbus_fast import Variant

    return [item_id, event_id, Variant("s", ""), 0]


def test_event_group_declares_group_signature():
    menu = _make_menu()
    descriptor = _descriptor(menu, "EventGroup")
    assert descriptor.in_signature == "a(isvu)"
    assert descriptor.out_signature == "ai"


def test_about_to_show_group_declares_signature():
    menu = _make_menu()
    descriptor = _descriptor(menu, "AboutToShowGroup")
    assert descriptor.in_signature == "ai"
    assert descriptor.out_signature == "aiai"


def test_event_group_dispatches_clicked_action():
    # A batched "clicked" for a plain action id enqueues that TrayAction, exactly
    # as the singular Event would.
    menu, _state, handle, _paths, _calls, _rev = _make_menu_ctx()
    id_errors = _descriptor(menu, "EventGroup").fn(menu, [_clicked(linux_tray._ID_EXIT)])
    assert handle.actions.get_nowait() is tray.TrayAction.EXIT
    assert id_errors == []


def test_event_group_login_toggle_flips_and_emits_layout_updated():
    # The login id flips state inline (via set_enabled) and bumps the revision +
    # emits LayoutUpdated, mirroring the singular Event's login handling.
    menu, state, _handle, _paths, calls, revision_ref = _make_menu_ctx(login_enabled=False)
    _descriptor(menu, "EventGroup").fn(menu, [_clicked(linux_tray._ID_LOGIN)])
    assert calls == [True]
    assert state.login_enabled is True
    assert revision_ref[0] == 1  # bumped for the LayoutUpdated emission


def test_event_group_returns_empty_for_all_known_ids():
    menu, _state, handle, _paths, _calls, _rev = _make_menu_ctx()
    id_errors = _descriptor(menu, "EventGroup").fn(
        menu, [_clicked(linux_tray._ID_OPEN), _clicked(linux_tray._ID_OPEN_FILE)]
    )
    assert id_errors == []
    # Both known clicks dispatched.
    assert handle.actions.get_nowait() is tray.TrayAction.OPEN
    assert handle.actions.get_nowait() is tray.TrayAction.OPEN_FILE


def test_event_group_reports_unknown_ids():
    menu, _state, handle, _paths, _calls, _rev = _make_menu_ctx()
    unknown = 999
    id_errors = _descriptor(menu, "EventGroup").fn(
        menu, [_clicked(linux_tray._ID_OPEN), _clicked(unknown)]
    )
    assert id_errors == [unknown]
    # The known id still dispatched; the unknown one was skipped, not dispatched.
    assert handle.actions.get_nowait() is tray.TrayAction.OPEN
    assert handle.actions.empty()


def test_event_group_ignores_non_clicked_events():
    # hovered/opened/closed must not activate an item.
    menu, state, handle, _paths, calls, _rev = _make_menu_ctx()
    id_errors = _descriptor(menu, "EventGroup").fn(
        menu, [_event(linux_tray._ID_EXIT, "hovered"), _event(linux_tray._ID_LOGIN, "hovered")]
    )
    assert id_errors == []  # both ids are known
    assert handle.actions.empty()  # no action queued for a hover
    assert calls == []  # no login write for a hover
    assert state.login_enabled is False


def test_about_to_show_group_known_ids_returns_two_empty_arrays():
    menu = _make_menu()
    updates_needed, id_errors = _descriptor(menu, "AboutToShowGroup").fn(
        menu, [linux_tray._ROOT_ID, linux_tray._ID_EXIT]
    )
    assert updates_needed == []
    assert id_errors == []


def test_about_to_show_group_reports_unknown_ids():
    menu = _make_menu()
    unknown = 999
    updates_needed, id_errors = _descriptor(menu, "AboutToShowGroup").fn(
        menu, [linux_tray._ROOT_ID, unknown]
    )
    assert updates_needed == []
    assert id_errors == [unknown]


# --- bring-up failure must not leak the connected bus (bus-free) -------------
#
# `_bringup` connects first, then exports/registers; any of the post-connect
# steps can raise (no watcher on the bus is the common case, out to the retry
# loop in tray.start()). run()'s local `bus` is still None at that point, so its
# finally-block disconnect never fires — _bringup itself must disconnect the
# connection it opened before re-raising, or every retry leaks a bus connection.


class _FailingConnection:
    """Fake dbus-fast connection: bring-up succeeds through request_name, then
    the watcher introspect raises (exactly what a watcher-less session does)."""

    def __init__(self):
        self.disconnected = False

    def export(self, path, interface):
        pass

    async def request_name(self, name):
        pass

    async def introspect(self, name, path):
        raise RuntimeError("no StatusNotifierWatcher on the bus")

    def disconnect(self):
        self.disconnected = True


class _FakeMessageBus:
    last_connection = None

    def __init__(self, bus_type=None):
        self._connection = _FailingConnection()
        _FakeMessageBus.last_connection = self._connection

    async def connect(self):
        return self._connection


@pytest.mark.skipif(
    not __import__("sys").platform.startswith("linux"),
    reason="run() imports the Linux supervisor backend",
)
def test_run_disconnects_bus_when_bringup_fails_after_connect(monkeypatch):
    pytest.importorskip("dbus_fast")
    import dbus_fast.aio

    monkeypatch.setattr(dbus_fast.aio, "MessageBus", _FakeMessageBus)
    _FakeMessageBus.last_connection = None

    handle = _stub_handle()
    state = tray._State(login_enabled=False)
    paths = _Paths()

    with pytest.raises(RuntimeError):
        linux_tray.run(1777, state, handle, paths)

    connection = _FakeMessageBus.last_connection
    assert connection is not None  # bring-up did connect
    assert connection.disconnected is True  # ...and cleaned up before re-raising


# --- watcher restart re-registration (bus-free) -------------------------------
#
# SNI registration is with the CURRENT StatusNotifierWatcher owner; when the
# host (waybar, the panel) restarts, the watcher name gets a new owner that has
# never heard of us — without re-registering, the tray icon is gone forever
# while run()'s loop spins happily. The bus glue subscribes to
# org.freedesktop.DBus NameOwnerChanged and re-calls
# RegisterStatusNotifierItem when the watcher name gains a new owner.


def test_should_reregister_only_on_watcher_gaining_an_owner():
    watcher = linux_tray._WATCHER_NAME
    assert linux_tray._should_reregister(watcher, ":1.99") is True  # new owner
    assert linux_tray._should_reregister(watcher, "") is False  # watcher vanished
    assert linux_tray._should_reregister("org.example.Other", ":1.99") is False


class _RecordingConnection:
    """Fake dbus-fast connection for the registration/subscription helpers:
    records RegisterStatusNotifierItem calls and captured NameOwnerChanged
    handlers instead of talking to a bus."""

    def __init__(self):
        self.registered = []
        self.handlers = []
        self.fail_introspect = False

    async def introspect(self, name, path):
        if self.fail_introspect:
            raise RuntimeError("introspect failed")
        return f"introspection:{name}"

    def get_proxy_object(self, name, path, introspection):
        connection = self

        class _Object:
            def get_interface(self, interface_name):
                if interface_name == linux_tray._WATCHER_NAME:
                    class _Watcher:
                        async def call_register_status_notifier_item(self, item_path):
                            connection.registered.append(item_path)

                    return _Watcher()

                class _DBus:
                    def on_name_owner_changed(self, handler):
                        connection.handlers.append(handler)

                return _DBus()

        return _Object()


def test_register_with_watcher_calls_register():
    import asyncio

    connection = _RecordingConnection()
    asyncio.run(linux_tray._register_with_watcher(connection))
    assert connection.registered == [linux_tray._SNI_PATH]


def test_watcher_restart_triggers_reregistration():
    import asyncio

    connection = _RecordingConnection()
    paths = _Paths()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            linux_tray._subscribe_watcher_restarts(connection, loop, paths)
        )
        assert len(connection.handlers) == 1  # subscribed to NameOwnerChanged
        handler = connection.handlers[0]

        # A restarted host: the watcher name gains a NEW owner → re-register.
        handler(linux_tray._WATCHER_NAME, ":1.5", ":1.99")
        loop.run_until_complete(asyncio.sleep(0))
        assert connection.registered == [linux_tray._SNI_PATH]

        # The watcher merely vanishing (empty new owner) must NOT re-register.
        handler(linux_tray._WATCHER_NAME, ":1.99", "")
        loop.run_until_complete(asyncio.sleep(0))
        assert connection.registered == [linux_tray._SNI_PATH]
    finally:
        loop.close()


def test_watcher_reregistration_failure_is_logged_not_raised():
    import asyncio

    connection = _RecordingConnection()
    paths = _Paths()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            linux_tray._subscribe_watcher_restarts(connection, loop, paths)
        )
        connection.fail_introspect = True  # the new watcher is flaky
        connection.handlers[0](linux_tray._WATCHER_NAME, "", ":1.7")
        loop.run_until_complete(asyncio.sleep(0))
        assert connection.registered == []
        assert paths.messages  # failure logged, loop keeps running
    finally:
        loop.close()


# --- XDG portal colour-scheme read / subscription (bus-free, D135) -----------


class _PortalConnection:
    """Fake dbus-fast connection exposing org.freedesktop.portal.Settings:
    answers Read with `value` (or raises `error`), and records the
    SettingChanged handler the subscription installs."""

    def __init__(self, value=2, error=None):
        self.value = value
        self.error = error
        self.reads = []
        self.handlers = []

    async def introspect(self, name, path):
        if self.error is not None:
            raise self.error
        return f"introspection:{name}"

    def get_proxy_object(self, name, path, introspection):
        connection = self

        class _Object:
            def get_interface(self, interface_name):
                class _Settings:
                    async def call_read(self, namespace, key):
                        connection.reads.append((namespace, key))
                        if connection.error is not None:
                            raise connection.error
                        return connection.value

                    def on_setting_changed(self, handler):
                        connection.handlers.append(handler)

                return _Settings()

        return _Object()


def test_read_color_scheme_reads_the_appearance_namespace():
    import asyncio

    connection = _PortalConnection(value=2)
    assert asyncio.run(linux_tray._read_color_scheme(connection)) == 2
    assert connection.reads == [
        (linux_tray._APPEARANCE_NAMESPACE, linux_tray._COLOR_SCHEME_KEY)
    ]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("no such interface"), TimeoutError("portal timed out")],
)
def test_read_color_scheme_falls_back_to_no_preference_on_any_failure(error):
    # No portal (stock minimal WMs), a D-Bus error, or a timeout: the tray must
    # still come up, with today's white glyph.
    import asyncio

    assert asyncio.run(linux_tray._read_color_scheme(_PortalConnection(error=error))) == 0


def test_setting_changed_retints_and_reemits_without_reregistering():
    import asyncio

    class FakeItem:
        def __init__(self):
            self.emitted = 0

        def NewIcon(self):  # noqa: N802
            self.emitted += 1

    connection = _PortalConnection(value=1)
    item = FakeItem()
    pixmap_ref = [linux_tray._icon_pixmap(linux_tray._ICON_PATH, linux_tray._TINT_WHITE)]
    paths = _Paths()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            linux_tray._subscribe_color_scheme(connection, paths, item, pixmap_ref)
        )
        assert len(connection.handlers) == 1
        handler = connection.handlers[0]

        # The desktop switches to a light panel → dark glyph, one NewIcon.
        handler(
            linux_tray._APPEARANCE_NAMESPACE, linux_tray._COLOR_SCHEME_KEY, 2
        )
        assert item.emitted == 1
        assert pixmap_ref[0] == linux_tray._icon_pixmap(
            linux_tray._ICON_PATH, linux_tray._TINT_DARK
        )

        # An unrelated appearance key must not touch the icon at all.
        handler(linux_tray._APPEARANCE_NAMESPACE, "accent-color", 2)
        assert item.emitted == 1

        # Back to a dark panel → white glyph again.
        handler(linux_tray._APPEARANCE_NAMESPACE, linux_tray._COLOR_SCHEME_KEY, 1)
        assert item.emitted == 2
        # Nothing here re-registers the status item — the subscription only ever
        # re-emits the icon.
        assert not paths.messages
    finally:
        loop.close()


def test_subscribing_to_a_missing_portal_is_logged_not_raised():
    # An absent portal must not stop the tray from registering; the icon just
    # stays white for the session.
    import asyncio

    connection = _PortalConnection(error=RuntimeError("no portal"))
    paths = _Paths()
    asyncio.run(
        linux_tray._subscribe_color_scheme(
            connection, paths, object(), [linux_tray._icon_pixmap(linux_tray._ICON_PATH)]
        )
    )
    assert connection.handlers == []
    assert paths.messages  # logged, not raised


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
