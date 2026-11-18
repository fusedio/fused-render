"""A specialist's one main chat (PRODUCT §2, PLAN A.2 "Clear").

`<home>/.specialist/chat.json` = `{"current": session_id | null, "folded": [...]}`. The page mounts the chat on
`current`; Clear folds it (newest last) and the next send starts a fresh Claude Code session in the home, so
memory and skills load natively and nothing else carries over. The router records the minted session as `current`
from the start's answer (`record_start`, called by stream 2's `agent_hooks`).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Literal

from fused_render.specialists import home as hm
from fused_render.specialists import paths

logger = logging.getLogger(__name__)

FOLDED_KEPT = 200
_LOCK = threading.Lock()


def _path(spec_id: str) -> str:
    return os.path.join(paths.app_dir(spec_id), "chat.json")


def state(spec_id: str) -> dict:
    d = hm.read_json(_path(spec_id), {})
    d = d if isinstance(d, dict) else {}
    cur = d.get("current")
    folded = [f for f in d.get("folded") or [] if isinstance(f, dict) and f.get("session_id")]
    return {"current": cur if isinstance(cur, str) and cur else None, "folded": folded}


def _summary(spec_id: str, session_id: str) -> dict:
    """Title (first user message) and message count of a session, read from its transcript; blanks when it
    cannot be read (the fold is still recorded)."""
    out = {"title": "", "messages": 0, "started": None}
    try:
        from fused_render.claude_agent import agent_module

        turns = agent_module()._history(paths.home(spec_id), session_id).get("turns") or []
        users = [t for t in turns if t.get("role") == "user"]
        out["messages"] = len(turns)
        if users:
            out["title"] = " ".join(str(users[0].get("text") or "").split())[:120]
            if isinstance(users[0].get("ts"), (int, float)):
                out["started"] = users[0]["ts"]
    except Exception:  # noqa: BLE001
        logger.debug("specialists: no summary for %s", session_id, exc_info=True)
    return out


def _fold(spec_id: str, d: dict) -> dict:
    cur = d.get("current")
    if cur and not any(f.get("session_id") == cur for f in d["folded"]):
        s = _summary(spec_id, cur)
        d["folded"].append({"session_id": cur, "started": s["started"] or d.get("current_since"),
                            "ended": time.time(), "title": s["title"], "messages": s["messages"]})
        d["folded"] = d["folded"][-FOLDED_KEPT:]
    d["current"] = None
    d.pop("current_since", None)
    return d


def record_start(spec_id: str, session_id: str) -> None:
    """A main-chat start answered with `session_id`. The same session again (a resume) changes nothing; a
    different one while another is current folds the old one first, so no conversation is ever dropped."""
    if not session_id or not os.path.isdir(paths.home(spec_id)):
        return
    with _LOCK:
        raw = hm.read_json(_path(spec_id), {})
        d = state(spec_id)
        if d["current"] == session_id:
            return
        d["current_since"] = raw.get("current_since") if isinstance(raw, dict) else None
        if d["current"]:
            d = _fold(spec_id, d)
        d["folded"] = [f for f in d["folded"] if f.get("session_id") != session_id]
        d["current"] = session_id
        d["current_since"] = time.time()
        hm.write_json(_path(spec_id), d)


def clear(spec_id: str) -> dict:
    """Fold the current session; the page then remounts with no session and the next send starts fresh."""
    if not os.path.isdir(paths.home(spec_id)):
        return state(spec_id)  # gone: never recreate a ghost home
    with _LOCK:
        raw = hm.read_json(_path(spec_id), {})
        d = state(spec_id)
        d["current_since"] = raw.get("current_since") if isinstance(raw, dict) else None
        d = _fold(spec_id, d)
        hm.write_json(_path(spec_id), d)
    return state(spec_id)


def session_live(spec_id: str, session_id: str) -> bool:
    """A run of `session_id` has a turn open in the home now."""
    from fused_render.specialists import store

    return any(session_id in (r.get("sessions") or ()) for r in store.live_runs(paths.home(spec_id)))


def main_sessions(spec_id: str) -> list[str]:
    """Every main-chat session id, oldest first (folded, then current)."""
    d = state(spec_id)
    out = [f["session_id"] for f in d["folded"]]
    if d["current"] and d["current"] not in out:
        out.append(d["current"])
    return out


def kind_of(spec_id: str, session_id: str) -> Literal["main", "breakout", ""]:
    """What a session in the home is: a breakout (stream 2's records), a main chat (current or folded), or ""."""
    if not session_id:
        return ""
    try:
        from fused_render.specialists import breakouts

        if session_id in (breakouts.session_ids(spec_id) or {}):
            return "breakout"
    except Exception:  # noqa: BLE001 — stream 2 not there yet: fall through
        pass
    return "main" if session_id in main_sessions(spec_id) else ""
