"""GET/POST /api/git-upstream — repos with a known upstream update, and the
opt-in actions the activity card's repo-update rows offer (SPEC §36).

GET is read-only and cheap: the check itself runs off the request path
(fused_render/git_upstream.py), throttled per repo root and triggered from
GET /render's D301 block. It only reads the in-memory result that check
populates — never shells out to git, never blocks — so it is a plain sync
def polled by the frontend dock.

POST runs one of the two mutations (`action: "update"` or `"rebase"`) the
card's buttons call, each against one `root` (a repo root the GET response
just named — never a client-typed path). A sync def for the same reason
community.py's endpoint is: these shell out to git, and FastAPI's threadpool
keeps that off the event loop.
"""
from fastapi import APIRouter, Body

from fused_render import git_upstream

router = APIRouter()


@router.get("/api/git-upstream")
def api_git_upstream():
    return {"repos": git_upstream.known_repos()}


@router.post("/api/git-upstream")
def api_git_upstream_action(body: dict = Body(...)):
    action = str(body.get("action") or "")
    root = str(body.get("root") or "")
    if not root:
        return {"ok": False, "reason": "missing", "message": "no repo root given"}
    if action == "update":
        return git_upstream.update_repo(root)
    if action == "rebase":
        return git_upstream.rebase_repo(root)
    return {"ok": False, "reason": "bad-action",
            "message": f"unknown action {action!r}"}
