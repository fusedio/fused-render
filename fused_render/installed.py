"""Version of fused-render installed on disk, as opposed to the one running.

A DMG install replaces the .app bundle in place while an old process keeps
running its already-loaded code — so the bundle's Info.plist is the ground
truth for "what would launch next time", and comparing it to the in-memory
__version__ tells the shell to ask for an app restart (not a page refresh,
which can't swap the server process).

Only the py2app path is covered: unpackaged runs (dev checkouts, pip
installs) and the Windows/Linux packages report None, which the shell reads
as "no restart signal available".
"""
import os
import plistlib
import sys


def installed_version() -> str | None:
    if getattr(sys, "frozen", None) != "macosx_app":
        return None
    # sys.executable is …/Contents/MacOS/python (see fusedcli.setup_cli_hint).
    contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    try:
        with open(os.path.join(contents, "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None
