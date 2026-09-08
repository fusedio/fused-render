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

Detection is TWO-STAGE (amends D486's single in-process probe). macOS caches
a process's TCC verdict for its lifetime — that is what the "Quit & Reopen"
dialog in the Settings pane is about — so once this process has probed
not-granted, the grant the user just made is invisible to it until relaunch.
The in-process probe therefore only answers "can THIS process read". When it
says no, `snapshot()` asks a FRESH CHILD (`/bin/ls` on the same target): a
child of the app bundle is attributed to the bundle's TCC identity but has
no cached verdict, so it sees the grant live. Child yes + self no is the
`pending_relaunch` state the shell turns into a Relaunch button
(fused-render://relaunch?reason=fda), instead of a "waiting…" spinner that
can never finish. The child probe is memoized for a few seconds because
/api/config is polled by several surfaces across tabs.

Every consumer — the onboarding step, the Home/Apps/explorer strip — reads
the ONE `fda` field of /api/config, and every route that hits a
PermissionError reports it through the ONE `refused()` helper (or the
server's PermissionError backstop handler, for routes that never caught it),
so "denied" means the same thing everywhere.
"""
import logging
import os
import subprocess
import sys
import time

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)

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


#: How long one child-probe answer stands in for the next. /api/config is
#: polled every few seconds by the status banner, the strip and the wizard
#: step, in every open tab; a fork per poll would be silly, and a grant
#: landing a couple of seconds late is invisible next to the relaunch it needs.
CHILD_PROBE_TTL_S = 3.0

#: Errno texts /bin/ls prints for a refused read: TCC denies as EPERM
#: ("Operation not permitted"), classic mode bits as EACCES.
_REFUSED_TEXTS = ("operation not permitted", "permission denied")

_child_memo: tuple[float, bool | None] = (0.0, None)


def _child_probe_target() -> str | None:
    """The first listdir probe target that exists. Directory targets only:
    `ls` on the TCC.db path is a stat, which succeeds without FDA — the
    child has to READ the directory (readdir), same as the in-process probe's
    os.listdir, or a bare stat would fake a grant on every install."""
    for raw, how in _PROBES:
        if how != "listdir":
            continue
        path = os.path.expanduser(raw)
        if os.path.exists(path):
            return path
    return None


def child_granted() -> bool | None:
    """Whether Full Disk Access is granted to a FRESHLY SPAWNED child of this
    process — i.e. to the app's TCC identity as of right now, uncached.

    True when `ls <FDA-gated dir>` succeeds, False when it is refused, None
    when there is no target to ask about or `ls` failed for another reason.
    Memoized for CHILD_PROBE_TTL_S. Never called on its own by the shell: it
    only matters once the in-process probe has said no (see snapshot()).
    """
    global _child_memo
    if os.environ.get(FORCE_ENV) == "demo":
        return False
    now = time.monotonic()
    stamp, cached = _child_memo
    if now - stamp < CHILD_PROBE_TTL_S:
        return cached
    result: bool | None = None
    target = _child_probe_target()
    if target is not None:
        try:
            # No -d and no trailing slash: those make ls stat the entry, which
            # succeeds without FDA. Plain `ls <dir>` opens and reads it.
            proc = subprocess.run(
                ["/bin/ls", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None:
            if proc.returncode == 0:
                result = True
            elif any(t in proc.stderr.lower() for t in _REFUSED_TEXTS):
                result = False
    _child_memo = (now, result)
    return result


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

    `granted` is what THIS process can do. `pending_relaunch` is the
    two-stage verdict (module docstring): this process cannot, but a fresh
    child can, so the grant has landed and only a relaunch stands between
    the user and it. Never both true.

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
    pending = (not state) and child_granted() is True
    return {"granted": state, "pending_relaunch": pending, "denied": denied}


def refused(path: str, exc: BaseException) -> JSONResponse:
    """THE answer to a refused read: record the denial for the warning and
    build the 403 the explorer keys its Full Disk Access card on. Every fs
    route that catches a PermissionError returns this, so the wire shape and
    the side effect cannot drift apart between routes."""
    note_denied(exc)
    return JSONResponse({"error": f"cannot read {path}: {exc}"}, status_code=403)


async def permission_error_handler(request, exc: PermissionError) -> JSONResponse:
    """Backstop for a PermissionError no route caught: it would otherwise
    surface as a generic 500 and the warning would never hear about the one
    failure it exists to explain. Registered by the server for the whole app;
    a route that handles its own denial (through refused()) never gets here.
    Logged with the request line — the 500 handler this preempts wrote a
    traceback, and a surprise denial must stay traceable."""
    path = request.query_params.get("path") or request.url.path
    logger.warning(
        "permission denied (uncaught) %s %s: %s", request.method, request.url.path, exc
    )
    return refused(path, exc)


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
