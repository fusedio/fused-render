"""Linux capture: a portal still, and recording fed by the page (`_sink`).

**The still goes through xdg-desktop-portal**, not through X11. There is no
grabbing the screen on Wayland — no compositor will let a client read the
framebuffer — so the only interface that works on both display servers and on
every desktop is `org.freedesktop.portal.Screenshot`: the compositor takes the
shot and hands back a PNG it wrote itself. dbus-fast carries the call, which is
already a dependency (the supervisor's tray is a StatusNotifierItem over the
same bus) and is pure Python with manylinux wheels — no PyGObject, no GTK, no
system packages.

**Recording is the page's `MediaRecorder` (`_sink`).** The native alternative
here is real — portal ScreenCast hands out a PipeWire node and GStreamer can
encode it — but it needs `gst-launch-1.0` plus `gst-plugins-good`,
`gst-plugins-bad` and `gst-libav` present on the user's machine, and it is a
second implementation of something Chromium already does through that exact
portal. One sink shared with Windows costs nothing extra and works wherever the
browser does. See `_sink.py`.

**What the portal cannot do is refused, not faked**: it takes no display and no
cursor option, so `display` and `cursor` say so on this platform and point at
what does work. A `rect` is honoured by cropping the PNG the compositor wrote.
"""

from __future__ import annotations

import logging
import os
import shutil
from urllib.parse import unquote, urlparse

from fused_render.capture._sink import (  # noqa: F401 - the seam, re-exported
    attach,
    detach,
    ext,
    failure,
    refuse as _sink_refuse,
    start_audio,
    start_screen,
    stop,
)

logger = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Screenshot"
REQUEST_IFACE = "org.freedesktop.portal.Request"

#: How long to wait for the compositor's reply. A first screenshot can sit
#: behind a one-time permission dialog the user has to answer, so this is
#: generous — but finite, because a request that never answers is worse than one
#: that fails.
WAIT_S = 120.0

#: Where a session bus lives, if one does. Checked rather than assumed: a
#: headless `fused-render` on a server has no portal and must say so instead of
#: hanging on a bus that is not there.
_SERVICE_DIRS = ("/usr/share/dbus-1/services",
                 "/usr/local/share/dbus-1/services",
                 "/run/host/usr/share/dbus-1/services")


def _dbus():
    try:
        import dbus_fast.aio                             # noqa: F401
        from dbus_fast.aio import MessageBus

        return MessageBus
    except ImportError:
        return None


def _pillow():
    try:
        from PIL import Image

        return Image
    except ImportError:
        return None


def _portal_installed() -> bool:
    """Is a desktop portal plausibly reachable? Answered WITHOUT calling it.

    Deliberately a heuristic, and the honest limit of a non-prompting probe: the
    only certain answer is a D-Bus round trip, and on some desktops the very
    first `Screenshot` call is what raises the permission dialog — which CP-7
    forbids a probe from doing. So this reads what a session has: a bus address
    and an activatable portal service. A machine that passes this and then fails
    for real gets the actual error from `screenshot()`, not a shrug.
    """
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime or not os.path.exists(os.path.join(runtime, "bus")):
            return False
    name = PORTAL_BUS + ".service"
    return any(os.path.exists(os.path.join(directory, name))
               for directory in _SERVICE_DIRS)


# ------------------------------------------------------------------- the probe


def probe() -> dict:
    """`sources()`'s payload. Reads the session's shape; prompts for nothing.

    `displays` is ALWAYS empty here, and that is the answer rather than a gap:
    the portal chooses what is shot (and the browser's picker chooses what is
    recorded), so there is no display for a caller to name — on Wayland the list
    cannot even be built without prompting. `display` is refused with a sentence
    saying as much, which is more use than a list nothing accepts.

    The recording halves are answered `true` and refined by the browser — see
    `_windows.probe` for why that direction, not the other.
    """
    bus = _dbus()
    image = _pillow()
    if bus is None:
        shot_reason = ("screenshots need dbus-fast — pip install dbus-fast "
                       "(the packaged app ships it)")
    elif image is None:
        shot_reason = ("screenshots need Pillow — pip install pillow (the "
                       "packaged app ships it)")
    elif not _portal_installed():
        shot_reason = (
            "no xdg-desktop-portal on this session — install "
            "xdg-desktop-portal plus the backend for your desktop "
            "(xdg-desktop-portal-gnome, -kde, -wlr or -gtk)")
    else:
        shot_reason = None
    return {
        "client": True,
        "video": {"available": True, "granted": True, "reason": None},
        "audio": {"available": True, "granted": True, "reason": None},
        "systemAudio": {"available": True, "reason": None},
        "screenshot": {"available": shot_reason is None, "granted": True,
                       "reason": shot_reason},
        "displays": [],
        "microphones": [],
    }


def refuse(mode: str, spec: dict) -> str | None:
    """`display` and `cursor` on a still; whatever `_sink` refuses on the rest."""
    if mode == "screenshot":
        if spec.get("display") not in (None, ""):
            return ("'display' cannot be chosen on Linux — the desktop portal "
                    "shoots the screen it owns, and on Wayland no client may "
                    "even enumerate the others. Pass a 'rect' to narrow the "
                    "shot instead")
        if spec.get("cursor") is not None:
            return ("'cursor' cannot be chosen on Linux — the desktop portal's "
                    "Screenshot interface has no such option, so the pointer is "
                    "whatever the compositor decides")
        return None
    return _sink_refuse(mode, spec)


# --------------------------------------------------------------------- still


def screenshot(out: str, spec: dict) -> dict:
    """One frame to `out`, taken by the compositor through the portal.

    The portal writes its own PNG into a directory it controls and hands back a
    URI; this moves it to `out` (a rename across filesystems is a copy, hence
    `shutil.move`) and converts only when it has to — a `.png` with no `rect` is
    a move and nothing else, so the common case re-encodes nothing.
    """
    from fused_render.capture import Unsupported

    bus = _dbus()
    if bus is None:
        raise Unsupported("screenshots need dbus-fast — pip install dbus-fast")
    if not _portal_installed():
        raise Unsupported(
            "no xdg-desktop-portal on this session — install "
            "xdg-desktop-portal and the backend for your desktop")

    uri = _portal_screenshot()
    source = _path_from_uri(uri)
    if not source or not os.path.isfile(source):
        raise RuntimeError(f"the portal returned no readable file: {uri!r}")

    rect = spec.get("rect")
    if not rect and not spec.get("jpeg"):
        shutil.move(source, out)
        return _size(out)

    image_mod = _pillow()
    if image_mod is None:
        raise Unsupported(
            "cropping or writing a JPEG needs Pillow — pip install pillow")
    with image_mod.open(source) as picture:
        if rect:
            x, y, width, height = (int(n) for n in rect)
            picture = picture.crop((x, y, x + width, y + height))
        if spec.get("jpeg"):
            picture.convert("RGB").save(out, "JPEG", quality=92)
        else:
            picture.save(out, "PNG")
        size = {"width": picture.width, "height": picture.height}
    try:
        os.remove(source)
    except OSError:
        pass
    return size


def _size(path: str) -> dict:
    """The PNG's dimensions, read from its own IHDR when Pillow is absent."""
    image_mod = _pillow()
    if image_mod is not None:
        with image_mod.open(path) as picture:
            return {"width": picture.width, "height": picture.height}
    import struct

    with open(path, "rb") as handle:                     # pragma: no cover
        head = handle.read(24)
    if len(head) >= 24 and head[12:16] == b"IHDR":       # pragma: no cover
        width, height = struct.unpack(">II", head[16:24])
        return {"width": int(width), "height": int(height)}
    return {"width": 0, "height": 0}                     # pragma: no cover


def _path_from_uri(uri: str) -> str | None:
    """`file:///a/b.png` -> `/a/b.png`, and anything else -> None.

    Pure, so it is tested off Linux. The portal always answers with a file URI,
    but percent-encoding is real (a screenshot lands under the user's name) and
    slicing a fixed seven characters is the bug `pasteboard/_linux.py` documents.
    """
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    if parsed.netloc not in ("", "localhost"):
        return None
    return unquote(parsed.path) or None


# ---------------------------------------------------------------- the D-Bus call


def _portal_screenshot() -> str:
    """Drive one `Screenshot` request to its `Response` signal. Returns the URI.

    The portal's shape is a two-step: the method returns a REQUEST OBJECT PATH
    and the answer arrives later as a `Response` signal on that path. So the
    handler has to be installed BEFORE the call is made — a compositor that
    answers instantly would otherwise be missed — and, because the path is only
    known after the reply, signals are BUFFERED and matched afterwards rather
    than filtered as they arrive. That ordering is the whole reason this is not
    three lines.
    """
    return _run(_screenshot_async(), timeout=WAIT_S)


def _run(coro, *, timeout: float):
    """Run one coroutine to completion from a synchronous route handler.

    `capture.screenshot` is called on a threadpool worker (FastAPI runs `def`
    routes there), so there is no loop on this thread and `asyncio.run` is
    correct. Guarded anyway: a caller that DOES have a running loop would
    otherwise get a `RuntimeError` from deep inside dbus-fast.
    """
    import asyncio

    async def bounded():
        return await asyncio.wait_for(coro, timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(bounded())
    raise RuntimeError(                                  # pragma: no cover
        "the portal screenshot must not be driven from an event loop thread")


async def _screenshot_async() -> str:
    import asyncio

    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        responses: dict[str, list] = {}
        arrived = asyncio.Event()

        def on_signal(message):
            if (message.message_type is MessageType.SIGNAL
                    and message.interface == REQUEST_IFACE
                    and message.member == "Response"):
                responses[str(message.path)] = list(message.body)
                arrived.set()

        bus.add_message_handler(on_signal)
        await bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch",
            signature="s",
            body=[f"type='signal',interface='{REQUEST_IFACE}',"
                  "member='Response'"]))

        reply = await bus.call(Message(
            destination=PORTAL_BUS, path=PORTAL_PATH, interface=PORTAL_IFACE,
            member="Screenshot", signature="sa{sv}",
            # `interactive: false` asks for the shot without a picker. A desktop
            # that insists on confirming still does — the portal is allowed to,
            # and that is exactly the dialog `_portal_installed` cannot
            # preflight without raising it.
            body=["", {"interactive": _variant(False),
                       "modal": _variant(False)}]))
        if reply.message_type is MessageType.ERROR:
            raise RuntimeError("the portal refused the screenshot: "
                               f"{reply.error_name}: {_first(reply.body)}")
        request = str(_first(reply.body) or "")

        while request not in responses:
            arrived.clear()
            await arrived.wait()
        return _uri_from_response(responses[request])
    finally:
        bus.disconnect()


def _uri_from_response(body: list) -> str:
    """The portal's `Response(u code, a{sv} results)` -> the file URI.

    Pure, so the three endings a compositor can give — took it, user said no,
    something else — are tested without a desktop. Code 1 is the user's own
    cancel and reads as one; anything non-zero is still a failure, because a
    screenshot nobody took must not resolve with a path to nothing.
    """
    code = body[0] if body else None
    results = body[1] if len(body) > 1 else {}
    if code != 0:
        raise RuntimeError(
            "the screenshot was cancelled or denied by the desktop"
            if code == 1 else
            f"the portal ended the screenshot request with code {code!r}")
    value = results.get("uri") if hasattr(results, "get") else None
    uri = getattr(value, "value", value)
    if not uri:
        raise RuntimeError("the portal reported success but returned no file")
    return str(uri)


def _variant(value):
    from dbus_fast import Variant

    return Variant("b", bool(value))


def _first(body):
    return body[0] if body else None
