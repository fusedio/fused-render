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
e. tidying: the memory budget (`tidy.run`): above `tidy_at` lessons, code
   picks heavily used lessons to promote, and a model review is asked only
   about the delete / merge candidates (code-enforced guard rails); above
   `limit`, the least used leave. Then
   skill pressure (`tidy.skill_pressure`): a skill over 80% of the body limit
   gets ONE "reorganise" proposal when none is open.

A contradiction (an observation whose `contradicts` names an existing lesson)
is never a bump of that lesson: `memory.contradict` counts it, and the
observation's claim (the corrected rule) goes to the model like any other
lesson observation, except that an `advice_contradicted` observation may never
be bumped (an answer that does is read as `add`) and may be answered
`{"op": "same", "as": <observation id>}` when another observation in the batch
already states that corrected rule. No observation in the batch may bump a
lesson the batch contradicts.

Only independent evidence raises a lesson's `count` (`counts`): a grounded
`repeated_request` / `user_correction` that is not `after_advice`, an
`error_then_fix` (and, in chat, the user's teaching). An `advice_confirmed`, an
`after_advice` statement or an ungrounded one is credited with
`memory.confirm` instead (`confirmed`, once per session; outcome op
`confirm`), and a new lesson from one starts at count 0.

A user correction capture verified (`by_user` with a `quote`) takes effect at
once: its lesson is trust `user`, `via: "session"`. When it contradicts a
lesson, ONE model call per pair decides (`resolve`): `replace` archives the
old lesson (reason `corrected`, `into` the new one), `narrow` rewrites the old
claim (history kept) and clears its mark; an unreadable answer marks it
contradicted as before. When the old rule was written into a skill, a
correction proposal for that skill is requested (`request_correction`), or
queued in `pending-corrections.json` while a proposal is open.

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
CORRECTIONS_FILE = "pending-corrections.json"
SNOOZE_KEY = "snoozed_until"

RESOLVE_PROMPT = """The user corrected one of an expert's lessons in a session. Decide what happens to the OLD lesson.

Old lesson: {old}
The user's corrected rule: {new}
What the user said (verbatim): {quote}

- "replace": the old lesson is simply wrong (or the corrected rule covers everything it said): it is retired.
- "narrow": the old lesson still holds in some cases; rewrite it so it only claims what still holds, as one
  self-contained sentence in "narrowed_claim" (the corrected rule stays beside it as its own lesson).

Answer with ONLY a JSON object: {{"resolution": "replace" | "narrow", "narrowed_claim": "<only for narrow>"}}"""

_RUNNING: set[str] = set()
_RUNNING_GUARD = threading.Lock()

PROMPT = """You keep an expert's memory of lessons. Below are its current lessons and new observations.
For EACH observation decide one edit:
- "bump": it states the SAME lesson as an existing one (same subject AND same advice), not merely a related one; give that lesson's id as "item".
- "add": it is a new, durable, reusable lesson; give the lesson as one short sentence in "claim". When in doubt between "bump" and "add", choose "add".
- "nothing": it is noise or not a lesson.

An observation of kind "repeated_request" describes what the user asks for, and is counted across sessions: it may only be bumped onto a lesson that is itself a request pattern (kind repeated_request) describing the same request; otherwise "add" it as its own lesson.

An observation of kind "advice_contradicted" states a CORRECTED rule: the session showed earlier advice wrong or too broad ("contradicts [id]" names that lesson). It is never "bump": "add" it as its own lesson, or, when another of the new observations above already states the same corrected rule, answer {{"obs": "<its id>", "op": "same", "as": "<that other observation's id>"}} so it is counted once. A lesson named after "contradicts" is never the target of a "bump" by any observation.

Current lessons (id, kind, claim):
{items}

New observations:
{obs}

Answer with ONLY a JSON list, one object per observation:
[{{"obs": "<observation id>", "op": "add" | "bump" | "nothing" | "same", "item": "<lesson id, for bump>", "claim": "<for add>", "as": "<observation id, for same>"}}]"""


_OUTER_OPEN = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*$")


def _unwrap(text: str) -> str:
    """`text` without ONE outer code-fence wrapper around the whole answer (a leading ```json / ``` line and a
    trailing ``` line). Nothing inside is touched: a fence inside a JSON string survives."""
    s = (text or "").strip()
    lines = s.split("\n")
    if len(lines) >= 2 and _OUTER_OPEN.match(lines[0].strip()) and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return s


def _balanced_end(s: str, i: int) -> int:
    """The index of the bracket closing the JSON value opened at `s[i]`, scanning with string / escape awareness
    (a bracket inside a string does not count); -1 when it never closes."""
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def parse_json(text, want=list):
    """The JSON value of type `want` (list or dict) in a model's answer; None when there is none.

    The contents of JSON strings are never altered: only an outer fence wrapper around the whole answer is removed
    before parsing; failing that, the first balanced JSON value of type `want` (found with string / escape
    awareness, so a code fence or bracket inside a string value survives byte for byte) is parsed."""
    s = _unwrap(text)
    try:
        val = json.loads(s)
    except ValueError:
        val = None
    if isinstance(val, want):
        return val
    opener = "[" if want is list else "{"
    i = s.find(opener)
    while i != -1:
        j = _balanced_end(s, i)
        if j != -1:
            try:
                val = json.loads(s[i:j + 1])
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
STAGES = ("lessons", "gaps", "researching", "proposing", "tidying")
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
    """`{"running": bool, "stage": "lessons"|"gaps"|"researching"|"proposing"|"tidying"|"", "gap": gid|None}`."""
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
    """Whether a run would do anything: observations due (or, with `force`, any pending observation, a gap
    the repair step would research, or memory over its `tidy_at`)."""
    if not force:
        return observations.due(bot)
    from fused_render.experts import gaps, tidy
    return bool(observations.pending(bot)) or bool(_repairable(bot)) or tidy.needed(bot) \
        or bool(queued_corrections(bot))


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


def verified(o) -> bool:
    """A statement capture proved is the user's: `by_user` true with the quote it was checked against."""
    return o.get("by_user") is True and bool(str(o.get("quote") or "").strip())


def echo(o) -> bool:
    """The expert's own advice echoed back: `advice_confirmed`, or a statement made only after its advice."""
    return o.get("kind") == "advice_confirmed" or bool(o.get("after_advice"))


def counts(o) -> bool:
    """Whether observation `o` is independent evidence that raises a lesson's `count` (once per session): an
    `error_then_fix`, or a grounded `repeated_request` / `user_correction` (or a user-stated `advice_contradicted`)
    that is not `after_advice`. Never an `advice_confirmed`. An observation stored before grounding existed (no
    `by_user` key) keeps the old rule (anything but an echo counts)."""
    if echo(o):
        return False
    if o.get("kind") == "error_then_fix":
        return True
    if "by_user" not in o:
        return True
    return verified(o)


def correction(o) -> bool:
    """A verified user correction: takes effect at once (trust `user`)."""
    return verified(o) and o.get("kind") in ("user_correction", "advice_contradicted") and not o.get("after_advice")


def snoozed(gap, now=None) -> bool:
    """A gap the user asked to be asked about later (`snoozed_until` in the future): not asked or researched."""
    import time
    try:
        until = float((gap or {}).get(SNOOZE_KEY) or 0)
    except (TypeError, ValueError):
        return False
    return until > (time.time() if now is None else now)


def _repairable(bot, touched=()) -> list:
    from fused_render.experts import gaps as gapmod
    return [g for g in gapmod.repairable(bot, touched) if not snoozed(g)]


def resolve(bot, old, new_claim, quote) -> dict | None:
    """ONE model decision for a verified user correction of lesson `old`: `{"resolution": "replace"}` or
    `{"resolution": "narrow", "narrowed_claim": str}`; None when the call fails or the answer cannot be read (the
    caller then keeps the old behaviour: the lesson is marked contradicted)."""
    prompt = RESOLVE_PROMPT.format(old=" ".join(str(old.get("claim") or "").split()),
                                   new=" ".join(str(new_claim or "").split()),
                                   quote=" ".join(str(quote or "").split()) or "(not given)")
    try:
        ans = parse_json(model.text(bot, prompt, system="You answer with JSON only."), dict)
    except Exception:  # noqa: BLE001 — no decision: nothing is assumed
        logger.warning("expert %s: correction of %s not resolved", bot.id, old.get("id"), exc_info=True)
        return None
    if not isinstance(ans, dict):
        return None
    res = str(ans.get("resolution") or "").strip().lower()
    if res == "replace":
        return {"resolution": "replace"}
    narrowed = " ".join(str(ans.get("narrowed_claim") or "").split()) if isinstance(ans.get("narrowed_claim"), str) \
        else ""
    if res == "narrow" and narrowed and memory.normalise(narrowed) != memory.normalise(new_claim):
        return {"resolution": "narrow", "narrowed_claim": narrowed}
    return None


def skill_of(bot, item_id) -> str:
    """The skill a lesson was written into: a live lesson's `skill`, the approved proposal that carried a lesson
    still flagged `promoted`, or its memory-archive record (reason `promoted` with a `skill`). "" when none."""
    live = next((i for i in memory.items(bot) if i.get("id") == item_id), None)
    if live is not None:
        if live.get("skill"):
            return str(live["skill"])
        if live.get("promoted"):
            from fused_render.experts import proposals
            for p in proposals.listing(bot):
                if p.get("status") == "approved" and item_id in (p.get("items") or []) and p.get("skill"):
                    return str(p["skill"])
    for rec in memory.promoted(bot):
        if rec.get("id") == item_id:
            return str(rec["skill"])
    return ""


def _corrections_path(bot) -> str:
    import os
    return os.path.join(store.expert_dir(bot), CORRECTIONS_FILE)


def queued_corrections(bot) -> list:
    """The skill corrections waiting for the proposal slot (`pending-corrections.json`), oldest first."""
    rows = store.read_json(_corrections_path(bot), [])
    return [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and r.get("item")]


def request_correction(bot, corr: dict) -> str | None:
    """Request a skill proposal that changes skill `corr["skill"]` to follow the user's correction (lesson
    `corr["item"]`), or queue it when a proposal is open (single flight). Returns the proposal id, or None when it
    was queued or its lesson is gone."""
    import time
    from fused_render.experts import proposals
    with store.lock(bot):
        if proposals.any_open(bot):
            rows = [r for r in queued_corrections(bot) if r.get("item") != corr.get("item")]
            store.write_json(_corrections_path(bot), rows + [dict(corr, at=time.time())])
            return None
    try:
        p = proposals.request(bot, [corr["item"]], topic=f"Correction: {corr.get('old') or ''}"[:200],
                              correction={"skill": corr.get("skill") or "", "old": corr.get("old") or "",
                                          "quote": corr.get("quote") or ""})
    except ValueError as e:
        if str(e) == proposals.BUSY:
            with store.lock(bot):
                rows = [r for r in queued_corrections(bot) if r.get("item") != corr.get("item")]
                store.write_json(_corrections_path(bot), rows + [dict(corr, at=time.time())])
        return None
    return p.get("id") if isinstance(p, dict) else None


def drain_corrections(bot) -> list:
    """The next queued correction, requested when no proposal is open (one per run: single flight). Entries whose
    lesson left memory are dropped. Returns the proposal ids started."""
    from fused_render.experts import proposals
    with store.lock(bot):
        rows = queued_corrections(bot)
        if not rows:
            return []
        live = {i["id"] for i in memory.items(bot)}
        rows = [r for r in rows if r.get("item") in live]
        if proposals.any_open(bot):
            store.write_json(_corrections_path(bot), rows)
            return []
        nxt, rest = (rows[0], rows[1:]) if rows else (None, [])
        store.write_json(_corrections_path(bot), rest)
    if nxt is None:
        return []
    pid = request_correction(bot, nxt)
    return [pid] if pid else []


def _question_of(obs) -> str:
    return _GAP_PREFIX.sub("", str(obs.get("claim") or "")).strip()


def _run(bot, force):
    from fused_render.experts import gaps as gapmod
    if not force and not observations.due(bot):
        return _result(False)
    from fused_render.experts import tidy as tidymod
    pend = observations.pending(bot)
    if not pend and not (force and (_repairable(bot) or tidymod.needed(bot) or queued_corrections(bot))):
        return _result(False)
    ok, why = model.available(bot)
    if not ok:
        return _result(False, reason=why or "no engine is available for this expert")
    _stage(bot, "lessons")
    gap_obs = [o for o in pend if o.get("trust") == "gap" or o.get("kind") == "gap"]
    real = [o for o in pend if o not in gap_obs]
    # An observation that `confirms` an existing lesson (capture validated the id) is a deterministic bump of
    # it, applied below once the model step has not bailed out; it never reaches the model.
    # A request pattern confirms only a request pattern (the same guard as a model "bump" below).
    live = memory.items(bot)
    item_kinds = {i["id"]: i.get("kind") for i in live}
    item_claims = {i["id"]: memory.normalise(i.get("claim")) for i in live}
    # A lesson already written into a skill (archived as promoted) may be contradicted too: its skill then follows.
    in_skills = {r["id"]: r for r in memory.promoted(bot) if r["id"] not in item_kinds}
    for rid, rec in in_skills.items():
        item_claims.setdefault(rid, memory.normalise(rec.get("claim")))
    # An observation that `contradicts` an existing lesson: a contradiction of it (applied below, never a bump),
    # while its claim, the corrected rule, goes to the model as a lesson of its own.
    contradicting = [o for o in real if str(o.get("contradicts") or "") in item_kinds
                     or str(o.get("contradicts") or "") in in_skills]
    contra_items = {str(o["contradicts"]) for o in contradicting}
    confirmed = [o for o in real if o not in contradicting and str(o.get("confirms") or "") in item_kinds
                 and (o.get("kind") != "repeated_request" or item_kinds[o["confirms"]] == "repeated_request")]
    real = [o for o in real if o not in confirmed]
    added, bumped, noted, contradicted, ignored = [], [], [], [], len(gap_obs)
    resolved: list[dict] = []
    open_gaps = gapmod.open_gaps(bot) if gap_obs else []
    offer = gap_obs if open_gaps else []  # gap observations reach the model only to be merged into a gap

    outcomes: dict[str, dict] = {}  # observation id -> what this run did with it (stored on the archived record)

    def note(o, item, before_ids):
        if item is None:
            return
        op = "add" if item["id"] not in before_ids else "bump" if counts(o) else "confirm"
        (added if op == "add" else bumped if op == "bump" else noted).append(item["id"])
        outcomes[o["id"]] = {"op": op, "item": item["id"]}

    def credit(o, item_id):
        # Independent evidence raises the count (once per session); an echo of the expert's advice, or a statement
        # not grounded in a user line, is only noted as confirmed.
        if counts(o):
            it, op = memory.bump(bot, item_id, session=o.get("session") or ""), "bump"
        else:
            it, op = memory.confirm(bot, item_id, session=o.get("session") or ""), "confirm"
        if it is not None:
            (bumped if op == "bump" else noted).append(it["id"])
            outcomes[o["id"]] = {"op": op, "item": it["id"]}
        return it

    def lesson_kind(o):
        # The corrected rule of a contradiction is a user correction when a USER line said so (capture's `by_user`).
        if o.get("kind") == "advice_contradicted" and o.get("by_user"):
            return "user_correction"
        return o.get("kind") or "lesson"

    def add(o, claim=None):
        claim = claim if claim is not None else o.get("claim")
        old = str(o.get("contradicts") or "")
        if old in item_claims and memory.normalise(claim) == item_claims[old]:
            return True  # the "corrected rule" is the contradicted lesson's own text: never a bump of it
        before = {i["id"] for i in memory.items(bot)}
        user = correction(o)  # a verified user correction takes effect at once, like chat teaching
        try:
            note(o, memory.add(bot, claim, kind=lesson_kind(o),
                               trust="user" if user else (o.get("trust") or "outcome"),
                               session=o.get("session") or "", about=o.get("about"),
                               via="session" if user else "", counted=counts(o)), before)
            return True
        except ValueError:
            return False

    edits, fallback = None, False
    if real or offer:
        current = memory.items(bot)
        items_txt = "\n".join(f"- [{i['id']}] ({i.get('kind', 'lesson')}) {i.get('claim', '')} "
                              f"(seen {i.get('count', 0)}×)"
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
    same: dict[str, str] = {}  # an advice_contradicted observation id -> the observation that states its rule
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
        if o.get("kind") == "advice_contradicted":
            if op == "same" and str(e.get("as") or "") in by_id and str(e.get("as")) != o["id"]:
                same[o["id"]] = str(e["as"])
                continue
            if op == "bump":  # a contradiction never raises confidence in anything: its rule is a lesson of its own
                op, e = "add", {**e, "claim": None}
        elif op == "bump" and str(e.get("item") or "") in contra_items:
            op, e = "add", {**e, "claim": None}  # never a bump of a lesson this batch contradicts
        if op == "bump" and o.get("kind") == "repeated_request":
            # A request pattern is its own lesson: bumped only onto a request pattern, else added.
            target = next((i for i in memory.items(bot) if i.get("id") == str(e.get("item") or "")), None)
            if target is None or target.get("kind") != "repeated_request":
                op = "add"
                e = {**e, "claim": None}
        if op == "bump" and e.get("item"):
            if credit(o, str(e["item"])) is None:
                ignored += 1  # a bump of a lesson that does not exist: nothing
        elif op == "add":
            claim = e["claim"] if isinstance(e.get("claim"), str) and e["claim"].strip() else o.get("claim")
            if not add(o, claim):
                ignored += 1
        elif o not in contradicting:
            ignored += 1  # "nothing", or an op we do not know
    if not fallback:
        # Omitted by the answer: nothing (a contradiction still counts below).
        ignored += len([o for o in real if o["id"] not in handled and o not in contradicting])
    for oid, other in same.items():
        # The corrected rule another observation of this batch states: counted toward that lesson, not added again.
        target = (outcomes.get(other) or {}).get("item")
        if target and target not in contra_items:
            credit(by_id[oid], target)
    corrections: list[dict] = []  # skill corrections to request once the observations are archived
    decided: dict[tuple, dict | None] = {}  # (old, new) -> the resolution, one model call per pair
    for o in contradicting:
        old_id = str(o["contradicts"])
        new = (outcomes.get(o["id"]) or {}).get("item")
        partner = by_id.get(same.get(o["id"], ""))
        user = correction(o) or (partner is not None and correction(partner))
        quote = o.get("quote") if verified(o) else (partner or {}).get("quote")
        old = next((i for i in memory.items(bot) if i.get("id") == old_id), None) or in_skills.get(old_id)
        if old is None:
            continue  # forgotten between the check above and now
        is_live = old_id not in in_skills
        skill = skill_of(bot, old_id) if user and new and new != old_id else ""
        res = None
        if user and new and new != old_id and is_live:
            key = (old_id, new)
            if key not in decided:
                new_claim = next((i.get("claim") for i in memory.items(bot) if i.get("id") == new), o.get("claim"))
                decided[key] = resolve(bot, old, new_claim, quote)
                d = decided[key]
                if d and d["resolution"] == "replace":
                    if not memory.remove(bot, [old_id], "corrected", into=new):
                        decided[key] = None
                elif d and d["resolution"] == "narrow":
                    if memory.narrow(bot, old_id, d["narrowed_claim"]) is None:
                        decided[key] = None
                if decided[key]:
                    resolved.append({"item": old_id, "resolution": decided[key]["resolution"], "into": new})
            res = decided[key]
        if is_live and res is None:
            it = memory.contradict(bot, old_id, session=o.get("session") or "")
            if it is not None:
                contradicted.append(it["id"])
        oc = {"op": "contradict", "item": old_id}
        if new and new != old_id:
            oc["new"] = new
        if res:
            oc["resolution"] = res["resolution"]
        outcomes[o["id"]] = oc
        if skill and not any(c["item"] == new and c["skill"] == skill for c in corrections):
            corrections.append({"item": new, "skill": skill, "old": old.get("claim", ""), "quote": quote or "",
                                "old_id": old_id})
    for o in confirmed:
        # memory.bump counts a session once, so a repeat from the same session does not inflate the count.
        if credit(o, str(o["confirms"])) is None:
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
    for g in _repairable(bot, touched):
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
    corrected = []
    try:
        # A skill that states a rule the user corrected comes first: the queued ones, then this run's.
        corrected = drain_corrections(bot)
        for c in corrections:
            pid = request_correction(bot, c)
            if pid:
                corrected.append(pid)
    except Exception:  # noqa: BLE001
        logger.warning("expert %s: skill correction not requested", bot.id, exc_info=True)
    try:
        proposed = corrected + proposals.propose_candidates(bot)
    except Exception:  # noqa: BLE001
        logger.warning("expert %s: proposing failed", bot.id, exc_info=True)
        proposed = list(corrected)
    # The memory budget: last, after lessons, gaps and the normal proposals.
    _stage(bot, "tidying")
    try:
        tidied = tidymod.run(bot)
    except Exception:  # noqa: BLE001 — a failed tidy never undoes the run
        logger.warning("expert %s: tidying failed", bot.id, exc_info=True)
        tidied = {"deleted": [], "merged": [], "promoting": [], "over_limit": []}
    # Skill pressure: a nearly full skill gets ONE reorganise proposal (no model call decides it).
    try:  # a proposal requested just now is open: no reorganise (skill_pressure would say the same)
        reorganise = None if proposed or tidied["promoting"] else tidymod.skill_pressure(bot)
    except Exception:  # noqa: BLE001
        logger.warning("expert %s: skill pressure check failed", bot.id, exc_info=True)
        reorganise = None
    out = _result(True, added, bumped, ignored)
    out.update(contradicted=list(dict.fromkeys(contradicted)), gaps=touched, researched=researched, proposed=proposed,
               tidied=tidied, reorganise=reorganise, confirmed=list(dict.fromkeys(noted)), resolved=resolved,
               corrections=corrected)
    if (researched or touched) and not proposed and not tidied["promoting"] and not reorganise:
        # A gap research could not answer may now be asked (the scheduler's cards.tick would, later).
        try:
            from fused_render.experts import cards
            cards.ask_if_needed(bot)
        except Exception:  # noqa: BLE001
            logger.warning("expert %s: gap question not raised", bot.id, exc_info=True)
    return out
