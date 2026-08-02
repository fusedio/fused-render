"""Linux pasteboard backend — wl-clipboard on Wayland, xclip on X11.

There is no OS-owned clipboard on X11 or Wayland: a live process must own the
selection and answer conversion requests for it. We are a web app with a
Python backend, not a toolkit client, so we shell out to whichever helper is
installed — `wl-copy`/`wl-paste` first (Wayland, and it works under XWayland
too), then `xclip`. With neither present the bridge reports unsupported and
the app keeps its existing in-app-only clipboard.

The two file managers disagree on the format:

  GNOME / Nautilus  x-special/gnome-copied-files   "copy\\nfile:///a\\nfile:///b"
  KDE / Dolphin     text/uri-list                  "file:///a\\r\\nfile:///b"

Reading tries both, so a paste *into* fused-render works on either desktop.
Writing can only publish one target per invocation — `xclip`/`wl-copy` take a
single `--type`, and a second call replaces the first rather than adding to it
— so the write picks the family the session actually reports via
`XDG_CURRENT_DESKTOP`. That's the one real platform limitation of this
feature; a resident GTK/Qt owner process could offer both targets at once and
is the documented upgrade path if it proves limiting.

Everything here is driven through `shutil.which` and `subprocess.run`, both
looked up on the module at call time, so the tests fake them and run on any
platform.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from urllib.parse import quote, unquote

GNOME_TARGET = "x-special/gnome-copied-files"
URI_LIST_TARGET = "text/uri-list"

# Read order: the GNOME target first because it's the only one carrying the
# copy/cut verb, so a desktop that offers both tells us more via that one.
READ_TARGETS = (GNOME_TARGET, URI_LIST_TARGET)

# A short timeout everywhere: these are local, instant tools, and a hung
# wl-paste (no compositor, no display) must not stall a focus-time read.
_TIMEOUT_S = 3


class NoClipboardTool(RuntimeError):
    """Neither wl-clipboard nor xclip is installed."""


# --------------------------------------------------------------- URI helpers

def path_to_uri(path: str) -> str:
    """Absolute path -> a file:// URI with per-segment percent-encoding.

    `safe="/"` keeps the separators literal — encoding them would collapse the
    whole path into one URI segment, which no file manager accepts.
    """
    return "file://" + quote(path, safe="/")


def uri_to_path(uri: str) -> str | None:
    """file:// URI -> absolute path, or None for anything else (http URLs get
    dropped on the floor rather than pasted as a nonsense path)."""
    if not uri.startswith("file://"):
        return None
    return unquote(uri[len("file://"):])


# ------------------------------------------------------------ tool selection

def _tool() -> tuple[list[str], list[str]]:
    """(read argv prefix, write argv prefix) for the best available helper.

    wl-clipboard is only usable if *both* halves are present — a half install
    that can copy but not paste is worse than falling through to xclip, which
    does both.
    """
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        return (["wl-paste", "--no-newline", "--type"], ["wl-copy", "--type"])
    if shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard", "-o", "-t"],
                ["xclip", "-selection", "clipboard", "-i", "-t"])
    raise NoClipboardTool(
        "no clipboard helper found — install wl-clipboard or xclip")


def _is_kde() -> bool:
    """Does the session report itself as KDE/Plasma?

    XDG_CURRENT_DESKTOP is a colon-separated list ("ubuntu:GNOME",
    "KDE:plasma"), so this is a substring test on the lowercased value rather
    than an equality check.
    """
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    return "kde" in desktop or "plasma" in desktop


# --------------------------------------------------------------------- read

def read_files() -> list[str]:
    read_argv, _ = _tool()
    for target in READ_TARGETS:
        proc = subprocess.run(
            read_argv + [target],
            capture_output=True, timeout=_TIMEOUT_S)
        if proc.returncode != 0 or not proc.stdout:
            # Non-zero here just means "the owner doesn't offer this target",
            # which is the normal answer for one of the two formats.
            continue
        paths = _parse(proc.stdout.decode("utf-8", "replace"))
        if paths:
            return paths
    return []


def _parse(text: str) -> list[str]:
    """Pull file paths out of either clipboard format.

    Both are line-based; the GNOME one prefixes a "copy"/"cut" verb line.
    Ignoring the verb is deliberate — cut is out of scope, and honouring it
    would mean deleting the user's source files on a guess.
    """
    paths = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line in ("copy", "cut"):
            continue
        p = uri_to_path(line)
        if p:
            paths.append(p)
    return paths


# -------------------------------------------------------------------- write

def write_files(paths: list[str]) -> None:
    _, write_argv = _tool()
    uris = [path_to_uri(p) for p in paths]
    if _is_kde():
        target = URI_LIST_TARGET
        payload = "\n".join(uris)
    else:
        target = GNOME_TARGET
        payload = "copy\n" + "\n".join(uris)

    proc = subprocess.run(
        write_argv + [target],
        input=payload.encode("utf-8"), capture_output=True, timeout=_TIMEOUT_S)
    if proc.returncode != 0:
        raise OSError(
            f"{write_argv[0]} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
