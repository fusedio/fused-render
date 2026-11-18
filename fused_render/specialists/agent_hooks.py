"""The router's post-dispatch hook for agent calls (PLAN A.5 `claude_agent.py`, C.2).

`after_agent_call(action, params, envelope)` runs after every `/api/claude/agent` call while the flag is on:

* `start` with a `specialist` param: main chat -> `chat.record_start`; breakout -> the minted session on its record.
* `decide` deny -> signal `deny`; `cancel` -> `stop`; `snapshot_revert` -> `undo` (against the task session).
* `decide` allow on a specialist session's service card -> the one-time grant `services.call` spends (CONTRACT
  C2). `agent._decide` calls `grant_from_decision` itself BEFORE writing the decision (so the call it lets through
  can never reach `services.call` first); the post-hook repeats it, which records nothing twice.

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


SERVICE_PREFIX = "mcp__specialist_services__"


def _main_cancelled(run_id) -> None:
    """Stop on a specialist's main chat: Claude Code fires no Stop hook for an interrupted turn, so the home's
    turn record is closed here (`history.main_turn_ended`)."""
    import json
    import os
    import re

    if not (isinstance(run_id, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$", run_id)):
        return
    try:
        from fused_render.claude_agent import agent_module
        from fused_render.specialists import history

        with open(os.path.join(agent_module().RUNS, run_id, "meta.json"), encoding="utf-8") as fh:
            sp = json.load(fh).get("specialist")
        if isinstance(sp, dict) and sp.get("kind") == "main" and sp.get("id"):
            history.main_turn_ended(str(sp["id"]))
    except (OSError, ValueError, AttributeError, ImportError):
        pass


def grant_from_decision(run_id, request_id) -> bool:
    """The user allowed card `request_id` of run `run_id`: when that card asks for a specialist service tool and
    the run is a specialist main chat / breakout, record the one-time grant for (specialist, kind, breakout,
    session, tool). Only the server calls this, on the user's decide; never raises."""
    import json
    import os
    import re

    ok = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
    if not (isinstance(run_id, str) and ok.match(run_id) and isinstance(request_id, str) and ok.match(request_id)):
        return False
    try:
        from fused_render.claude_agent import agent_module
        from fused_render.specialists import flag, launch

        if not flag.on():
            return False
        run_dir = os.path.join(agent_module().RUNS, run_id)
        with open(os.path.join(run_dir, "perm", request_id + ".req.json"), encoding="utf-8") as fh:
            tool = str(json.load(fh).get("tool") or "")
        if not tool.startswith(SERVICE_PREFIX):
            return False
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        sp = meta.get("specialist") if isinstance(meta, dict) else None
        if not isinstance(sp, dict) or sp.get("kind") not in ("main", "breakout") or not sp.get("id"):
            return False
        session = str(meta.get("session_id") or meta.get("resumed_from") or "")
        return launch.record_grant(str(sp["id"]), str(sp["kind"]), str(sp.get("breakout_id") or ""), session, tool,
                                   f"{run_id}/{request_id}")
    except (OSError, ValueError, AttributeError, ImportError):
        return False
    except Exception:  # noqa: BLE001
        logger.exception("specialists: could not record a service grant")
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
        if str(params.get("decision") or "") == "allow":
            if res.get("decision") == "allow":
                grant_from_decision(params.get("run_id"), params.get("request_id"))
            return
        if not res:
            return
        if _offer_card(params.get("run_id"), params.get("request_id")):
            return  # "Keep trying" on an offer is not the user refusing the task's work
        sid = _session_of_run(params.get("run_id"))
        if sid:
            signals.record(sid, "deny", {"request_id": str(params.get("request_id") or "")})
        return
    if action == "cancel":
        _main_cancelled(params.get("run_id"))
        sid = str(res.get("session_id") or "") or _session_of_run(params.get("run_id"))
        if sid:
            signals.record(sid, "stop", {"run_id": str(params.get("run_id") or "")})
        return
    if action == "snapshot_revert":
        sid = str(params.get("session_id") or "")
        if sid and res:
            signals.record(sid, "undo", {"version_id": str(params.get("version_id") or "")})
