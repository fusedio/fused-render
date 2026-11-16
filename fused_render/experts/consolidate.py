"""Fold pending observations into memory (docs/experts.md §4 `consolidate.py`).

One `model.text` call proposes an edit per observation; code applies them.
The model is the filter between transcript text and memory, so without it
nothing is added: no engine -> `ran: False` with a `reason`; a failed call or
an answer that cannot be parsed leaves the observations pending and counts a
failure (`<expert>/consolidate-failures.json`). Only after
`FALLBACK_AFTER` consecutive failures does it fall back to one exact-match
`memory.add` per observation. An unknown op, a bump of an unknown lesson, or
an observation the answer omitted is `nothing` (archived, not added). `gap`
observations are never lessons.
"""
from __future__ import annotations

import json
import re
import threading

from fused_render.experts import memory, model, observations, store

FALLBACK_AFTER = 3
FAILURES_FILE = "consolidate-failures.json"

_RUNNING: set[str] = set()
_RUNNING_GUARD = threading.Lock()

PROMPT = """You keep an expert's memory of lessons. Below are its current lessons and new observations.
For EACH observation decide one edit:
- "bump": it repeats an existing lesson; give that lesson's id as "item".
- "add": it is a new, durable, reusable lesson; give the lesson as one short sentence in "claim".
- "nothing": it is a one-off, noise, or not a lesson.

Current lessons:
{items}

New observations:
{obs}

Answer with ONLY a JSON list, one object per observation:
[{{"obs": "<observation id>", "op": "add" | "bump" | "nothing", "item": "<lesson id, for bump>", "claim": "<for add>"}}]"""


def _strip_fences(text: str) -> str:
    return re.sub(r"```[a-zA-Z]*", "", text or "")


def parse_json(text, want=list):
    """The first JSON value of type `want` (list or dict) in `text`, code fences
    stripped; None when there is none."""
    s = _strip_fences(text)
    dec = json.JSONDecoder()
    opener = "[" if want is list else "{"
    i = s.find(opener)
    while i != -1:
        try:
            val, _ = dec.raw_decode(s, i)
        except ValueError:
            val = None
        if isinstance(val, want):
            return val
        i = s.find(opener, i + 1)
    return None


def _result(ran, added=(), bumped=(), ignored=0, reason=""):
    added = list(dict.fromkeys(added))
    bumped = [b for b in dict.fromkeys(bumped) if b not in added]
    out = {"ran": ran, "added": added, "bumped": bumped, "ignored": ignored}
    if reason:
        out["reason"] = reason
    return out


def _failures_path(bot) -> str:
    import os
    return os.path.join(store.expert_dir(bot), FAILURES_FILE)


def failures(bot) -> int:
    data = store.read_json(_failures_path(bot), {})
    try:
        return max(0, int((data or {}).get("count") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _set_failures(bot, n: int, error: str = "") -> None:
    import time
    store.write_json(_failures_path(bot), {"count": int(n), "error": error[:300], "at": time.time()})


def run(bot, *, force=False) -> dict:
    key = str(bot.id)
    with _RUNNING_GUARD:
        if key in _RUNNING:
            return _result(False)
        _RUNNING.add(key)
    try:
        return _run(bot, force)
    finally:
        with _RUNNING_GUARD:
            _RUNNING.discard(key)


def _run(bot, force):
    if not force and not observations.due(bot):
        return _result(False)
    pend = observations.pending(bot)
    if not pend:
        return _result(False)
    ok, why = model.available(bot)
    if not ok:
        return _result(False, reason=why or "no engine is available for this expert")
    gaps = [o for o in pend if o.get("trust") == "gap" or o.get("kind") == "gap"]
    real = [o for o in pend if o not in gaps]
    added, bumped, ignored = [], [], len(gaps)

    def note(item, before_ids):
        if item is None:
            return
        (bumped if item["id"] in before_ids else added).append(item["id"])

    def add(o, claim=None):
        before = {i["id"] for i in memory.items(bot)}
        try:
            note(memory.add(bot, claim if claim is not None else o.get("claim"), kind=o.get("kind") or "lesson",
                            trust=o.get("trust") or "outcome", session=o.get("session") or "",
                            about=o.get("about")), before)
            return True
        except ValueError:
            return False

    edits = None
    if real:
        current = memory.items(bot)
        items_txt = "\n".join(f"- [{i['id']}] {i.get('claim', '')} (seen {i.get('count', 0)}×)"
                              for i in current) or "(none yet)"
        obs_txt = "\n".join(f"- [{o['id']}] ({o.get('kind', '')}, trust {o.get('trust', '')}) {o.get('claim', '')}"
                            for o in real)
        error = ""
        try:
            answer = model.text(bot, PROMPT.format(items=items_txt, obs=obs_txt),
                                system="You answer with JSON only.")
            edits = parse_json(answer, list)
            if edits is None:
                error = "the model's answer could not be read"
        except Exception as e:  # noqa: BLE001 — counted below; nothing is added on a failed call
            error = f"the model call failed: {e}"
        if edits is None:
            n = failures(bot) + 1
            if n < FALLBACK_AFTER:
                _set_failures(bot, n, error)
                return _result(False, reason=f"{error}; the observations stay pending ({n} of "
                                             f"{FALLBACK_AFTER} tries)")
            # Consecutive failures: exact-match adds (memory.add's dedupe), so the queue cannot grow forever.
            for o in real:
                add(o)
            _set_failures(bot, 0)
            observations.archive(bot, [o["id"] for o in pend])
            return _result(True, added, bumped, ignored)
    _set_failures(bot, 0)

    by_id = {o["id"]: o for o in real}
    handled: set[str] = set()
    for e in edits or []:
        if not isinstance(e, dict):
            continue
        o = by_id.get(str(e.get("obs") or ""))
        if o is None or o["id"] in handled:
            continue
        handled.add(o["id"])
        op = str(e.get("op") or "").lower()
        if op == "bump" and e.get("item"):
            it = memory.bump(bot, str(e["item"]), session=o.get("session") or "")
            if it is not None:
                bumped.append(it["id"])
            else:
                ignored += 1  # a bump of a lesson that does not exist: nothing
        elif op == "add":
            claim = e["claim"] if isinstance(e.get("claim"), str) and e["claim"].strip() else o.get("claim")
            if not add(o, claim):
                ignored += 1
        else:
            ignored += 1  # "nothing", or an op we do not know
    ignored += len([o for o in real if o["id"] not in handled])  # omitted by the answer: nothing
    observations.archive(bot, [o["id"] for o in pend])
    return _result(True, added, bumped, ignored)
