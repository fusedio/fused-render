"""Drafts to people that wait for the user (PRODUCT §6 "Drafts to people are never sent by the job"; PLAN B.6).

    <home>/.specialist/drafts/<did>.json
    {"id","at","to":{"name","service","handle"},"subject","text","why","from":{"kind":"nightly","run"},
     "status":"waiting|sending|sent|discarded"}
    <home>/.specialist/pending_sends.json   {"<main session id or ''>": {"draft","at"}}

The nightly job only writes them (`materialize`). Nothing sends a draft but the user: **Review and send**
(`send`) answers the SEND_DRAFT message the page sends into the main chat, and records the send as pending for
that session, so the services policy forces a card on the turn's service writes (`send_pending`). When that turn
ends (`after_turn`, the Stop hook) the draft is `sent` if the turn made a successful service write after the
message, else it waits again. **Discard** marks it `discarded` (kept on disk, out of the list).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time

from fused_render.specialists.counts import read_json, write_json

logger = logging.getLogger(__name__)

SEND_DRAFT = ("Review and send this draft to {name} through {service} ({handle}). Show me the exact text first, then "
              "send it with the service tool; I will approve the send.\n\nSubject: {subject}\n\n{text}")
STATUSES = ("waiting", "sending", "sent", "discarded")
DRAFT_ID_RE = re.compile(r"^d-[0-9a-f]{12}$")
SERVICES_PREFIX = "mcp__specialist_services__"
PENDING_FILE = "pending_sends.json"

_LOCK = threading.RLock()


def _dir(spec_id: str) -> str:
    from fused_render.specialists import paths
    return os.path.join(paths.app_dir(spec_id), "drafts")


def _pending_path(spec_id: str) -> str:
    from fused_render.specialists import paths
    return os.path.join(paths.app_dir(spec_id), PENDING_FILE)


def get(spec_id: str, did: str) -> dict | None:
    if not isinstance(did, str) or not DRAFT_ID_RE.match(did):
        return None
    d = read_json(os.path.join(_dir(spec_id), did + ".json"))
    return d if isinstance(d, dict) and d.get("id") == did else None


def _save(spec_id: str, d: dict) -> None:
    write_json(os.path.join(_dir(spec_id), d["id"] + ".json"), d)


def all_drafts(spec_id: str) -> list[dict]:
    """Every draft, newest first."""
    out = []
    try:
        names = os.listdir(_dir(spec_id))
    except OSError:
        return out
    for name in names:
        if name.endswith(".json") and DRAFT_ID_RE.match(name[:-5]):
            d = get(spec_id, name[:-5])
            if d is not None:
                out.append(d)
    return sorted(out, key=lambda d: (float(d.get("at") or 0), d["id"]), reverse=True)


def waiting(spec_id: str) -> list[dict]:
    """The drafts under "Waiting for you": waiting, or being sent right now."""
    return [d for d in all_drafts(spec_id) if d.get("status") in ("waiting", "sending")]


def sent(spec_id: str) -> list[dict]:
    """The drafts the user sent: the open reports the nightly job follows up."""
    return [d for d in all_drafts(spec_id) if d.get("status") == "sent"]


def _s(v, cap: int) -> str:
    return str(v if isinstance(v, (str, int, float)) else "").strip()[:cap]


def materialize(spec_id: str, run: str, items: list) -> list[str]:
    """Write the validated drafts of nightly run `run` (idempotent: a draft's id is derived from the run and its
    position, so a run finished again after a restart writes no second copy). Returns their ids."""
    ids = []
    for i, item in enumerate(items or []):
        if not isinstance(item, dict) or not _s(item.get("text"), 1):
            continue
        did = "d-" + hashlib.sha1(f"{spec_id}/{run}/{i}".encode()).hexdigest()[:12]  # noqa: S324 — an id
        ids.append(did)
        with _LOCK:
            if get(spec_id, did) is not None:
                continue
            to = item.get("to") if isinstance(item.get("to"), dict) else {}
            _save(spec_id, {"id": did, "at": time.time(),
                            "to": {k: _s(to.get(k), 200) for k in ("name", "service", "handle")},
                            "subject": _s(item.get("subject"), 300), "text": _s(item.get("text"), 8000),
                            "why": _s(item.get("why"), 500), "from": {"kind": "nightly", "run": run},
                            "status": "waiting"})
    return ids


def message(d: dict) -> str:
    to = d.get("to") if isinstance(d.get("to"), dict) else {}
    return SEND_DRAFT.format(name=to.get("name") or "the owner", service=to.get("service") or "the right service",
                             handle=to.get("handle") or "no handle given", subject=d.get("subject") or "(none)",
                             text=d.get("text") or "")


def _current(spec_id: str) -> str:
    try:
        from fused_render.specialists import chat
        return str((chat.state(spec_id) or {}).get("current") or "")
    except Exception:  # noqa: BLE001
        return ""


def send(spec_id: str, did: str) -> dict:
    """Review and send: `{"message","session_id"}` for the page to send into the main chat. Raises LookupError
    for an unknown draft, ValueError for one already sent or discarded."""
    with _LOCK:
        d = get(spec_id, did)
        if d is None:
            raise LookupError(f"no such draft: {did}")
        if d.get("status") not in ("waiting", "sending"):
            raise ValueError(f"this draft was already {d.get('status')}")
        sid = _current(spec_id)
        pending = read_json(_pending_path(spec_id), {})
        pending = pending if isinstance(pending, dict) else {}
        pending[sid] = {"draft": did, "at": time.time()}
        write_json(_pending_path(spec_id), pending)
        d["status"] = "sending"
        _save(spec_id, d)
    return {"message": message(d), "session_id": sid or None}


def discard(spec_id: str, did: str) -> None:
    with _LOCK:
        d = get(spec_id, did)
        if d is None:
            raise LookupError(f"no such draft: {did}")
        if d.get("status") == "sent":
            raise ValueError("this draft was already sent")
        d["status"] = "discarded"
        _save(spec_id, d)
        pending = read_json(_pending_path(spec_id), {})
        if isinstance(pending, dict):
            left = {k: v for k, v in pending.items() if not (isinstance(v, dict) and v.get("draft") == did)}
            if left != pending:
                write_json(_pending_path(spec_id), left)


def _pending_key(spec_id: str, session_id: str, pending: dict) -> str | None:
    """The pending-send key `session_id` answers to: its own, or "" (sent while the main chat had no session yet)
    when it is the main chat's current session now."""
    if session_id and session_id in pending:
        return session_id
    if "" in pending and session_id and session_id == _current(spec_id):
        return ""
    return None


def send_pending(spec_id, session_id) -> bool:
    """Whether a draft send waits on this main-chat session (the services policy then forces a card)."""
    try:
        pending = read_json(_pending_path(spec_id), {})
        return isinstance(pending, dict) and _pending_key(spec_id, str(session_id or ""), pending) is not None
    except Exception:  # noqa: BLE001
        return False


def _is_write(spec_id: str, tool: str) -> bool:
    """Whether service tool `tool` (full MCP name) writes, from the roster's description prefix (B.4:
    `[<Service title>, read-only|writes]`). Unknown tools count as writes."""
    try:
        from fused_render.specialists import services, store
        spec = store.get(spec_id)
        short = tool[len(SERVICES_PREFIX):]
        for t in services.roster(spec, "main") if spec is not None else []:
            if isinstance(t, dict) and t.get("name") == short:
                desc = str(t.get("description") or "")
                return "read-only]" not in desc.split("\n", 1)[0]
    except Exception:  # noqa: BLE001
        logger.debug("drafts: roster unavailable", exc_info=True)
    return True


def turn_sent(spec_id: str, turns: list, d: dict) -> bool | None:
    """Did the conversation send draft `d`? None when the SEND_DRAFT message is not in it yet (the turn that
    ended is an earlier one); else whether a successful service write follows the message."""
    first = message(d).split("\n", 1)[0]
    subject = f"Subject: {d.get('subject') or '(none)'}"
    at = None
    for i, t in enumerate(turns or []):
        if isinstance(t, dict) and t.get("role") == "user":
            text = str(t.get("text") or "")
            if first in text and subject in text:
                at = i
    if at is None:
        return None
    for t in turns[at + 1:]:
        if not isinstance(t, dict) or t.get("role") != "assistant":
            continue
        for seg in t.get("segments") or []:
            if (isinstance(seg, dict) and seg.get("kind") == "tool"
                    and str(seg.get("name") or "").startswith(SERVICES_PREFIX)
                    and seg.get("status") == "ok" and _is_write(spec_id, str(seg.get("name")))):
                return True
    return False


def _turns(spec_id: str, session_id: str) -> list:
    from fused_render.claude_agent import agent_module
    from fused_render.specialists import paths
    hist = agent_module()._history(paths.home(spec_id), session_id)
    return list(hist.get("turns") or []) if isinstance(hist, dict) else []


def after_turn(spec_id, session_id) -> None:
    """A main-chat turn ended (Stop hook): settle a pending draft send of this session. Never raises."""
    try:
        sid = str(session_id or "")
        pending = read_json(_pending_path(spec_id), {})
        if not isinstance(pending, dict) or not pending:
            return
        key = _pending_key(spec_id, sid, pending)
        if key is None:
            return
        did = (pending.get(key) or {}).get("draft") if isinstance(pending.get(key), dict) else None
        d = get(spec_id, did) if did else None
        result = turn_sent(spec_id, _turns(spec_id, sid), d) if d is not None else False
        if result is None:
            return  # the turn that ended came before the send message: it is still pending
        with _LOCK:
            if d is not None and d.get("status") == "sending":
                d["status"] = "sent" if result else "waiting"
                if result:
                    d["sent_at"] = time.time()
                _save(spec_id, d)
            pending = read_json(_pending_path(spec_id), {})
            if isinstance(pending, dict):
                pending.pop(key, None)
                write_json(_pending_path(spec_id), pending)
    except Exception:  # noqa: BLE001
        logger.warning("specialist %s: could not settle a draft send", spec_id, exc_info=True)
