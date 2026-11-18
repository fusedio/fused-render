"""Receipts: which task pulled which specialist's guidance, when, at which memory version (PRODUCT §1, §3.2; PLAN B.3).

    <home>/.specialist/receipts/<rid>.json

A receipt is the reviewer's cursor (`lines` = rendered transcript lines of the task at the pull) and the record that
the task saw these entries. It also keeps the exact text the task was given (`text`), for the chip's View.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid

logger = logging.getLogger(__name__)

RID_RE = re.compile(r"^r-[0-9a-f]{12}$")
REVIEW_IDLE_S = 1200
REVIEW_IDLE_ENV = "FUSED_RENDER_SPECIALIST_REVIEW_IDLE_S"
WEEK_S = 7 * 86400
_LOCK = threading.RLock()


def _json():
    from fused_render.specialists import counts

    return counts.read_json, counts.write_json


def review_idle_s() -> float:
    try:
        v = float(os.environ.get(REVIEW_IDLE_ENV, ""))
        if v > 0 and v == v and v != float("inf"):
            return v
    except ValueError:
        pass
    return float(REVIEW_IDLE_S)


def _dir(spec_id: str) -> str:
    from fused_render.specialists import paths

    return os.path.join(paths.app_dir(spec_id), "receipts")


def _spec_ids() -> list[str]:
    try:
        from fused_render.specialists import store

        return [s.id for s in store.all()]
    except Exception:  # noqa: BLE001
        return []


def get(spec_id: str, rid: str) -> dict | None:
    if not isinstance(rid, str) or not RID_RE.match(rid):
        return None
    read_json, _ = _json()
    rec = read_json(os.path.join(_dir(spec_id), rid + ".json"))
    return rec if isinstance(rec, dict) else None


def all_for(spec_id: str) -> list[dict]:
    read_json, _ = _json()
    out = []
    try:
        names = sorted(os.listdir(_dir(spec_id)))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and RID_RE.match(n[:-5]):
            rec = read_json(os.path.join(_dir(spec_id), n))
            if isinstance(rec, dict):
                out.append(rec)
    return sorted(out, key=lambda r: float(r.get("at") or 0))


def transcript_lines(cwd: str, session_id: str) -> int:
    """Rendered transcript lines of task session `session_id` now (the reviewer's cursor); 0 when unreadable."""
    if not cwd or not session_id:
        return 0
    try:
        from fused_render.claude_agent import agent_module
        from fused_render.experts import learn

        hist = agent_module()._history(cwd, session_id)
        turns = hist.get("turns") if isinstance(hist, dict) else None
        return len(learn._lines(list(turns or []))[0])
    except Exception:  # noqa: BLE001
        return 0


def record(spec_id, *, session_id, run_id, cwd, trigger, match, lines, entries=(), said=(), text="",
           rid: str | None = None) -> dict:
    """Write one receipt and return it. `entries`/`said`/`text` are what the task was given (`pull.compose`)."""
    from fused_render.specialists import history

    try:
        version = history.memory_version(spec_id) or ""
    except Exception:  # noqa: BLE001
        version = ""
    rec = {"id": rid or "r-" + uuid.uuid4().hex[:12], "at": time.time(), "specialist": spec_id,
           "session_id": str(session_id or ""), "run_id": str(run_id or ""), "cwd": str(cwd or ""),
           "trigger": trigger if trigger in ("choice", "signal") else "choice",
           "match": {"folder": (match or {}).get("folder"), "keywords": list((match or {}).get("keywords") or [])},
           "memory_version": version, "entries": list(entries), "said": list(said),
           "lines": int(lines or 0), "text": str(text or ""),
           "review": {"state": "pending", "attempts": 0, "row": "", "outcome": "", "note": ""}}
    _, write_json = _json()
    with _LOCK:
        write_json(os.path.join(_dir(spec_id), rec["id"] + ".json"), rec)
        _mark_pulled(rec["session_id"], spec_id, version)
    return rec


_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _pulled_path(session_id: str) -> str | None:
    if not isinstance(session_id, str) or not _SID.match(session_id):
        return None
    from fused_render.specialists import paths

    return os.path.join(paths.state_root(), "pulled", session_id + ".json")


def _mark_pulled(session_id: str, spec_id: str, version: str) -> None:
    path = _pulled_path(session_id)
    if path is None:
        return
    read_json, write_json = _json()
    idx = read_json(path, {})
    idx = idx if isinstance(idx, dict) else {}
    versions = idx.get(spec_id) if isinstance(idx.get(spec_id), list) else []
    if version not in versions:
        idx[spec_id] = (versions + [version])[-50:]
        try:
            write_json(path, idx)
        except OSError:
            logger.debug("specialists: pulled index", exc_info=True)


def pulled(session_id: str, spec_id: str, version: str) -> bool:
    """Whether task session `session_id` already pulled `spec_id` at memory version `version` (the per-session
    index `<root>/.state/pulled/<sid>.json`; a signal pull happens once per version). One small file per prompt."""
    path = _pulled_path(session_id)
    if path is None:
        return False
    read_json, _ = _json()
    idx = read_json(path, {})
    versions = idx.get(spec_id) if isinstance(idx, dict) else None
    return isinstance(versions, list) and version in versions


def fill_lines(spec_id: str, rid: str) -> None:
    """Set receipt `rid`'s `lines` from its task's transcript now (a signal pull answers first). Never raises."""
    try:
        rec = get(spec_id, rid)
        if rec is None:
            return
        n = transcript_lines(rec.get("cwd") or "", rec.get("session_id") or "")
        _, write_json = _json()
        with _LOCK:
            rec = get(spec_id, rid)
            if rec is not None:
                rec["lines"] = n
                write_json(os.path.join(_dir(spec_id), rid + ".json"), rec)
    except Exception:  # noqa: BLE001
        logger.debug("specialists: receipt lines", exc_info=True)


def exists_for(spec_id: str, session_id: str, version: str) -> bool:
    """Whether `session_id` already has a receipt from `spec_id` at memory version `version` (signal: once each)."""
    return any(r.get("session_id") == session_id and r.get("memory_version") == version for r in all_for(spec_id))


def set_review(spec_id, rid, **state) -> None:
    """Merge `state` into receipt `rid`'s `review`. Raises LookupError for an unknown receipt."""
    _, write_json = _json()
    with _LOCK:
        rec = get(spec_id, rid)
        if rec is None:
            raise LookupError(f"no such receipt: {rid}")
        review = rec.get("review") if isinstance(rec.get("review"), dict) else {}
        review.update(state)
        rec["review"] = review
        write_json(os.path.join(_dir(spec_id), rid + ".json"), rec)


def _last_activity(rec: dict) -> float | None:
    """When the task last did something, from its transcript; None when it cannot be found."""
    try:
        from fused_render import session_liveness
        from fused_render.claude_agent import agent_module

        agent = agent_module()
        path = agent._session_transcript_path(agent._workdir(rec.get("cwd") or ""), rec.get("session_id") or "")
        mtime = os.path.getmtime(path)
        _activity, last = session_liveness.tail_activity(path, mtime)
        return last.timestamp() if last is not None else mtime
    except Exception:  # noqa: BLE001
        return None


def due_for_review(now: float) -> list[tuple[str, dict]]:
    """(specialist id, receipt) for every receipt whose review is pending and whose task has been idle
    `REVIEW_IDLE_S` (by its transcript's last activity), oldest pull first."""
    idle = review_idle_s()
    out = []
    for spec_id in _spec_ids():
        for rec in all_for(spec_id):
            review = rec.get("review") if isinstance(rec.get("review"), dict) else {}
            if review.get("state", "pending") != "pending":
                continue
            last = _last_activity(rec)
            if last is None:
                last = float(rec.get("at") or 0)
            if now - last >= idle:
                out.append((spec_id, rec))
    return sorted(out, key=lambda p: float(p[1].get("at") or 0))


def for_session(session_id) -> list[dict]:
    """Every receipt of task session `session_id`, across specialists, oldest first."""
    if not session_id:
        return []
    out = [r for spec_id in _spec_ids() for r in all_for(spec_id) if r.get("session_id") == session_id]
    return sorted(out, key=lambda r: float(r.get("at") or 0))


def guided_week(spec_id) -> int:
    """How many distinct task sessions pulled `spec_id`'s guidance in the last 7 days."""
    since = time.time() - WEEK_S
    return len({r.get("session_id") or r.get("id") for r in all_for(spec_id) if float(r.get("at") or 0) >= since})
