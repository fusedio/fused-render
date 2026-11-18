"""What the task transcript shows about specialists (PLAN B.3 "Task event", B.5 `task-events`).

    <root>/.state/tasks/<session_id>.jsonl    one TaskEvent per line (pull, stuck, breakout_*)

Kept per TASK session, in root state rather than in a home, so a task's chips survive the specialist being deleted
(scenario Z). Notes come from the inbox itself (`inbox.by_session`), so "Send to a specialist" from the page and
`specialist_note` from the task both show, with `undoable` while the note is undigested.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

KINDS = ("pull", "note", "breakout_open", "breakout_closed", "breakout_discarded", "breakout_deleted", "stuck")
BREAKOUT_KINDS = ("breakout_open", "breakout_closed", "breakout_discarded", "breakout_deleted")
_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LOCK = threading.Lock()


def valid_session(session_id) -> bool:
    return isinstance(session_id, str) and bool(_SID.match(session_id))


def _path(session_id: str) -> str:
    from fused_render.specialists import paths

    return os.path.join(paths.state_root(), "tasks", session_id + ".jsonl")


def _name(spec_id: str) -> str:
    try:
        from fused_render.specialists import store

        spec = store.get(spec_id)
        return spec.name if spec is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def event(kind: str, spec_id: str, name: str = "", **fields) -> dict:
    """A TaskEvent with every B.3 key present."""
    out = {"kind": kind, "at": time.time(), "specialist": {"id": spec_id, "name": name or _name(spec_id)},
           "receipt_id": "", "entries": 0, "trigger": "", "note_id": "", "undoable": False,
           "breakout_id": "", "session_id": ""}
    if kind == "pull":
        out.update(skills=0, knowledge=0)  # what the pull listed besides memory (CONTRACT C5)
    if kind == "note":
        # The note's basis, and whether it is in effect now (said.md): a model-claimed user-said note waits for
        # the user, so the chip can offer "Use now".
        out.update(basis="", in_effect=False)
    out.update({k: v for k, v in fields.items() if k in out})
    return out


def add(task_session: str, kind: str, spec_id: str, name: str = "", **fields) -> dict | None:
    """Append one event for task session `task_session` (`fields` may carry the event's own `session_id`, e.g. a
    breakout's). Never raises."""
    if not valid_session(task_session) or kind not in KINDS:
        return None
    ev = event(kind, spec_id, name, **fields)
    try:
        path = _path(task_session)
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("specialists: could not record a task event", exc_info=True)
    return ev


def recorded(session_id: str) -> list[dict]:
    """The index rows of `session_id`, oldest first."""
    if not valid_session(session_id):
        return []
    out = []
    try:
        with open(_path(session_id), encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("kind") in KINDS:
                    out.append(row)
    except OSError:
        pass
    return out


def for_session(session_id: str) -> list[dict]:
    """Every TaskEvent of task session `session_id`, oldest first (index rows + the inbox's notes)."""
    if not valid_session(session_id):
        return []
    events: list[dict] = []
    by_bid: dict[str, dict] = {}
    for r in recorded(session_id):
        kind, bid = r.get("kind"), r.get("breakout_id")
        if kind == "note":
            continue
        if kind in BREAKOUT_KINDS and bid:
            # One event per breakout (CONTRACT C5), at the place it opened, whose kind is its current state.
            ev = by_bid.get(bid)
            if ev is None:
                by_bid[bid] = ev = dict(r)
                events.append(ev)
            else:
                ev["kind"] = kind
                for k in ("note_id", "session_id"):
                    if r.get(k):
                        ev[k] = r[k]
            continue
        events.append(r)
    for bid, ev in by_bid.items():
        state = _breakout_state((ev.get("specialist") or {}).get("id") or "", bid)
        if state:
            ev["kind"] = state[0]
            if state[1]:
                ev["session_id"] = state[1]
    try:
        from fused_render.specialists import inbox

        said: dict = {}
        for spec_id, note in inbox.by_session(session_id):
            src = note.get("source") if isinstance(note.get("source"), dict) else {}
            if src.get("type") not in ("task", "user"):
                continue
            if spec_id not in said:
                try:
                    said[spec_id] = {n.get("id") for n in inbox.said(spec_id)} if spec_id != inbox.GENERAL else set()
                except Exception:  # noqa: BLE001
                    said[spec_id] = set()
            ev = event("note", spec_id, _name(spec_id) if spec_id != inbox.GENERAL else "general inbox",
                       note_id=str(note.get("id") or ""), undoable=not note.get("digested"),
                       session_id=session_id)
            ev["at"] = float(note.get("at") or 0)
            ev["basis"] = str(note.get("basis") or "")
            ev["in_effect"] = note.get("id") in said[spec_id]
            events.append(ev)
    except Exception:  # noqa: BLE001 — the inbox is another stream's; chips without notes still answer
        logger.debug("specialists: notes for task events", exc_info=True)
    return sorted(events, key=lambda e: float(e.get("at") or 0))


_STATE_KIND = {"open": "breakout_open", "closed": "breakout_closed", "discarded": "breakout_discarded",
               "deleted": "breakout_deleted"}


def _breakout_state(spec_id: str, bid: str) -> tuple[str, str] | None:
    """(event kind of the breakout's current state, its session id) from its record; `breakout_deleted` when its
    specialist is gone; None when it cannot be told (the recorded events stand)."""
    try:
        from fused_render.specialists import breakouts, store

        if not spec_id:
            return None
        if store.get(spec_id) is None:
            return "breakout_deleted", ""
        rec = breakouts.get(spec_id, bid)
        if rec is None:
            return None
        kind = _STATE_KIND.get(str(rec.get("status") or ""))
        return (kind, str(rec.get("session_id") or "")) if kind else None
    except Exception:  # noqa: BLE001
        return None


PRUNE_AFTER_S = 30 * 86400
PRUNED_DIRS = ("tasks", "signals", "stuck", "pulled")


def prune(now: float | None = None, max_age_s: float = PRUNE_AFTER_S) -> int:
    """Remove per-session state files (`.state/{tasks,signals,stuck,pulled}/`) untouched for `max_age_s`. Returns
    how many went. Never raises."""
    from fused_render.specialists import paths

    now = time.time() if now is None else now
    gone = 0
    for sub in PRUNED_DIRS:
        folder = os.path.join(paths.state_root(), sub)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for n in names:
            path = os.path.join(folder, n)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_s:
                    os.remove(path)
                    gone += 1
            except OSError:
                continue
    return gone
