"""GET /api/claude/health — is Claude Code usable on this machine.

Its own endpoint rather than a field on /api/config, deliberately: /api/config is
read on every page load and by the status-banner poll, and the facts here are
backed by process spawns behind a disk cache. Bolting them onto that payload
would put a version probe on the hot path to pay for a strip that only renders
while something is wrong.

It is also NOT `/api/claude-config/status`, which stays what it is: one
`isdir(~/.claude)` answering "is there config to edit". That was always an honest
gate for the Preferences tab and was never a claim about whether Claude Code
works — this endpoint is that claim.
"""
import logging

from fastapi import APIRouter, Body, Header
from fastapi.concurrency import run_in_threadpool

from fused_render import claude_health
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/claude/health")
async def api_claude_health():
    """The cached snapshot. A read — no X-Fused guard.

    In a threadpool because a cold cache measures inline (a `--version` spawn,
    and possibly the login-shell probe), and blocking the event loop would stall
    every other request in the app — including the ones the page fires beside
    this one. A warm cache is a single small file read and returns immediately.
    """
    return await run_in_threadpool(claude_health.summary)


@router.post("/api/claude/health/refresh")
async def api_claude_health_refresh(x_fused: str | None = Header(default=None)):
    """Re-probe now, ignoring the cache — the "Check again" action.

    Carries the D3 X-Fused guard: it is a POST that spawns subprocesses (and on
    POSIX the user's login shell), which is not something a blind cross-origin
    form may kick off, even though what it writes is only a cache.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return await run_in_threadpool(claude_health.summary_refreshed)


# --- repairing what the report found -----------------------------------------
#
# Everything above establishes what is wrong. These three apply the fix, and they
# exist because the alternative — which is what shipped first — was a card that
# knew the answer and asked the user to go and type it in a terminal.


@router.post("/api/claude/install")
async def api_claude_install(body: dict = Body(default=None),
                             x_fused: str | None = Header(default=None)):
    """Run the native installer, or `claude update`, in the background.

    The X-Fused guard is not a formality here: this spawns a process that
    downloads and writes an executable into the user's home, which is the last
    thing a blind cross-origin POST may be allowed to start.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import claude_install

    action = (body or {}).get("action", "install")
    try:
        return await run_in_threadpool(claude_install.start, action)
    except claude_install.InstallError as e:
        # A refusal with a sentence the strip can show as-is — an update that
        # would no-op says which command WOULD work, and that text is the whole
        # value of the refusal.
        return _error(str(e), status=409)


@router.get("/api/claude/install")
async def api_claude_install_status():
    """The current install/update record. A read — no guard, no spawn."""
    from fused_render import claude_install

    return claude_install.status()


@router.post("/api/claude/doctor")
async def api_claude_doctor(x_fused: str | None = Header(default=None)):
    """`claude doctor` on demand — the answer to "the install is broken".

    The health snapshot already carries doctor's report whenever it ran one, but
    it only runs one when something looked wrong. This is the explicit "tell me
    what the CLI thinks of itself" action, so it always spawns.

    Guarded despite being read-only: it is a POST that spawns a subprocess, the
    same line every other probe endpoint here draws.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import claude_health as health

    def _run():
        path, _source = health.resolve(allow_shell=False)
        if not path or not health.executable(path):
            return {"ok": False, "doctor": None,
                    "error": "there is no Claude Code on this machine to diagnose"}
        report = health._doctor(path)
        if report is None:
            # Doctor could not be asked. That is itself the finding — two probes
            # have now failed to get a word out of this binary — so it is
            # reported as one rather than dressed up as a diagnosis.
            return {"ok": False, "doctor": None, "path": path,
                    "error": f"Claude Code is installed at {path} but would not "
                             "run its own diagnostics"}
        return {"ok": True, "doctor": report, "path": path}

    return await run_in_threadpool(_run)


# --- signing in ---------------------------------------------------------------
#
# The third repair, and the one that needed a second door rather than more code:
# `/login` is a TUI slash command, but `claude auth login` opens the browser and
# completes on its own loopback callback, so the app can start a real sign-in
# without ever handling an OAuth code. See fused_render/claude_login.py.
#
# These are separate endpoints rather than a third `/api/claude/install` action
# because this one waits on a person: it needs a cancel, and it must not occupy
# the install slot while a browser window sits open.


@router.post("/api/claude/login")
async def api_claude_login(x_fused: str | None = Header(default=None)):
    """Start a browser sign-in, and return the opening record.

    The X-Fused guard, like its neighbours: this spawns a process AND opens a
    browser window on the user's desktop, which is not something a blind
    cross-origin POST may do.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import claude_login

    try:
        return await run_in_threadpool(claude_login.start)
    except claude_login.LoginError as e:
        # A refusal with a sentence the strip shows as-is — "a sign-in is
        # already waiting in your browser" is the whole value of the 409, since
        # the window the user needs is already open behind the app.
        return _error(str(e), status=409)


@router.get("/api/claude/login")
async def api_claude_login_status():
    """The current sign-in record. A read — no guard, no spawn."""
    from fused_render import claude_login

    return claude_login.status()


@router.post("/api/claude/login/cancel")
async def api_claude_login_cancel(x_fused: str | None = Header(default=None)):
    """Stop a sign-in the user gave up on. Idempotent, and guarded because it
    signals a process."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import claude_login

    return await run_in_threadpool(claude_login.cancel)
