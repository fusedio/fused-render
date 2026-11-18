"""Learn, step 3: lessons into skills (docs/experts.md "distill").

A lesson has earned a skill when it was seen `propose_at` times, when tidy's budget says it is used a lot
(`tidy.promote_ready`), or when it is the user's verified correction of a rule a skill states (the lessons step
hands those over with `remember_correction`). ONE model call drafts section operations on one skill, or a new skill;
the step yields that as a `skill_edit` / `skill_create` change and, once `knowledge.apply` reports it applied,
a `lesson_remove` (reason `promoted`) for each lesson it carried. Applied at once: nothing waits for anyone. A
draft that cannot be used is one `note skipped` line; the lessons stay in memory and the next run tries again.

The model never returns a whole skill, only the sections it changes; `knowledge.py` applies them and leaves
every other section byte for byte. A correction must `replace` or `remove` the section that states the old rule
in the skill that states it (`rule_section`: when the old rule's text is found in exactly one section, that one).

Pending corrections are Learn's private state, `<expert>/corrections.json` = `{"items": [{"item", "skill", "old",
"quote", "tries"}]}`, one per (lesson id, skill). An entry leaves only once that skill's update succeeded, or
after `MAX_TRIES` failed tries (one `note skipped`); the corrected lesson leaves memory only when no entry for
it is left. A correction's lesson is never part of the ordinary draft of the same run.
"""
from __future__ import annotations

import os
import re

from fused_render.experts import knowledge, memory, model, store, tidy
from fused_render.experts.engine import parse_json

_OPS_HELP = """How to change a skill: you NEVER return the whole skill. You name its `## ` sections and give operations;
every section you do not name stays exactly as it is. Operations, applied in order:
{{"op": "add", "heading": "<new heading>", "section": "<markdown content of the new section>"}}
{{"op": "replace", "heading": "<an existing heading>", "section": "<the new FULL content of that section>",
  "new_heading": "<optional: a new heading for it>"}}
{{"op": "remove", "heading": "<an existing heading>"}}
Headings must be copied exactly from the skill. `section` is ONLY the markdown under the heading: it must NOT contain
name, title or trigger lines, the section's own heading, nor the "### name: …" header used in the listing.

Rules:
- Prefer changing the section that already covers the topic over adding a new one.
- When a lesson supersedes or contradicts existing text, replace or remove that text.
- Keep code blocks unless they are wrong.
- Return only the sections you change.

How to write a skill:
- Prefer a reference to a canvas file (its path, and what to look at in it) over copying the file's content.
- Write the rules in the user's own words.
- Keep the skill short: a few plain rules beat a long explanation.
- Use code only where code explains it best."""

DRAFT_PROMPT = """You maintain an expert's skills (playbooks). These lessons keep recurring and should become part of
a skill. PREFER changing an existing skill whose subject fits; create a new skill only when none fits.

Lessons:
{lessons}

{chosen}

Other skills (name | title | trigger | sections):
{skills}

""" + _OPS_HELP + """

Answer with ONLY a JSON object, one of:
{{"action": "update", "skill": "<existing skill name>", "ops": [...], "triggers_add": ["new trigger words"],
  "triggers_remove": ["trigger words that no longer fit"], "why": "<one short sentence>"}}
{{"action": "create", "title": "<skill title>", "trigger": "<comma-separated trigger words>",
  "body": "<the new skill's markdown body>", "why": "<one short sentence>"}}
`body` must NOT contain name, title or trigger lines either. `triggers_add` lists only trigger words the skill does
not have yet (it may be empty)."""

CORRECTION_NOTE = """THE USER CORRECTED A RULE THAT THE SKILL "{skill}" STATES. Old rule (now wrong): {old}
What the user said, verbatim: "{quote}"
The lesson above is the corrected rule. Change the skill "{skill}" (action "update"): `replace` the section that
states the old rule with corrected content, or `remove` it when the corrected rule makes it pointless. Do NOT only
add a section beside the old text: the old rule must not stay in the skill.

"""

_STOP = {"with", "from", "that", "this", "when", "into", "your", "their", "them", "then", "than", "have", "what",
         "about", "should", "must", "always", "never", "each", "only", "uses", "used", "using"}


CORRECTIONS_FILE = "corrections.json"
MAX_TRIES = 3


class Unfit(ValueError):
    """The draft cannot be used; the message says why."""


# ------------------------------------------------------------------ pending corrections ---
def _corr_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), CORRECTIONS_FILE)


def corrections(bot) -> list:
    """The skill corrections not written yet, oldest first: `{"item": lesson id, "skill", "old", "quote",
    "tries"}`."""
    data = store.read_json(_corr_path(bot), {})
    rows = data.get("items") if isinstance(data, dict) else None
    return [c for c in (rows if isinstance(rows, list) else [])
            if isinstance(c, dict) and c.get("item") and c.get("skill")]


def remember_correction(bot, item, skill, old="", quote="") -> None:
    """The lessons step's hand-over: lesson `item` corrects a rule the skill `skill` states (`old`). Kept until
    that skill was updated; one entry per (lesson, skill)."""
    with store.lock(bot):
        rows = corrections(bot)
        if any(c["item"] == item and c["skill"] == skill for c in rows):
            return
        rows.append({"item": str(item), "skill": str(skill), "old": str(old or ""), "quote": str(quote or ""),
                     "tries": 0})
        store.write_json(_corr_path(bot), {"items": rows})


def _settle(bot, corr, done) -> bool:
    """Correction `corr` was tried: `done` clears it; a failure is counted and, at `MAX_TRIES`, clears it too.
    True when it was given up."""
    with store.lock(bot):
        rows = corrections(bot)
        mine = next((c for c in rows if c["item"] == corr["item"] and c["skill"] == corr["skill"]), None)
        if mine is None:
            return False
        try:
            tries = int(mine.get("tries") or 0) + 1
        except (TypeError, ValueError):
            tries = 1
        mine["tries"] = tries
        gave_up = not done and tries >= MAX_TRIES
        if done or gave_up:
            rows.remove(mine)
        store.write_json(_corr_path(bot), {"items": rows})
        return gave_up


def _flat(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def rule_section(body, old):
    """The heading of the ONE section of a skill body that states the rule `old`: the section that contains its
    text (letters and digits compared), else the one sharing at least 4 in 5 of its words. None when no section
    or more than one does: the rule cannot be located with confidence."""
    secs = knowledge.parse_sections(knowledge.norm_body(body))[1]
    key = _flat(old)
    if not key:
        return None
    hits = [s for s in secs if key in _flat(s["text"])]
    if not hits:
        want = _stems(old)
        hits = [s for s in secs if len(want) >= 3 and len(want & _stems(s["text"])) * 5 >= len(want) * 4]
    return hits[0]["heading"] if len(hits) == 1 else None


def candidates(bot) -> list:
    """The lessons that earned a skill, most seen first: seen `propose_at` times, or used a lot while memory is
    above `tidy_at`. Never an unanswered question, never a lesson a session contradicted."""
    b = store.manifest(bot)["memory"]
    rows = [i for i in memory.items(bot) if i.get("trust") != "gap" and not memory.contradicted(i)]
    heavy = {i["id"] for i in tidy.promote_ready(rows, b)} if len(rows) > b["tidy_at"] else set()
    rows = [i for i in rows if int(i.get("count") or 0) >= b["propose_at"] or i["id"] in heavy]
    rows.sort(key=lambda i: -int(i.get("count") or 0))
    return rows


def _stems(text) -> set:
    return {w.lower()[:5] for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", str(text or ""))
            if w.lower() not in _STOP}


def _best_skill(skills, lessons):
    """The skill whose words overlap the lessons' most (title / trigger count most, then headings, then body);
    the only skill when there is just one; None when nothing overlaps."""
    if len(skills) == 1:
        return skills[0]
    want = _stems(" ".join(str(i.get("claim", "")) + " " + " ".join(str(a) for a in (i.get("about") or []))
                           for i in lessons))
    best, score = None, 0
    for s in skills:
        sc = (3 * len(want & _stems(f"{s.get('title', '')} {s.get('trigger', '')}"))
              + 2 * len(want & _stems(" ".join(knowledge.headings(s.get("body") or ""))))
              + len(want & _stems(s.get("body"))))
        if sc > score:
            best, score = s, sc
    return best


def _prompt(skills, chosen, lessons, correction) -> str:
    if chosen is None:
        shown = "No existing skill looks like a close fit (their sections are listed below)."
    else:
        shown = (f"The skill most likely to fit, in full:\nname: {chosen['name']}\ntitle: {chosen.get('title', '')}\n"
                 f"trigger: {chosen.get('trigger', '')}\n<<<BEGIN SKILL BODY>>>\n"
                 f"{knowledge.norm_body(chosen.get('body'))}\n<<<END SKILL BODY>>>")
    others = "\n".join(f"- {s['name']} | {s.get('title', '')} | {s.get('trigger', '')} | "
                       f"{'; '.join(knowledge.headings(s.get('body') or '')) or '(no sections)'}"
                       for s in skills if s is not chosen) or "(none)"
    out = DRAFT_PROMPT.format(
        lessons="\n".join(f"- [{i['id']}] {memory.shown(i)} (seen {i.get('count', 0)}×)" for i in lessons),
        chosen=shown, skills=others)
    if correction:
        out = CORRECTION_NOTE.format(skill=correction["skill"], old=" ".join(str(correction.get("old") or "").split()),
                                     quote=" ".join(str(correction.get("quote") or "").split())) + out
    return out


def _ask(bot, prompt):
    try:
        out = parse_json(model.text(bot, prompt, system="You answer with JSON only."), dict)
    except Exception:  # noqa: BLE001 — no draft
        return None
    return out if isinstance(out, dict) else None


def draft(bot, lessons, correction=None) -> dict:
    """The `skill_edit` / `skill_create` change for `lessons`. One model call, plus one more when it chose a skill
    whose body it was not shown. Raises `Unfit` when the answer cannot be used. `correction` (`{"skill", "old",
    "quote"}`): the user corrected a rule that skill states; anything but a replace / remove in that skill is
    `Unfit`. With no readable answer (and no correction) the lessons become a new skill, one bullet each."""
    skills = list(bot.skills())
    by_name = {s["name"]: s for s in skills}
    fix = (correction or {}).get("skill")
    if fix and fix not in by_name:
        raise Unfit(f"the skill “{fix}” no longer exists")
    chosen = by_name[fix] if fix else _best_skill(skills, lessons)
    d = _ask(bot, _prompt(skills, chosen, lessons, correction))
    name = d.get("skill") if d else None
    if d and d.get("action") == "update" and name in by_name and by_name[name] is not chosen:
        # It chose a skill whose body it was not shown: ask once more, showing that one in full.
        d = _ask(bot, _prompt(skills, by_name[name], lessons, correction))
        name = d.get("skill") if d else None
    if d is None:
        if fix:
            raise Unfit("the draft could not be read")
        first = " ".join(str(lessons[0].get("claim") or "").split())
        about = [str(a) for i in lessons for a in (i.get("about") or [])]
        words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", first) if w.lower() not in _STOP]
        d = {"action": "create", "title": "Lessons: " + first, "trigger": (about + words)[:5] or ["lessons"],
             "body": "\n".join(f"- {memory.shown(i)}" for i in lessons)}
    why = " ".join(d["why"].split())[:300] if isinstance(d.get("why"), str) else ""
    if d.get("action") == "update" and name in by_name:
        ops = [o for o in (d.get("ops") if isinstance(d.get("ops"), list) else []) if isinstance(o, dict)]
        changed = {knowledge.heading_key(o.get("heading")) for o in ops
                   if str(o.get("op") or "").strip().lower() in ("replace", "remove")}
        if fix and (name != fix or not changed):
            # Only adding a rule would leave the corrected one standing beside it.
            raise Unfit(f"the draft did not replace or remove the section of “{fix}” that states the corrected rule")
        states = rule_section(by_name[fix].get("body"), correction.get("old")) if fix else None
        if states and knowledge.heading_key(states) not in changed:
            # It rewrote another section: the old rule would stay where it is.
            raise Unfit(f"the draft did not replace or remove “{states}”, the section of “{fix}” that states the "
                        "corrected rule")
        return {"op": "skill_edit", "skill": name, "ops": ops, "triggers_add": d.get("triggers_add") or [],
                "triggers_remove": d.get("triggers_remove") or [],
                "why": why or ("You corrected a rule this skill stated." if fix else "These lessons keep recurring.")}
    if fix:
        raise Unfit(f"the draft did not change the skill “{fix}” that states the corrected rule")
    if not str(d.get("body") or "").strip() or not str(d.get("title") or "").strip():
        raise Unfit("the draft named no existing skill and gave no new one")
    return {"op": "skill_create", "title": d["title"], "trigger": d.get("trigger") or d.get("triggers") or "",
            "body": d["body"], "why": why or "These lessons keep recurring and no skill covered them."}


def _write(bot, lessons, correction=None):
    """Draft, apply (the caller's `yield`), then take the lessons out of memory: the skill carries them now. With
    a pending `correction` the try is settled (`_settle`): cleared when the skill was updated, counted when not;
    its lesson stays in memory while another skill still waits for the same correction."""
    def failed():
        if correction and _settle(bot, correction, False):
            yield {"op": "note", "action": "skipped", "target": correction["skill"],
                   "summary": f"Gave up correcting the skill “{correction['skill']}” after {MAX_TRIES} tries.",
                   "why": "The corrected lesson stays in memory; the skill still states the old rule.",
                   "evidence": {"item": lessons[0]["id"]}}

    try:
        change = draft(bot, lessons, correction)
    except Unfit as e:
        yield {"op": "note", "action": "skipped", "why": str(e)[:300],
               "summary": f"Could not write {len(lessons)} lesson{'s' if len(lessons) != 1 else ''} into a skill."}
        yield from failed()
        return
    before = {s["name"] for s in bot.skills()}
    quote = (correction or {}).get("quote")
    line = yield {**change, "evidence": {"item": lessons[0]["id"], **({"quote": quote} if quote else {})}}
    if not line.get("ok"):
        yield from failed()
        return
    if correction:
        _settle(bot, correction, True)
    waits = {c["item"] for c in corrections(bot)}  # another skill still states the rule this lesson corrects
    skill = change.get("skill") or next((s["name"] for s in bot.skills() if s["name"] not in before), "")
    for i in lessons:
        if i["id"] in waits:
            continue
        yield {"op": "lesson_remove", "id": i["id"], "reason": "promoted", "skill": skill, "because": line["id"],
               "why": f"It is now part of the skill “{skill}”."}


def step(bot, ctx):
    """Step 3 of a Learn run: the pending corrections of skill rules first (one draft each), then every lesson
    that earned a skill as ONE draft. A correction's lesson is never part of that ordinary draft in the same run,
    failed or not: without the correction's constraint the draft could put it anywhere and leave the old rule."""
    pending = corrections(bot)
    held = {c["item"] for c in pending}
    for corr in pending:
        lesson = next((i for i in memory.items(bot) if i.get("id") == corr["item"]), None)
        if lesson is None:
            _settle(bot, corr, True)  # the lesson is gone (forgotten, undone): nothing to write any more
            continue
        yield from _write(bot, [lesson], corr)
    rows = [i for i in candidates(bot) if i["id"] not in held]
    if rows:
        yield from _write(bot, rows)
