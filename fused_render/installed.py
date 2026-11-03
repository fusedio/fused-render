"""Version of fused-render installed on disk, as opposed to the one running.

"Installed" here means WHAT WOULD LAUNCH NEXT TIME, and comparing it to the
in-memory __version__ tells the shell to ask for an app restart (not a page
refresh, which can't swap the server process). On macOS two things can make
that differ from the running code:

* A manual DMG drag (or `brew upgrade`) replaces the .app bundle in place
  while the old process keeps running its already-loaded code — the
  bundle's own Info.plist is the answer then.
* The in-app updater (update/mac.py) STAGES its download as a hidden
  sibling of the bundle (`staged_bundle_path`) and only renames it into
  place on quit — the swap used to happen at install time, which moved the
  bundle out from under every open window and child process: a lazily
  imported route chunk asked for a hash that was no longer on disk ("This
  page didn't load" on every release, 2026-11-03), and a lazy Python import
  read new code into the old process. The staged sibling is what launches
  next, so its version wins here whenever it is newer than the bundle's.

Linux has no equivalent of Info.plist to read out of an AppImage — its swap
(update/linux.py) instead writes a small stamp JSON next to the app's state
dir (never next to the AppImage itself, which may live on a directory that is
shared or synced) recording the path/size/mtime of the AppImage it just
produced, plus the version. `installed_version()` on Linux honours that stamp
only while $APPIMAGE still names the exact file the stamp describes — so a
user who replaces the AppImage by hand, or moves it, gets an honest "no
restart signal available" rather than a stale banner.

Unpackaged runs (dev checkouts, pip installs) report None: they have no
restart-drift signal at all, which the shell reads as "no restart signal
available".
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


# The hidden sibling update/mac.py stages a downloaded bundle under, next to
# the installed bundle: `/Applications/.FusedRender-update.app` for
# `/Applications/FusedRender.app` (`FusedBot-feature.app` ->
# `.FusedBot-feature-update.app`). Derived from the bundle's own basename
# rather than from a flavor constant so this module stays below `update/`
# and so a Caskroom bundle reached through a symlink stages beside the REAL
# bundle (callers pass the resolved path).
_STAGED_SUFFIX = "-update.app"


def staged_bundle_path(bundle: str) -> str:
    """Where a downloaded update for `bundle` waits until the next quit."""
    parent, name = os.path.split(bundle.rstrip(os.sep))
    stem = name[:-4] if name.endswith(".app") else name
    return os.path.join(parent, "." + stem + _STAGED_SUFFIX)


def bundle_version(bundle: str) -> str | None:
    """CFBundleShortVersionString of a .app bundle, None when unreadable."""
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None


def staged_version(bundle: str) -> str | None:
    """The version staged beside `bundle`, ONLY when it is newer than the
    bundle itself; None otherwise. The "newer" test is the rule every
    reader shares (this module, the update manager's `_disk_version`, and
    the quit-time apply): a staged copy that is not newer — the user dragged
    a newer DMG over the app while an older download sat staged — must never
    win, or the quit would downgrade the app and the restart dialog would
    relaunch into it."""
    staged = staged_bundle_path(bundle)
    if not os.path.isdir(staged):
        return None
    version = bundle_version(staged)
    if version is None:
        return None
    current = bundle_version(bundle)
    if current is not None:
        from fused_render.update.common import is_newer  # leaf module, no cycle

        try:
            if not is_newer(version, current):
                return None
        except ValueError:
            return None
    return version


def installed_version() -> str | None:
    # `sys.frozen == "macosx_app"` (py2app's own marker) is checked before
    # the platform test, not after: the unit tests fake a py2app bundle by
    # setting `sys.frozen` while running on whatever host CI happens to use
    # (often Linux), and that faked marker has to keep meaning "read the
    # bundle's Info.plist" regardless of the real host platform, exactly as
    # it always has.
    if getattr(sys, "frozen", None) == "macosx_app":
        # sys.executable is …/Contents/MacOS/python (see fusedcli.setup_cli_hint).
        # realpath, not abspath: the staged sibling lives next to the REAL
        # bundle (see `staged_bundle_path`), and a symlinked bundle's link
        # would otherwise be looked beside.
        contents = os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))
        bundle = os.path.dirname(contents)
        staged = staged_version(bundle)
        if staged is not None:
            return staged
        return bundle_version(bundle)
    if sys.platform.startswith("linux"):
        from fused_render.supervisor._linux import startup
        appimage = startup.appimage_path()
        return _linux_installed_version(str(appimage) if appimage is not None else None)
    return None
