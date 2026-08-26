"""GET /api/git-upstream — repos with a known upstream update, for the
activity card's repo-update rows (SPEC §36).

Read-only and cheap: the check itself runs off the request path
(fused_render/git_upstream.py), throttled per repo root and triggered from
GET /render's D301 block. This endpoint only reads the in-memory result that
check populates — it never shells out to git and never blocks, so it is a
plain sync def polled by the frontend dock.
"""
from fastapi import APIRouter

from fused_render import git_upstream

router = APIRouter()


@router.get("/api/git-upstream")
def api_git_upstream():
    return {"repos": git_upstream.known_repos()}
