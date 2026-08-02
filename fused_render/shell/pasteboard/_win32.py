"""Windows pasteboard backend — CF_HDROP via ctypes.

Explorer's Copy puts a `CF_HDROP` on the clipboard: a `DROPFILES` header
followed by a double-NUL-terminated list of NUL-separated wide-char paths.
That's what we read (via `DragQueryFileW`, which knows the layout) and what we
write (building the block ourselves onto `GlobalAlloc`'d memory). The write
also sets `CF_UNICODETEXT` so a terminal or editor paste yields the paths.

Path form matters in both directions. Windows wants backslashes; the shell's
canonical path form is forward-slash even on Windows (see
`frontend/src/lib/fs-actions.ts`), so `to_native`/`to_shell` sit on the two
edges and everything in between speaks one dialect. Those two, plus the
`DROPFILES` buffer construction, are pure functions with no Win32 in them —
deliberately, so they are tested on every platform rather than only on CI's
Windows runner.

`ctypes.windll` is touched only inside functions (the same posture as
`winopen.py`), so importing this module on macOS or Linux — which the tests
do — is harmless.
"""
from __future__ import annotations

import ctypes
import struct

CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

# sizeof(DROPFILES): DWORD pFiles + POINT pt (2 LONG) + BOOL fNC + BOOL fWide.
_DROPFILES_SIZE = 20


# ------------------------------------------------------------ pure helpers

def to_native(path: str) -> str:
    """Shell path -> the backslash form Windows APIs and Explorer expect."""
    return path.replace("/", "\\")


def to_shell(path: str) -> str:
    """A Win32 path -> the shell's canonical forward-slash form."""
    return path.replace("\\", "/")


def build_dropfiles(paths: list[str]) -> ctypes.Array:
    """The complete CF_HDROP payload for `paths`, as a ctypes byte buffer.

    Layout: DROPFILES header with pFiles pointing just past it and fWide=TRUE,
    then the UTF-16-LE paths, each NUL-terminated, with one extra NUL closing
    the list. Explorer reads exactly to that double NUL, so the trailing pair
    is load-bearing, not padding.
    """
    names = "".join(to_native(p) + "\0" for p in paths) + "\0"
    encoded = names.encode("utf-16-le")
    header = struct.pack("<IiiII", _DROPFILES_SIZE, 0, 0, 0, 1)
    buf = ctypes.create_string_buffer(len(header) + len(encoded))
    buf.raw = header + encoded
    return buf


def text_payload(paths: list[str]) -> str:
    """The CF_UNICODETEXT companion: native paths, one per line (CRLF, since
    that's what Notepad and cmd.exe expect to receive)."""
    return "\r\n".join(to_native(p) for p in paths)


# ------------------------------------------------------------ clipboard I/O

class _Clipboard:
    """OpenClipboard/CloseClipboard as a context manager.

    The clipboard is a global, single-owner resource: another app can hold it,
    so OpenClipboard is retried briefly before giving up. Failing to close it
    would wedge the clipboard for every app on the machine, hence the finally.
    """

    def __init__(self, attempts: int = 5):
        self.attempts = attempts

    def __enter__(self):
        import time

        user32 = ctypes.windll.user32
        for i in range(self.attempts):
            if user32.OpenClipboard(None):
                return self
            time.sleep(0.02 * (i + 1))
        raise OSError("could not open the Windows clipboard")

    def __exit__(self, *_exc):
        # Always closed, and the caller's exception is always re-raised
        # (returning False, never True): a failed clipboard op must reach
        # pasteboard.read_files/write_files, which is the ONE place that
        # decides a platform failure means `supported: False`. Swallowing it
        # here would report success on a clipboard we never touched.
        ctypes.windll.user32.CloseClipboard()
        return False


def read_files() -> list[str]:
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    shell32.DragQueryFileW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
    shell32.DragQueryFileW.restype = ctypes.c_uint
    user32.GetClipboardData.restype = ctypes.c_void_p

    with _Clipboard():
        if not user32.IsClipboardFormatAvailable(CF_HDROP):
            return []  # text, an image, nothing — all "no files to paste"
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        # 0xFFFFFFFF asks DragQueryFileW for the count rather than a name.
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(count):
            # First call with a NULL buffer returns the length sans NUL.
            length = shell32.DragQueryFileW(handle, i, None, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(handle, i, buf, length + 1)
            if buf.value:
                paths.append(to_shell(buf.value))
        return paths


def _set(fmt: int, payload: bytes) -> None:
    """Copy `payload` onto moveable global memory and hand it to the clipboard.

    SetClipboardData takes *ownership* of the handle on success, so the
    allocation must not be freed here; on failure we free it ourselves, or the
    block leaks for the life of the process.
    """
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise OSError("GlobalAlloc failed for the clipboard payload")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise OSError("GlobalLock failed for the clipboard payload")
    ctypes.memmove(ptr, payload, len(payload))
    kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)
        raise OSError(f"SetClipboardData failed for format {fmt}")


def write_files(paths: list[str]) -> None:
    user32 = ctypes.windll.user32
    with _Clipboard():
        # EmptyClipboard both clears the previous owner and makes us the
        # owner — required before any SetClipboardData call.
        user32.EmptyClipboard()
        _set(CF_HDROP, bytes(build_dropfiles(paths)))
        _set(CF_UNICODETEXT, text_payload(paths).encode("utf-16-le") + b"\x00\x00")
