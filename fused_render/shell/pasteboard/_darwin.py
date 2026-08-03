"""macOS pasteboard backend — NSPasteboard file URLs via pyobjc.

Finder's copy puts `public.file-url` items on the general pasteboard; that is
the flavor we both read and write. The write additionally publishes the
newline-joined POSIX paths as plain text, so a ⌘V in Finder pastes the actual
files while a ⌘V in a terminal or an editor yields the path — one copy, both
useful destinations.

AppKit is imported *inside* each function, not at module scope: pyobjc only
ships in the packaged `.app` (the `[app]` extra), so a source install on macOS
has no AppKit and must degrade to `supported=False` rather than exploding on
import. The contract module's `_load_backend` catches an import error at load
time; these in-function imports cover a partially-installed pyobjc too.
"""
from __future__ import annotations


def read_files() -> list[str]:
    """POSIX paths for every file URL on the general pasteboard.

    Anything that isn't a file URL (plain text, an image, a web URL) yields an
    empty list — "the bridge works, there's nothing to paste".
    """
    from AppKit import NSPasteboard, NSURL
    from Foundation import NSDictionary

    pb = NSPasteboard.generalPasteboard()
    # readObjectsForClasses_options_ is the modern multi-item API: it returns
    # NSURLs for every file-url item at once, so a multi-file Finder copy
    # comes back in one call and in the order Finder put them on.
    # NSPasteboardURLReadingFileURLsOnlyKey filters out http:// URLs, which
    # would otherwise arrive as NSURLs with no .path.
    options = NSDictionary.dictionaryWithObject_forKey_(True, "NSPasteboardURLReadingFileURLsOnlyKey")
    urls = pb.readObjectsForClasses_options_([NSURL], options)
    if not urls:
        return []

    paths = []
    for url in urls:
        p = url.path()
        if p:
            paths.append(str(p))
    return paths


def write_files(paths: list[str]) -> None:
    """Publish `paths` as file URLs plus their plain-text form."""
    from AppKit import NSPasteboard, NSPasteboardTypeString, NSURL

    pb = NSPasteboard.generalPasteboard()
    # clearContents both wipes the previous owner's items and bumps
    # changeCount — required before writing, or the write is a no-op.
    pb.clearContents()

    urls = [NSURL.fileURLWithPath_(p) for p in paths]
    pb.writeObjects_(urls)
    # Declared after writeObjects_ so it joins the same pasteboard "item set"
    # rather than replacing it: file-url flavors stay intact for Finder, and
    # a text-only consumer still finds something to paste.
    pb.setString_forType_("\n".join(paths), NSPasteboardTypeString)
