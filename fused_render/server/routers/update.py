"""Self-update endpoints (update/mac.py, update/linux.py). Status itself rides
/api/config's `update` field so the shell's existing 5s poll carries it —
these POSTs only trigger work. Both mutate (network + an artifact swap), so
they carry the D3 X-Fused guard; they 404 when no update manager is running
(dev server, CLI, or Windows — which updates entirely through the
supervisor's own path, supervisor/_win32/update.py, with no in-app
manager)."""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from fused_render import update
from fused_render.server.common import _require_fused

router = APIRouter()


def _manager():
    manager = update.manager()
    if manager is None:
        raise HTTPException(status_code=404, detail="self-update is not available here")
    return manager


@router.post("/api/update/check")
def api_update_check(x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    # Throttled (mac_update.MIN_CHECK_GAP_S): the client fires this when the
    # app comes back to the front, and a run of focus flips must not become a
    # run of CDN fetches. The auto loop's own tick passes force=True.
    return _manager().check()


class InstallRequest(BaseModel):
    # The `latest_version` the client's own status had on screen when the
    # button was pressed. UpdateManager.install() compares this against
    # what its pre-install recheck confirms is actually current, and defers
    # rather than installs on a mismatch — see its docstring for why a
    # server-side snapshot alone can't catch that race. Optional so an
    # older client build (with no such field) still gets the previous,
    # looser behavior instead of a hard validation error.
    expected_version: str | None = None


@router.post("/api/update/install")
def api_update_install(body: InstallRequest | None = None,
                       x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    return _manager().install(expected_version=body.expected_version if body else None)
