"""Self-update endpoints (update/mac.py). Status itself rides /api/config's
`update` field so the shell's existing 5s poll carries it — these POSTs only
trigger work. Both mutate (network + a bundle swap), so they carry the D3
X-Fused guard; they 404 when no update manager is running (dev server, CLI,
Windows/Linux packages — those update through the supervisor's own path)."""
from fastapi import APIRouter, Header, HTTPException

from fused_render.server.common import _require_fused
from fused_render.update import mac as mac_update

router = APIRouter()


def _manager():
    manager = mac_update.manager()
    if manager is None:
        raise HTTPException(status_code=404, detail="self-update is not available here")
    return manager


@router.post("/api/update/check")
def api_update_check(x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    return _manager().check()


@router.post("/api/update/install")
def api_update_install(x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    return _manager().install()
