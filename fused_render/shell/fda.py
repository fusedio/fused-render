"""Full Disk Access detection + the one-time nudge's backend (macOS).

The packaged app triggers a separate TCC prompt per protected-folder
category (Desktop, Documents, Downloads, removable volumes, network
volumes) the first time it reads under each one. Full Disk Access — granted
once in System Settings — silences all of them permanently, and because the
release build is Developer ID signed with a stable bundle id (D73), the
grant survives upgrades. macOS has no API to request FDA: an app can only
DETECT it and open the right Settings pane. That is everything this module
does; the shell renders the warning strip (platform/ui/FdaStrip.tsx, same
posture as the Claude Code setup strip) off the `fda` field /api/config
gets from snapshot(). The strip shows on the first PermissionError an fs
route actually hits (note_denied) — the moment trouble is real, not at
launch. The per-folder prompts can be lost entirely (a backend read under
a protected folder while the app is not frontmost records a silent deny),
which used to strand the user on "permission denied" with no prompt ever
shown and no explanation; the denial itself is now the trigger, so that
silent-deny case still surfaces the warning without nagging every fresh
install up front.

Detection probes paths that only FDA unlocks. FDA-class paths never raise a
TCC prompt (unlike the per-folder categories) — a failed probe is silent,
so probing on every /api/config read costs nothing but a stat.
"""
import os
import subprocess
import sys

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

router = APIRouter()

#: Deep-link straight to System Settings -> Privacy & Security -> Full Disk
#: Access. The legacy `com.apple.preference.security` anchor still routes on
#: macOS 13+'s System Settings.
SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
)

#: Force the nudge on ("1") or off ("0") regardless of packaging — a dev
#: server is never `sys.frozen == "macosx_app"`, so without the override the
#: banner could not be exercised outside a DMG install. "demo" additionally
#: forces the probe to answer not-granted: a terminal-launched dev server
#: inherits the TERMINAL's TCC identity, which on a dev machine usually has
#: FDA already, so "1" alone often probes True and renders nothing.
FORCE_ENV = "FUSED_RENDER_FDA_BANNER"

#: FDA-gated probe targets, each `(path, how)`. `listdir` needs the dir's
#: contents (a bare stat on the dir succeeds without FDA); `read` opens the
#: file and reads one byte. Several candidates because none is guaranteed to
#: exist on every install — the first that exists decides.
_PROBES: list[tuple[str, str]] = [
    ("~/Library/Safari", "listdir"),
    ("~/Library/Mail", "listdir"),
    ("/Library/Application Support/com.apple.TCC/TCC.db", "read"),
]


def offered() -> bool:
    """Whether this process should surface the FDA nudge at all.

    True only for the packaged macOS app (`py2app` sets `sys.frozen` to
    "macosx_app") — a dev-from-source server's TCC identity is the terminal
    that launched it, so a grant would land on the wrong app. FORCE_ENV
    overrides both ways for testing.
    """
    force = os.environ.get(FORCE_ENV)
    if force in ("1", "demo"):
        return True
    if force == "0":
        return False
    return sys.platform == "darwin" and getattr(sys, "frozen", None) == "macosx_app"


def granted() -> bool | None:
    """Whether Full Disk Access is granted to THIS process.

    True on the first probe that succeeds, False on the first that raises
    PermissionError, None when every probe target is missing (no basis to
    nag — the caller must treat None as "don't show anything").
    """
    if os.environ.get(FORCE_ENV) == "demo":
        return False
    for raw, how in _PROBES:
        path = os.path.expanduser(raw)
        try:
            if how == "listdir":
                os.listdir(path)
            else:
                with open(path, "rb") as fh:
                    fh.read(1)
            return True
        except PermissionError:
            return False
        except OSError:
            continue
    return None


#: Whether THIS session has hit a PermissionError on an fs route — the moment
#: macOS actually refused a read, prompted or silent. The strip renders only
#: after that: a user whose files all open fine never needs a word about Full
#: Disk Access. In-memory on purpose — persisting it would bring the strip
#: back at launch on every later session, which is the up-front nag this
#: trigger exists to avoid. Bare bool write under the GIL; no lock needed.
_denied = False


def note_denied(exc: BaseException) -> None:
    """Record an fs-route failure; flips the session's denied flag when it is
    a PermissionError. Called from the fs routes' error paths — off the hot
    path by construction, since a request only lands here once it has already
    failed."""
    global _denied
    if _denied or not offered():
        return
    if isinstance(exc, PermissionError):
        _denied = True


def snapshot() -> dict | None:
    """The `fda` field of /api/config, or None to omit it.

    Omitted when the warning isn't offered (non-mac, dev server) and when the
    probe is inconclusive — an absent field is the shell's "render nothing
    AND stop watching", so uncertainty never nags and never polls.

    `denied` is the trigger: the strip renders only while the current denial
    stands unacknowledged. Dismissing clears it ON THE SERVER (every tab
    converges), and the NEXT PermissionError raises it again — an ✕ is "not
    now", not "never": a user still hitting refused reads still needs the
    warning. "demo" forces it, so a dev server can render the strip without
    manufacturing a real denial.
    """
    if not offered():
        return None
    state = granted()
    if state is None:
        return None
    denied = _denied or os.environ.get(FORCE_ENV) == "demo"
    return {"granted": state, "denied": denied}


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _require_offered() -> JSONResponse | None:
    if not offered():
        return JSONResponse({"error": "not available"}, status_code=404)
    return None


@router.post("/api/fda/settings")
def api_fda_settings(x_fused: str | None = Header(default=None)):
    """Open System Settings on the Full Disk Access pane."""
    guard = _require_fused(x_fused) or _require_offered()
    if guard is not None:
        return guard
    # `open` returns immediately; no output worth capturing. Popen (not run)
    # so a wedged LaunchServices can't hold the request thread.
    subprocess.Popen(
        ["open", SETTINGS_URL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True}


@router.post("/api/fda/dismiss")
def api_fda_dismiss(x_fused: str | None = Header(default=None)):
    """Acknowledge the current denial: clears the server-side flag so every
    tab's strip hides. The next PermissionError raises it again."""
    guard = _require_fused(x_fused) or _require_offered()
    if guard is not None:
        return guard
    global _denied
    _denied = False
    return {"ok": True}
