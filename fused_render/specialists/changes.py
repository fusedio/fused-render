"""What each nightly update changed, and Revert (PRODUCT §6; PLAN A.2 "Change list", B.6).

    <home>/.specialist/changes/<run>.json
    {"run","at","summary","status":"done|interrupted|error","commit","row","fallback":bool,"drafts":[ids],
     "items":[{"id":"i1","kind","text","entry","file","from":[...],"undo","commit"}]}
    <home>/.specialist/reverts.json   {"<run>/<item>": {"session_id","at","state":"requested"|"done"}}

The change list is applied already when it is written: there is no approval step. **Revert** on an item is a
message the page sends into the specialist's main chat (`revert` returns it, B.7 REVERT) asking it to undo that
change; the item reads `reverting` until the main-chat turn that carried the message ends (`after_turn`, the Stop
hook) and `reverted` after. The notes the change came from stay digested, so it is not learned again tomorrow:
the next nightly reads the main chat where it was reverted.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from fused_render.specialists.counts import read_json, write_json

logger = logging.getLogger(__name__)

KINDS = ("Learned", "Corrected", "Retired", "Moved", "New skill", "New check", "People", "Trigger")
FALLBACK_KIND = "Changed"
ITEMS_MAX = 50
TEXT_MAX = 500
RUN_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
ITEM_RE = re.compile(r"^i[0-9]{1,3}$")
REVERT = ('Revert last night\'s change: {kind}, "{text}" (nightly update {run}, commit {sha}; how to undo: {undo}). '
          "Put memory, knowledge and skills back the way they were before that change, keep the notes it came "
          "from, and tell me what you changed.")

_LOCK = threading.RLock()


def _app(spec_id: str) -> str:
    from fused_render.specialists import paths
    return paths.app_dir(spec_id)


def path(spec_id: str, run: str) -> str:
    return os.path.join(_app(spec_id), "changes", run + ".json")


def _reverts_path(spec_id: str) -> str:
    return os.path.join(_app(spec_id), "reverts.json")


# ------------------------------------------------------------------ validation ---
def _s(v, cap: int) -> str:
    return str(v).strip()[:cap] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def validate(raw) -> dict | None:
    """The job's changes.json (B.6) cleaned: unknown keys dropped, text capped, at most `ITEMS_MAX` items, an item
    without text dropped, an unknown kind read as `Changed`. None when it is not a JSON object with an `items`
    list (the caller then falls back to the files the run changed)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return None
    items = []
    for it in raw["items"]:
        if not isinstance(it, dict) or not _s(it.get("text"), TEXT_MAX):
            continue
        kind = _s(it.get("kind"), 40)
        entry = _s(it.get("entry"), 20)
        frm = it.get("from") if isinstance(it.get("from"), list) else []
        items.append({"kind": kind if kind in KINDS else FALLBACK_KIND, "text": _s(it.get("text"), TEXT_MAX),
                      "entry": entry if re.match(r"^m-[0-9a-f]{4,8}$", entry) else None,
                      "file": _s(it.get("file"), 300) or None,
                      "from": [_s(x, 60) for x in frm if _s(x, 60)][:20],
                      "undo": _s(it.get("undo"), TEXT_MAX)})
        if len(items) >= ITEMS_MAX:
            break
    drafts = []
    for d in raw.get("drafts") if isinstance(raw.get("drafts"), list) else []:
        if not isinstance(d, dict) or not _s(d.get("text"), 8000):
            continue
        to = d.get("to") if isinstance(d.get("to"), dict) else {}
        drafts.append({"to": {k: _s(to.get(k), 200) for k in ("name", "service", "handle")},
                       "subject": _s(d.get("subject"), 300), "text": _s(d.get("text"), 8000),
                       "why": _s(d.get("why"), TEXT_MAX)})
    dig = raw.get("digested") if isinstance(raw.get("digested"), list) else []
    rb = raw.get("repeat_breakouts")
    return {"summary": _s(raw.get("summary"), TEXT_MAX), "items": items, "drafts": drafts[:10],
            "digested": [x for x in dig if isinstance(x, str) and re.match(r"^n-[a-z0-9-]{1,64}$", x)],
            "repeat_breakouts": rb if isinstance(rb, int) and not isinstance(rb, bool) and 0 <= rb <= 100 else 0}


def fallback_items(files, before: str) -> list[dict]:
    """One `Changed` item per changed tracked file (the job left no valid change list, or left files out)."""
    return [{"kind": FALLBACK_KIND, "text": f"Changed {f}", "entry": None, "file": f, "from": [],
             "undo": f"Put {f} back as it was at commit {before}." if before else f"Put {f} back as it was."}
            for f in files][:ITEMS_MAX]


# ---------------------------------------------------------------------- records ---
def save(spec_id: str, run: str, *, summary: str, items: list, commit: str | None, row: str, status: str,
         fallback: bool, drafts: list) -> dict:
    rec = {"run": run, "at": time.time(), "summary": summary, "status": status, "commit": commit or "",
           "row": row, "fallback": fallback, "drafts": list(drafts),
           "items": [{**it, "id": f"i{n}", "commit": commit or ""} for n, it in enumerate(items, 1)]}
    write_json(path(spec_id, run), rec)
    return rec


def get(spec_id: str, run: str) -> dict | None:
    if not isinstance(run, str) or not RUN_RE.match(run):
        return None
    rec = read_json(path(spec_id, run))
    return rec if isinstance(rec, dict) and rec.get("run") == run else None


def _reverts(spec_id: str) -> dict:
    r = read_json(_reverts_path(spec_id), {})
    return r if isinstance(r, dict) else {}


def _with_state(spec_id: str, rec: dict, reverts: dict | None = None) -> dict:
    reverts = _reverts(spec_id) if reverts is None else reverts
    items = []
    for it in rec.get("items") or []:
        r = reverts.get(f"{rec['run']}/{it.get('id')}")
        state = "active"
        if isinstance(r, dict):
            state = "reverted" if r.get("state") == "done" else "reverting"
        items.append({**it, "state": state})
    return {**rec, "items": items}


def runs(spec_id: str, limit: int = 5) -> list[dict]:
    """The latest change lists with each item's state, newest first."""
    try:
        names = sorted((n[:-5] for n in os.listdir(os.path.join(_app(spec_id), "changes"))
                        if n.endswith(".json") and RUN_RE.match(n[:-5])), reverse=True)
    except OSError:
        return []
    reverts = _reverts(spec_id)
    out = []
    for run in names:
        rec = get(spec_id, run)
        if rec is not None:
            out.append(_with_state(spec_id, rec, reverts))
        if len(out) >= max(1, int(limit)):
            break
    return out


# ------------------------------------------------------------------------ revert ---
def _current(spec_id: str) -> str:
    try:
        from fused_render.specialists import chat
        return str((chat.state(spec_id) or {}).get("current") or "")
    except Exception:  # noqa: BLE001
        return ""


def message(rec: dict, item: dict) -> str:
    return REVERT.format(kind=item.get("kind", ""), text=str(item.get("text", "")).replace('"', "'"),
                         run=rec["run"], sha=item.get("commit") or rec.get("commit") or "none",
                         undo=item.get("undo") or "put it back as it was")


def revert(spec_id: str, run: str, item_id: str) -> dict:
    """Request a Revert: `{"message","session_id"}` (the main chat's current session, or None when it has none
    yet). Raises LookupError for an unknown run or item."""
    rec = get(spec_id, run)
    item = next((it for it in (rec or {}).get("items") or [] if it.get("id") == item_id), None)
    if rec is None or item is None or not ITEM_RE.match(str(item_id)):
        raise LookupError("no such change")
    sid = _current(spec_id)
    with _LOCK:
        reverts = _reverts(spec_id)
        prev = reverts.get(f"{run}/{item_id}")
        if not (isinstance(prev, dict) and prev.get("state") == "done"):
            reverts[f"{run}/{item_id}"] = {"session_id": sid, "at": time.time(), "state": "requested"}
            write_json(_reverts_path(spec_id), reverts)
    return {"message": message(rec, item), "session_id": sid or None}


def _turns(spec_id: str, session_id: str) -> list:
    from fused_render.claude_agent import agent_module
    from fused_render.specialists import paths
    hist = agent_module()._history(paths.home(spec_id), session_id)
    return list(hist.get("turns") or []) if isinstance(hist, dict) else []


def _answered(turns: list, marker: str) -> bool:
    """A user turn carrying `marker` with an assistant turn after it."""
    seen = False
    for t in turns or []:
        if not isinstance(t, dict):
            continue
        if t.get("role") == "user" and marker in str(t.get("text") or ""):
            seen = True
        elif seen and t.get("role") == "assistant" and str(t.get("text") or "").strip():
            return True
    return False


def after_turn(spec_id, session_id) -> None:
    """A main-chat turn ended (Stop hook): every Revert requested for this session whose message the
    conversation now holds, answered, reads `reverted`. Never raises."""
    try:
        sid = str(session_id or "")
        if not sid:
            return
        reverts = _reverts(spec_id)
        mine = {k: v for k, v in reverts.items() if isinstance(v, dict) and v.get("state") == "requested"
                and (v.get("session_id") == sid or (not v.get("session_id") and sid == _current(spec_id)))}
        if not mine:
            return
        turns = _turns(spec_id, sid)
        done = []
        for key in mine:
            run, _, item_id = key.partition("/")
            rec = get(spec_id, run)
            item = next((it for it in (rec or {}).get("items") or [] if it.get("id") == item_id), None)
            if rec is not None and item is not None and _answered(turns, message(rec, item).split("; how to undo")[0]):
                done.append(key)
        if not done:
            return
        with _LOCK:
            reverts = _reverts(spec_id)
            for key in done:
                if isinstance(reverts.get(key), dict):
                    reverts[key] = {**reverts[key], "state": "done", "session_id": sid, "done_at": time.time()}
            write_json(_reverts_path(spec_id), reverts)
    except Exception:  # noqa: BLE001
        logger.warning("specialist %s: could not settle reverts", spec_id, exc_info=True)
