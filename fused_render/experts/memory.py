"""An expert's lessons (docs/experts.md §4 `memory.py`): `<expert>/memory.json`
= `{"version": 1, "items": [Item]}`.

A claim equal to an existing one after normalisation (lowercase, collapsed
whitespace, no trailing punctuation) bumps that item instead of adding a
second. A bump counts once per session: `count` grows only when the session
is new or empty. `contradict` is the opposite count (`contradicted`, once per
session, sessions in `contradicted_sessions`): a session showed the lesson
wrong or too broad. It never raises `count`.

`confirm` is a sighting that is NOT independent evidence (the expert's own advice echoed back by a session,
or a statement capture could not ground in a user line): `confirmed += 1` once per session
(`confirmed_sessions`), `last_used` set, `count` untouched. Only `count` decides what is used in answers and
proposed (docs/experts.md "Only independent evidence raises a count").

A user correction verified in a session is stored with trust `user` and `via: "session"`. `narrow` rewrites a
lesson's claim (the old wording kept in `history`) and clears its contradiction mark; a lesson a user correction
replaced leaves memory with reason `corrected` and `into` (the correction's lesson id).

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
REASONS = ("stale", "promoted", "over_limit", "forgotten", "merged", "corrected")
HISTORY_MAX = 10
MERGE_CLAIM_MAX = 400
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


def _defaults(it) -> dict:
    """Fields an older item lacks, filled for readers (not written back)."""
    it.setdefault("confirmed", 0)
    it.setdefault("via", "")
    it.setdefault("history", [])
    return it


def items(bot) -> list:
    """Every lesson; `confirmed` (0), `via` ("") and `history` ([]) are filled in when absent."""
    with store.lock(bot):
        return [_defaults(i) for i in _load(bot)["items"]]


def _bump_in(item, session, now) -> None:
    sessions = item.setdefault("sessions", [])
    if not session or session not in sessions:
        item["count"] = int(item.get("count") or 0) + 1
        item["updated"] = now
    if session and session not in sessions:
        sessions.append(session)
    item["last_used"] = now


def _confirm_in(item, session, now) -> None:
    seen = item.get("confirmed_sessions")
    if not isinstance(seen, list):
        seen = item["confirmed_sessions"] = []
    if not session or session not in seen:
        item["confirmed"] = _n(item.get("confirmed")) + 1
    if session and session not in seen:
        seen.append(session)
    item["last_used"] = now


def add(bot, claim, *, kind="lesson", trust="outcome", session="", about=None, gap="", via="",
        counted=True) -> dict:
    """`gap`: the gap id a `research` lesson answers (stored as `gap`). `via`: where a `user` lesson was said
    ("session": a user correction verified in a task session). `counted` False: the sighting is not independent
    evidence, so an equal existing lesson is `confirm`ed instead of bumped, and a new lesson starts at `count` 0,
    `confirmed` 1."""
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
                if counted:
                    _bump_in(it, session, now)
                else:
                    _confirm_in(it, session, now)
                # A lesson the user states outranks the same one seen in a session.
                if _TRUST_RANK.get(trust, 0) > _TRUST_RANK.get(it.get("trust"), 0):
                    it["trust"] = trust
                    if via:
                        it["via"] = str(via)
                for a in about:
                    if a not in it.setdefault("about", []):
                        it["about"].append(a)
                if gap and not it.get("gap"):
                    it["gap"] = str(gap)
                _save(bot, data)
                return _defaults(dict(it))
        it = {"id": store.new_id("m"), "claim": claim, "kind": str(kind or "lesson"), "trust": trust,
              "count": 1 if counted else 0, "sessions": [session] if session and counted else [], "about": about,
              "created": now, "updated": now, "proposed": False, "promoted": False, "rejected": False,
              "contradicted": 0, "contradicted_sessions": [], "cited": 0, "last_used": None,
              "confirmed": 0 if counted else 1,
              "confirmed_sessions": [session] if session and not counted else []}
        if gap:
            it["gap"] = str(gap)
        if via:
            it["via"] = str(via)
        data["items"].append(it)
        _save(bot, data)
        return _defaults(dict(it))


def bump(bot, item_id, *, session="") -> dict | None:
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if it.get("id") == item_id:
                _bump_in(it, str(session or ""), time.time())
                _save(bot, data)
                return dict(it)
    return None


def confirm(bot, item_id, *, session="") -> dict | None:
    """A session showed this lesson again, but not as independent evidence (it echoed the expert's own advice):
    `confirmed += 1` only when `session` is new to `confirmed_sessions` or empty; sets `last_used`. Never touches
    `count`."""
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if it.get("id") == item_id:
                _confirm_in(it, str(session or ""), time.time())
                _save(bot, data)
                return _defaults(dict(it))
    return None


def narrow(bot, item_id, claim) -> dict | None:
    """Rewrite a lesson's claim to a narrower one (a user correction showed it too broad): the previous wording is
    appended to `history` as `{claim, at}`, and its contradiction mark is cleared. None when the item is gone or the
    claim is empty."""
    claim = " ".join(str(claim or "").split())[:CLAIM_MAX]
    if not claim:
        return None
    now = time.time()
    with store.lock(bot):
        data = _load(bot)
        for it in data["items"]:
            if it.get("id") == item_id:
                hist = it.get("history") if isinstance(it.get("history"), list) else []
                if normalise(claim) != normalise(it.get("claim")):
                    hist = (hist + [{"claim": it.get("claim", ""), "at": now}])[-HISTORY_MAX:]
                    it["claim"] = claim
                it["history"] = hist
                it["contradicted"] = 0
                it["contradicted_sessions"] = []
                it["updated"] = now
                _save(bot, data)
                return _defaults(dict(it))
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


def _archive(bot, rows, reason, *, skill="", whys=None, into="") -> None:
    """Append each removed item to the archive (full item + removed_at, reason, skill?, why?, into?)."""
    now = time.time()
    for it in rows:
        rec = dict(it, removed_at=now, reason=reason)
        if reason == "promoted":
            rec["promoted"] = True
        if skill:
            rec["skill"] = str(skill)
        if into:
            rec["into"] = str(into)
        why = (whys or {}).get(it.get("id"))
        if why:
            rec["why"] = str(why)[:500]
        store.append_jsonl(_archive_path(bot), rec)


def remove(bot, ids, reason, *, skill="", whys=None, into="") -> list:
    """Move the items with these ids out of memory into the archive with `reason` (one of `REASONS`), the
    `skill` that now carries them (promoted), a per-id `why` (`whys`) and `into` (corrected: the lesson that replaced
    them). Returns the removed items."""
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
        _archive(bot, gone, reason, skill=skill, whys=whys, into=into)
        return [dict(i) for i in gone]


def merge(bot, ids, claim, *, why="") -> tuple[dict, list] | None:
    """Generalise 2-3 lessons into one (the tidy step's `merge`). `claim` equal to one original after
    normalisation keeps that item and folds the others into it; otherwise a new lesson (kind "lesson") holds the
    merged claim. Either way: sessions = union (kept for the record), count = the LARGEST original count (never
    the size of the union: two lessons each seen once are not a lesson seen twice, and two sightings is what makes
    a session lesson usable in answers; a later bump still counts only a session not in the union), cited = sum,
    created =
    earliest, updated / last_used = latest, about = union, trust "user" only when every original was, else
    "outcome", `merged_from` = the originals folded in ({"id","claim"}). The others are archived with reason
    "merged" and `into`. Returns (the merged item, the archived originals) or None when fewer than 2 of `ids`
    exist."""
    claim = " ".join(str(claim or "").split())
    want = list(dict.fromkeys(str(i) for i in (ids or [])))
    if not claim or len(claim) > MERGE_CLAIM_MAX:
        return None
    key = normalise(claim)
    with store.lock(bot):
        data = _load(bot)
        orig = [i for i in data["items"] if i.get("id") in want]
        if len(orig) < 2 or len(orig) != len(want):
            return None
        keep = next((i for i in orig if normalise(i.get("claim")) == key), None)
        sessions = list(dict.fromkeys(s for i in orig for s in (i.get("sessions") or []) if s))
        about = list(dict.fromkeys(a for i in orig for a in (i.get("about") or []) if a))
        last = max((max(_f(i.get("updated")), _f(i.get("last_used"))) for i in orig), default=0.0)
        fields = {
            "sessions": sessions, "about": about,
            "count": max(_n(i.get("count")) for i in orig),
            "cited": sum(cited(i) for i in orig),
            "created": min(_f(i.get("created")) or time.time() for i in orig),
            "updated": last or time.time(), "last_used": last or None,
            "trust": "user" if all(i.get("trust") == "user" for i in orig) else "outcome",
        }
        folded = [i for i in orig if i is not keep]
        if keep is not None:
            keep.update(fields)
            keep["merged_from"] = list(keep.get("merged_from") or []) + [
                {"id": i["id"], "claim": i.get("claim", "")} for i in folded]
            target = keep
        else:
            target = {"id": store.new_id("m"), "claim": claim, "kind": "lesson", **fields,
                      "proposed": False, "promoted": False, "rejected": False,
                      "contradicted": 0, "contradicted_sessions": [],
                      "merged_from": [{"id": i["id"], "claim": i.get("claim", "")} for i in orig]}
        gone_ids = {i["id"] for i in folded}
        at = next(n for n, i in enumerate(data["items"]) if i.get("id") in want)
        rest = [i for i in data["items"] if i.get("id") not in gone_ids and i is not target]
        rest.insert(min(at, len(rest)), target)
        data["items"] = rest
        _save(bot, data)
        _archive(bot, folded, "merged", into=target["id"], whys={i["id"]: why for i in folded} if why else None)
        return dict(target), [dict(i) for i in folded]


def archived(bot, limit=100) -> list:
    """The archive, NEWEST first (at most `limit`)."""
    return list(reversed(store.read_jsonl(_archive_path(bot), limit=limit)))


def promoted(bot, limit=None) -> list:
    """Archived lessons that went into a skill (reason `promoted` with a `skill` name), newest first, one per id."""
    out, seen = [], set()
    for rec in archived(bot, limit=limit):
        if isinstance(rec, dict) and rec.get("reason") == "promoted" and rec.get("skill") and rec.get("id") \
                and rec["id"] not in seen:
            seen.add(rec["id"])
            out.append(rec)
    return out


def promoted_ids(bot) -> set:
    return {r["id"] for r in promoted(bot)}


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
