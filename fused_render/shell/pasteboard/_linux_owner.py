"""Resident multi-target clipboard owner, run under a SEPARATE interpreter
from the app server's own.

`_linux.py`'s fallback write can only publish one clipboard target per
`xclip`/`wl-copy` invocation. GTK4, via PyGObject, can own a selection with
several targets at once (`Gdk.ContentProvider.new_union`) — but the app venv
has no PyGObject and shouldn't gain one just for this, so `_linux.py` spawns
this file as a script under whatever system interpreter its own probe found
capable of `import gi`, instead of importing this module directly.

Because of that, this module must not import anything from `fused_render` —
it runs somewhere the app's own package and dependencies are not installed.
The `gi` import itself lives inside `main()`, not at module scope, so the
module stays importable (and its payload logic unit-testable) on any
machine, CI included, that has neither GTK bindings nor a display.
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import quote

GNOME_TARGET = "x-special/gnome-copied-files"
URI_LIST_TARGET = "text/uri-list"
PLAIN_TARGET = "text/plain"
PLAIN_UTF8_TARGET = "text/plain;charset=utf-8"


def path_to_uri(path: str) -> str:
    """Absolute path -> a file:// URI with per-segment percent-encoding.

    The same rule as `_linux.py`'s `path_to_uri` (`safe="/"` keeps the
    separators literal). Copied rather than imported — see the module
    docstring — so keep the two in step by hand if either one changes.
    """
    return "file://" + quote(path, safe="/")


def build_payloads(paths: list[str]) -> dict[str, bytes]:
    """The four mime payloads this owner offers simultaneously.

    Byte-exact per the table measured on the target machine (see
    SPEC-linux-multitarget-clipboard.md): the two file-manager formats carry
    `file://` URIs, while both `text/plain` flavors carry the bare paths
    themselves — the same newline-joined form macOS puts in
    `NSPasteboardTypeString` — so a paste into a plain text field yields
    something a human or a shell can use directly, not an encoded URI.
    """
    uris = [path_to_uri(p) for p in paths]
    plain = "\n".join(paths).encode("utf-8")
    return {
        GNOME_TARGET: ("copy\n" + "\n".join(uris)).encode("utf-8"),
        # CRLF per RFC 2483, matching `_linux.py`'s fallback write.
        URI_LIST_TARGET: ("\r\n".join(uris) + "\r\n").encode("utf-8"),
        PLAIN_TARGET: plain,
        PLAIN_UTF8_TARGET: plain,
    }


def main() -> None:
    """Read `{"paths": [...]}` off stdin, own the selection, and hold it
    until something else takes the clipboard away from us.

    Readiness handshake with the parent: print exactly one line, flush it,
    then `os.dup2` /dev/null over fd 1. The parent needs that one line to
    know the selection is actually set before it treats this write as done;
    after that, nothing must be able to block on this process's stdout ever
    again, including this process itself — it is resident by design and
    will hold fd 1 for as long as it owns the clipboard, which on the
    SUCCESS path is indefinite.

    We must exit once we lose ownership — otherwise every copy leaks a
    process that outlives its usefulness. `Gdk.Clipboard`'s `changed` signal
    fires on our own `set_content()` call too, so the exit condition is
    `changed` AND `not is_local()`: local means WE still hold the selection,
    non-local means somebody else just took it.
    """
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    request = json.loads(sys.stdin.read() or "{}")
    payloads = build_payloads(list(request.get("paths") or []))

    Gtk.init()
    providers = [
        Gdk.ContentProvider.new_for_bytes(mime, GLib.Bytes.new(data))
        for mime, data in payloads.items()
    ]
    clipboard = Gdk.Display.get_default().get_clipboard()
    clipboard.set_content(Gdk.ContentProvider.new_union(providers))

    loop = GLib.MainLoop()

    def _on_changed(cb: "Gdk.Clipboard") -> None:
        if not cb.is_local():
            loop.quit()

    clipboard.connect("changed", _on_changed)

    print("ready", flush=True)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)

    loop.run()


if __name__ == "__main__":
    main()
