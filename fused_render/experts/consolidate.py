"""Fold pending observations into memory (docs/experts.md §4 `consolidate.py`).

One `model.text` call proposes an edit per observation; code applies them.
An answer that cannot be parsed (or a failed call) falls back to one
`memory.add` per observation, whose exact-match dedupe keeps the memory
clean, so nothing is lost. `gap` observations are never lessons.
"""
from __future__ import annotations

import json
import re
import threading

from fused_render.experts import memory, model, observations, store

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


def _result(ran, added=(), bumped=(), ignored=0):
    added = list(dict.fromkeys(added))
    bumped = [b for b in dict.fromkeys(bumped) if b not in added]
    return {"ran": ran, "added": added, "bumped": bumped, "ignored": ignored}


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
    gaps = [o for o in pend if o.get("trust") == "gap" or o.get("kind") == "gap"]
    real = [o for o in pend if o not in gaps]
    added, bumped, ignored = [], [], len(gaps)

    edits = None
    if real:
        current = memory.items(bot)
        items_txt = "\n".join(f"- [{i['id']}] {i.get('claim', '')} (seen {i.get('count', 0)}×)"
                              for i in current) or "(none yet)"
        obs_txt = "\n".join(f"- [{o['id']}] ({o.get('kind', '')}, trust {o.get('trust', '')}) {o.get('claim', '')}"
                            for o in real)
        try:
            answer = model.text(bot, PROMPT.format(items=items_txt, obs=obs_txt),
                                system="You answer with JSON only.")
            edits = parse_json(answer, list)
        except Exception:  # noqa: BLE001 — a failed call falls back like an unreadable answer
            edits = None

    def note(item, before_ids):
        if item is None:
            return
        (bumped if item["id"] in before_ids else added).append(item["id"])

    def fallback_add(o):
        before = {i["id"] for i in memory.items(bot)}
        try:
            note(memory.add(bot, o.get("claim"), kind=o.get("kind") or "lesson",
                            trust=o.get("trust") or "outcome", session=o.get("session") or "",
                            about=o.get("about")), before)
        except ValueError:
            pass

    by_id = {o["id"]: o for o in real}
    handled: set[str] = set()
    if edits is not None:
        for e in edits:
            if not isinstance(e, dict):
                continue
            o = by_id.get(str(e.get("obs") or ""))
            if o is None or o["id"] in handled:
                continue
            handled.add(o["id"])
            op = str(e.get("op") or "").lower()
            if op == "nothing":
                ignored += 1
            elif op == "bump" and e.get("item"):
                it = memory.bump(bot, str(e["item"]), session=o.get("session") or "")
                if it is not None:
                    bumped.append(it["id"])
                else:
                    fallback_add(o)
            elif op == "add":
                claim = e.get("claim") if isinstance(e.get("claim"), str) and e["claim"].strip() \
                    else o.get("claim")
                before = {i["id"] for i in memory.items(bot)}
                try:
                    note(memory.add(bot, claim, kind=o.get("kind") or "lesson",
                                    trust=o.get("trust") or "outcome", session=o.get("session") or "",
                                    about=o.get("about")), before)
                except ValueError:
                    ignored += 1
            else:
                fallback_add(o)
    for o in real:
        if o["id"] not in handled:
            fallback_add(o)
    observations.archive(bot, [o["id"] for o in pend])
    return _result(True, added, bumped, ignored)
