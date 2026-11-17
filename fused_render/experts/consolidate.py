"""Learn: the one loop (docs/experts.md "Learn : one loop").

`run(bot, force=False)` is one pass, synchronously; `start` runs it in a background thread; `status` says
whether one is running, its stage and how many inbox entries wait. Nothing here waits for a person and nothing
here writes memory or skills: a run is a list of independent steps, each a generator of changes, and the loop
applies every change through `knowledge.apply` (the step receives the activity line back from its `yield`).

1. `lessons` (this module): inbox entries into lesson changes.
2. `research` (`gaps.step`): open unsure questions.
3. `distill` (`distill.step`): lessons that earned a skill.
4. `tidy` (`tidy.step`): the memory budget.

A step that raises is logged as a `note skipped` line and the next step still runs. Steps share one dict, `ctx`:
`inbox` (the entries waiting when the run began). The lessons step's hand-over to distill (a verified user
correction of a rule a skill states) is kept on disk (`distill.remember_correction`), so it survives a failed run.

Due (`due`): `consolidate_at` entries wait, or the oldest is `consolidate_after_s` old, or any waiting entry is
from the user (`taught`, `forget`, or a correction with a verified quote), or `force`. Never when the manifest
says `learning: false`.

The inbox is `<expert>/observations/pending/*.json`; a handled entry moves to `observations/done/` with what
happened to it (`outcome`). An entry whose change `knowledge.apply` could not make (`ok: false`) is NOT handled:
it stays waiting for the next run, at most `MAX_TRIES` times (`inbox-retries.json`), then it is archived with
outcome `failed` and one `note skipped`.

The lessons step, rule by rule (docs/experts.md "Capture"):

- `taught` / `forget` (chat, `by_user` true only): a `user` lesson, or the matching lessons leave. No model.
  A `forget` also cancels every older `taught` entry of the same inbox whose text it matches: neither is applied.
- An entry whose `confirms` names an existing lesson is credited to it with no model decision. For the rest ONE
  `model.text` call answers an edit per entry (add / bump / nothing). The model is the filter between transcript
  text and memory, so a failed call or an unreadable answer adds nothing: the entries stay waiting and the
  failure is counted (`consolidate-failures.json`); only after `FALLBACK_AFTER` consecutive failures does each
  become an exact-match add.
- Only independent evidence raises a lesson's `count` (`counts`): an `error_then_fix`, or a grounded
  `repeated_request` / `user_correction` that is not `after_advice`. Anything else is a `lesson_confirm`, and a
  new lesson from it starts at count 0. Once per session either way.
- A contradiction (`contradicts` names a lesson) is never a bump of that lesson; its claim, the corrected rule,
  is a lesson of its own. A verified user correction (`correction`) takes effect at once: its lesson is trust
  `user`, and ONE model call per pair (`resolve`) decides whether the old lesson is replaced (removed, reason
  `corrected`) or narrowed (`lesson_edit`). Without a decision the old lesson is only marked contradicted. When
  the old rule already lives in a skill, the pair goes to distill (`distill.remember_correction`).
- Unsure questions (`gap` entries) are recorded and grouped (`gaps.record`, `gaps.merge`); never lessons.
- `worked` (the need and the approach that satisfied it) and `failed` (the need, what was tried, why it failed)
  become lessons of their own kind, trust `outcome`, under the same evidence rule: once per session, used in
  answers after two sessions; a `worked` that only repeats this expert's advice (`after_advice`) does not count.
  They are bumped only onto a lesson of the same kind. A `failed` entry that became (or was counted toward) a
  lesson also leaves an open question for the research step (`gaps.record`: "What works for: <need>? Tried and
  failed: <tried>."), under the gaps' own caps. Its `contradicts`, when the failed approach was the expert's own
  advice, is handled like any contradiction.

The activity label of a lesson change (`learned` / `noticed` / `counted`) is `knowledge.apply`'s, not this step's.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from fused_render.experts import distill, gaps, knowledge, memory, model, store, tidy
from fused_render.experts.engine import parse_json

logger = logging.getLogger(__name__)

FALLBACK_AFTER = 3
FAILURES_FILE = "consolidate-failures.json"
RETRIES_FILE = "inbox-retries.json"
MAX_TRIES = 3
USER_KINDS = ("taught", "forget")

RESOLVE_PROMPT = """The user corrected one of an expert's lessons in a session. Decide what happens to the OLD lesson.

Old lesson: {old}
The user's corrected rule: {new}
What the user said (verbatim): {quote}

- "replace": the old lesson is simply wrong (or the corrected rule covers everything it said): it is retired.
- "narrow": the old lesson still holds in some cases; rewrite it so it only claims what still holds, as one
  self-contained sentence in "narrowed_claim" (the corrected rule stays beside it as its own lesson).

Answer with ONLY a JSON object: {{"resolution": "replace" | "narrow", "narrowed_claim": "<only for narrow>"}}"""

PROMPT = """You keep an expert's memory of lessons. Below are its current lessons and new observations.
For EACH observation decide one edit:
- "bump": it states the SAME lesson as an existing one (same subject AND same advice), not merely a related one; give that lesson's id as "item".
- "add": it is a new, durable, reusable lesson; give the lesson as one short sentence in "claim". When in doubt between "bump" and "add", choose "add".
- "nothing": it is noise or not a lesson.

An observation of kind "repeated_request" describes what the user asks for, and is counted across sessions: it may only be bumped onto a lesson that is itself a request pattern (kind repeated_request) describing the same request; otherwise "add" it as its own lesson.

An observation of kind "worked" states a need and the approach that satisfied it in a session; one of kind "failed" states a need, the approach that was tried and why it did NOT work. Both are lessons worth keeping: "add" them unless they are noise. A "worked" observation may only be bumped onto a lesson of kind worked that states the same need AND the same approach; a "failed" one only onto a lesson of kind failed about the same approach. Nothing else is ever bumped onto a lesson of kind failed (it says what does not work). Keep the claim's reference to the file, dataset or call.

An observation of kind "advice_contradicted" states a CORRECTED rule: the session showed earlier advice wrong or too broad ("contradicts [id]" names that lesson). It is never "bump": "add" it as its own lesson, or, when another of the new observations above already states the same corrected rule, answer {{"obs": "<its id>", "op": "same", "as": "<that other observation's id>"}} so it is counted once. A lesson named after "contradicts" is never the target of a "bump" by any observation.

Current lessons (id, kind, claim):
{items}

New observations:
{obs}

Answer with ONLY a JSON list, one object per observation:
[{{"obs": "<observation id>", "op": "add" | "bump" | "nothing" | "same", "item": "<lesson id, for bump>", "claim": "<for add>", "as": "<observation id, for same>"}}]"""

GAP_SECTION = """

Some observations above have kind "gap": a question the expert could not answer. Below are the open gaps (questions
already recorded). For a gap observation that asks the SAME thing as one of these in other words, answer
{{"obs": "<observation id>", "op": "gap", "gap": "<gap id>"}}; otherwise "nothing". A gap observation is never
"add" or "bump".

Open gaps:
{gaps}"""

_GAP_PREFIX = re.compile(r"^\s*could not answer:\s*", re.I)
_WHY = {"taught": "You taught this in chat.", "error_then_fix": "An error in a task was fixed this way.",
        "repeated_request": "You asked for this in a task.", "worked": "This worked in a task.",
        "failed": "This did not work in a task.",
        "advice_confirmed": "A task followed this advice and it held."}
#: Kinds that are counted across sessions only within their own kind (a request onto a request, what worked onto
#: what worked, what failed onto what failed), and the kinds whose outcome is evidence in itself.
OWN_KIND = ("repeated_request", "worked", "failed")
OUTCOME_KINDS = ("error_then_fix", "worked", "failed")
QUESTION_PART_MAX = 300

_GUARD = threading.Lock()
#: bot id -> the stage of the run in flight.
_RUNNING: dict[str, str] = {}


# ------------------------------------------------------------------ the inbox (read side) ---
def _dir(bot, which) -> str:
    return os.path.join(store.expert_dir(bot), "observations", which)


def waiting(bot) -> list:
    """The inbox entries not handled yet, oldest first."""
    path = _dir(bot, "pending")
    try:
        names = [n for n in os.listdir(path) if n.endswith(".json")]
    except OSError:
        return []
    rows = [store.read_json(os.path.join(path, n), None) for n in names]
    rows = [o for o in rows if isinstance(o, dict) and o.get("id")]
    rows.sort(key=lambda o: (float(o.get("ts") or 0), str(o["id"])))
    return rows


def _archive(bot, ids, outcomes) -> None:
    """pending/ -> done/, each entry with what the run did with it: `outcome` = `{"op": "add" | "bump" | "confirm"
    | "nothing" | "gap" | "contradict" | "forget" | "failed", "at", "item"?, "new"?, "resolution"?, "gap"?}`
    (`gap` also on a `failed` entry: the open question it left; op `failed`: its change could not be made, with
    `error` and `tries`; `forget` with `by` on a `taught` entry: the forget entry that cancelled it)."""
    now = time.time()
    os.makedirs(_dir(bot, "done"), exist_ok=True)
    with store.lock(bot):
        for oid in ids:
            name = os.path.basename(str(oid)) + ".json"
            src = os.path.join(_dir(bot, "pending"), name)
            obs = store.read_json(src, None)
            if not isinstance(obs, dict):
                continue
            obs["outcome"] = {**(outcomes.get(oid) or {"op": "nothing"}), "at": now}
            try:
                store.write_json(os.path.join(_dir(bot, "done"), name), obs)
                os.remove(src)
            except OSError:
                logger.warning("expert %s: inbox entry %s not archived", bot.id, oid, exc_info=True)


def _retries_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), RETRIES_FILE)


def _settle(bot, entries, outcomes, failed):
    """Archive the handled `entries`. One whose change could not be made (`failed`: entry id -> the error) stays
    waiting for the next run instead, at most `MAX_TRIES` times; then it is archived as `failed` with one
    `note skipped`, so a bad entry cannot make every run fail on it forever."""
    data = store.read_json(_retries_path(bot), {})
    still = {o["id"] for o in waiting(bot)}
    tries = {k: v for k, v in (data if isinstance(data, dict) else {}).items() if k in still and isinstance(v, int)}
    done = []
    for o in entries:
        oid = o["id"]
        if oid in failed:
            n = tries.get(oid, 0) + 1
            if n < MAX_TRIES:
                tries[oid] = n
                continue
            outcomes[oid] = {"op": "failed", "error": str(failed[oid])[:300], "tries": n}
            yield {"op": "note", "action": "skipped", "evidence": {"item": oid},
                   "summary": f"Gave up on “{' '.join(str(o.get('claim') or '').split())[:200]}” after {n} tries.",
                   "why": (str(failed[oid]) or "The change could not be made.")[:300]}
        tries.pop(oid, None)
        done.append(oid)
    _archive(bot, done, outcomes)
    if tries or data:
        store.write_json(_retries_path(bot), tries)


# ------------------------------------------------------------------ evidence rules ---
def verified(o) -> bool:
    """A statement proved to be the user's: `by_user` true with the quote it was checked against."""
    return o.get("by_user") is True and bool(str(o.get("quote") or "").strip())


def echo(o) -> bool:
    """The expert's own advice echoed back: `advice_confirmed`, or a statement made only after its advice."""
    return o.get("kind") == "advice_confirmed" or bool(o.get("after_advice"))


def counts(o) -> bool:
    """Whether entry `o` is independent evidence that raises a lesson's `count` (once per session): an
    `error_then_fix`, a `worked` (not `after_advice`) or a `failed`, or a grounded `repeated_request` / `user_correction` (or a user-stated `advice_contradicted`)
    that is not `after_advice`. Never an `advice_confirmed`. An entry stored before grounding existed (no
    `by_user` key) keeps the old rule (anything but an echo counts)."""
    if echo(o):
        return False
    if o.get("kind") in OUTCOME_KINDS or "by_user" not in o:
        return True
    return verified(o)


def same_kind(o, lesson_kind) -> bool:
    """Whether entry `o` may be counted toward a lesson of kind `lesson_kind`: a request pattern, what worked and
    what failed only toward their own kind, and nothing else toward a "does not work" lesson."""
    kind = o.get("kind")
    if kind in OWN_KIND:
        return lesson_kind == kind
    return lesson_kind != "failed"


def correction(o) -> bool:
    """A verified user correction: takes effect at once (trust `user`)."""
    return verified(o) and o.get("kind") in ("user_correction", "advice_contradicted") and not o.get("after_advice")


def from_user(o) -> bool:
    """An entry that makes a run due at once: taught or forgotten in chat, or a verified correction."""
    return (o.get("kind") in USER_KINDS and o.get("by_user") is True) or correction(o)


def due(bot, force=False, now=None) -> bool:
    man = store.manifest(bot)
    if man.get("learning") is False:
        return False
    if force:
        return True
    rows = waiting(bot)
    if not rows:
        return False
    mem = man["memory"]
    now = time.time() if now is None else now
    return (len(rows) >= mem["consolidate_at"] or now - float(rows[0].get("ts") or now) >= mem["consolidate_after_s"]
            or any(from_user(o) or o.get("kind") == "failed" for o in rows))  # a failure is researched at once


# ------------------------------------------------------------------ the loop ---
def status(bot) -> dict:
    """`{"running": bool, "stage": "lessons" | "research" | "distill" | "tidy" | "", "waiting": int}`."""
    with _GUARD:
        stage = _RUNNING.get(str(bot.id))
    return {"running": stage is not None, "stage": stage or "", "waiting": len(waiting(bot))}


def _claim(bot) -> bool:
    with _GUARD:
        if str(bot.id) in _RUNNING:
            return False
        _RUNNING[str(bot.id)] = ""
        return True


def _release(bot) -> None:
    with _GUARD:
        _RUNNING.pop(str(bot.id), None)


def run(bot, force=False) -> dict:
    """One pass: `{"ran": bool, "reason"?: str, "lines"?: [activity line]}`. Not `ran` when nothing is due, the
    expert's learning is off, no engine is available, or a run is already in flight for this expert."""
    if not _claim(bot):
        return {"ran": False, "reason": "already running"}
    try:
        return _run(bot, force)
    finally:
        _release(bot)


def start(bot, force=False) -> bool:
    """`run` in a background thread. False when one is already running for this expert or nothing is due."""
    if not _claim(bot):
        return False
    try:
        if not due(bot, force):
            _release(bot)
            return False

        def go():
            try:
                _run(bot, force)
            except Exception:  # noqa: BLE001 — never out of a helper thread
                logger.warning("expert %s: learning failed", getattr(bot, "id", "?"), exc_info=True)
            finally:
                _release(bot)

        threading.Thread(target=go, daemon=True, name=f"expert-learn-{bot.id}").start()
        return True
    except Exception:
        _release(bot)
        raise


def _run(bot, force) -> dict:
    if not due(bot, force):
        return {"ran": False}
    ok, why = model.available(bot)
    if not ok:
        return {"ran": False, "reason": why or "no engine is available for this expert"}
    ctx = {"inbox": waiting(bot)}
    lines = []
    for name, step in (("lessons", lessons), ("research", gaps.step), ("distill", distill.step),
                       ("tidy", tidy.step)):
        with _GUARD:
            _RUNNING[str(bot.id)] = name
        gen, line = step(bot, ctx), None
        try:
            while True:
                line = knowledge.apply(bot, {"stage": name, **gen.send(line)})
                lines.append(line)
        except StopIteration:
            pass
        except Exception as e:  # noqa: BLE001 — one failed step never stops the others
            logger.warning("expert %s: the %s step failed", bot.id, name, exc_info=True)
            lines.append(knowledge.apply(bot, {
                "op": "note", "action": "skipped", "stage": name, "summary": f"The {name} step failed.",
                "why": f"{type(e).__name__}: {e}"[:300]}))
    return {"ran": True, "lines": lines}


# ------------------------------------------------------------------ step 1: lessons ---
def _failures_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), FAILURES_FILE)


def failures(bot) -> int:
    data = store.read_json(_failures_path(bot), {})
    try:
        return max(0, int((data or {}).get("count") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _set_failures(bot, n: int, error: str = "") -> None:
    if n or failures(bot):
        store.write_json(_failures_path(bot), {"count": int(n), "error": error[:300], "at": time.time()})


def resolve(bot, old, new_claim, quote) -> dict | None:
    """ONE model decision for a verified user correction of lesson `old`: `{"resolution": "replace"}` or
    `{"resolution": "narrow", "narrowed_claim": str}`; None when the call fails or the answer cannot be read (the
    old lesson is then only marked contradicted)."""
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
    narrowed = " ".join(ans["narrowed_claim"].split()) if isinstance(ans.get("narrowed_claim"), str) else ""
    if res == "narrow" and narrowed and memory.normalise(narrowed) != memory.normalise(new_claim):
        return {"resolution": "narrow", "narrowed_claim": narrowed}
    return None


def _source(o) -> dict | None:
    """Where a task's entry came from, as the activity line keeps it: `{"canvas", "target", "backend"}` (always
    all three; `canvas` is the canvas folder's name, "" when the task was not in one) plus `files` when the entry
    names the files that show it. None for an entry that names no task."""
    src = o.get("source")
    if not isinstance(src, dict) or not any(src.values()):
        return None
    out = {k: str(src.get(k) or "") for k in ("canvas", "target", "backend")}
    files = [str(f) for f in (src.get("files") or []) if isinstance(f, str) and f.strip()] \
        if isinstance(src.get("files"), list) else []
    if files:
        out["files"] = files
    return out


def question_for(o) -> str:
    """The open question a `failed` entry leaves for research: "What works for: <need>? Tried and failed:
    <tried>."; from the claim alone when capture named neither."""
    need, tried = (" ".join(str(o.get(k) or "").split()).rstrip(".?! ")[:QUESTION_PART_MAX] for k in ("need", "tried"))
    if need and tried:
        return f"What works for: {need}? Tried and failed: {tried}."
    claim = " ".join(str(o.get("claim") or "").split()).rstrip(".?! ")
    return f"What works instead, given this did not: {claim}?" if claim else ""


def _because(o) -> dict:
    """The `why` and `evidence` every change made for inbox entry `o` carries."""
    ev = {"session": o.get("session") or "", "quote": o.get("quote") or "", "source": _source(o), "item": o["id"]}
    why = "You said so in a session." if correction(o) else _WHY.get(o.get("kind"), "Seen in a session.")
    return {"why": why, "evidence": {k: v for k, v in ev.items() if v}}


def _live(bot, item_id):
    return next((i for i in memory.items(bot) if i.get("id") == item_id), None)


def _lesson_kind(o) -> str:
    # The corrected rule of a contradiction is a user correction when a USER line said so (capture's `by_user`).
    if o.get("kind") == "advice_contradicted" and o.get("by_user"):
        return "user_correction"
    return o.get("kind") or "lesson"


def lessons(bot, ctx):
    """Step 1 (module docstring). Yields lesson changes and notes; archives every entry it handled (`_settle`:
    an entry whose change failed stays waiting)."""
    pend = ctx["inbox"]
    outcomes: dict[str, dict] = {}  # inbox entry id -> what this run did with it
    failed_apply: dict[str, str] = {}  # inbox entry id -> why its change could not be made

    def applied(o, line) -> bool:
        if not line.get("ok"):
            failed_apply[o["id"]] = str(line.get("error") or "")
        return bool(line.get("ok"))

    # What the user said in chat: no model, and never held back by a failed model call below.
    said = [o for o in pend if o.get("kind") in USER_KINDS]
    texts = {o["id"]: " ".join(str(o.get("claim") or "").split()) for o in said}
    # A forget that names a lesson still waiting here (taught a moment before, not learned yet) cancels it.
    cancelled: dict[str, str] = {}  # a waiting `taught` entry id -> the later `forget` entry that names it
    for n, o in enumerate(said):
        if o["kind"] == "forget" and o.get("by_user") is True and texts[o["id"]]:
            for t in said[:n]:
                if t["kind"] == "taught" and t.get("by_user") is True and t["id"] not in cancelled \
                        and memory.text_matches(texts[o["id"]], texts[t["id"]]):
                    cancelled[t["id"]] = o["id"]
    for o in said:
        text = texts[o["id"]]
        if o.get("by_user") is not True or not text:
            continue  # only a message typed at the computer may teach or forget
        if o["id"] in cancelled:
            outcomes[o["id"]] = {"op": "forget", "by": cancelled[o["id"]]}
            yield {"op": "note", "action": "skipped", "summary": f"Did not learn “{text[:200]}”.",
                   "why": "You asked to forget it before it was learned.", "evidence": {"item": o["id"]}}
            continue
        if o["kind"] == "taught":
            line = yield {"op": "lesson_add", "claim": text, "kind": "lesson", "trust": "user", **_because(o)}
            it = memory.find(bot, text) if applied(o, line) else None
            if it:
                outcomes[o["id"]] = {"op": "add", "item": it["id"]}
            continue
        rows = memory.match(bot, text)
        outcomes[o["id"]] = {"op": "forget"}
        if not rows and o["id"] not in cancelled.values():
            yield {"op": "note", "action": "skipped", "summary": f"No lesson matches “{text[:200]}”.",
                   "why": "You asked to forget it in chat.", "evidence": {"item": o["id"]}}
        for i in rows:
            applied(o, (yield {"op": "lesson_remove", "id": i["id"], "reason": "forgotten",
                               "why": "You asked to forget this in chat.", "evidence": {"item": o["id"]}}))
    yield from _settle(bot, said, outcomes, failed_apply)

    gap_obs = [o for o in pend if o not in said and (o.get("trust") == "gap" or o.get("kind") == "gap")]
    real = [o for o in pend if o not in said and o not in gap_obs]
    if not real and not gap_obs:
        return
    live = memory.items(bot)
    item_kinds = {i["id"]: i.get("kind") for i in live}
    item_claims = {i["id"]: memory.normalise(i.get("claim")) for i in live}
    # A rule already written into a skill (archived as promoted) may be contradicted too: its skill then follows.
    in_skills = {r["id"]: r for r in memory.promoted(bot) if r["id"] not in item_kinds}
    for rid, rec in in_skills.items():
        item_claims[rid] = memory.normalise(rec.get("claim"))
    contradicting = [o for o in real if str(o.get("contradicts") or "") in item_claims]
    contra_items = {str(o["contradicts"]) for o in contradicting}
    # An entry that `confirms` an existing lesson is credited to it with no model decision (a request pattern
    # confirms only a request pattern, what worked / failed only its own kind: the guard of a model "bump" below).
    confirmed = [o for o in real if o not in contradicting and str(o.get("confirms") or "") in item_kinds
                 and same_kind(o, item_kinds[o["confirms"]])]
    failed = [o for o in real if o.get("kind") == "failed"]
    real = [o for o in real if o not in confirmed]
    open_gaps = gaps.open_gaps(bot) if gap_obs else []
    offer = gap_obs if open_gaps else []  # gap entries reach the model only to be grouped with an open gap

    def add(o, claim=None):
        """The entry's claim as a lesson (or the equal existing one, credited). False when nothing was stored."""
        claim = claim if claim is not None else o.get("claim")
        if memory.normalise(claim) in {item_claims[c] for c in contra_items}:
            # The text of a lesson this batch contradicts (the entry's own "corrected rule", or any other entry's
            # claim): adding it would find that lesson and raise its count. Never a bump of it.
            return True
        if not str(claim or "").strip():
            return False
        before = {i["id"] for i in memory.items(bot)}
        user = correction(o)  # a verified user correction takes effect at once, like chat teaching
        line = yield {"op": "lesson_add", "claim": claim, "kind": _lesson_kind(o),
                      "trust": "user" if user else (o.get("trust") or "outcome"), "via": "session" if user else "",
                      "session": o.get("session") or "", "about": o.get("about") or [], "counted": counts(o),
                      **_because(o)}
        it = memory.find(bot, claim) if applied(o, line) else None
        if it is None:
            return False
        op = "add" if it["id"] not in before else "bump" if counts(o) else "confirm"
        outcomes[o["id"]] = {"op": op, "item": it["id"]}
        return True

    def credit(o, item_id):
        """Entry `o` seen again as lesson `item_id`: independent evidence raises its count (once per session), an
        echo or an ungrounded statement is only noted, and a verified user correction also raises the lesson to
        trust `user` (so it is used at once). False when the lesson is gone."""
        target = _live(bot, item_id)
        if target is None:
            return False
        ref = {"id": item_id, "session": o.get("session") or "", **_because(o)}
        first = memory.find(bot, target.get("claim"))
        if correction(o) and first is not None and first["id"] == item_id:
            line = yield {"op": "lesson_add", "claim": target.get("claim") or "", "kind": target.get("kind") or "lesson",
                          "trust": "user", "via": "session", "session": ref["session"], "counted": counts(o),
                          **_because(o)}
            op = "bump"
        elif counts(o) or correction(o):
            line, op = (yield {"op": "lesson_bump", **ref}), "bump"
        else:
            line, op = (yield {"op": "lesson_confirm", **ref}), "confirm"
        if not applied(o, line):
            return False
        outcomes[o["id"]] = {"op": op, "item": item_id}
        return True

    edits, fallback = None, False
    if real or offer:
        items_txt = "\n".join(f"- [{i['id']}] ({i.get('kind', 'lesson')}) {i.get('claim', '')} "
                              f"(seen {i.get('count', 0)}×)" for i in memory.items(bot)) or "(none yet)"
        obs_txt = "\n".join(f"- [{o['id']}] ({o.get('kind', '')}, trust {o.get('trust', '')}) {o.get('claim', '')}"
                            for o in real + offer)
        prompt = PROMPT.format(items=items_txt, obs=obs_txt)
        if offer:
            prompt += GAP_SECTION.format(gaps="\n".join(
                f"- [{g['id']}] {g['question']} (asked {g.get('count', 0)}×)" for g in open_gaps))
        error = ""
        try:
            edits = parse_json(model.text(bot, prompt, system="You answer with JSON only."), list)
            if edits is None:
                error = "the model's answer could not be read"
        except Exception as e:  # noqa: BLE001 — counted below; nothing is added on a failed call
            error = f"the model call failed: {e}"
        if edits is None and real:
            n = failures(bot) + 1
            if n < FALLBACK_AFTER:
                _set_failures(bot, n, error)
                yield {"op": "note", "action": "skipped", "why": error[:300],
                       "summary": f"{len(pend) - len(said)} things stay waiting (try {n} of {FALLBACK_AFTER})."}
                return
            # Consecutive failures: exact-match adds, so the queue cannot grow forever.
            for o in real:
                yield from add(o)
            fallback = True
    if real or not offer or edits is not None:
        _set_failures(bot, 0)

    by_id = {o["id"]: o for o in real}
    gap_ids = {o["id"] for o in offer}
    open_ids = {g["id"] for g in open_gaps}
    merges: dict[str, str] = {}
    handled: set[str] = set()
    same: dict[str, str] = {}  # an advice_contradicted entry id -> the entry that states its rule
    for e in (edits or []) if not fallback else []:
        if not isinstance(e, dict):
            continue
        oid = str(e.get("obs") or "")
        if oid in gap_ids:
            if str(e.get("op") or "").lower() == "gap" and str(e.get("gap") or "") in open_ids:
                merges.setdefault(oid, str(e["gap"]))
            continue  # never a lesson
        o = by_id.get(oid)
        if o is None or oid in handled:
            continue
        handled.add(oid)
        op = str(e.get("op") or "").lower()
        claim = e["claim"] if isinstance(e.get("claim"), str) and e["claim"].strip() else None
        if o.get("kind") == "advice_contradicted":
            if op == "same" and str(e.get("as") or "") in by_id and str(e.get("as")) != oid:
                same[oid] = str(e["as"])
                continue
            if op == "bump":  # a contradiction never raises confidence in anything: its rule is a lesson of its own
                op, claim = "add", None
        elif op == "bump" and str(e.get("item") or "") in contra_items:
            op, claim = "add", None  # never a bump of a lesson this batch contradicts
        if op == "bump" and not same_kind(o, (_live(bot, str(e.get("item") or "")) or {}).get("kind")):
            op, claim = "add", None  # a request / what worked / what failed is bumped only onto its own kind
        if op == "bump" and e.get("item"):
            yield from credit(o, str(e["item"]))
        elif op == "add":
            yield from add(o, claim)
    for oid, other in same.items():
        # The corrected rule another entry of this batch states: counted toward that lesson, not added again.
        target = (outcomes.get(other) or {}).get("item")
        if target and target not in contra_items:
            yield from credit(by_id[oid], target)

    decided: dict[tuple, dict | None] = {}  # (old, new) -> the resolution, one model call per pair
    for o in contradicting:
        old_id = str(o["contradicts"])
        new = (outcomes.get(o["id"]) or {}).get("item")
        partner = by_id.get(same.get(o["id"], ""))
        user = correction(o) or (partner is not None and correction(partner))
        quote = o.get("quote") if verified(o) else (partner or {}).get("quote")
        old = _live(bot, old_id)
        res = None
        if old is not None and user and new and new != old_id:
            key = (old_id, new)
            if key not in decided:
                d = resolve(bot, old, (_live(bot, new) or {}).get("claim") or o.get("claim"), quote)
                if d:
                    line = yield ({"op": "lesson_remove", "id": old_id, "reason": "corrected", "into": new}
                                  if d["resolution"] == "replace" else
                                  {"op": "lesson_edit", "id": old_id, "claim": d["narrowed_claim"]}) | _because(o)
                    d = d if line.get("ok") else None
                decided[key] = d
            res = decided[key]
        if old is not None and res is None:
            yield {"op": "lesson_contradict", "id": old_id, "session": o.get("session") or "", **_because(o)}
        oc = {"op": "contradict", "item": old_id}
        if new and new != old_id:
            oc["new"] = new
        if res:
            oc["resolution"] = res["resolution"]
        outcomes[o["id"]] = oc
        skill = (in_skills.get(old_id) or {}).get("skill")
        if skill and user and new:
            # The old rule lives in a skill: distill rewrites that section from the corrected lesson. Kept on disk
            # (one entry per lesson and skill) until that skill was updated: this entry is archived below.
            distill.remember_correction(bot, new, skill, in_skills[old_id].get("claim", ""), quote or "")
    for o in confirmed:
        if str(o["confirms"]) not in contra_items:  # never a bump of a lesson this batch contradicts
            yield from credit(o, str(o["confirms"]))

    # What failed -> an open question, so the research step looks for what does work (the gaps' own caps apply).
    for o in failed:
        q = question_for(o)
        if q and (outcomes.get(o["id"]) or {}).get("op") in ("add", "bump", "confirm", "contradict"):
            outcomes[o["id"]]["gap"] = gaps.record(bot, q, session=o.get("session") or "", origin="failed")["id"]

    # Unsure questions -> gap records (one count per distinct consult), grouped where the model said so.
    renamed: dict[str, str] = {}  # a gap id merged away this run -> the gap it went into
    for o in gap_obs:
        q = _GAP_PREFIX.sub("", str(o.get("claim") or "")).strip()
        if not q:
            continue
        g = gaps.record(bot, q, consult=o.get("consult") or "", session=o.get("session") or "")
        target = merges.get(o["id"])
        kept = gaps.merge(bot, g["id"], target) if target and target != g["id"] else None
        if kept is not None:
            renamed.update({gone: kept["id"] for gone in (g["id"], target) if gone != kept["id"]})
            g = kept
        outcomes[o["id"]] = {"op": "gap", "gap": g["id"]}
        yield {"op": "note", "action": "noticed", "quiet": True, "summary": f"Could not answer: {q[:300]}",
               "why": "A session asked this and I was not sure.", "evidence": _because(o)["evidence"]}
    for oc in outcomes.values():
        while oc.get("gap") in renamed:
            oc["gap"] = renamed[oc["gap"]]
    yield from _settle(bot, [o for o in pend if o not in said], outcomes, failed_apply)
