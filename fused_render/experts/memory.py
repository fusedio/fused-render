"""An expert's lessons (docs/experts.md §4 `memory.py`): `<expert>/memory.json`
= `{"version": 1, "items": [Item]}`.

A claim equal to an existing one after normalisation (lowercase, collapsed
whitespace, no trailing punctuation) bumps that item instead of adding a
second. A bump counts once per session: `count` grows only when the session
is new or empty.
"""
from __future__ import annotations

import os
import re
import time

from fused_render.experts import store

KINDS_TRUST = ("user", "outcome", "research", "gap")
_TRUST_RANK = {"gap": 0, "research": 1, "outcome": 1, "user": 2}
FLAGS = ("proposed", "promoted", "rejected")
CLAIM_MAX = 1000


def _path(bot) -> str:
    return os.path.join(store.expert_dir(bot), "memory.json")


def normalise(claim) -> str:
    s = " ".join(str(claim or "").split()).lower()
    return re.sub(r"[\s.,;:!?…]+$", "", s)


def _load(bot) -> dict:
    data = store.read_json(_path(bot), {"version": 1, "items": []})
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"version": 1, "items": []}
    data["items"] = [i for i in data["items"] if isinstance(i, dict) and i.get("id")]
    return data


def _save(bot, data) -> None:
    store.write_json(_path(bot), {"version": 1, "items": data["items"]})


def items(bot) -> list:
    with store.lock(bot):
        return _load(bot)["items"]


def _bump_in(item, session, now) -> None:
    sessions = item.setdefault("sessions", [])
    if not session or session not in sessions:
        item["count"] = int(item.get("count") or 0) + 1
        item["updated"] = now
    if session and session not in sessions:
        sessions.append(session)


def add(bot, claim, *, kind="lesson", trust="outcome", session="", about=None, gap="") -> dict:
    """`gap`: the gap id a `research` lesson answers (stored as `gap`)."""
    claim = " ".join(str(claim or "").split())[:CLAIM_MAX]
    if not claim:
        raise ValueError("a lesson needs a claim")
    trust = trust if trust in _TRUST_RANK else "outcome"
    about = [str(a) for a in (about or []) if str(a).strip()]
    session = str(session or "")
    key = normalise(claim)
    now = time.time()
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if normalise(it.get("claim")) == key:
                _bump_in(it, session, now)
                # A lesson the user states outranks the same one seen in a session.
                if _TRUST_RANK.get(trust, 0) > _TRUST_RANK.get(it.get("trust"), 0):
                    it["trust"] = trust
                for a in about:
                    if a not in it.setdefault("about", []):
                        it["about"].append(a)
                if gap and not it.get("gap"):
                    it["gap"] = str(gap)
                _save(bot, data)
                return dict(it)
        it = {"id": store.new_id("m"), "claim": claim, "kind": str(kind or "lesson"), "trust": trust,
              "count": 1, "sessions": [session] if session else [], "about": about,
              "created": now, "updated": now, "proposed": False, "promoted": False, "rejected": False}
        if gap:
            it["gap"] = str(gap)
        data["items"].append(it)
        _save(bot, data)
        return dict(it)


def bump(bot, item_id, *, session="") -> dict | None:
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if it.get("id") == item_id:
                _bump_in(it, str(session or ""), time.time())
                _save(bot, data)
                return dict(it)
    return None


def forget(bot, id_or_text) -> int:
    """Remove the item with this id, else every item whose claim equals the
    text after normalisation, else every item whose claim contains it (at least
    3 characters). Returns how many were removed."""
    needle = str(id_or_text or "").strip()
    if not needle:
        return 0
    key = normalise(needle)
    with store.lock(bot):
        data = _load(bot)
        before = len(data["items"])
        keep = [i for i in data["items"] if i.get("id") != needle]
        if len(keep) == before:
            keep = [i for i in data["items"] if normalise(i.get("claim")) != key]
        if len(keep) == before and len(key) >= 3:
            keep = [i for i in data["items"] if key not in normalise(i.get("claim"))]
        removed = before - len(keep)
        if removed:
            data["items"] = keep
            _save(bot, data)
        return removed


def mark(bot, item_id, **flags) -> dict | None:
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if it.get("id") == item_id:
                for k, v in flags.items():
                    if k in FLAGS:
                        it[k] = bool(v)
                it["updated"] = time.time()
                _save(bot, data)
                return dict(it)
    return None


#: A lesson read from a task session (trust `outcome`) is loaded into every answer only once it was seen in this
#: many sessions; one the user stated (trust `user`) is loaded at once. `gap` items never are.
OUTCOME_MIN_COUNT = 2


def indexed(item) -> bool:
    """Whether `item` is part of the always-loaded index (before the `index_lines` cap)."""
    if not isinstance(item, dict):
        return False
    trust = item.get("trust")
    if trust == "user":
        return True
    if trust == "outcome":
        return int(item.get("count") or 0) >= OUTCOME_MIN_COUNT
    if trust == "research":
        # A gap's finding reaches answers only once its skill fix was approved (the skill carries it then).
        return bool(item.get("promoted"))
    return False


def index_text(bot) -> str:
    """`- [<id>] <claim> (seen N×)`, most seen first, capped at the manifest's index_lines. Only `indexed`
    items: user lessons, and outcome lessons seen in two sessions."""
    cap = store.manifest(bot)["memory"]["index_lines"]
    rows = [i for i in items(bot) if indexed(i)]
    rows.sort(key=lambda i: (-int(i.get("count") or 0), -float(i.get("updated") or 0)))
    return "\n".join(f"- [{i['id']}] {i.get('claim', '')} (seen {int(i.get('count') or 0)}×)"
                     for i in rows[:cap])
