"""Windows capture: a GDI still, and recording fed by the page (`_sink`).

**Split by capability, not by platform**, and the split is forced. A STILL is
one `BitBlt` away and needs no permission, no prompt and no dependency beyond
Pillow — so it is native here, instant, and can shoot a specific monitor. A
RECORDING with system audio in it has no OS API a non-packaged Python process
can reach: `AppRecordingManager` needs MSIX identity, `Windows.Graphics.Capture`
hands out D3D surfaces with no muxer behind them, Media Foundation has no screen
source, and ffmpeg's Windows inputs are dshow/gdigrab/vfwcap — no WASAPI, so no
loopback without a third-party driver. Chromium already does what a native
recorder would (WGC plus WASAPI loopback, hardware-encoded), so `_sink` takes
the chunks its `MediaRecorder` produces. See `_sink.py` for the rest of that
argument.

**Everything Win32 is ctypes, touched only inside functions** — the same posture
`shell/pasteboard/_win32.py` and `winopen.py` take, so this module imports
cleanly on a Mac and the pure parts (the monitor maths, the rect scaling) are
tested on every platform rather than only on a Windows runner.
"""

from __future__ import annotations

import ctypes
import logging

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

#: `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`. Without it Windows
#: lies to this process about every coordinate on a scaled display: monitor
#: rects come back in the 96-DPI "virtual" space, so a shot of a 150 %-scaled
#: 2560-wide panel is silently 1707 wide and blurry — the same half-resolution
#: bug the macOS backend hit through `SCDisplay.width`, in a different disguise.
_DPI_PER_MONITOR_V2 = -4

#: `BitBlt` flags: plain copy, plus CAPTUREBLT so layered windows (menus,
#: tooltips, the DWM's own translucency) are included instead of leaving holes.
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000

_DIB_RGB_COLORS = 0
_BI_RGB = 0


def _u32():
    return ctypes.windll.user32


def _gdi():
    return ctypes.windll.gdi32


def _dpi_aware() -> None:
    """Best-effort, once per process. An old build simply stays as it was."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(_DPI_PER_MONITOR_V2)
    except (AttributeError, OSError):                    # pragma: no cover
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


# --------------------------------------------------------------- the monitors


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _monitors() -> list[dict]:
    """Every monitor, in physical pixels, main first-flagged.

    `EnumDisplayMonitors` rather than `GetSystemMetrics`: the latter describes
    only the primary display, and a `display` argument that cannot name the
    second monitor is a `display` argument that does not work.
    """
    _dpi_aware()
    found: list[dict] = []
    # LPARAM is pointer-sized and INTEGRAL. Declaring it as a double is the
    # kind of ABI slip that limps along on x64 (the value is unused here) and
    # corrupts the frame somewhere else — worth getting right in the one
    # function no test on this machine can reach.
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(_RECT), ctypes.c_ssize_t)
    primary = (_u32().GetSystemMetrics(0), _u32().GetSystemMetrics(1))

    def each(handle, _dc, rect, _data):
        box = rect.contents
        found.append({
            "id": len(found) + 1,
            "x": int(box.left),
            "y": int(box.top),
            "width": int(box.right - box.left),
            "height": int(box.bottom - box.top),
            "main": bool(box.left == 0 and box.top == 0),
            # This monitor's own DPI scale (1.0 at 96 dpi), for `locate`: on a
            # mixed-DPI desk each monitor has its own, and the page's
            # `devicePixelRatio` only describes the one the page is on.
            "scale": _monitor_scale(handle),
        })
        return 1

    _u32().EnumDisplayMonitors(None, None, proto(each), 0)
    if not found:                                        # pragma: no cover
        found.append({"id": 1, "x": 0, "y": 0, "width": primary[0],
                      "height": primary[1], "main": True, "scale": 1.0})
    return found


def _monitor_scale(handle) -> float:
    """`GetDpiForMonitor(MDT_EFFECTIVE_DPI) / 96`, or 1.0 where shcore is
    missing (pre-8.1) — the fallback that keeps `_monitors()` an answer."""
    try:
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        if ctypes.windll.shcore.GetDpiForMonitor(
                ctypes.c_void_p(handle), 0, ctypes.byref(dpi_x),
                ctypes.byref(dpi_y)) == 0 and dpi_x.value:
            return dpi_x.value / 96.0
    except (AttributeError, OSError):                    # pragma: no cover
        pass
    return 1.0


def _pick(display, monitors: list[dict]) -> dict:
    """The monitor a `display` names, or the whole virtual desktop for none.

    No `display` means EVERY screen, not the primary one: a still of "the
    screen" on a two-monitor desk is both of them, which is also what
    `PrintWindow`-era tools and the OS's own Snip do.
    """
    if display in (None, "", 0):
        left = min(m["x"] for m in monitors)
        top = min(m["y"] for m in monitors)
        right = max(m["x"] + m["width"] for m in monitors)
        bottom = max(m["y"] + m["height"] for m in monitors)
        return {"id": 0, "x": left, "y": top, "width": right - left,
                "height": bottom - top, "main": True}
    try:
        wanted = int(display)
    except (TypeError, ValueError):
        from fused_render.capture import CaptureError

        raise CaptureError(f"'display' must be a display id, not {display!r}")
    for monitor in monitors:
        if monitor["id"] == wanted:
            return monitor
    from fused_render.capture import CaptureError

    known = ", ".join(str(m["id"]) for m in monitors)
    raise CaptureError(f"no such display: {wanted} (this machine has {known})")


def _region(monitor: dict, rect) -> tuple[int, int, int, int]:
    """The pixels to copy: the monitor, or a `rect` inside it.

    **`rect` is in the units the `displays` entry it applies to reports**, which
    here means PHYSICAL PIXELS: this process is per-monitor DPI aware, so
    `EnumDisplayMonitors` hands back real pixels and `sources().displays` says
    so. That rule is what makes one `rect` argument mean one thing across three
    backends without a scale factor a caller has to know — macOS reports points
    and records at the panel's pixel scale, so a rect there is points. The only
    work left is offsetting by the monitor's own origin in the virtual desktop.

    Pure arithmetic, deliberately: it is the part most likely to be wrong and
    the only part testable off Windows.
    """
    if not rect:
        return monitor["x"], monitor["y"], monitor["width"], monitor["height"]
    x, y, w, h = rect
    return (monitor["x"] + int(x), monitor["y"] + int(y), int(w), int(h))


def locate(rect, dpr: float):
    """`capture.shot_region`'s hook: the monitor a browser-measured rect is on,
    and the rect in that monitor's PHYSICAL pixels."""
    return _locate(rect, _monitors(), dpr)


def _locate(rect, monitors: list[dict], dpr: float):
    """The arithmetic behind `locate`, off-platform testable.

    The browser measures in DIPs; `_monitors()` reports physical pixels. With
    one DPI everywhere the map is one multiplication by the page's
    `devicePixelRatio`. On a MIXED-DPI desk it is not: Chromium lays its DIP
    desktop out per display — each monitor is its physical size divided by
    ITS OWN scale, placed ADJACENT to the neighbour it touches (`_dip_layout`)
    — so a window on a 100% external beside a 200% laptop panel reports DIPs
    that, multiplied by the page's dpr, land inside the wrong monitor's
    physical bounds and pass containment there. So containment runs in that
    DIP layout, and the local rect is scaled by the matched monitor's scale,
    not the page's. `dpr` stands in only for a monitor whose scale could not
    be read.
    """
    from fused_render.capture import CaptureError

    x, y, w, h = (float(n) for n in rect)
    for m, (dx, dy, dw, dh) in zip(monitors, _dip_layout(monitors, dpr)):
        if dx <= x and dy <= y and x + w <= dx + dw and y + h <= dy + dh:
            s = float(m.get("scale") or dpr or 1.0)
            return m["id"], ((x - dx) * s, (y - dy) * s, w * s, h * s)
    raise CaptureError(
        "the region to photograph is not entirely on one monitor — move the "
        "window fully on screen and export again")


def _dip_layout(monitors: list[dict], dpr: float) -> list[tuple]:
    """Each monitor's bounds in the browser's DIP desktop, same order.

    Chromium's rule (ScreenWin): the primary sits at the origin at its own
    scale; every other display is placed against the display it TOUCHES in
    physical space, on that same edge, its offset along the edge scaled by the
    PARENT's factor (the edge belongs to the parent). Walked outward from the primary so a chain of monitors places
    in order; one that touches nothing (a gap in the arrangement) falls back to
    physical/scale, which is exact whenever all scales are equal.
    """
    def scale(m):
        return float(m.get("scale") or dpr or 1.0)

    n = len(monitors)
    dip: list = [None] * n
    order = sorted(range(n), key=lambda i: not monitors[i].get("main"))
    if n:
        first = order[0]
        m = monitors[first]
        dip[first] = (0.0, 0.0, m["width"] / scale(m), m["height"] / scale(m))
    changed = True
    while changed:
        changed = False
        for i in range(n):
            if dip[i] is not None:
                continue
            m, s = monitors[i], scale(monitors[i])
            mw, mh = m["width"] / s, m["height"] / s
            for j in range(n):
                if dip[j] is None:
                    continue
                p, (px, py, pw, ph) = monitors[j], dip[j]
                # The offset ALONG the shared edge is a distance on the
                # parent's edge, so it scales by the PARENT's factor
                # (Chromium ScreenWin `ScaleOffset`: offset / parent_scale),
                # not the child's.
                ps = scale(p)
                if m["x"] == p["x"] + p["width"]:            # right of p
                    dip[i] = (px + pw, py + (m["y"] - p["y"]) / ps, mw, mh)
                elif m["x"] + m["width"] == p["x"]:          # left of p
                    dip[i] = (px - mw, py + (m["y"] - p["y"]) / ps, mw, mh)
                elif m["y"] == p["y"] + p["height"]:         # below p
                    dip[i] = (px + (m["x"] - p["x"]) / ps, py + ph, mw, mh)
                elif m["y"] + m["height"] == p["y"]:         # above p
                    dip[i] = (px + (m["x"] - p["x"]) / ps, py - mh, mw, mh)
                else:
                    continue
                changed = True
                break
    for i in range(n):
        if dip[i] is None:
            m, s = monitors[i], scale(monitors[i])
            dip[i] = (m["x"] / s, m["y"] / s, m["width"] / s, m["height"] / s)
    return dip


# ------------------------------------------------------------------- the probe


def _pillow():
    try:
        from PIL import Image

        return Image
    except ImportError:
        return None


def probe() -> dict:
    """`sources()`'s payload — nothing here prompts and nothing here changes.

    **The recording halves are answered TRUE and refined by the browser.** On
    this platform the encoder is the page's, so whether a recording is possible
    is a fact about the browser (does it have `MediaRecorder`, will it share
    system audio) and not about this machine — and a server route cannot know
    which browser is asking. `runtime.js` merges its own probe over these,
    keyed by `client`, and strips the key before a page sees the payload. The
    alternative was answering `false` here and hoping the client corrects it,
    which shows a disabled record button for a second on every page load.
    """
    image = _pillow()
    shot_reason = None if image else (
        "screenshots need Pillow — pip install pillow (the packaged app ships "
        "it)")
    try:
        displays = _monitors()
    except Exception as e:                               # noqa: BLE001
        # A probe may not raise (see `capture.sources`), and an empty display
        # list is a usable answer: a still with no `display` still works.
        logger.warning("enumerating monitors failed", exc_info=True)
        displays = []
        shot_reason = shot_reason or f"could not list displays: {e}"
    return {
        "client": True,
        "video": {"available": True, "granted": True, "reason": None},
        "audio": {"available": True, "granted": True, "reason": None},
        "systemAudio": {"available": True, "reason": None},
        "screenshot": {"available": bool(image), "granted": True,
                       "reason": shot_reason},
        "displays": displays,
        "microphones": [],
    }


def refuse(mode: str, spec: dict) -> str | None:
    """Recording refusals come from `_sink`; the still honours everything."""
    return _sink_refuse(mode, spec)


# --------------------------------------------------------------------- still


def screenshot(out: str, spec: dict) -> dict:
    """One frame to `out` via `BitBlt`, encoded by Pillow.

    Two known blind spots, named here because a black rectangle with no
    explanation is worse than a documented limit: DRM-protected windows (a
    paid-video player) come back black, and a full-screen exclusive game may
    present through a path GDI cannot see. `Windows.Graphics.Capture` covers
    both and needs D3D interop, which is the trade this file's header explains.
    """
    from fused_render.capture import Unsupported

    image_mod = _pillow()
    if image_mod is None:
        raise Unsupported(
            "screenshots need Pillow — pip install pillow")

    monitor = _pick(spec.get("display"), _monitors())
    x, y, width, height = _region(monitor, spec.get("rect"))
    if width <= 0 or height <= 0:                        # pragma: no cover
        from fused_render.capture import CaptureError

        raise CaptureError("the requested region is empty")

    gdi, u32 = _gdi(), _u32()
    screen_dc = u32.GetDC(None)
    memory_dc = gdi.CreateCompatibleDC(screen_dc)
    bitmap = gdi.CreateCompatibleBitmap(memory_dc, width, height)
    try:
        gdi.SelectObject(memory_dc, bitmap)
        ok = gdi.BitBlt(memory_dc, 0, 0, width, height, screen_dc, x, y,
                        _SRCCOPY | _CAPTUREBLT)
        if not ok:
            raise RuntimeError(
                f"BitBlt failed (GetLastError {ctypes.get_last_error()})")
        # `cursor` is drawn in, rather than being a property of the copy: GDI
        # never includes the pointer, so honouring the option means compositing
        # it. A still defaults to NO pointer, like the other backends.
        if spec.get("cursor"):
            _draw_cursor(memory_dc, x, y)
        raw = _pixels(memory_dc, bitmap, width, height)
    finally:
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory_dc)
        u32.ReleaseDC(None, screen_dc)

    # BGRA bottom-up out of a DIB; Pillow's "BGRX" reader plus a flip is the
    # whole conversion — no per-pixel Python.
    picture = image_mod.frombuffer("RGB", (width, height), raw, "raw", "BGRX",
                                   0, -1)
    if spec.get("jpeg"):
        picture.save(out, "JPEG", quality=92)
    else:
        picture.save(out, "PNG")
    return {"width": width, "height": height}


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def _pixels(memory_dc, bitmap, width: int, height: int) -> bytes:
    """The bitmap's bytes, 32-bit BGRA, bottom-up (`biHeight` positive)."""
    header = _BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = _BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    copied = _gdi().GetDIBits(memory_dc, bitmap, 0, height, buffer,
                             ctypes.byref(header), _DIB_RGB_COLORS)
    if not copied:                                       # pragma: no cover
        raise RuntimeError("GetDIBits returned no scanlines")
    return buffer.raw


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos_x", ctypes.c_long),
                ("ptScreenPos_y", ctypes.c_long)]


def _draw_cursor(memory_dc, origin_x: int, origin_y: int) -> None:
    """Composite the pointer at its screen position. Best-effort by design:
    a cursor that could not be drawn must not lose the screenshot."""
    try:
        info = _CURSORINFO()
        info.cbSize = ctypes.sizeof(_CURSORINFO)
        if not _u32().GetCursorInfo(ctypes.byref(info)) or not info.flags:
            return
        _u32().DrawIconEx(memory_dc, info.ptScreenPos_x - origin_x,
                          info.ptScreenPos_y - origin_y, info.hCursor,
                          0, 0, 0, None, 0x0003)         # DI_NORMAL
    except Exception:                                    # noqa: BLE001
        logger.debug("could not draw the cursor into the screenshot",
                     exc_info=True)
