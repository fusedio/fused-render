"""Consolidation: the one learning loop (docs/experts.md §4 `consolidate.py`, §11).

observations -> consolidation -> memory -> skill fixes. One `run`:

a. lessons: an observation whose `confirms` names an existing lesson is a
   bump of it with the observation's session, no model decision; for the
   rest one `model.text` call proposes an edit per lesson observation
   (add / bump / nothing); code applies them. The model is the filter between
   transcript text and memory, so a failed call or an unreadable answer adds
   nothing: the observations stay pending and a failure is counted
   (`consolidate-failures.json`); only after `FALLBACK_AFTER` consecutive
   failures does it fall back to one exact-match `memory.add` each.
b. gaps: gap observations (unsure consults) become gap records
   (`gaps.record`); the same call may answer `{"op": "gap", "gap": id}` to fold
   a question asked in other words into an open gap. Never lessons.
c. repair: up to `gaps.REPAIR_PER_RUN` open gaps are researched
   (`gaps.research`); a grounded finding is a `research` lesson plus a skill
   proposal.
d. proposing: every due lesson as one proposal (`proposals.propose_candidates`).

Each archived observation records what happened to it (`outcome`). `start`
runs it in a background thread; `status` says which stage is running.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from fused_render.experts import memory, model, observations, store

logger = logging.getLogger(__name__)

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


#: bot id -> {"stage", "gap"} while a run is in flight.
_STAGE: dict[str, dict] = {}
STAGES = ("lessons", "gaps", "researching", "proposing")
_GAP_PREFIX = re.compile(r"^\s*could not answer:\s*", re.I)

GAP_SECTION = """

Some observations above have kind "gap": a question the expert could not answer. Below are the open gaps (questions
already recorded). For a gap observation that asks the SAME thing as one of these in other words, answer
{{"obs": "<observation id>", "op": "gap", "gap": "<gap id>"}}; otherwise "nothing". A gap observation is never
"add" or "bump".

Open gaps:
{gaps}"""


def _stage(bot, stage, gap=None) -> None:
    with _RUNNING_GUARD:
        _STAGE[str(bot.id)] = {"stage": stage, "gap": gap}


def status(bot) -> dict:
    """`{"running": bool, "stage": "lessons"|"gaps"|"researching"|"proposing"|"", "gap": gid|None}`."""
    key = str(bot.id)
    with _RUNNING_GUARD:
        if key not in _RUNNING:
            return {"running": False, "stage": "", "gap": None}
        st = _STAGE.get(key) or {}
        return {"running": True, "stage": st.get("stage") or "", "gap": st.get("gap")}


def _claim(bot) -> bool:
    key = str(bot.id)
    with _RUNNING_GUARD:
        if key in _RUNNING:
            return False
        _RUNNING.add(key)
        _STAGE[key] = {"stage": "", "gap": None}
        return True


def _release(bot) -> None:
    key = str(bot.id)
    with _RUNNING_GUARD:
        _RUNNING.discard(key)
        _STAGE.pop(key, None)


def would_run(bot, force=False) -> bool:
    """Whether a run would do anything: observations due (or, with `force`, any pending observation or a gap
    the repair step would research)."""
    if not force:
        return observations.due(bot)
    from fused_render.experts import gaps
    return bool(observations.pending(bot)) or bool(gaps.repairable(bot))


def run(bot, *, force=False) -> dict:
    """One consolidation, synchronously (the scheduler's thread): lessons, gap records, gap repair, proposals."""
    if not _claim(bot):
        return _result(False)
    try:
        return _run(bot, force)
    finally:
        _release(bot)


def start(bot, *, force=False) -> bool:
    """`run` in a background thread. False when one is already running for this expert or there is nothing to
    do (`would_run`)."""
    if not _claim(bot):
        return False
    try:
        if not would_run(bot, force):
            _release(bot)
            return False

        def go():
            try:
                _run(bot, force)
            except Exception:  # noqa: BLE001 — never out of a helper thread
                logger.warning("expert %s: consolidation failed", getattr(bot, "id", "?"), exc_info=True)
            finally:
                _release(bot)

        threading.Thread(target=go, daemon=True, name=f"expert-consolidate-{bot.id}").start()
        return True
    except Exception:
        _release(bot)
        raise


def _question_of(obs) -> str:
    return _GAP_PREFIX.sub("", str(obs.get("claim") or "")).strip()


def _run(bot, force):
    from fused_render.experts import gaps as gapmod
    if not force and not observations.due(bot):
        return _result(False)
    pend = observations.pending(bot)
    if not pend and not (force and gapmod.repairable(bot)):
        return _result(False)
    ok, why = model.available(bot)
    if not ok:
        return _result(False, reason=why or "no engine is available for this expert")
    _stage(bot, "lessons")
    gap_obs = [o for o in pend if o.get("trust") == "gap" or o.get("kind") == "gap"]
    real = [o for o in pend if o not in gap_obs]
    # An observation that `confirms` an existing lesson (capture validated the id) is a deterministic bump of
    # it, applied below once the model step has not bailed out; it never reaches the model.
    item_ids = {i["id"] for i in memory.items(bot)}
    confirmed = [o for o in real if str(o.get("confirms") or "") in item_ids]
    real = [o for o in real if o not in confirmed]
    added, bumped, ignored = [], [], len(gap_obs)
    open_gaps = gapmod.open_gaps(bot) if gap_obs else []
    offer = gap_obs if open_gaps else []  # gap observations reach the model only to be merged into a gap

    outcomes: dict[str, dict] = {}  # observation id -> what this run did with it (stored on the archived record)

    def note(o, item, before_ids):
        if item is None:
            return
        op = "bump" if item["id"] in before_ids else "add"
        (bumped if op == "bump" else added).append(item["id"])
        outcomes[o["id"]] = {"op": op, "item": item["id"]}

    def add(o, claim=None):
        before = {i["id"] for i in memory.items(bot)}
        try:
            note(o, memory.add(bot, claim if claim is not None else o.get("claim"), kind=o.get("kind") or "lesson",
                               trust=o.get("trust") or "outcome", session=o.get("session") or "",
                               about=o.get("about")), before)
            return True
        except ValueError:
            return False

    edits, fallback = None, False
    if real or offer:
        current = memory.items(bot)
        items_txt = "\n".join(f"- [{i['id']}] {i.get('claim', '')} (seen {i.get('count', 0)}×)"
                              for i in current) or "(none yet)"
        obs_txt = "\n".join(f"- [{o['id']}] ({o.get('kind', '')}, trust {o.get('trust', '')}) {o.get('claim', '')}"
                            for o in real + offer)
        prompt = PROMPT.format(items=items_txt, obs=obs_txt)
        if offer:
            prompt += GAP_SECTION.format(gaps="\n".join(
                f"- [{g['id']}] {g['question']} (asked {g.get('count', 0)}×)" for g in open_gaps))
        error = ""
        try:
            answer = model.text(bot, prompt, system="You answer with JSON only.")
            edits = parse_json(answer, list)
            if edits is None:
                error = "the model's answer could not be read"
        except Exception as e:  # noqa: BLE001 — counted below; nothing is added on a failed call
            error = f"the model call failed: {e}"
        if edits is None and real:
            n = failures(bot) + 1
            if n < FALLBACK_AFTER:
                _set_failures(bot, n, error)
                return _result(False, reason=f"{error}; the observations stay pending ({n} of "
                                             f"{FALLBACK_AFTER} tries)")
            # Consecutive failures: exact-match adds (memory.add's dedupe), so the queue cannot grow forever.
            for o in real:
                add(o)
            fallback = True
    if real or not offer or edits is not None:
        _set_failures(bot, 0)

    by_id = {o["id"]: o for o in real}
    gap_ids = {o["id"] for o in offer}
    open_ids = {g["id"] for g in open_gaps}
    merges: dict[str, str] = {}
    handled: set[str] = set()
    for e in (edits or []) if not fallback else []:
        if not isinstance(e, dict):
            continue
        oid = str(e.get("obs") or "")
        if oid in gap_ids:
            if str(e.get("op") or "").lower() == "gap" and str(e.get("gap") or "") in open_ids:
                merges.setdefault(oid, str(e["gap"]))
            continue  # never a lesson
        o = by_id.get(oid)
        if o is None or o["id"] in handled:
            continue
        handled.add(o["id"])
        op = str(e.get("op") or "").lower()
        if op == "bump" and e.get("item"):
            it = memory.bump(bot, str(e["item"]), session=o.get("session") or "")
            if it is not None:
                bumped.append(it["id"])
                outcomes[o["id"]] = {"op": "bump", "item": it["id"]}
            else:
                ignored += 1  # a bump of a lesson that does not exist: nothing
        elif op == "add":
            claim = e["claim"] if isinstance(e.get("claim"), str) and e["claim"].strip() else o.get("claim")
            if not add(o, claim):
                ignored += 1
        else:
            ignored += 1  # "nothing", or an op we do not know
    if not fallback:
        ignored += len([o for o in real if o["id"] not in handled])  # omitted by the answer: nothing
    for o in confirmed:
        # memory.bump counts a session once, so a repeat from the same session does not inflate the count.
        it = memory.bump(bot, str(o["confirms"]), session=o.get("session") or "")
        if it is not None:
            bumped.append(it["id"])
            outcomes[o["id"]] = {"op": "bump", "item": it["id"]}
        else:
            ignored += 1  # forgotten between the check above and now

    # Gap observations -> gap records (one count per distinct consult), merged where the model said so.
    _stage(bot, "gaps")
    touched: list[str] = []
    renamed: dict[str, str] = {}  # a gap id merged away this run -> the gap it went into
    for o in gap_obs:
        q = _question_of(o)
        if not q:
            continue
        try:
            g = gapmod.record(bot, q, consult=o.get("consult") or "", session=o.get("session") or "")
            target = merges.get(o["id"])
            if target and target != g["id"]:
                kept = gapmod.merge(bot, g["id"], target)
                if kept is not None:
                    touched = [t for t in touched if t not in (g["id"], target)]
                    for gone in (g["id"], target):
                        if gone != kept["id"]:
                            renamed[gone] = kept["id"]
                    g = kept
            if g["id"] not in touched:
                touched.append(g["id"])
            outcomes[o["id"]] = {"op": "gap", "gap": g["id"]}
        except Exception:  # noqa: BLE001 — one bad record never blocks the run
            logger.warning("expert %s: gap observation %s not recorded", bot.id, o.get("id"), exc_info=True)
    for oc in outcomes.values():
        while oc.get("gap") in renamed:
            oc["gap"] = renamed[oc["gap"]]
    observations.archive(bot, [o["id"] for o in pend], outcomes)

    # Repair: research the open gaps touched now, plus older ones still worth a try.
    researched = []
    for g in gapmod.repairable(bot, touched):
        _stage(bot, "researching", g["id"])
        try:
            gapmod.research(bot, g["id"])
            researched.append(g["id"])
        except Exception:  # noqa: BLE001
            logger.warning("expert %s: gap %s not researched", bot.id, g["id"], exc_info=True)

    # Skill fixes: every lesson that is due (count >= propose_at, or an un-proposed research lesson) as ONE
    # proposal; none while one is being prepared (the rest wait for the next run).
    _stage(bot, "proposing")
    from fused_render.experts import proposals
    try:
        proposed = proposals.propose_candidates(bot)
    except Exception:  # noqa: BLE001
        logger.warning("expert %s: proposing failed", bot.id, exc_info=True)
        proposed = []
    out = _result(True, added, bumped, ignored)
    out.update(gaps=touched, researched=researched, proposed=proposed)
    if (researched or touched) and not proposed:
        # A gap research could not answer may now be asked (the scheduler's cards.tick would, later).
        try:
            from fused_render.experts import cards
            cards.ask_if_needed(bot)
        except Exception:  # noqa: BLE001
            logger.warning("expert %s: gap question not raised", bot.id, exc_info=True)
    return out
