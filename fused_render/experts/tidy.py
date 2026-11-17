"""The memory budget (docs/experts.md "Memory budget").

Memory holds at most `limit` lessons (manifest `memory.limit`, 50). When it
holds more than `tidy_at` (30), the end of a consolidation run also tidies it:
one `model.text` call reviews every lesson and answers, per lesson, `delete`
(old and rarely used), `promote` (used a lot) or `keep`. Code enforces the
guard rails whatever the model says:

- `delete` only when `uses <= rare_uses`, idle for `stale_days` or more, not
  taught by the user and not inside an open proposal. Archived as `stale`.
- `promote` only when `uses >= heavy_uses`, not contradicted, not rejected,
  not a gap, and not inside an open proposal. All promotions go into ONE
  `proposals.request`, and none while a proposal is preparing or waiting on its
  card. The lessons stay in memory until the user approves.
- `merge` (2-3 related, compatible lessons -> one generalised lesson) only for
  distinct existing ids outside open proposals, not contradicted and not
  promote-eligible, with a claim of 1-400 characters; at most `MAX_MERGES` per
  tidy. A claim equal to one original keeps that item and folds the others in.
  The originals are archived as `merged` with `into`.
- Each lesson id belongs to the first action that names it.
- An unreadable answer or a failed call: no deletions, merges or promotions.

Then, with no model, the hard limit: while memory holds more than `limit`,
lessons leave (archived as `over_limit`): first lessons not taught by the user
and outside any open proposal, lowest uses then longest idle; a user-taught
lesson only when nothing else is left.

Old data: an item still flagged `promoted` (from before promoted lessons left
memory on approval) is archived as `promoted` whenever tidy runs.
"""
from __future__ import annotations

import logging
import os
import time

from fused_render.experts import memory, model, store

logger = logging.getLogger(__name__)

LAST_FILE = "tidy-last.json"
MAX_MERGES = 5
_SOURCES = {"user": "taught by you", "outcome": "from sessions", "research": "found by research",
            "gap": "an unanswered question"}

PROMPT = """You keep an expert's memory of lessons tidy. Memory holds {total} lessons; it should stay well under {limit}.
Review every lesson below and decide what to do with each:
- "delete": the lesson is OLD and RARELY USED (seen and cited {rare} times or fewer in total, nothing in the last
  {stale} days). Lessons taught by the user should be kept.
- "promote": the lesson is USED A LOT (seen and cited {heavy} times or more in total) and is still right: it should
  become part of a skill (a playbook) and leave memory.
- "merge": two or three lessons about the SAME subject that say COMPATIBLE things, used enough to keep but not
  enough to promote, become ONE lesson. Write one self-contained lesson that keeps what they share and the most
  useful specifics. Never merge lessons that disagree. When unsure, keep them separate.
- "keep": anything else. When unsure, keep.

Lessons (one per line: id | claim | where it came from | seen N times | cited N times | age in days | days since
last seen or used | contradicted N times | proposal open):
{lines}

Answer with ONLY a JSON list, one object per action (lessons you leave out are kept; name each lesson at most once):
[{{"id": "<lesson id>", "do": "delete" | "promote" | "keep", "why": "<one short reason>"}},
 {{"do": "merge", "ids": ["<lesson id>", "<lesson id>"], "claim": "<the one generalised lesson>", "why": "<reason>"}}]"""


def _last_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), LAST_FILE)


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


def _review(bot, rows, open_ids, b, now):
    """The model's actions in answer order, `[{"do", "ids": [..], "claim"?, "why"}]`, or None when the call failed
    or the answer cannot be read. Each lesson id belongs to the first action that names it; a later action naming
    an id already taken is dropped whole."""
    from fused_render.experts.consolidate import parse_json
    prompt = PROMPT.format(total=len(rows), limit=b["limit"], rare=b["rare_uses"], stale=b["stale_days"],
                           heavy=b["heavy_uses"], lines="\n".join(_line(i, open_ids, now) for i in rows))
    try:
        answer = model.text(bot, prompt, system="You answer with JSON only.")
    except Exception:  # noqa: BLE001 — no verdicts: nothing deleted, merged or promoted by the model
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
        elif do in ("delete", "promote", "keep"):
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
    return (memory.uses(i) >= b["heavy_uses"] and not memory.contradicted(i) and not i.get("rejected")
            and not i.get("promoted") and i.get("trust") != "gap" and i["id"] not in open_ids)


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


def run(bot) -> dict:
    """One tidy: `{"deleted": [ids], "merged": [{"into", "claim", "from": [ids]}], "promoting": [ids],
    "over_limit": [ids]}`. Order: deletions, merges, promotions, the hard limit. Never raises for a model failure
    (nothing is deleted, merged or promoted then)."""
    from fused_render.experts import proposals
    b = budget(bot)
    now = time.time()
    result = {"deleted": [], "merged": [], "promoting": [], "over_limit": []}
    summary = {"at": now, "deleted": [], "merged": [], "promoting": [], "over_limit": [], "swept": [],
               "reviewed": False, "error": ""}
    swept = _sweep_promoted(bot)
    summary["swept"] = [_brief(i) for i in swept]
    rows = memory.items(bot)
    summary["total_before"] = len(rows) + len(swept)

    if len(rows) > b["tidy_at"]:
        open_ids = proposals.open_item_ids(bot)
        actions = _review(bot, rows, open_ids, b, now)
        summary["reviewed"] = True
        if actions is None:
            summary["error"] = "the model's answer could not be read; nothing was deleted, merged or promoted"
        else:
            by_id = {i["id"]: i for i in rows}
            # 1. deletions
            deletes = {a["ids"][0]: a["why"] for a in actions
                       if a["do"] == "delete" and a["ids"][0] in by_id
                       and can_delete(by_id[a["ids"][0]], open_ids, b, now)}
            if deletes:
                gone = memory.remove(bot, list(deletes), "stale", whys=deletes)
                result["deleted"] = [i["id"] for i in gone]
                summary["deleted"] = [_brief(i, deletes.get(i["id"], "")) for i in gone]
            # 2. merges (at most MAX_MERGES)
            for a in [a for a in actions if a["do"] == "merge"]:
                if len(result["merged"]) >= MAX_MERGES:
                    break
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
            # 3. promotions: ONE request, none while a proposal is preparing or waiting on its card
            current = {i["id"]: i for i in memory.items(bot)}
            promotes = [current[a["ids"][0]] for a in actions
                        if a["do"] == "promote" and a["ids"][0] in current
                        and can_promote(current[a["ids"][0]], open_ids, b)]
            busy = any(p.get("status") in proposals.OPEN_STATUSES for p in proposals.listing(bot))
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

    if summary["reviewed"] or swept or result["over_limit"]:
        summary["total_after"] = len(memory.items(bot))
        summary["counts"] = {k: len(summary[k]) for k in ("deleted", "merged", "promoting", "over_limit", "swept")}
        try:
            store.write_json(_last_path(bot), summary)
        except OSError:
            logger.warning("expert %s: tidy summary not saved", getattr(bot, "id", "?"), exc_info=True)
    return result
