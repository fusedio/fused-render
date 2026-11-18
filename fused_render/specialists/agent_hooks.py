"""The router's post-dispatch hook for agent calls (PLAN A.5 `claude_agent.py`, C.2).

`after_agent_call(action, params, envelope)` runs after every `/api/claude/agent` call while the flag is on:

* `start` with a `specialist` param: main chat -> `chat.record_start`; breakout -> the minted session on its record.
* `decide` deny -> signal `deny`; `cancel` -> `stop`; `snapshot_revert` -> `undo` (against the task session).

Never raises (the router wraps it too).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _result(envelope) -> dict:
    res = envelope.get("result") if isinstance(envelope, dict) else None
    return res if isinstance(res, dict) and not res.get("error") else {}


def _session_of_run(run_id) -> str:
    try:
        from fused_render.experts import learn

        return learn.caller(run_id).get("session_id") or ""
    except Exception:  # noqa: BLE001
        return ""


def _offer_card(run_id, request_id) -> bool:
    """Whether card `request_id` of run `run_id` is a specialist_breakout offer."""
    import json
    import os
    import re

    ok = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
    if not (isinstance(run_id, str) and ok.match(run_id) and isinstance(request_id, str) and ok.match(request_id)):
        return False
    try:
        from fused_render.claude_agent import agent_module

        path = os.path.join(agent_module().RUNS, run_id, "perm", request_id + ".req.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("tool") == "mcp__fused_render__specialist_breakout"
    except (OSError, ValueError, AttributeError):
        return False


def after_agent_call(action: str, params: dict, envelope: dict) -> None:
    try:
        _after(action, params if isinstance(params, dict) else {}, envelope)
    except Exception:  # noqa: BLE001
        logger.exception("specialists: after agent call %s", action)


def _after(action: str, params: dict, envelope: dict) -> None:
    from fused_render.specialists import signals

    res = _result(envelope)
    if action == "start":
        key = str(params.get("specialist") or "")
        sid = str(res.get("session_id") or "")
        if not key or not sid:
            return
        parts = key.split(":")
        if len(parts) >= 2 and parts[1] == "main":
            from fused_render.specialists import chat

            chat.record_start(parts[0], sid)
        elif len(parts) == 3 and parts[1] == "breakout":
            from fused_render.specialists import breakouts

            breakouts.set_session(parts[0], parts[2], sid)
        return
    if action == "decide":
        if str(params.get("decision") or "") == "allow" or not res:
            return
        if _offer_card(params.get("run_id"), params.get("request_id")):
            return  # "Keep trying" on an offer is not the user refusing the task's work
        sid = _session_of_run(params.get("run_id"))
        if sid:
            signals.record(sid, "deny", {"request_id": str(params.get("request_id") or "")})
        return
    if action == "cancel":
        sid = str(res.get("session_id") or "") or _session_of_run(params.get("run_id"))
        if sid:
            signals.record(sid, "stop", {"run_id": str(params.get("run_id") or "")})
        return
    if action == "snapshot_revert":
        sid = str(params.get("session_id") or "")
        if sid and res:
            signals.record(sid, "undo", {"version_id": str(params.get("version_id") or "")})
