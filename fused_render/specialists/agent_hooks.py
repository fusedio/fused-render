"""The router's hook after an `/api/claude/agent` call, while specialists are on.

A `start` with a `specialist` key ("<id>:main" / "<id>:breakout") records the session it answered with, so the
pane knows the main chat and a breakout's turns reach the inbox (`history.turn_ended`). Never raises.
"""
from __future__ import annotations

import logging

from fused_render.specialists import chat
from fused_render.specialists import services  # noqa: F401 — loaded with the hooks: agent._decide records grants

logger = logging.getLogger(__name__)


def after_agent_call(action: str, params: dict, envelope: dict) -> None:
    res = envelope.get("result")
    key = str(params.get("specialist") or "")
    if action != "start" or not key or not isinstance(res, dict) or not res.get("session_id"):
        return
    spec_id, _, kind = key.partition(":")
    try:
        chat.record_start(spec_id, kind, str(res["session_id"]))
    except Exception:  # noqa: BLE001
        logger.exception("specialists: could not record the %s session", kind)
