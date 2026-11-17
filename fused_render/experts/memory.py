"""An expert's lessons (docs/experts.md §4 `memory.py`): `<expert>/memory.json`
= `{"version": 1, "items": [Item]}`.

A claim equal to an existing one after normalisation (lowercase, collapsed
whitespace, no trailing punctuation) bumps that item instead of adding a
second. A bump counts once per session: `count` grows only when the session
is new or empty. `contradict` is the opposite count (`contradicted`, once per
session, sessions in `contradicted_sessions`): a session showed the lesson
wrong or too broad. It never raises `count`.

Usage: `cite` counts an answer (consult or chat) that relied on a lesson
(`cited`, `last_used`); `bump` also sets `last_used`. `uses = count + cited`.
A lesson that leaves memory (forgotten, promoted into a skill, tidied away)
is appended to `<expert>/memory-archive.jsonl` with `removed_at` and `reason`
(docs/experts.md "Memory budget").
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
ARCHIVE_FILE = "memory-archive.jsonl"
REASONS = ("stale", "promoted", "over_limit", "forgotten")
DAY_S = 86400.0


def _path(bot) -> str:
    return os.path.join(store.expert_dir(bot), "memory.json")


def _archive_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), ARCHIVE_FILE)


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
    item["last_used"] = now


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
              "created": now, "updated": now, "proposed": False, "promoted": False, "rejected": False,
              "contradicted": 0, "contradicted_sessions": [], "cited": 0, "last_used": None}
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


def contradict(bot, item_id, *, session="") -> dict | None:
    """A session showed this lesson wrong or too broad: `contradicted += 1` only when `session` is new to
    `contradicted_sessions` or empty (the rule `bump` follows). Never touches `count`."""
    session = str(session or "")
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if it.get("id") == item_id:
                seen = it.get("contradicted_sessions")
                if not isinstance(seen, list):
                    seen = it["contradicted_sessions"] = []
                if not session or session not in seen:
                    it["contradicted"] = int(it.get("contradicted") or 0) + 1
                    it["updated"] = time.time()
                if session and session not in seen:
                    seen.append(session)
                _save(bot, data)
                return dict(it)
    return None


def contradicted(item) -> int:
    """How many sessions contradicted `item` (0 for an item stored before the field existed)."""
    try:
        return max(0, int((item or {}).get("contradicted") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


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
            kept = {id(i) for i in keep}
            gone = [i for i in data["items"] if id(i) not in kept]
            data["items"] = keep
            _save(bot, data)
            _archive(bot, gone, "forgotten")
        return removed


def cite(bot, ids) -> list:
    """An answer (consult or chat) relied on these lessons: `cited += 1` and `last_used = now` for each id that
    exists (each id once per call). Returns the ids counted."""
    want = list(dict.fromkeys(str(i) for i in (ids or []) if str(i or "").strip()))
    if not want:
        return []
    now = time.time()
    done = []
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if it.get("id") in want:
                it["cited"] = _n(it.get("cited")) + 1
                it["last_used"] = now
                done.append(it["id"])
        if done:
            _save(bot, data)
    return done


def _n(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def cited(item) -> int:
    return _n((item or {}).get("cited"))


def uses(item) -> int:
    """`count + cited`: sessions that showed the lesson plus answers that relied on it."""
    return _n((item or {}).get("count")) + cited(item)


def age_days(item, now=None) -> float:
    now = time.time() if now is None else now
    created = _f((item or {}).get("created"))
    return max(0.0, (now - created) / DAY_S) if created else 0.0


def idle_days(item, now=None) -> float:
    """Days since the lesson was last seen or used: from `max(updated, last_used)` (else `created`)."""
    now = time.time() if now is None else now
    it = item or {}
    last = max(_f(it.get("updated")), _f(it.get("last_used"))) or _f(it.get("created"))
    return max(0.0, (now - last) / DAY_S) if last else 0.0


def _archive(bot, rows, reason, *, skill="", whys=None) -> None:
    """Append each removed item to the archive (full item + removed_at, reason, skill?, why?)."""
    now = time.time()
    for it in rows:
        rec = dict(it, removed_at=now, reason=reason)
        if reason == "promoted":
            rec["promoted"] = True
        if skill:
            rec["skill"] = str(skill)
        why = (whys or {}).get(it.get("id"))
        if why:
            rec["why"] = str(why)[:500]
        store.append_jsonl(_archive_path(bot), rec)


def remove(bot, ids, reason, *, skill="", whys=None) -> list:
    """Move the items with these ids out of memory into the archive with `reason` (one of `REASONS`), the
    `skill` that now carries them (promoted) and a per-id `why` (`whys`). Returns the removed items."""
    if reason not in REASONS:
        raise ValueError(f"unknown reason: {reason}")
    want = {str(i) for i in (ids or [])}
    if not want:
        return []
    with store.lock(bot):
        data = _load(bot)
        gone = [i for i in data["items"] if i.get("id") in want]
        if not gone:
            return []
        data["items"] = [i for i in data["items"] if i.get("id") not in want]
        _save(bot, data)
        _archive(bot, gone, reason, skill=skill, whys=whys)
        return [dict(i) for i in gone]


def archived(bot, limit=100) -> list:
    """The archive, NEWEST first (at most `limit`)."""
    return list(reversed(store.read_jsonl(_archive_path(bot), limit=limit)))


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
    items: user lessons, and outcome lessons seen in two sessions. A contradicted item's line ends with
    " (contradicted in N session(s); prefer newer lessons on this)"."""
    cap = store.manifest(bot)["memory"]["index_lines"]
    rows = [i for i in items(bot) if indexed(i)]
    rows.sort(key=lambda i: (-int(i.get("count") or 0), -float(i.get("updated") or 0)))

    def line(i):
        text = f"- [{i['id']}] {i.get('claim', '')} (seen {int(i.get('count') or 0)}×)"
        n = contradicted(i)
        if n:
            text += f" (contradicted in {n} session(s); prefer newer lessons on this)"
        return text
    return "\n".join(line(i) for i in rows[:cap])
