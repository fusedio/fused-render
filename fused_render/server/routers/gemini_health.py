"""/api/gemini/* — is Google Gemini CLI usable on this machine, and the three
repairs. The twin of routers/copilot_health.py (docs/GEMINI_BACKEND.md D10),
route for route. Same bodies, same X-Fused rules: reads are bare, anything
that spawns or writes carries the guard.

**A disabled Gemini is never spawned (D17).** With the Preferences switch off,
the health read answers the cached snapshot or null and never measures, and
every POST but `login/cancel` answers 400 with `registry.disabled_error`'s
body. The Preferences row only mounts with the switch on, so no UI meets the
400.
"""
import logging

from fastapi import APIRouter, Body, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from fused_render import gemini_health
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
    if not registry.is_enabled("gemini"):
        return JSONResponse(registry.disabled_error("gemini"), status_code=400)
    return None


@router.get("/api/gemini/health")
async def api_gemini_health():
    """The cached snapshot. A read — no guard; threadpool because a cold
    cache measures inline. Disabled: the cache or null, never a measure."""
    if not registry.is_enabled("gemini"):
        cached = gemini_health.cached()
        return gemini_health._public(cached) if cached else None
    return await run_in_threadpool(gemini_health.summary)


@router.post("/api/gemini/health/refresh")
async def api_gemini_health_refresh(x_fused: str | None = Header(default=None)):
    """Re-probe now, ignoring the cache — "Check again". Guarded: it spawns."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate
    return await run_in_threadpool(gemini_health.summary_refreshed)


@router.post("/api/gemini/install")
async def api_gemini_install(body: dict = Body(default=None),
                            x_fused: str | None = Header(default=None)):
    """Run `npm install -g @google/gemini-cli` in the background — the install and
    the update are the same line."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate
    from fused_render import gemini_install

    action = (body or {}).get("action", "install")
    try:
        return await run_in_threadpool(gemini_install.start, action)
    except gemini_install.InstallError as e:
        return _error(str(e), status=409)


@router.get("/api/gemini/install")
async def api_gemini_install_status():
    """The current install/update record. A read — no guard, no spawn."""
    from fused_render import gemini_install

    return gemini_install.status()


@router.post("/api/gemini/link-path")
async def api_gemini_link_path(x_fused: str | None = Header(default=None)):
    """Append the PATH line to the user's shell rc — the fix for a CLI the app
    can see and the terminal cannot. Guarded: it writes a shell profile."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate

    def _run():
        result = gemini_health.add_to_shell_path()
        if result.get("ok"):
            try:
                gemini_health.summary_refreshed()
            except Exception:  # noqa: BLE001 - the write, not the re-probe, is the fix
                logger.warning("PATH line written but the health re-probe failed")
        return result

    result = await run_in_threadpool(_run)
    if not result.get("ok"):
        return _error(result.get("error") or "could not update the shell profile",
                      status=409)
    return result


@router.post("/api/gemini/login")
async def api_gemini_login(x_fused: str | None = Header(default=None)):
    """Start a browser sign-in (ACP `authenticate`, Google OAuth), and return
    the opening record. Guarded: spawns a process and opens a browser
    window."""
    gate = _gate(x_fused)
    if gate is not None:
        return gate
    from fused_render import gemini_login

    try:
        return await run_in_threadpool(gemini_login.start)
    except gemini_login.LoginError as e:
        return _error(str(e), status=409)


@router.get("/api/gemini/login")
async def api_gemini_login_status():
    """The current sign-in record. A read — no guard, no spawn."""
    from fused_render import gemini_login

    return gemini_login.status()


@router.post("/api/gemini/login/cancel")
async def api_gemini_login_cancel(x_fused: str | None = Header(default=None)):
    """Stop a sign-in the user gave up on. Idempotent; guarded because it
    signals a process. NOT behind the D17 switch: it only signals our own
    child, and a Gemini turned off mid-sign-in must still be stoppable."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import gemini_login

    return await run_in_threadpool(gemini_login.cancel)
