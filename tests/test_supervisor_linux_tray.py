"""Linux tray pure helpers: icon → ARGB32 big-endian pixmap, com.canonical.
dbusmenu layout, and event → TrayAction dispatch. All bus-free."""
import queue

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


def test_icon_pixmap_is_argb32_big_endian(tmp_path):
    # A solid image already at the target size resizes to itself (no interpolated
    # edges), so every 4-byte group must be the ARGB32 (network byte order)
    # repack of the source RGBA pixel — A, R, G, B.
    src = tmp_path / "solid.png"
    size = linux_tray._ICON_SIZE
    Image.new("RGBA", (size, size), (10, 20, 30, 40)).save(src)

    w, h, data = linux_tray._icon_pixmap(src)

    assert data[:4] == bytes([40, 10, 20, 30])  # A, R, G, B
    assert data == bytes([40, 10, 20, 30]) * (w * h)


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
