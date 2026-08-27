"""GET/POST /api/git-upstream — repos with a known upstream update, and the
opt-in actions the activity card's repo-update rows offer (SPEC §36).

GET is read-only and cheap: the check itself runs off the request path
(fused_render/git_upstream.py), throttled per repo root and triggered from
GET /render's D301 block. It only reads the in-memory result that check
populates — never shells out to git, never blocks — so it is a plain sync
def polled by the frontend dock.

POST runs one of the two mutations (`action: "update"` or `"switch"`) the
card's buttons call, each against one `root`. Two guards, not one:
`X-Fused` (D3) so a blind cross-origin POST from an unrelated open page
can't reach it at all — the same guard every other mutating POST carries
(`server/common.py::_require_fused`, e.g. `routers/ai_models.py`'s delete
endpoint) — and, on top of that, `root` must be a path THIS server's own
background check has already recorded state for
(`git_upstream.is_known_repo`), never an arbitrary client-supplied path.
The second guard matters even same-origin: without it, any page open in
the app could POST `{"action": "update", "root": "/any/repo/on/disk"}` and
mutate a repository the card never showed a row for. A sync def for the
same reason community.py's endpoint is: these shell out to git, and
FastAPI's threadpool keeps that off the event loop. A `"rebase"` action
was accepted here for one release, backing a secondary Rebase button the
card offered; it is refused now (falls through to `bad-action`) — the
button was removed as too dangerous to offer (D554 amendment) and the
mutation went with it.
"""
from fastapi import APIRouter, Body, Header

from fused_render import git_upstream
from fused_render.server.common import _require_fused

router = APIRouter()


@router.get("/api/git-upstream")
def api_git_upstream():
    return {"repos": git_upstream.known_repos()}


@router.post("/api/git-upstream")
def api_git_upstream_action(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    action = str(body.get("action") or "")
    root = str(body.get("root") or "")
    if not root:
        return {"ok": False, "reason": "missing", "message": "no repo root given"}
    if not git_upstream.is_known_repo(root):
        # Same {ok, reason, message} shape every other refusal here has
        # (not a raw 403/_error), so the card renders it the ordinary way
        # rather than needing a special case for this one failure mode.
        return {"ok": False, "reason": "unknown-repo",
                "message": "that repository is not one this app has checked."}
    if action == "update":
        return git_upstream.update_repo(root)
    if action == "switch":
        return git_upstream.switch_repo(root)
    return {"ok": False, "reason": "bad-action",
            "message": f"unknown action {action!r}"}
