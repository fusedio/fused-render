"""POST /api/community — the community marketplace backend (fused_render/
community.py), for the /apps hub's Showcase tab.

A sync def on purpose: community.main shells out to git, and FastAPI runs
sync endpoints on its threadpool, keeping those subprocess waits off the
event loop. community.main returns {status:"error", message} for its own
user-facing failures, so the endpoint never raises for those — the client
(platform/lib/community.ts) surfaces the message verbatim.

Retired while the Marketplace is on (docs/CONTRIB_LOOP.md §18.5, the
owner's "Showcase" default): the answer is `{status: "retired"}` and the
shell hides the tab. Only the answer changes — the clone on disk is left
alone (the owner keeps the folder) and `community.main` is untouched, so
turning the Marketplace off brings the tab back as it was.
"""
from fastapi import APIRouter, Body

from fused_render import community

router = APIRouter()


def _retired() -> bool:
    try:
        from fused_render.shell import prefs

        return prefs.marketplace_enabled()
    except Exception:  # noqa: BLE001 — unreadable prefs read as off (the tab stays)
        return False


@router.post("/api/community")
def api_community(body: dict = Body(...)):
    if _retired():
        return {"status": "retired",
                "message": "The Showcase is now the Marketplace."}
    return community.main(action=str(body.get("action") or "catalog"))
