"""Version of fused-render installed on disk, as opposed to the one running.

A DMG install replaces the .app bundle in place while an old process keeps
running its already-loaded code — so the bundle's Info.plist is the ground
truth for "what would launch next time", and comparing it to the in-memory
__version__ tells the shell to ask for an app restart (not a page refresh,
which can't swap the server process).

Linux has no equivalent of Info.plist to read out of an AppImage — its swap
(update/linux.py) instead writes a small stamp JSON next to the app's state
dir (never next to the AppImage itself, which may live on a directory that is
shared or synced) recording the path/size/mtime of the AppImage it just
produced, plus the version. `installed_version()` on Linux honours that stamp
only while $APPIMAGE still names the exact file the stamp describes — so a
user who replaces the AppImage by hand, or moves it, gets an honest "no
restart signal available" rather than a stale banner.

Windows reports None: unpackaged runs (dev checkouts, pip installs) and the
Windows package have no restart-drift signal at all, which the shell reads as
"no restart signal available".
"""
from __future__ import annotations

import os
import plistlib
import sys

from fused_render.shell import storage

# Stamp file name under shell.storage.home_dir() — the same flat ~/.fused-render
# dotdir the desktop supervisor's DesktopPaths.state resolves to (see
# supervisor/paths.py's discover_linux() docstring), reused here rather than
# invented, so the stamp lives wherever the rest of the app's durable state
# already does instead of next to the (possibly shared/synced) AppImage.
_LINUX_STAMP_NAME = "linux-update-stamp.json"


def _linux_stamp_path() -> str:
    return os.path.join(storage.home_dir(), _LINUX_STAMP_NAME)


def write_linux_stamp(appimage: str, version: str) -> None:
    """Record that `appimage` (an absolute path) now holds `version`, keyed
    to its current size and mtime — called once, right after update/linux.py's
    os.replace() swap lands. Best-effort by the caller's own contract (the
    swap itself is what matters; a stamp that fails to write only costs a
    later "restart to finish" banner, never the update itself)."""
    stat = os.stat(appimage)
    storage.write_json(_linux_stamp_path(), {
        "path": appimage,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "version": version,
    })


def _linux_installed_version(appimage: str | None) -> str | None:
    """The version update/linux.py's stamp says is on disk, honoured only
    when `appimage` (the CURRENT $APPIMAGE, or a manager's target path) still
    matches the stamp's path/size/mtime exactly — a hand-replaced or moved
    AppImage must not keep showing a restart banner for a swap that never
    happened to it.

    INVARIANT: the stamp is keyed by the AppImage's RESOLVED path
    (write_linux_stamp() is always called with os.path.realpath()'s result —
    see update/linux.py's _install_appimage). `$APPIMAGE`/a manager's `bundle`
    can legitimately name a symlink (some ancestor directory swapped out from
    under a stable launcher path), so `appimage` is resolved here too, once,
    before comparing — the one place both callers (installed_version() below
    and update/linux.py's _disk_version()) get this for free, rather than
    each having to remember to realpath its own argument first."""
    if appimage is None:
        return None
    appimage = os.path.realpath(appimage)
    stamp = storage.read_json(_linux_stamp_path())
    if not isinstance(stamp, dict):
        return None
    try:
        current = os.stat(appimage)
    except OSError:
        return None
    if (stamp.get("path") != appimage
            or stamp.get("size") != current.st_size
            or stamp.get("mtime") != current.st_mtime):
        return None
    return stamp.get("version")


def installed_version() -> str | None:
    # `sys.frozen == "macosx_app"` (py2app's own marker) is checked before
    # the platform test, not after: the unit tests fake a py2app bundle by
    # setting `sys.frozen` while running on whatever host CI happens to use
    # (often Linux), and that faked marker has to keep meaning "read the
    # bundle's Info.plist" regardless of the real host platform, exactly as
    # it always has.
    if getattr(sys, "frozen", None) == "macosx_app":
        # sys.executable is …/Contents/MacOS/python (see fusedcli.setup_cli_hint).
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        try:
            with open(os.path.join(contents, "Info.plist"), "rb") as f:
                return plistlib.load(f).get("CFBundleShortVersionString")
        except (OSError, plistlib.InvalidFileException):
            return None
    if sys.platform.startswith("linux"):
        from fused_render.supervisor._linux import startup
        appimage = startup.appimage_path()
        return _linux_installed_version(str(appimage) if appimage is not None else None)
    return None
