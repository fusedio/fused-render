"""Is there a usable git on this Mac — and the one button that installs one.

The wizard's Git step (frontend shell/onboarding/GitStep.tsx) and the
onboarding meter (shell/onboarding.py `_observe`) ask `probe()`; the step's
Install button calls `start_install()`. macOS only: the owner's call — the
step is `n/a` elsewhere and nothing here is tuned for Linux or Windows.

WHY NOT `shutil.which("git")`. Every Mac ships `/usr/bin/git`, whether or not
the Command Line Tools are installed: it is an xcode-select SHIM that, run on
a machine without them, pops Apple's "The git command requires the command
line developer tools" dialog and exits nonzero. `which` finds it, so it proves
nothing (community._require_git learned this the hard way, PR #994) — and
EXECUTING it is worse here: `_observe` runs on every /api/config read, every
few seconds, in every tab, and a dialog on a timer is not a setup step. So the
shim is never run. The probe walks the places a REAL git lives (the active
developer dir, the Command Line Tools, Xcode, Homebrew, MacPorts), and only a
binary found there is asked for its version — those do not prompt.

The developer dir is read off `/var/db/xcode_select_link`, the symlink
`xcode-select -p` prints, with os.readlink: no spawn. The Tools' own folder
and Xcode's are listed as well, for a machine where the link is stale or
points at an Xcode that was deleted.

`start_install()` runs `/usr/bin/xcode-select --install`, which does not
install anything itself: it asks Software Update to put up the Command Line
Tools dialog and returns at once (exit 0, "install requested"), or exits 1
with "already installed". The actual download happens in Apple's UI, outside
this window, so the step re-reads the stage when the window regains focus
and on its Check again button — the Chrome step's pattern. Homebrew's
`brew install git` is the other way and is shown as a line to copy; the app
does not run brew for the user.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

from fused_render.claude_health import SUBPROCESS_KWARGS

log = logging.getLogger(__name__)

#: The xcode-select shim. Listed so nothing below ever puts it in a candidate
#: list by mistake; it is compared against, never run.
SHIM = "/usr/bin/git"

#: What `xcode-select -p` reads: a symlink to the active developer directory.
XCODE_SELECT_LINK = "/var/db/xcode_select_link"

#: Where a real git lives on a Mac, in the order one is trusted. The active
#: developer dir goes first at probe time (it is a readlink, not a constant).
CANDIDATES = (
    "/Library/Developer/CommandLineTools/usr/bin/git",
    "/Applications/Xcode.app/Contents/Developer/usr/bin/git",
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/opt/local/bin/git",
)

#: How long one answer stands in for the next. /api/config is read every few
#: seconds by every open tab; a version spawn per read would be silly, and an
#: install landing a few seconds late is invisible next to the download it
#: took. Same figure as fda.CHILD_PROBE_TTL_S, for the same reason.
PROBE_TTL_S = 3.0

_memo: tuple[float, dict | None] = (0.0, None)


def offered() -> bool:
    """Whether this machine has a Git step at all. macOS only."""
    return sys.platform == "darwin"


def candidates() -> list[str]:
    """The paths to try, deduped, the active developer dir's git first.
    Never contains the shim."""
    out: list[str] = []
    try:
        dev = os.readlink(XCODE_SELECT_LINK)
        out.append(os.path.join(dev, "usr", "bin", "git"))
    except OSError:
        pass
    out.extend(CANDIDATES)
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p == SHIM or p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def _version(path: str) -> str | None:
    """`git --version` of a REAL binary (never the shim): "2.39.3" or None
    when it cannot run — a Tools folder left half-deleted, a Homebrew git
    whose keg is gone. Nonzero exit is "unusable", no stderr matching."""
    try:
        r = subprocess.run(
            [path, "--version"],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
            **SUBPROCESS_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    # "git version 2.39.3 (Apple Git-146)" → "2.39.3"
    parts = (r.stdout or "").strip().split()
    return parts[2] if len(parts) >= 3 and parts[0] == "git" else (r.stdout or "").strip() or None


def probe(force: bool = False) -> dict | None:
    """`{"path", "version"}` of the first usable git, or None when there is
    none. Memoized for PROBE_TTL_S; `force` drops the memo (the step's Check
    again, the install button). Never raises."""
    global _memo
    if not offered():
        return None
    now = time.monotonic()
    stamp, cached = _memo
    if not force and now - stamp < PROBE_TTL_S:
        return cached
    found: dict | None = None
    try:
        for path in candidates():
            if not os.path.isfile(path) or not os.access(path, os.X_OK):
                continue
            version = _version(path)
            if version is None:
                continue
            found = {"path": path, "version": version}
            break
    except Exception:  # noqa: BLE001 — a probe must not take /api/config down
        log.debug("git_tools: probe failed", exc_info=True)
        found = None
    _memo = (time.monotonic(), found)
    return found


#: Shown beside the Install button as the other way in. Not run by the app.
BREW_COMMAND = "brew install git"


def start_install() -> dict:
    """Ask macOS to install the Command Line Tools. Returns
    `{"ok": bool, "already": bool, "message": str}` — `ok` means the dialog
    was requested (or the tools were there already); the install itself is
    Apple's, outside this process, so there is nothing to poll here."""
    if not offered():
        return {"ok": False, "already": False, "message": "Git install is only offered on macOS."}
    if probe(force=True) is not None:
        return {"ok": True, "already": True, "message": "A git is already installed."}
    try:
        r = subprocess.run(
            ["/usr/bin/xcode-select", "--install"],
            capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
            **SUBPROCESS_KWARGS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "already": False, "message": f"could not start the installer: {exc}"}
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0:
        return {"ok": True, "already": False, "message": "Apple's installer dialog is open — follow it, then come back here."}
    if "already installed" in text.lower():
        # The Tools are there but no git ran — a half-removed folder. Say so;
        # Homebrew is the way round it.
        probe(force=True)
        return {"ok": False, "already": True,
                "message": "macOS says the Command Line Tools are already installed, but no git ran. "
                           f"Try `{BREW_COMMAND}` instead."}
    return {"ok": False, "already": False,
            "message": text.splitlines()[-1] if text else f"xcode-select exited with code {r.returncode}"}
