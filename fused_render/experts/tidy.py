"""The memory budget (docs/experts.md "Memory budget").

Memory holds at most `limit` lessons (manifest `memory.limit`, 50). When it
holds more than `tidy_at` (30), the end of a consolidation run also tidies it.
Code decides first; the model is asked only when it can change something:

- `delete_candidates`: `uses <= rare_uses`, idle for `stale_days` or more, not
  taught by the user and not inside an open proposal (`can_delete`).
- `promote_ready` (`can_promote`): `uses >= heavy_uses` AND `count >= 2`
  (being cited alone never promotes: citations are inflated, a lesson needs
  independent sightings too), not contradicted, not rejected, not a gap, not
  inside an open proposal. No model: all of them go into ONE
  `proposals.request`, none while a proposal is preparing or waiting on its
  card. The lessons stay in memory until the user approves.
- `merge_candidates`: groups of 2-3 lessons plausibly about the same subject,
  found without a model (shared `about` tags, or claim word overlap after
  dropping stop words); contradicted, proposal-held and promote-ready (or
  `uses >= heavy_uses`) lessons are left out.

ONE `model.text` call, only when there are delete or merge candidates, sees
ONLY those lessons: which delete candidates to delete (it may keep one), and
which groups to merge into what claim. Code guard rails still hold: a delete
must be a delete candidate that `can_delete`; a merge (`can_merge`) must name
2-3 ids of ONE shown group, none contradicted / promote-eligible / in an open
proposal, with a 1-400 character claim; at most `MAX_MERGES` per tidy; a
claim equal to one original keeps that item and folds the others in. Each
lesson id belongs to the first action that names it. An unreadable answer or a
failed call: no deletions or merges.

The whole review is skipped, with no model call, when the memory fingerprint
(`fingerprint`: ids, counts, cited, contradicted, proposal flags, sorted, plus
the day number, the budget and whether a proposal is open) equals the one saved
in `tidy-fingerprint.json` by the last tidy that ran cleanly and changed
nothing.

Then, with no model, the hard limit: while memory holds more than `limit`,
lessons leave (archived as `over_limit`): first lessons not taught by the user
and outside any open proposal, lowest uses then longest idle; a user-taught
lesson only when nothing else is left.

Old data: an item still flagged `promoted` (from before promoted lessons left
memory on approval) is archived as `promoted` whenever tidy runs.

Skill pressure (`skill_pressure`, also in the tidy stage, after `run`; no
model call decides it): when a skill's body is over `PRESSURE` (80%) of the
body limit and no proposal is open, ONE "reorganise" proposal is requested for
the largest such skill (`proposals.request_reorganise`). A skill whose
reorganise was rejected in the last `REORGANISE_COOLDOWN_S` (7 days), or
failed in the last `REORGANISE_FAILED_COOLDOWN_S` (1 day), is skipped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

from fused_render.experts import memory, model, store

logger = logging.getLogger(__name__)

LAST_FILE = "tidy-last.json"
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
#: A skill body over this share of the body limit asks for a reorganise.
PRESSURE = 0.8
REORGANISE_COOLDOWN_S = 7 * 86400
REORGANISE_FAILED_COOLDOWN_S = 86400
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
or used | contradicted N times | proposal open.

Answer with ONLY a JSON list, one object per action (lessons you leave out are kept; name each lesson at most once):
[{{"id": "<lesson id>", "do": "delete" | "keep", "why": "<one short reason>"}},
 {{"do": "merge", "ids": ["<lesson id>", "<lesson id>"], "claim": "<the one generalised lesson>", "why": "<reason>"}}]"""


def _last_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), LAST_FILE)


def _fp_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), FP_FILE)


def last(bot):
    """The last tidy summary (`tidy-last.json`), or None."""
    data = store.read_json(_last_path(bot), None)
    return data if isinstance(data, dict) else None


def budget(bot) -> dict:
    return store.manifest(bot)["memory"]


def needed(bot) -> bool:
    """Memory holds more than `tidy_at` lessons (or more than `limit`)."""
    b = budget(bot)
    return len(memory.items(bot)) > min(b["tidy_at"], b["limit"])


def _line(i, open_ids, now) -> str:
    claim = " ".join(str(i.get("claim") or "").split())[:300]
    return (f"{i['id']} | {claim} | {_SOURCES.get(i.get('trust'), 'from sessions')} | "
            f"seen {int(i.get('count') or 0)} | cited {memory.cited(i)} | age {int(memory.age_days(i, now))} | "
            f"idle {int(memory.idle_days(i, now))} | contradicted {memory.contradicted(i)} | "
            f"proposal open: {'yes' if i['id'] in open_ids else 'no'}")


def _review(bot, prompt):
    """The model's actions in answer order, `[{"do", "ids": [..], "claim"?, "why"}]` (`do`: delete / keep /
    merge), or None when the call failed or the answer cannot be read. Each lesson id belongs to the first action
    that names it; a later action naming an id already taken is dropped whole."""
    from fused_render.experts.consolidate import parse_json
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


def can_delete(i, open_ids, b, now) -> bool:
    return (memory.uses(i) <= b["rare_uses"] and memory.idle_days(i, now) >= b["stale_days"]
            and i.get("trust") != "user" and i["id"] not in open_ids)


def can_promote(i, open_ids, b) -> bool:
    """Used a lot (`uses >= heavy_uses`) AND seen in at least `PROMOTE_MIN_COUNT` sessions: citations alone never
    promote a lesson."""
    return (memory.uses(i) >= b["heavy_uses"] and int(i.get("count") or 0) >= PROMOTE_MIN_COUNT
            and not memory.contradicted(i) and not i.get("rejected")
            and not i.get("promoted") and i.get("trust") != "gap" and i["id"] not in open_ids)


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


def delete_candidates(rows, open_ids, b, now) -> list:
    return [i for i in rows if can_delete(i, open_ids, b, now)]


def promote_ready(rows, open_ids, b) -> list:
    return [i for i in rows if can_promote(i, open_ids, b)]


def merge_candidates(rows, open_ids, b) -> list:
    """Groups (lists) of 2 or 3 lessons plausibly about the same subject, strongest pairs first, each lesson in at
    most one group; at most `MAX_GROUPS`. Contradicted, proposal-held, promote-ready and heavily used lessons are
    left out (they could never be merged)."""
    pool = [i for i in rows if i["id"] not in open_ids and not memory.contradicted(i)
            and not can_promote(i, open_ids, b) and memory.uses(i) < b["heavy_uses"]]
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


def fingerprint(rows, open_ids, busy, b, now) -> str:
    """Memory's state as far as tidy can tell: ids, counts, cited, contradicted and proposal flags (sorted), the
    day number, the budget and whether a proposal is open."""
    items = sorted([str(i["id"]), int(i.get("count") or 0), memory.cited(i), memory.contradicted(i),
                    i["id"] in open_ids, bool(i.get("proposed")), bool(i.get("promoted")), bool(i.get("rejected"))]
                   for i in rows)
    raw = json.dumps([int(now // memory.DAY_S), bool(busy), sorted(b.items()), items], default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prompt(total, deletes, groups, open_ids, b, now) -> str:
    dl = "\n".join(_line(i, open_ids, now) for i in deletes) or "(none)"
    gl = "\n".join(f"Group {k}:\n" + "\n".join("  " + _line(i, open_ids, now) for i in g)
                   for k, g in enumerate(groups, 1)) or "(none)"
    return PROMPT.format(total=total, limit=b["limit"], rare=b["rare_uses"], stale=b["stale_days"],
                         deletes=dl, groups=gl)


def can_merge(rows, ids, claim, open_ids, b) -> bool:
    """2 or 3 distinct existing lessons, none under an open proposal, contradicted or promote-eligible; a claim
    1-400 characters long that is not already another lesson's."""
    by_id = {i["id"]: i for i in rows}
    if len(ids) != len(set(ids)) or not 2 <= len(ids) <= 3 or any(i not in by_id for i in ids):
        return False
    if any(i in open_ids or memory.contradicted(by_id[i]) or memory.uses(by_id[i]) >= b["heavy_uses"]
           for i in ids):
        return False
    claim = " ".join(str(claim or "").split())
    if not claim or len(claim) > memory.MERGE_CLAIM_MAX:
        return False
    key = memory.normalise(claim)
    return not any(memory.normalise(i.get("claim")) == key for i in rows if i["id"] not in ids)


def _promoted_skill(bot, iid, approved) -> str:
    for p in approved:
        if iid in (p.get("items") or []) and p.get("skill"):
            return str(p["skill"])
    return ""


def _sweep_promoted(bot) -> list:
    """Old data: lessons still flagged `promoted` leave memory (reason promoted, with the approved skill)."""
    from fused_render.experts import proposals
    rows = [i for i in memory.items(bot) if i.get("promoted")]
    if not rows:
        return []
    approved = [p for p in proposals.listing(bot) if p.get("status") == "approved"]
    gone = []
    by_skill: dict[str, list] = {}
    for i in rows:
        by_skill.setdefault(_promoted_skill(bot, i["id"], approved), []).append(i["id"])
    for skill, ids in by_skill.items():
        gone += memory.remove(bot, ids, "promoted", skill=skill)
    return gone


def _over_limit_order(rows, open_ids, now) -> list:
    """Removal order for the hard limit: not user-taught and outside open proposals first, then the rest not
    user-taught, then user-taught; within each, lowest uses then longest idle."""
    def key(i):
        tier = 2 if i.get("trust") == "user" else (1 if i["id"] in open_ids else 0)
        return (tier, memory.uses(i), -memory.idle_days(i, now))
    return sorted(rows, key=key)


def _brief(i, why="") -> dict:
    out = {"id": i["id"], "claim": i.get("claim", "")}
    if why:
        out["why"] = why
    return out


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


def run(bot) -> dict:
    """One tidy: `{"deleted": [ids], "merged": [{"into", "claim", "from": [ids]}], "promoting": [ids],
    "over_limit": [ids]}`. Order: deletions, merges, promotions, the hard limit. Never raises for a model failure
    (nothing is deleted or merged then). The model is called only when there are delete or merge candidates."""
    from fused_render.experts import proposals
    b = budget(bot)
    now = time.time()
    result = {"deleted": [], "merged": [], "promoting": [], "over_limit": []}
    summary = {"at": now, "deleted": [], "merged": [], "promoting": [], "over_limit": [], "swept": [],
               "reviewed": False, "model_called": False, "skipped": "", "error": ""}
    swept = _sweep_promoted(bot)
    summary["swept"] = [_brief(i) for i in swept]
    rows = memory.items(bot)
    summary["total_before"] = len(rows) + len(swept)
    above = len(rows) > b["tidy_at"]

    if above:
        open_ids = proposals.open_item_ids(bot)
        busy = any(p.get("status") in proposals.OPEN_STATUSES for p in proposals.listing(bot))
        if not swept and fingerprint(rows, open_ids, busy, b, now) == _saved_fp(bot):
            summary["skipped"] = "unchanged"
        else:
            summary["reviewed"] = True
            by_id = {i["id"]: i for i in rows}
            deletes_c = delete_candidates(rows, open_ids, b, now)
            groups = merge_candidates(rows, open_ids, b)
            actions = []
            if deletes_c or groups:
                summary["model_called"] = True
                actions = _review(bot, _prompt(len(rows), deletes_c, groups, open_ids, b, now))
                if actions is None:
                    summary["error"] = "the model's answer could not be read; nothing was deleted or merged"
                    actions = []
            else:
                summary["skipped"] = "no_candidates"
            # 1. deletions: delete candidates only
            del_ids = {i["id"] for i in deletes_c}
            deletes = {a["ids"][0]: a["why"] for a in actions
                       if a["do"] == "delete" and a["ids"][0] in del_ids
                       and can_delete(by_id[a["ids"][0]], open_ids, b, now)}
            if deletes:
                gone = memory.remove(bot, list(deletes), "stale", whys=deletes)
                result["deleted"] = [i["id"] for i in gone]
                summary["deleted"] = [_brief(i, deletes.get(i["id"], "")) for i in gone]
            # 2. merges: ids of ONE shown group (at most MAX_MERGES)
            group_of = {i["id"]: k for k, g in enumerate(groups) for i in g}
            for a in [a for a in actions if a["do"] == "merge"]:
                if len(result["merged"]) >= MAX_MERGES:
                    break
                if len({group_of.get(x) for x in a["ids"]}) != 1 or a["ids"][0] not in group_of:
                    continue
                current = memory.items(bot)
                if not can_merge(current, a["ids"], a.get("claim"), open_ids, b):
                    continue
                done = memory.merge(bot, a["ids"], a["claim"], why=a["why"])
                if done is None:
                    continue
                new, _folded = done
                originals = [{"id": i, "claim": by_id[i].get("claim", "")} for i in a["ids"]]
                result["merged"].append({"into": new["id"], "claim": new.get("claim", ""), "from": list(a["ids"])})
                summary["merged"].append({"into": new["id"], "claim": new.get("claim", ""), "from": originals,
                                          **({"why": a["why"]} if a["why"] else {})})
            # 3. promotions, no model: ONE request, none while a proposal is preparing or waiting on its card
            promotes = promote_ready(memory.items(bot), open_ids, b)
            if promotes and not busy:
                try:
                    p = proposals.request(bot, promotes, topic="used a lot")
                    result["promoting"] = list(p.get("items") or [])
                    summary["proposal"] = p.get("id")
                except ValueError as e:  # single flight, or nothing left to propose
                    logger.info("expert %s: tidy promotion skipped: %s", getattr(bot, "id", "?"), e)
                summary["promoting"] = [_brief(i) for i in promotes if i["id"] in result["promoting"]]

    # The hard limit: no model.
    rows = memory.items(bot)
    extra = len(rows) - b["limit"]
    if extra > 0:
        open_ids = proposals.open_item_ids(bot)
        drop = _over_limit_order(rows, open_ids, now)[:extra]
        gone = {i["id"] for i in memory.remove(bot, [i["id"] for i in drop], "over_limit")}
        drop = [i for i in drop if i["id"] in gone]  # in removal order
        result["over_limit"] = [i["id"] for i in drop]
        summary["over_limit"] = [_brief(i) for i in drop]

    changed = bool(swept or any(result.values()))
    if summary["reviewed"]:
        # Remember a clean tidy that changed nothing; anything else is reviewed again next time.
        if not changed and not summary["error"]:
            rows = memory.items(bot)
            open_ids = proposals.open_item_ids(bot)
            busy = any(p.get("status") in proposals.OPEN_STATUSES for p in proposals.listing(bot))
            _save_fp(bot, fingerprint(rows, open_ids, busy, b, now))
        else:
            _save_fp(bot, "")
    if above or changed:
        summary["total_after"] = len(memory.items(bot))
        summary["counts"] = {k: len(summary[k]) for k in ("deleted", "merged", "promoting", "over_limit", "swept")}
        try:
            store.write_json(_last_path(bot), summary)
        except OSError:
            logger.warning("expert %s: tidy summary not saved", getattr(bot, "id", "?"), exc_info=True)
    return result


def skill_pressure(bot, now=None) -> str | None:
    """Request ONE "reorganise" proposal for the largest skill whose body is over `PRESSURE` of the body limit, when
    no proposal is open and that skill is not cooling down after a rejected (7 days) or failed (1 day) reorganise.
    Returns the new proposal's id, or None. No model call decides it; the draft itself is one call."""
    from fused_render.experts import proposals
    now = time.time() if now is None else now
    if proposals.any_open(bot):
        return None
    limit = PRESSURE * proposals.body_cap()
    full = [s for s in bot.skills() if len(proposals.norm_body(s.get("body"))) > limit]
    if not full:
        return None
    cooling = set()
    for p in proposals.listing(bot):
        if p.get("kind") != "reorganise":
            continue
        wait = {"rejected": REORGANISE_COOLDOWN_S, "failed": REORGANISE_FAILED_COOLDOWN_S}.get(p.get("status"))
        if wait and now - float(p.get("decided") or p.get("ts") or 0) < wait:
            cooling.add(p.get("skill"))
    full = [s for s in full if s["name"] not in cooling]
    if not full:
        return None
    target = max(full, key=lambda s: len(proposals.norm_body(s.get("body"))))
    try:
        return proposals.request_reorganise(bot, target["name"])["id"]
    except ValueError as e:  # single flight
        logger.info("expert %s: reorganise not requested: %s", getattr(bot, "id", "?"), e)
        return None
