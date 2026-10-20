from __future__ import annotations

from fastapi import APIRouter, Body, Header

from fused_render import claude_remote
from fused_render.server.common import _error, _require_fused

router = APIRouter()


@router.post("/api/claude/remote-control")
def api_claude_remote_control(body: dict = Body(default={}),
                              x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    action = body.get("action")
    session_id = str(body.get("session_id") or "")
    if action == "start":
        return claude_remote.start(str(body.get("file") or ""), session_id,
                                   str(body.get("name") or "").strip())
    if action == "status":
        return claude_remote.status(session_id)
    if action == "stop":
        return claude_remote.stop(session_id)
    return _error(f"unknown remote control action: {action!r}")
