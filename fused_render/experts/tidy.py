"""Learn, step 4: the memory budget (docs/experts-autonomous.md "tidy"; rules unchanged from docs/experts.md
"Memory budget").

Memory holds at most `limit` lessons (manifest `memory.limit`, 50). When it holds more than `tidy_at` (30), the
end of a Learn run also tidies it. This module writes nothing: `step` yields `lesson_remove` / `lesson_merge`
changes and the loop applies them. Code decides first; the model is asked only when it can change something:

- `delete_candidates`: `uses <= rare_uses`, idle for `stale_days` or more, not taught by the user (`can_delete`).
- `merge_candidates`: groups of 2-3 lessons plausibly about the same subject, found without a model (shared
  `about` tags, or claim word overlap after dropping stop words); contradicted and heavily used lessons are
  left out.
- `promote_ready` (`can_promote`): `uses >= heavy_uses` AND `count >= 2` (being cited alone never promotes),
  not contradicted, not a gap. Tidy only names them: `distill` writes them into a skill.

ONE `model.text` call, only when there are delete or merge candidates, sees ONLY those lessons: which delete
candidates to delete (it may keep one), and which groups to merge into what claim. Code guard rails still hold:
a delete must be a delete candidate; a merge (`can_merge`) must name 2-3 ids of ONE shown group, none
contradicted or heavily used, with a 1-400 character claim; at most `MAX_MERGES` per tidy. Each lesson id
belongs to the first action that names it. An unreadable answer or a failed call: no deletions or merges.

The review is skipped, with no model call, when the memory fingerprint (`fingerprint`: ids, counts, cited,
contradicted, sorted, plus the day number and the budget) equals the one saved in `tidy-fingerprint.json` by
the last tidy that ran cleanly and changed nothing.

Then, with no model, the hard limit: while memory holds more than `limit`, lessons leave (reason `over_limit`):
first lessons not taught by the user, lowest uses then longest idle; a user-taught lesson only when nothing else
is left.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

from fused_render.experts import memory, model, store
from fused_render.experts.engine import parse_json

logger = logging.getLogger(__name__)

FP_FILE = "tidy-fingerprint.json"
MAX_MERGES = 5
#: Merge candidate groups shown to the model, at most.
MAX_GROUPS = 10
#: Two claims are related when they share at least `MERGE_MIN_SHARED` content words and the shared words are at
#: least `MERGE_OVERLAP` of the shorter claim's content words.
MERGE_MIN_SHARED = 2
MERGE_OVERLAP = 0.5
#: Promotion needs this many independent sightings (`count`) besides `uses >= heavy_uses`.
PROMOTE_MIN_COUNT = 2
_SOURCES = {"user": "taught by you", "outcome": "from sessions", "research": "found by research",
            "gap": "an unanswered question"}
_STOP = frozenset("""a about above after again against all also always an and any are as at be because been before
being below between both but by can cannot could did do does doing done down during each either else ever every
few for from further had has have having here how however i if in into is it its itself just less like may might
more most must my never no nor not now of off on once one only or other our out over own per same shall should so
some such than that the their them then there these they this those through to too under until up upon us very
via was we were what when where whether which while who whom why will with within without would yet you your""".split())

PROMPT = """You keep an expert's memory of lessons tidy. Memory holds {total} lessons; it should stay well under {limit}.
Code has already picked the only lessons that may change. Decide about these lessons alone.

DELETE CANDIDATES: OLD and RARELY USED (seen and cited {rare} times or fewer in total, nothing in the last {stale}
days). Answer "delete" for each one that is no longer worth keeping, "keep" for one that still looks valuable.
{deletes}

MERGE CANDIDATE GROUPS: lessons that may be about the SAME subject. When two or three lessons of ONE group say
COMPATIBLE things about the same subject, answer "merge" with their ids and ONE self-contained lesson that keeps what
they share and the most useful specifics. Never merge lessons that disagree; never mix groups. When unsure, keep them
separate.
{groups}

When unsure, keep.

Each lesson line: id | claim | where it came from | seen N times | cited N times | age in days | days since last seen
or used | contradicted N times.

Answer with ONLY a JSON list, one object per action (lessons you leave out are kept; name each lesson at most once):
[{{"id": "<lesson id>", "do": "delete" | "keep", "why": "<one short reason>"}},
 {{"do": "merge", "ids": ["<lesson id>", "<lesson id>"], "claim": "<the one generalised lesson>", "why": "<reason>"}}]"""


def _fp_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), FP_FILE)


def budget(bot) -> dict:
    return store.manifest(bot)["memory"]


def needed(bot) -> bool:
    """Memory holds more than `tidy_at` lessons (or more than `limit`)."""
    b = budget(bot)
    return len(memory.items(bot)) > min(b["tidy_at"], b["limit"])


def _line(i, now) -> str:
    claim = " ".join(str(i.get("claim") or "").split())[:300]
    return (f"{i['id']} | {claim} | {_SOURCES.get(i.get('trust'), 'from sessions')} | "
            f"seen {int(i.get('count') or 0)} | cited {memory.cited(i)} | age {int(memory.age_days(i, now))} | "
            f"idle {int(memory.idle_days(i, now))} | contradicted {memory.contradicted(i)}")


def _review(bot, prompt):
    """The model's actions in answer order, `[{"do", "ids": [..], "claim"?, "why"}]` (`do`: delete / keep /
    merge), or None when the call failed or the answer cannot be read. Each lesson id belongs to the first action
    that names it; a later action naming an id already taken is dropped whole."""
    try:
        answer = model.text(bot, prompt, system="You answer with JSON only.")
    except Exception:  # noqa: BLE001 — no verdicts: nothing deleted or merged by the model
        logger.warning("expert %s: tidy review failed", getattr(bot, "id", "?"), exc_info=True)
        return None
    raw = parse_json(answer, list)
    if raw is None:
        return None
    out, taken = [], set()
    for r in raw:
        if not isinstance(r, dict):
            continue
        do = str(r.get("do") or "").strip().lower()
        if do == "merge":
            ids = r.get("ids")
            ids = [str(x) for x in ids if isinstance(x, str) and x.strip()] if isinstance(ids, list) else []
        elif do in ("delete", "keep"):
            ids = [str(r.get("id") or "")] if r.get("id") else []
        else:
            continue
        if not ids or taken.intersection(ids):
            continue
        taken.update(ids)
        why = r.get("why")
        act = {"do": do, "ids": ids, "why": " ".join(str(why).split())[:300] if isinstance(why, str) else ""}
        if do == "merge":
            act["claim"] = r.get("claim") if isinstance(r.get("claim"), str) else ""
        out.append(act)
    return out


def can_delete(i, b, now) -> bool:
    return (memory.uses(i) <= b["rare_uses"] and memory.idle_days(i, now) >= b["stale_days"]
            and i.get("trust") != "user")


def can_promote(i, b) -> bool:
    """Used a lot (`uses >= heavy_uses`) AND seen in at least `PROMOTE_MIN_COUNT` sessions: citations alone never
    promote a lesson."""
    return (memory.uses(i) >= b["heavy_uses"] and int(i.get("count") or 0) >= PROMOTE_MIN_COUNT
            and not memory.contradicted(i) and i.get("trust") != "gap")


# ------------------------------------------------------------------ candidates (no model)
def _words(claim) -> frozenset:
    out = set()
    for w in re.findall(r"[a-z0-9_]+", str(claim or "").lower()):
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return frozenset(out)


def _about(i) -> frozenset:
    about = i.get("about") if isinstance(i.get("about"), list) else []
    return frozenset(str(a).strip().lower() for a in about if str(a or "").strip())


def related(a, b) -> float:
    """How plausibly two lessons are about the same subject, 0 when not: 1.0 for a shared `about` tag, else the
    claims' shared content words over the shorter claim's, when at least `MERGE_MIN_SHARED` words are shared and
    that share reaches `MERGE_OVERLAP`."""
    if _about(a) & _about(b):
        return 1.0
    wa, wb = _words(a.get("claim")), _words(b.get("claim"))
    if not wa or not wb:
        return 0.0
    shared = len(wa & wb)
    if shared < MERGE_MIN_SHARED:
        return 0.0
    score = shared / min(len(wa), len(wb))
    return score if score >= MERGE_OVERLAP else 0.0


def delete_candidates(rows, b, now) -> list:
    return [i for i in rows if can_delete(i, b, now)]


def promote_ready(rows, b) -> list:
    return [i for i in rows if can_promote(i, b)]


def merge_candidates(rows, b) -> list:
    """Groups (lists) of 2 or 3 lessons plausibly about the same subject, strongest pairs first, each lesson in at
    most one group; at most `MAX_GROUPS`. Contradicted and heavily used lessons are left out (they could never be
    merged)."""
    pool = [i for i in rows if not memory.contradicted(i) and memory.uses(i) < b["heavy_uses"]]
    edges = []
    for x in range(len(pool)):
        for y in range(x + 1, len(pool)):
            s = related(pool[x], pool[y])
            if s > 0:
                edges.append((-s, x, y))
    edges.sort()
    group_of: dict[int, list] = {}
    groups: list[list] = []
    for _s, x, y in edges:
        gx, gy = group_of.get(x), group_of.get(y)
        if gx is None and gy is None:
            g = [x, y]
            groups.append(g)
            group_of[x] = group_of[y] = g
        elif (gx is None) != (gy is None):
            g, new = (gx, y) if gx is not None else (gy, x)
            if len(g) < 3 and all(related(pool[new], pool[m]) > 0 for m in g):
                g.append(new)
                group_of[new] = g
    return [[pool[k] for k in g] for g in groups[:MAX_GROUPS]]


def fingerprint(rows, b, now) -> str:
    """Memory's state as far as tidy can tell: ids, counts, cited and contradicted (sorted), the day number and
    the budget."""
    items = sorted([str(i["id"]), int(i.get("count") or 0), memory.cited(i), memory.contradicted(i)] for i in rows)
    raw = json.dumps([int(now // memory.DAY_S), sorted(b.items()), items], default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prompt(total, deletes, groups, b, now) -> str:
    dl = "\n".join(_line(i, now) for i in deletes) or "(none)"
    gl = "\n".join(f"Group {k}:\n" + "\n".join("  " + _line(i, now) for i in g)
                   for k, g in enumerate(groups, 1)) or "(none)"
    return PROMPT.format(total=total, limit=b["limit"], rare=b["rare_uses"], stale=b["stale_days"],
                         deletes=dl, groups=gl)


def can_merge(rows, ids, claim, b) -> bool:
    """2 or 3 distinct existing lessons, none contradicted or heavily used; a claim 1-400 characters long that is
    not already another lesson's."""
    by_id = {i["id"]: i for i in rows}
    if len(ids) != len(set(ids)) or not 2 <= len(ids) <= 3 or any(i not in by_id for i in ids):
        return False
    if any(memory.contradicted(by_id[i]) or memory.uses(by_id[i]) >= b["heavy_uses"] for i in ids):
        return False
    claim = " ".join(str(claim or "").split())
    if not claim or len(claim) > memory.MERGE_CLAIM_MAX:
        return False
    key = memory.normalise(claim)
    return not any(memory.normalise(i.get("claim")) == key for i in rows if i["id"] not in ids)


def _over_limit_order(rows, now) -> list:
    """Removal order for the hard limit: not user-taught first, then user-taught; within each, lowest uses then
    longest idle."""
    return sorted(rows, key=lambda i: (i.get("trust") == "user", memory.uses(i), -memory.idle_days(i, now)))


def _save_fp(bot, fp) -> None:
    try:
        if fp:
            store.write_json(_fp_path(bot), {"fp": fp, "at": time.time()})
        elif os.path.exists(_fp_path(bot)):
            os.remove(_fp_path(bot))
    except OSError:
        logger.warning("expert %s: tidy fingerprint not saved", getattr(bot, "id", "?"), exc_info=True)


def _saved_fp(bot) -> str:
    data = store.read_json(_fp_path(bot), None)
    return str(data.get("fp") or "") if isinstance(data, dict) else ""


def step(bot, ctx):
    """Step 4 of a Learn run. Order: deletions, merges, the hard limit. Never raises for a model failure (nothing
    is deleted or merged then). The model is called only when there are delete or merge candidates."""
    b = budget(bot)
    now = time.time()
    rows = memory.items(bot)
    if len(rows) > b["tidy_at"] and fingerprint(rows, b, now) != _saved_fp(bot):
        deletes_c = delete_candidates(rows, b, now)
        groups = merge_candidates(rows, b)
        actions = _review(bot, _prompt(len(rows), deletes_c, groups, b, now)) if deletes_c or groups else []
        failed = actions is None
        changed = 0
        # 1. deletions: delete candidates only
        del_ids = {i["id"] for i in deletes_c}
        for a in actions or []:
            if a["do"] == "delete" and a["ids"][0] in del_ids:
                line = yield {"op": "lesson_remove", "id": a["ids"][0], "reason": "stale",
                              "why": a["why"] or "Old and rarely used."}
                changed += bool(line.get("ok"))
        # 2. merges: ids of ONE shown group (at most MAX_MERGES)
        group_of = {i["id"]: k for k, g in enumerate(groups) for i in g}
        merged = 0
        for a in [a for a in actions or [] if a["do"] == "merge"]:
            if merged >= MAX_MERGES:
                break
            if len({group_of.get(x) for x in a["ids"]}) != 1 or a["ids"][0] not in group_of \
                    or not can_merge(memory.items(bot), a["ids"], a.get("claim"), b):
                continue
            line = yield {"op": "lesson_merge", "ids": a["ids"], "claim": a["claim"],
                          "why": a["why"] or "These lessons said the same thing."}
            merged += bool(line.get("ok"))
        # Remember a clean tidy that changed nothing; anything else is reviewed again next time.
        _save_fp(bot, "" if failed or changed or merged else fingerprint(memory.items(bot), b, now))

    # The hard limit: no model.
    rows = memory.items(bot)
    for i in _over_limit_order(rows, now)[:max(0, len(rows) - b["limit"])]:
        yield {"op": "lesson_remove", "id": i["id"], "reason": "over_limit",
               "why": f"Memory holds at most {b['limit']} lessons and this was the least used."}
