"""POST /api/community — the community marketplace backend (fused_render/
community.py), for the /apps hub's Showcase tab and the explorer preview's
Clone button.

A sync def on purpose: community.main shells out to git, and FastAPI runs
sync endpoints on its threadpool, keeping those subprocess waits off the
event loop. community.main returns {status:"error", message} for its own
user-facing failures, so the endpoint never raises for those — the client
(platform/lib/community.ts) surfaces the message verbatim.
"""
from fastapi import APIRouter, Body

from fused_render import community

router = APIRouter()


@router.post("/api/community")
def api_community(body: dict = Body(...)):
    return community.main(
        action=str(body.get("action") or "catalog"),
        slug=str(body.get("slug") or ""),
        force=bool(body.get("force")),
    )
