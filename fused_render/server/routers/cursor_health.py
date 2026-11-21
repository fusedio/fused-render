"""/api/cursor/* — is Cursor usable on this machine, and the three
repairs. The twin of routers/gemini_health.py (docs/OPENCODE_BACKEND.md D10),
route for route. Same bodies, same X-Fused rules: reads are bare, anything
that spawns or writes carries the guard.

**A disabled Cursor is never spawned (D17).** With the Preferences switch off,
the health read answers the cached snapshot or null and never measures, and
every POST but `login/cancel` answers 400 with `registry.disabled_error`'s
body. The Preferences row only mounts with the switch on, so no UI meets the
400.
"""
import logging

from fastapi import APIRouter, Body, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from fused_render import cursor_health
from fused_render.agent_backends import registry
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()


def _gate(x_fused: str | None):
    """The X-Fused guard, then the D17 switch — the response to answer
    instead, or None to go ahead."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not registry.is_enabled("cursor"):
        return JSONResponse(registry.disabled_error("cursor"), status_code=400)
    return None


@router.get("/api/cursor/health")
async def api_cursor_health():
    """The cached snapshot. A read — no guard; threadpool because a cold
    cache measures inline. Disabled: the cache or null, never a measure."""
    if not registry.is_enabled("cursor"):
        cached = cursor_health.cached()
        return cursor_health._public(cached) if cached else None
    return await run_in_threadpool(cursor_health.summary)


@router.post("/api/cursor/health/refresh")
async def api_cursor_health_refresh(x_fused: str | None = Header(default=None)):
    """Re-probe now, ignoring the cache — "Check again". Guarded: it spawns."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate
    return await run_in_threadpool(cursor_health.summary_refreshed)


@router.post("/api/cursor/install")
async def api_cursor_install(body: dict = Body(default=None),
                            x_fused: str | None = Header(default=None)):
    """Run Cursor's installer (`curl https://cursor.com/install -fsS | bash`)
    or, with `action: "update"`, `cursor-agent update`, in the background."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate
    from fused_render import cursor_install

    action = (body or {}).get("action", "install")
    try:
        return await run_in_threadpool(cursor_install.start, action)
    except cursor_install.InstallError as e:
        return _error(str(e), status=409)


@router.get("/api/cursor/install")
async def api_cursor_install_status():
    """The current install/update record. A read — no guard, no spawn."""
    from fused_render import cursor_install

    return cursor_install.status()


@router.post("/api/cursor/link-path")
async def api_cursor_link_path(x_fused: str | None = Header(default=None)):
    """Append the PATH line to the user's shell rc — the fix for a CLI the app
    can see and the terminal cannot. Guarded: it writes a shell profile."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate

    def _run():
        result = cursor_health.add_to_shell_path()
        if result.get("ok"):
            try:
                cursor_health.summary_refreshed()
            except Exception:  # noqa: BLE001 - the write, not the re-probe, is the fix
                logger.warning("PATH line written but the health re-probe failed")
        return result

    result = await run_in_threadpool(_run)
    if not result.get("ok"):
        return _error(result.get("error") or "could not update the shell profile",
                      status=409)
    return result


@router.post("/api/cursor/login")
async def api_cursor_login(x_fused: str | None = Header(default=None)):
    """Start `cursor-agent login` (D10: a browser sign-in the CLI opens and
    polls for itself), and return the opening record. Guarded: spawns a
    process."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate
    from fused_render import cursor_login

    try:
        return await run_in_threadpool(cursor_login.start)
    except cursor_login.LoginError as e:
        return _error(str(e), status=409)


@router.get("/api/cursor/login")
async def api_cursor_login_status():
    """The current sign-in record. A read — no guard, no spawn."""
    from fused_render import cursor_login

    return cursor_login.status()


@router.post("/api/cursor/login/cancel")
async def api_cursor_login_cancel(x_fused: str | None = Header(default=None)):
    """Stop a sign-in the user gave up on. Idempotent; guarded because it
    signals a process. NOT behind the D17 switch: it only signals our own
    child, and an Cursor turned off mid-sign-in must still be stoppable."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import cursor_login

    return await run_in_threadpool(cursor_login.cancel)
