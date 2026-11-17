"""Skill proposals (docs/experts.md §4 `proposals.py`).

Lessons seen often enough become a proposed skill change: one `model.text`
call drafts it (changing an existing skill is preferred) and the draft is
saved as "ready" for an approval card. Only `approve` writes a skill. A
proposal never runs the quality gate (`gate.py` is the Checks tab's, run by
the user); `score_before` / `score_after` stay on the record, always None.

A proposal EDITS a skill by section; it never rewrites one. A skill body is a
preamble (the text before the first `## ` heading) and sections (`## Heading`
+ its content up to the next `## ` heading or the end); `parse_sections` /
`serialise` split and rejoin it exactly. The model answers a list of section
operations on ONE skill (add / replace / remove / move, plus trigger words to
add or remove) and never returns the whole skill; code applies them
(`apply_ops`), strictly: an operation naming no existing section is dropped
and recorded, and every section no operation names stays byte for byte as it
was. Code also computes warnings from old vs new (a section removed, code
blocks lost, a large shrink, sections moved to a new skill) for the card.

Nothing is ever truncated: a result over `Bot.skill_save`'s limits gets one
retry asking the model to `move` sections out, then falls back to a separate
new skill for the added material. `approve` backs the skill(s) up, writes,
reads them back and restores every backup byte for byte when what was stored
is not what was intended.

`request` is the asynchronous entry point (chat, routes, scheduler): it saves
the record as "preparing" and marks its lessons `proposed` at once, then one
daemon thread runs `_draft` and turns it "ready" (and calls `cards.show`) or
"failed" with an `error` (its lessons are un-marked). One proposal is
prepared per expert at a time. `prepare` is the same work done synchronously.
`request_reorganise` asks for a change that makes a nearly full skill smaller
(tidy's skill pressure); it carries no lessons.

Statuses: preparing | ready | shown | approved | rejected | failed. A failed
approve (the skill could not be saved) is "failed" too, never re-shown.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import threading
import time

from fused_render.experts import memory, model, store
from fused_render.experts.consolidate import parse_json

logger = logging.getLogger(__name__)

STATUSES = ("preparing", "ready", "shown", "approved", "rejected", "failed")
OPEN_STATUSES = ("preparing", "ready", "shown")
BUSY = "a skill proposal is already being prepared"
#: A "preparing" record this process is not working on, older than this, was cut short (the app quit).
PREPARE_STALE_S = 3600
#: `Bot.skill_save` cuts the trigger line at this many characters. It is a literal inside that method (not an
#: importable constant), so it is mirrored here; `trigger_cap()` prefers `Bot.SKILL_TRIGGER_CAP` should one appear.
TRIGGER_CAP = 300
LEARNED_FILE = "learned-sections.json"
BACKUP_DIR = "skill-backups"
BACKUPS_KEPT = 20
DEFAULT_HEADING = "Learned lessons"
FULL_NOTE = "That skill is full, so this goes into a new skill"
NO_SECTION = "the draft named no section that exists"
#: A body that gets more than this much smaller is warned about on the card.
SHRINK_WARN = 0.25
OPS = ("add", "replace", "remove", "move")

_PREPARING: set[str] = set()
_PREPARING_GUARD = threading.Lock()

_OPS_HELP = """How to change a skill: you NEVER return the whole skill. You name its `## ` sections and give operations;
every section you do not name stays exactly as it is. Operations, applied in order:
{{"op": "add", "heading": "<new heading>", "section": "<markdown content of the new section>"}}
{{"op": "replace", "heading": "<an existing heading>", "section": "<the new FULL content of that section>",
  "new_heading": "<optional: a new heading for it>"}}
{{"op": "remove", "heading": "<an existing heading>"}}
{{"op": "move", "headings": ["<existing heading>", ...], "to": {{"title": "<new skill title>",
  "trigger": "<comma-separated trigger words>"}}}}   (takes those sections out, unchanged, into a new skill)
Headings must be copied exactly from the skill. `section` is ONLY the markdown under the heading: it must NOT contain
name, title or trigger lines, the section's own heading, nor the "### name: …" header used in the listing.

Rules:
- Prefer changing the section that already covers the topic over adding a new one.
- When a lesson supersedes or contradicts existing text, replace or remove that text.
- Merge overlapping sections (replace one with the merged content and remove the other).
- Keep code blocks unless they are wrong.
- Keep the skill focused and short.
- Return only the sections you change."""

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

REORGANISE_PROMPT = """You maintain an expert's skills (playbooks). The skill below is nearly full: its body is {size}
characters and the limit is {cap}. Change it so it is clearly smaller WITHOUT losing what matters: merge overlapping
sections, remove what is superseded or repeated, or `move` a coherent group of sections into a new skill.

{chosen}

Other skills (name | title | trigger | sections):
{skills}

""" + _OPS_HELP + """

Answer with ONLY a JSON object:
{{"action": "update", "skill": "{name}", "ops": [...], "triggers_add": [], "triggers_remove": [],
  "why": "<one short sentence>"}}"""

CORRECTION_NOTE = """THE USER CORRECTED A RULE THAT THE SKILL "{skill}" STATES. Old rule (now wrong): {old}
What the user said, verbatim: "{quote}"
The lesson above is the corrected rule. Change the skill "{skill}" (action "update"): `replace` the section that
states the old rule with corrected content, or `remove` it when the corrected rule makes it pointless. Do NOT only
add a section beside the old text: the old rule must not stay in the skill.

"""

RETRY_PROMPT = """The change you proposed to the skill "{name}" does not fit: {why}.
Nothing may be cut, so give operations, applied AFTER yours, that bring it under the limits: a `move` of a coherent
group of sections into a new skill (with its own title and trigger words), and/or `triggers_remove` for trigger
words that no longer fit.

The skill's sections after your change (heading: characters):
{sizes}
Its trigger line would be: {trigger}

Answer with ONLY a JSON object: {{"ops": [...], "triggers_remove": [...]}}
""" + _OPS_HELP

_META_LINE = re.compile(r"^(#+\s*)?(name|title|trigger)\s*:", re.I)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")


class Unfit(ValueError):
    """The material cannot be saved without cutting it, not even as a new skill (or the draft is unusable)."""


# ------------------------------------------------------------------ limits ---
def _bot_class():
    from fused_render.bots.bot import Bot
    return Bot


def body_cap() -> int:
    """The body limit `Bot.skill_save` enforces (`Bot.SKILL_BODY_CAP`)."""
    return int(_bot_class().SKILL_BODY_CAP)


def trigger_cap() -> int:
    """The trigger-line limit `Bot.skill_save` enforces."""
    return int(getattr(_bot_class(), "SKILL_TRIGGER_CAP", TRIGGER_CAP))


def skill_max() -> int:
    return int(_bot_class().SKILL_MAX)


# ------------------------------------------------------------- normalising ---
def norm_body(body) -> str:
    """A body as `skill_save` stores it and `Bot.skills()` reads it back (stripped, lines joined with "\n")."""
    return "\n".join(str(body or "").strip().splitlines()).strip()


def trigger_words(trigger) -> list:
    """The trigger phrases in `trigger` (a comma / newline separated string, or a list of them), in order."""
    parts = trigger if isinstance(trigger, (list, tuple)) else [trigger]
    out = []
    for part in parts:
        if isinstance(part, str):
            out += [t.strip() for t in re.split(r"[,\n]", part) if t.strip()]
    return out


def norm_trigger(trigger) -> str:
    """A trigger line as `skill_save` stores it ("a, b, c")."""
    return ", ".join(trigger_words(trigger))


def _dedupe(words, have=()) -> list:
    seen = {w.casefold() for w in have}
    out = []
    for w in words:
        if w.casefold() not in seen:
            seen.add(w.casefold())
            out.append(w)
    return out


# ---------------------------------------------------------------- sections ---
def _fence_step(fence, line):
    """One line of fence tracking: (the fence open after `line`, whether `line` is a `## ` heading)."""
    bare = line.rstrip("\n").rstrip("\r")
    m = _FENCE.match(bare)
    if fence is None:
        if m:
            return m.group(1), False
        return None, bare.startswith("## ")
    if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and \
            not bare.strip()[len(m.group(1)):].strip():
        return None, False
    return fence, False


def parse_sections(body) -> tuple[str, list]:
    """Split a skill body into its preamble (the text before the first `## ` heading) and its sections
    `[{"heading": str, "text": str}]`, where `text` is the section verbatim: its `## ` line and everything up to the
    next `## ` heading or the end. A `## ` line inside fenced code is not a heading.
    `serialise(*parse_sections(b)) == b` for every string `b`."""
    pre, secs, fence = [], [], None
    for line in _LINE.findall(str(body or "")):
        fence, is_heading = _fence_step(fence, line)
        if is_heading:
            secs.append({"heading": line.rstrip("\n").rstrip("\r")[3:].strip(), "text": line})
        elif secs:
            secs[-1]["text"] += line
        else:
            pre.append(line)
    return "".join(pre), secs


def serialise(preamble, sections) -> str:
    """The body `parse_sections` split: the preamble and each section's text, joined as they are."""
    return str(preamble or "") + "".join(s["text"] for s in sections)


def _join(preamble, sections) -> str:
    """Like `serialise`, but a piece that does not end with a newline (the old last section, followed now by another)
    gets a blank line after it. Pieces that already end with a newline are joined untouched."""
    out = str(preamble or "")
    for s in sections:
        if out and not out.endswith("\n"):
            out += "\n\n"
        out += s["text"]
    return out


def headings(body) -> list:
    """The `## ` headings of a skill body (outside fenced code), in order."""
    return [s["heading"] for s in parse_sections(body)[1]]


def fenced_blocks(body) -> int:
    """How many fenced code blocks `body` opens."""
    count, fence = 0, None
    for line in _LINE.findall(str(body or "")):
        was = fence
        fence, _h = _fence_step(fence, line)
        if was is None and fence is not None:
            count += 1
    return count


def _clean_body(body, title="") -> str:
    """`body` without the leading metadata lines a model copies from the skill listing ("### name: …",
    "title: …", "trigger: …"), without a leading "# <title>" heading equal to the title, and without leading or
    trailing blank lines."""
    lines = str(body or "").splitlines()
    want = " ".join(str(title or "").split()).casefold()
    while lines:
        head = lines[0].strip()
        if not head or _META_LINE.match(head):
            lines.pop(0)
            continue
        m = re.match(r"^#\s+(.*)$", head)
        if m and want and " ".join(m.group(1).split()).casefold() == want:
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def _clean_heading(heading) -> str:
    first = next((ln for ln in str(heading or "").splitlines() if ln.strip()), "")
    return " ".join(first.strip().lstrip("#").split())


def _hkey(heading) -> str:
    return _clean_heading(heading).casefold()


def _clean_section(section, heading, title="") -> str:
    """A section's markdown from the model: copied metadata lines, a copied "# <skill title>" and a repeated heading
    dropped, and its own `#` / `##` headings (outside fenced code) demoted to `###` so it stays one section."""
    lines = _clean_body(section, title).split("\n")
    want = heading.casefold()
    while lines and (not lines[0].strip() or (re.match(r"^#{1,6}\s", lines[0])
                                              and _clean_heading(lines[0]).casefold() == want)):
        lines = lines[1:]
    fence = None
    for n, line in enumerate(lines):
        was = fence
        fence, _h = _fence_step(fence, line)
        if was is None and fence is None and re.match(r"^#{1,2}\s", line):
            lines[n] = "### " + line.lstrip("#").strip()
    return norm_body("\n".join(lines))


def _section_text(heading_line, content) -> str:
    """A new section's text: its heading line, a blank line, the content, a blank line after."""
    head = heading_line if heading_line.endswith("\n") else heading_line + "\n"
    return f"{head}\n{content}\n\n"


# ---------------------------------------------------------------- ops ---
def norm_ops(raw) -> list:
    """The model's `ops` as dicts with a known `op` (anything else kept, to be dropped with a reason)."""
    return [o for o in (raw if isinstance(raw, list) else []) if isinstance(o, dict)]


def _find(sections, heading) -> tuple[list, str]:
    """The indexes of the sections whose heading matches `heading` (trimmed, case-insensitive), and why not one."""
    key = _hkey(heading)
    if not key:
        return [], "no heading was named"
    hits = [n for n, s in enumerate(sections) if _hkey(s["heading"]) == key]
    if not hits:
        return hits, f"no section “{_clean_heading(heading)}” in this skill"
    if len(hits) > 1:
        return hits, f"“{_clean_heading(heading)}” names {len(hits)} sections"
    return hits, ""


def apply_ops(body, ops, *, title="") -> dict:
    """Apply section `ops` to `body`, in order, deterministically and strictly. Returns
    `{"body", "applied": [ops as applied], "dropped": [{"op", "reason"}], "moved": {"headings", "title", "trigger",
    "body"} | None, "renamed": [old headings], "added": [{"heading", "section"}]}`.
    A replace / remove / move must name an existing section (one match, after trimming, case-insensitive), else it
    is dropped with a reason; an add whose heading exists replaces that section. Untouched sections and the preamble
    stay byte for byte as they were."""
    pre, secs = parse_sections(norm_body(body))
    applied, dropped, renamed, added = [], [], [], []
    moved = None

    def drop(op, reason):
        dropped.append({"op": op, "reason": reason})

    for op in norm_ops(ops):
        kind = str(op.get("op") or "").strip().lower()
        if kind not in OPS:
            drop(op, f"unknown operation “{op.get('op')}”")
            continue
        if kind == "move":
            names = op.get("headings")
            names = [h for h in names if isinstance(h, str)] if isinstance(names, list) else []
            to = op.get("to") if isinstance(op.get("to"), dict) else {}
            to_title = _short_title(_clean_heading(to.get("title")))
            if moved is not None:
                drop(op, "only one move per change")
                continue
            if not names:
                drop(op, "the move named no sections")
                continue
            if not to_title:
                drop(op, "the move gave the new skill no title")
                continue
            idx, why = [], ""
            for h in names:
                hits, why = _find(secs, h)
                if why:
                    break
                if hits[0] not in idx:
                    idx.append(hits[0])
            if why:
                drop(op, why)
                continue
            idx.sort()
            taken = [secs[n] for n in idx]
            secs = [s for n, s in enumerate(secs) if n not in idx]
            moved = {"headings": [s["heading"] for s in taken], "title": to_title,
                     "trigger": norm_trigger(trigger_words(to.get("trigger"))) or norm_trigger(
                         _dedupe([_clean_heading(s["heading"]) for s in taken])[:5]),
                     "body": norm_body(_join("", taken))}
            applied.append({"op": "move", "headings": moved["headings"],
                            "to": {"title": moved["title"], "trigger": moved["trigger"]}})
            continue
        heading = _clean_heading(op.get("heading"))
        hits, why = _find(secs, heading)
        if kind == "remove":
            if why:
                drop(op, why)
                continue
            gone = secs.pop(hits[0])
            applied.append({"op": "remove", "heading": gone["heading"]})
            continue
        # add / replace
        if kind == "add" and not heading:
            heading = DEFAULT_HEADING
            hits, why = _find(secs, heading)
        if kind == "add" and not hits:
            content = _clean_section(op.get("section"), heading, title)
            if not content:
                drop(op, "the new section has no content")
                continue
            secs.append({"heading": heading, "text": _section_text(f"## {heading}", content)})
            applied.append({"op": "add", "heading": heading, "section": content})
            added.append({"heading": heading, "section": content})
            continue
        if why:  # a replace naming nothing, or an add / replace naming several sections
            drop(op, why)
            continue
        old = secs[hits[0]]
        new_heading = _clean_heading(op.get("new_heading")) if kind == "replace" else ""
        if new_heading and _hkey(new_heading) != _hkey(old["heading"]) and _find(secs, new_heading)[0]:
            drop(op, f"a section “{new_heading}” already exists")
            continue
        shown_heading = new_heading or old["heading"]
        content = _clean_section(op.get("section"), shown_heading, title)
        if not content:
            drop(op, "the section's new content is empty (use remove to drop a section)")
            continue
        first = _LINE.findall(old["text"])[0]
        line = f"## {new_heading}" if new_heading and new_heading != old["heading"] else first
        secs[hits[0]] = {"heading": shown_heading, "text": _section_text(line, content)}
        if new_heading and new_heading != old["heading"]:
            renamed.append(old["heading"])
        rec = {"op": "replace", "heading": old["heading"], "section": content}
        if new_heading and new_heading != old["heading"]:
            rec["new_heading"] = new_heading
        applied.append(rec)
        added.append({"heading": shown_heading, "section": content})
    return {"body": norm_body(_join(pre, secs)), "applied": applied, "dropped": dropped, "moved": moved,
            "renamed": renamed, "added": added}


def apply_triggers(trigger, add=(), remove=()) -> dict:
    """`{"trigger", "added", "removed", "dropped"}`: `add` appended (deduplicated case-insensitively), `remove`d
    words that exist taken out; a removal that would leave no trigger word at all is dropped."""
    words = trigger_words(trigger)
    added = _dedupe(trigger_words(list(add or [])), words)
    rm = {w.casefold() for w in trigger_words(list(remove or []))}
    removed = [w for w in words if w.casefold() in rm]
    kept = [w for w in words if w.casefold() not in rm] + added
    dropped = []
    if not kept:
        kept, removed = words + added, []
        dropped.append({"op": {"triggers_remove": sorted(rm)}, "reason": "it would leave the skill no trigger words"})
    return {"trigger": norm_trigger(kept), "added": added, "removed": removed, "dropped": dropped}


def warnings_for(old_body, new_body, *, moved=None, renamed=()) -> list:
    """The card's warnings for a change from `old_body` to `new_body` (code, not the model): a section removed, code
    blocks lost, the body more than `SHRINK_WARN` smaller, sections moved to a new skill. None for a pure addition."""
    old, new = norm_body(old_body), norm_body(new_body)
    out = []
    moved_keys = {_hkey(h) for h in ((moved or {}).get("headings") or [])}
    skip = moved_keys | {_hkey(h) for h in renamed}
    have = {_hkey(h) for h in headings(new)}
    for h in headings(old):
        if _hkey(h) not in have and _hkey(h) not in skip:
            out.append(f"removes the section “{h}”")
    lost = fenced_blocks(old) - fenced_blocks(new) - fenced_blocks((moved or {}).get("body") or "")
    if lost > 0:
        out.append(f"removes {lost} code block{'s' if lost != 1 else ''}")
    if old and (len(old) - len(new)) / len(old) > SHRINK_WARN:
        out.append(f"shrinks this skill by {round(100 * (len(old) - len(new)) / len(old))}%")
    if moved:
        k = len(moved.get("headings") or [])
        out.append(f"moves {k} section{'s' if k != 1 else ''} into a new skill “{moved.get('title', '')}”")
    return out


# ------------------------------------------------------------------ storage ---
def _dir(bot) -> str:
    p = os.path.join(store.expert_dir(bot), "proposals")
    os.makedirs(p, exist_ok=True)
    return p


def _path(bot, pid) -> str:
    return os.path.join(_dir(bot), os.path.basename(str(pid or "")) + ".json")


def _save(bot, p) -> None:
    store.write_json(_path(bot, p["id"]), p)


def get(bot, pid):
    if not pid:
        return None
    p = store.read_json(_path(bot, pid), None)
    return p if isinstance(p, dict) and p.get("id") else None


def listing(bot) -> list:
    out = []
    try:
        names = os.listdir(_dir(bot))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json"):
            p = store.read_json(os.path.join(_dir(bot), n), None)
            if isinstance(p, dict) and p.get("id"):
                p.setdefault("error", "")
                out.append(p)
    out.sort(key=lambda p: float(p.get("ts") or 0), reverse=True)
    return out


def open_item_ids(bot) -> set:
    """The lesson ids inside any open proposal (preparing, ready or shown)."""
    return {iid for p in listing(bot) if p.get("status") in OPEN_STATUSES for iid in (p.get("items") or [])}


def any_open(bot) -> bool:
    """Whether any proposal is preparing, ready or shown."""
    return any(p.get("status") in OPEN_STATUSES for p in listing(bot))


def candidates(bot) -> list:
    at = store.manifest(bot)["memory"]["propose_at"]
    busy = open_item_ids(bot)
    from fused_render.experts import gaps
    closed = gaps.closed_ids(bot)  # a research lesson whose gap was dismissed or answered is not proposed again
    rows = [i for i in memory.items(bot)
            if (int(i.get("count") or 0) >= at or i.get("trust") == "research") and i.get("trust") != "gap"
            and not (i.get("trust") == "research" and i.get("gap") in closed) and i.get("id") not in busy
            and not i.get("proposed") and not i.get("promoted") and not i.get("rejected")
            and not memory.contradicted(i)]  # a session showed it wrong: never written into a skill
    rows.sort(key=lambda i: -int(i.get("count") or 0))
    return rows


def propose_candidates(bot) -> list:
    """`candidates(bot)`, all of them, as ONE proposal started in the background (`request`). Returns the new
    proposal ids (none or one); none when one is already being prepared."""
    items = list(candidates(bot) or [])
    if not items:
        return []
    try:
        prop = request(bot, items)
    except ValueError as e:
        if str(e) == BUSY:
            return []
        raise
    return [prop["id"]] if isinstance(prop, dict) and prop.get("id") else []


def _slug(name) -> str:
    """The file name `Bot.skill_save` derives from `name`."""
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60] or "playbook"


def _short_title(text, limit=80) -> str:
    """`text` on one line; a long one ends at a word boundary with "…" (a title, never skill content)."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit - 1].rsplit(" ", 1)[0] or t[:limit - 1]
    return cut.rstrip(" ,;:-") + "…"


def _free_name(title, taken) -> str:
    """A skill file name for a NEW skill titled `title` that no existing skill uses."""
    base = _slug(title)
    name, k = base, 2
    while name in taken:
        name = f"{base[:55].rstrip('-')}-{k}"
        k += 1
    return name


_STOP = {"with", "from", "that", "this", "when", "into", "your", "their", "them", "then", "than", "have", "what",
         "about", "should", "must", "always", "never", "each", "only", "uses", "used", "using"}


def _fallback_trigger(lessons, text="") -> list:
    about = [a for i in lessons for a in (i.get("about") or []) if isinstance(a, str) and a.strip()]
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text + " " + " ".join(
        i.get("claim", "") for i in lessons)) if w.lower() not in _STOP]
    return _dedupe(trigger_words(about) + words)[:5] or ["lessons"]


def _fallback_draft(lessons) -> dict:
    first = lessons[0].get("claim", "") if lessons else ""
    return {"action": "create", "skill": None, "title": _short_title("Lessons: " + first),
            "trigger": norm_trigger(_fallback_trigger(lessons)),
            "body": "\n".join(f"- {i.get('claim', '')}" for i in lessons)}


def _resolve_items(bot, items) -> list:
    by_id = {i["id"]: i for i in memory.items(bot)}
    out = []
    for it in items or []:
        iid = it.get("id") if isinstance(it, dict) else str(it)
        if iid in by_id and by_id[iid] not in out:
            out.append(by_id[iid])
    return out


# --------------------------------------------------------- learned sections ---
def _learned_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), LEARNED_FILE)


def learned_sections(bot) -> list:
    """`[{"skill", "heading"}]`: the sections approved proposals added (`learned-sections.json`; a record only)."""
    rows = store.read_json(_learned_path(bot), [])
    return [{"skill": str(r["skill"]), "heading": str(r["heading"])} for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict) and r.get("skill") and r.get("heading")]


def is_learned(bot, skill, heading) -> bool:
    h = str(heading or "").casefold()
    return any(r["skill"] == skill and r["heading"].casefold() == h for r in learned_sections(bot))


def _add_learned(bot, skill, heading) -> None:
    if not is_learned(bot, skill, heading):
        store.write_json(_learned_path(bot), learned_sections(bot) + [{"skill": skill, "heading": heading}])


def forget_learned(bot, skill) -> int:
    """Drop `skill`'s entries from `learned-sections.json`; returns how many went."""
    rows = learned_sections(bot)
    keep = [r for r in rows if r["skill"] != skill]
    if len(keep) != len(rows):
        store.write_json(_learned_path(bot), keep)
    return len(rows) - len(keep)


# ------------------------------------------------------------------ backups ---
def _skill_file(bot, name) -> str:
    return os.path.join(bot.skills_dir, os.path.basename(str(name)) + ".md")


def _backup_dir(bot) -> str:
    return os.path.join(store.expert_dir(bot), BACKUP_DIR)


def backups(bot, name) -> list:
    """`name`'s backup files, oldest first (`<expert_dir>/skill-backups/<name>.<unix ts>.md`)."""
    d = _backup_dir(bot)
    try:
        files = os.listdir(d)
    except OSError:
        return []
    out = []
    for f in files:
        if f.startswith(name + ".") and f.endswith(".md"):
            try:
                out.append((float(f[len(name) + 1:-3]), os.path.join(d, f)))
            except ValueError:
                continue
    return [p for _ts, p in sorted(out)]


def backup_skill(bot, name) -> str | None:
    """Copy skill `name`'s file (byte for byte) to the backups, keeping the newest `BACKUPS_KEPT`. Returns the
    backup's path, or None when the skill has no file yet."""
    src = _skill_file(bot, name)
    try:
        with open(src, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    d = _backup_dir(bot)
    os.makedirs(d, exist_ok=True)
    ts = time.time()
    while os.path.exists(os.path.join(d, f"{name}.{ts:.6f}.md")):
        ts += 0.000001
    path = os.path.join(d, f"{name}.{ts:.6f}.md")
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    for old in backups(bot, name)[:-BACKUPS_KEPT]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def _restore(bot, name, backup) -> None:
    """Put skill `name`'s file back to `backup`'s bytes; no backup (a new skill): remove the file."""
    dst = _skill_file(bot, name)
    if backup is None:
        try:
            os.remove(dst)
        except FileNotFoundError:
            pass
        return
    with open(backup, "rb") as f:
        data = f.read()
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst + ".restore.tmp", "wb") as f:
        f.write(data)
    os.replace(dst + ".restore.tmp", dst)


# -------------------------------------------------------------------- plans ---
def _diff(old_body, new_body, old_trigger, new_trigger, name, created) -> str:
    old_lines = old_body.splitlines()
    new_lines = new_body.splitlines()
    if old_trigger != new_trigger:
        old_lines = ([f"trigger: {old_trigger}", ""] if not created else []) + old_lines
        new_lines = [f"trigger: {new_trigger}", ""] + new_lines
    return "".join(difflib.unified_diff(
        [ln + "\n" for ln in old_lines], [ln + "\n" for ln in new_lines],
        fromfile="/dev/null" if created else f"skills/{name}.md", tofile=f"skills/{name}.md"))


def _fits(plan) -> str:
    """"" when `plan` ({"body", "trigger"}) fits `skill_save`'s limits, else why not."""
    if len(plan["body"]) > body_cap():
        return f"the skill body would be {len(plan['body'])} characters (the limit is {body_cap()})"
    if len(plan["trigger"]) > trigger_cap():
        return f"the trigger line would be {len(plan['trigger'])} characters (the limit is {trigger_cap()})"
    if not plan["body"]:
        return "the skill would have no content"
    if not plan["trigger"]:
        return "the skill would have no trigger words"
    return ""


def _create_plan(bot, title, trigger, body, taken, lessons) -> dict:
    """A new, separate skill. Raises `Unfit` when even a new skill cannot hold it uncut."""
    title = _short_title(title) or _fallback_draft(lessons)["title"]
    trigger = norm_trigger(trigger) or norm_trigger(_fallback_trigger(lessons, title))
    body = norm_body(body)
    if len(taken) >= skill_max():
        raise Unfit(f"this expert already has {skill_max()} skills, so a new skill cannot be added")
    plan = {"action": "create", "skill": None, "name": _free_name(title, taken), "title": title,
            "trigger": trigger, "body": body, "ops": [], "dropped_ops": [], "triggers_added": [],
            "triggers_removed": [], "moved": None, "warnings": [], "_old_body": "", "_old_trigger": ""}
    why = _fits(plan)
    if why:
        raise Unfit(f"the material does not fit even a new skill: {why}")
    return plan


def _update(skill, ops, add, remove, taken) -> dict:
    """Apply `ops` and trigger changes to `skill` (a `bot.skills()` row): the plan, unchecked against the limits."""
    res = apply_ops(skill.get("body") or "", ops, title=skill.get("title") or "")
    old_trigger = norm_trigger(skill.get("trigger"))
    trig = apply_triggers(old_trigger, add, remove)
    moved = res["moved"]
    if moved is not None:
        moved = {**moved, "name": _free_name(moved["title"], set(taken) | {skill["name"]})}
    old_body = norm_body(skill.get("body"))
    return {"action": "update", "skill": skill["name"], "name": skill["name"],
            "title": skill.get("title") or skill["name"], "trigger": trig["trigger"], "body": res["body"],
            "ops": res["applied"], "dropped_ops": res["dropped"] + trig["dropped"],
            "triggers_added": trig["added"], "triggers_removed": trig["removed"], "moved": moved,
            "warnings": warnings_for(old_body, res["body"], moved=moved, renamed=res["renamed"]),
            "_added": res["added"], "_old_body": old_body, "_old_trigger": old_trigger}


def _plan_fits(plan, taken) -> str:
    why = _fits(plan)
    if why:
        return why
    moved = plan.get("moved")
    if moved:
        if len(taken) >= skill_max():
            return f"this expert already has {skill_max()} skills, so the moved sections have nowhere to go"
        why = _fits(moved)
        if why:
            return "the new skill for the moved sections: " + why
    return ""


def _section_listing(skills) -> str:
    rows = [f"- {s['name']} | {s.get('title', '')} | {s.get('trigger', '')} | "
            f"{'; '.join(headings(s.get('body') or '')) or '(no sections)'}" for s in skills]
    return "\n".join(rows) or "(none)"


def _chosen_block(skill) -> str:
    if skill is None:
        return "No existing skill looks like a close fit (their sections are listed below)."
    body = norm_body(skill.get("body"))
    return (f"The skill most likely to fit, in full ({len(body)} of {body_cap()} characters):\n"
            f"name: {skill['name']}\ntitle: {skill.get('title', '')}\ntrigger: {skill.get('trigger', '')}\n"
            f"<<<BEGIN SKILL BODY>>>\n{body}\n<<<END SKILL BODY>>>")


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
              + 2 * len(want & _stems(" ".join(headings(s.get("body") or ""))))
              + len(want & _stems(s.get("body"))))
        if sc > score:
            best, score = s, sc
    return best


def _ask(bot, prompt):
    try:
        out = parse_json(model.text(bot, prompt, system="You answer with JSON only."), dict)
    except Exception:  # noqa: BLE001
        return None
    return out if isinstance(out, dict) else None


def _answer_ops(draft) -> tuple[list, list, list]:
    """(ops, triggers_add, triggers_remove) from an update answer. The earlier answer shape (one `heading` +
    `section`, `triggers`) is read as one add."""
    ops = norm_ops(draft.get("ops"))
    if not ops and (draft.get("heading") or draft.get("section")):
        ops = [{"op": "add", "heading": draft.get("heading"), "section": draft.get("section")}]
    add = trigger_words(draft.get("triggers_add")) or trigger_words(draft.get("triggers"))
    return ops, add, trigger_words(draft.get("triggers_remove"))


def _sizes(body) -> str:
    pre, secs = parse_sections(body)
    rows = ([f"(text before the first section): {len(pre.strip())}"] if pre.strip() else []) + \
        [f"{s['heading']}: {len(s['text'].strip())}" for s in secs]
    return "\n".join(rows) or "(none)"


def _draft(bot, lessons, *, reorganise=None, correction=None) -> dict:
    """The draft for `lessons` (or, with `reorganise` = a skill name, a change that makes that skill smaller): the
    proposal's fields (no id, no status). One model call, plus at most one re-ask showing a different skill the
    model chose and one retry when the result is over the limits. Raises `Unfit` when the material cannot be saved
    uncut, or the draft changes nothing. `correction` (`{"skill","old","quote"}`): the user corrected a rule that
    skill states; the draft is shown that skill and told to replace or remove the old rule's text, and anything but
    an update of that skill is `Unfit`."""
    skills = list(bot.skills())
    by_name = {s["name"]: s for s in skills}
    claims_md = "\n".join(f"- {i.get('claim', '')}" for i in lessons)

    def prompt_for(chosen):
        others = [s for s in skills if chosen is None or s["name"] != chosen["name"]]
        if reorganise:
            body = norm_body(chosen.get("body"))
            return REORGANISE_PROMPT.format(size=len(body), cap=body_cap(), chosen=_chosen_block(chosen),
                                            skills=_section_listing(others), name=chosen["name"])
        lessons_txt = "\n".join(f"- [{i['id']}] {i.get('claim', '')} (seen {i.get('count', 0)}×)" for i in lessons)
        out = DRAFT_PROMPT.format(lessons=lessons_txt, chosen=_chosen_block(chosen), skills=_section_listing(others))
        if fix:
            out = CORRECTION_NOTE.format(skill=fix, old=" ".join(str(correction.get("old") or "").split()),
                                         quote=" ".join(str(correction.get("quote") or "").split())) + out
        return out

    fix = str((correction or {}).get("skill") or "") if isinstance(correction, dict) else ""
    fix = fix if fix in by_name else ""
    if reorganise:
        chosen = by_name.get(reorganise)
        if chosen is None:
            raise Unfit(f"the skill “{reorganise}” no longer exists")
    else:
        chosen = by_name[fix] if fix else _best_skill(skills, lessons)
    draft = _ask(bot, prompt_for(chosen))
    name = draft.get("skill") if isinstance(draft, dict) and isinstance(draft.get("skill"), str) else None
    action = str((draft or {}).get("action") or "").strip().lower()
    if reorganise:
        if draft is None or name not in (None, reorganise) or action not in ("", "update"):
            raise Unfit("the draft did not answer with changes to that skill")
        name, action = reorganise, "update"
    elif action == "update" and name in by_name and (chosen is None or name != chosen["name"]):
        # It chose a skill whose body it was not shown: ask once more, showing that one in full.
        again = _ask(bot, prompt_for(by_name[name]))
        if isinstance(again, dict) and str(again.get("action") or "").strip().lower() in ("update", "create"):
            draft = again
            name = draft.get("skill") if isinstance(draft.get("skill"), str) else None
            action = str(draft.get("action") or "").strip().lower()
    if fix and (not isinstance(draft, dict) or action != "update" or name != fix):
        raise Unfit(f"the draft did not change the skill “{fix}” that states the corrected rule")
    if not isinstance(draft, dict):
        draft, action, name = _fallback_draft(lessons), "create", None
    note = ""
    why_text = " ".join(str(draft.get("why") or "").split())[:300] if isinstance(draft.get("why"), str) else ""
    if action == "update" and name in by_name:
        if _slug(name) != name:
            raise Unfit(f"the skill file “{name}” cannot be saved under its own name")
        skill = by_name[name]
        ops, add, remove = _answer_ops(draft)
        plan = _update(skill, ops, add, remove, by_name)
        if not plan["ops"]:
            raise Unfit(NO_SECTION)
        if fix and not any(o["op"] in ("replace", "remove") for o in plan["ops"]):
            # Only adding a rule would leave the corrected one standing beside it.
            raise Unfit(f"the draft did not replace or remove the section of “{fix}” that states the corrected rule")
        why = _plan_fits(plan, by_name)
        if why:
            # One retry: ask for a move (and / or trigger removals) that brings it under the limits.
            extra = _ask(bot, RETRY_PROMPT.format(name=name, why=why, sizes=_sizes(plan["body"]),
                                                  trigger=plan["trigger"]))
            if isinstance(extra, dict):
                e_ops, e_add, e_remove = _answer_ops(extra)
                retry = _update(skill, ops + e_ops, add + e_add, remove + e_remove, by_name)
                if retry["ops"] and not _plan_fits(retry, by_name):
                    plan, why = retry, ""
        if why:
            if reorganise or fix or not plan["_added"]:
                # A correction must change the skill that states the old rule: never a separate skill beside it.
                raise Unfit(f"the change does not fit the skill: {why}")
            # Never cut: the added material goes into a separate new skill; the existing skill stays untouched.
            first = plan["_added"][0]["heading"]
            if len(plan["_added"]) == 1:
                body = plan["_added"][0]["section"]
            else:
                body = "\n\n".join(f"## {a['heading']}\n\n{a['section']}" for a in plan["_added"])
            words = _dedupe(plan["triggers_added"] or add) or _fallback_trigger(lessons, first)
            note = f"{FULL_NOTE} “{_short_title(first)}” instead."
            new = _create_plan(bot, first, words, body, by_name, lessons)
            new.update(overflow_from=name, overflow_title=skill.get("title") or name)
            plan = new
    else:
        title = (draft.get("title") if isinstance(draft.get("title"), str) else "") or \
            _clean_heading(draft.get("heading")) or (name or "")
        body = _clean_body(draft.get("body") or draft.get("section") or "", title) or claims_md
        trigger = draft.get("trigger") if draft.get("trigger") else draft.get("triggers")
        plan = _create_plan(bot, title, trigger_words(trigger), body, by_name, lessons)
    if plan["action"] == "update" and plan["body"] == plan["_old_body"] and plan["trigger"] == plan["_old_trigger"] \
            and not plan.get("moved"):
        raise Unfit("the draft changed nothing")
    diff = _diff(plan["_old_body"], plan["body"], plan["_old_trigger"], plan["trigger"], plan["name"],
                 plan["action"] == "create")
    if plan.get("moved"):
        m = plan["moved"]
        diff += _diff("", m["body"], "", m["trigger"], m["name"], True)
    out = {k: v for k, v in plan.items() if not k.startswith("_")}
    if fix:
        out.update(reason="correction")
    out.update(diff=diff, note=note, why=why_text, kind="reorganise" if reorganise else "lessons",
               base_body=plan["_old_body"], base_trigger=plan["_old_trigger"],
               items=[i["id"] for i in lessons], claims=[i.get("claim", "") for i in lessons],
               score_before=None, score_after=None)
    return out


def prepare(bot, items) -> dict:
    """`request`'s work, synchronously: draft, save as "ready", mark the lessons proposed. Material that cannot be
    saved uncut (`Unfit`) is saved as a "failed" record instead (its lessons stay un-marked)."""
    lessons = _resolve_items(bot, items)
    if not lessons:
        raise ValueError("no lessons to propose")
    base = {"id": store.new_id("p"), "ts": time.time(), "topic": "", "card_seq": None, "error": "",
            "items": [i["id"] for i in lessons], "claims": [i.get("claim", "") for i in lessons]}
    try:
        fields = _draft(bot, lessons)
    except Unfit as e:
        p = {**base, "status": "failed", "action": None, "skill": None, "title": "", "trigger": "", "body": "",
             "diff": "", "score_before": None, "score_after": None, "error": str(e), "decided": time.time()}
        with store.lock(bot):
            _save(bot, p)
        return p
    p = {**base, "status": "ready", **fields}
    with store.lock(bot):
        _save(bot, p)
        for i in lessons:
            memory.mark(bot, i["id"], proposed=True)
    return p


def _gap_ids(p) -> list:
    out = [g for g in ([(p or {}).get("gap")] + list((p or {}).get("gaps") or [])) if g]
    return list(dict.fromkeys(out))


def _gap_settled(bot, p, approved) -> None:
    """A proposal that carries research lessons settled: tell `gaps` (answered on approve, dismissed otherwise)."""
    for gid in _gap_ids(p):
        try:
            from fused_render.experts import gaps
            gaps.proposal_settled(bot, gid, p.get("id"), approved=approved)
        except Exception:  # noqa: BLE001 — the proposal's own outcome stands either way
            logger.warning("expert %s: gap %s not settled", getattr(bot, "id", "?"), gid, exc_info=True)


def _unmark(bot, item_ids) -> None:
    for iid in item_ids or []:
        try:
            memory.mark(bot, iid, proposed=False)
        except Exception:  # noqa: BLE001
            logger.warning("expert %s: lesson %s not un-marked", getattr(bot, "id", "?"), iid, exc_info=True)


def _fail(bot, pid, error) -> dict | None:
    """Set proposal `pid` "failed" with `error` and un-mark its lessons (they may be proposed again)."""
    with store.lock(bot):
        p = get(bot, pid)
        if p is None:
            return None
        p.update(status="failed", error=str(error or "failed")[:500], decided=time.time())
        _save(bot, p)
        _unmark(bot, p.get("items"))
        _gap_settled(bot, p, False)
        return p


def _preparing(bot) -> list:
    """The open "preparing" records; one that was cut short (not worked on here, and old) is failed first."""
    out = []
    now = time.time()
    for p in listing(bot):
        if p.get("status") != "preparing":
            continue
        with _PREPARING_GUARD:
            live = p["id"] in _PREPARING
        if not live and now - float(p.get("ts") or 0) > PREPARE_STALE_S:
            _fail(bot, p["id"], "interrupted: the app stopped while this proposal was being prepared")
            continue
        out.append(p)
    return out


def _start(bot, p) -> dict:
    with _PREPARING_GUARD:
        _PREPARING.add(p["id"])
    t = threading.Thread(target=_prepare_bg, args=(bot, p["id"]), daemon=True,
                         name=f"expert-proposal-{getattr(bot, 'id', '?')}")
    t.start()
    return dict(p)


def request(bot, items, *, topic="", gap="", correction=None) -> dict:
    """Start a proposal for `items` in the background and return its record at once (status "preparing"; its
    lessons are marked `proposed` now). `gap` (or the `gap` link of a `research` lesson among the items) records
    the gap the proposal fixes; those gaps turn "proposed". `correction` (`{"skill","old","quote"}`): the items are
    a user's correction of a rule skill `skill` states; the record gets `reason: "correction"` and `correction`, and
    the draft must replace or remove the old rule's text in that skill. ValueError when one is already being
    prepared for this expert, or no lesson is left to propose."""
    with store.lock(bot):
        if _preparing(bot):
            raise ValueError(BUSY)
        busy = open_item_ids(bot)
        lessons = [i for i in _resolve_items(bot, items) if i["id"] not in busy]
        if not lessons:
            raise ValueError("no lessons to propose")
        p = {"id": store.new_id("p"), "ts": time.time(), "status": "preparing", "kind": "lessons",
             "topic": str(topic or "")[:200], "action": None, "skill": None, "title": "", "trigger": "", "body": "",
             "diff": "", "items": [i["id"] for i in lessons], "claims": [i.get("claim", "") for i in lessons],
             "score_before": None, "score_after": None, "card_seq": None, "error": ""}
        if isinstance(correction, dict) and correction.get("skill"):
            p["reason"] = "correction"
            p["correction"] = {k: str(correction.get(k) or "")[:1000] for k in ("skill", "old", "quote")}
        linked = list(dict.fromkeys([str(gap)] * bool(gap) + [str(i["gap"]) for i in lessons if i.get("gap")]))
        if linked:
            p["gap"], p["gaps"] = linked[0], linked
        _save(bot, p)
        for i in lessons:
            memory.mark(bot, i["id"], proposed=True)
        with _PREPARING_GUARD:
            _PREPARING.add(p["id"])  # before the lock is released: never seen as a stale "preparing"
        if linked:
            from fused_render.experts import gaps
            for gid in linked:
                gaps.mark_proposed(bot, gid, p["id"])
    return _start(bot, p)


def request_reorganise(bot, skill) -> dict:
    """Start a "reorganise" proposal for skill `skill` (nearly full) in the background: no lessons, one draft call
    asking for ops that make it smaller. ValueError when any proposal is open (preparing, ready or shown) or the
    skill does not exist."""
    with store.lock(bot):
        _preparing(bot)  # fails a stale record first
        if any_open(bot):
            raise ValueError(BUSY)
        cur = next((s for s in bot.skills() if s.get("name") == skill), None)
        if cur is None:
            raise ValueError(f"no skill “{skill}”")
        p = {"id": store.new_id("p"), "ts": time.time(), "status": "preparing", "kind": "reorganise",
             "topic": "reorganise", "action": None, "skill": skill, "title": cur.get("title") or skill,
             "trigger": "", "body": "", "diff": "", "items": [], "claims": [], "score_before": None,
             "score_after": None, "card_seq": None, "error": ""}
        _save(bot, p)
        with _PREPARING_GUARD:
            _PREPARING.add(p["id"])
    return _start(bot, p)


def _prepare_bg(bot, pid) -> None:
    ready = None
    try:
        p = get(bot, pid)
        if p is None:
            return
        if p.get("kind") == "reorganise":
            fields = _draft(bot, [], reorganise=p.get("skill"))
        else:
            lessons = _resolve_items(bot, p.get("items"))
            if not lessons:
                raise ValueError("its lessons were forgotten")
            fields = (_draft(bot, lessons, correction=p["correction"]) if p.get("correction")
                      else _draft(bot, lessons))
        with store.lock(bot):
            p = get(bot, pid)
            if p is None or p.get("status") != "preparing":
                return
            p.update(fields, status="ready", error="")
            _save(bot, p)
            ready = p
    except Exception as e:  # noqa: BLE001 — a failed draft is a "failed" record, never a stuck "preparing"
        logger.warning("expert %s: proposal %s failed", getattr(bot, "id", "?"), pid, exc_info=True)
        _fail(bot, pid, str(e) or type(e).__name__)
    finally:
        with _PREPARING_GUARD:
            _PREPARING.discard(pid)
    if ready is not None:
        try:
            from fused_render.experts import cards
            cards.show(bot, ready)  # False (the bot is busy) is fine: cards.tick / tick_one shows it later
        except Exception:  # noqa: BLE001
            logger.warning("expert %s: proposal %s card not shown", getattr(bot, "id", "?"), pid, exc_info=True)


def _decide(bot, pid):
    p = get(bot, pid)
    if p is None:
        raise ValueError("no such proposal")
    if p.get("status") == "failed":
        raise ValueError("this proposal failed" + (f": {p['error']}" if p.get("error") else ""))
    if p.get("status") in ("approved", "rejected"):
        raise ValueError(f"this proposal was already {p['status']}")
    if p.get("status") == "preparing":
        raise ValueError("this proposal is still being prepared")
    return p


def _edited_since(base_body, body, ops) -> str:
    """The first heading `ops` replace, remove, move or add whose section differs between `base_body` (what the
    draft was made against) and `body` (the skill now), else "". Sections no op names may differ freely."""
    _pre, base = parse_sections(norm_body(base_body))
    _pre, now = parse_sections(norm_body(body))
    for op in norm_ops(ops):
        kind = str(op.get("op") or "").strip().lower()
        names = op.get("headings") if kind == "move" else [op.get("heading") or (DEFAULT_HEADING if kind == "add"
                                                                                  else "")]
        for h in names if isinstance(names, list) else []:
            key = _hkey(h) if isinstance(h, str) else ""
            if key and [s["text"].rstrip() for s in base if _hkey(s["heading"]) == key] != \
                    [s["text"].rstrip() for s in now if _hkey(s["heading"]) == key]:
                return _clean_heading(h)
    return ""


def _final(bot, p) -> list:
    """What `approve` writes for `p`, rebuilt against the skills as they are NOW (another approval may have changed
    them since the draft): `[{"name","title","trigger","body","added": [headings]}]`, the edited skill first and the
    new skill of a `move` second. ValueError when it cannot be written as drafted, or uncut."""
    skills = list(bot.skills())
    by_name = {s["name"]: s for s in skills}
    if p.get("action") == "update":
        ops = p.get("ops")
        if not isinstance(ops, list):
            if not p.get("heading") or not p.get("section"):
                # A record drafted before proposals became patches carries a whole rewritten body: never written.
                raise ValueError("this proposal rewrote the whole skill (an older draft) and is not applied; "
                                 "its lessons will be proposed again")
            ops = [{"op": "add", "heading": p["heading"], "section": p["section"]}]  # an append-only era record
        cur = by_name.get(p.get("skill"))
        if cur is None:
            raise ValueError(f"the skill “{p.get('skill')}” no longer exists")
        if isinstance(p.get("base_body"), str):
            stale = _edited_since(p["base_body"], cur.get("body") or "", ops)
            if stale:
                raise ValueError(f"the skill changed since this was drafted: its section “{stale}” was edited, so "
                                 "this change is not applied over it; ask for a new draft")
        plan = _update(cur, ops, p.get("triggers_added") or [], p.get("triggers_removed") or [], by_name)
        if plan["dropped_ops"] or len(plan["ops"]) != len(ops):
            raise ValueError("the skill changed since this was drafted: "
                             + "; ".join(d["reason"] for d in plan["dropped_ops"]))
        why = _plan_fits(plan, by_name)
        if why:
            raise ValueError(f"the skill changed since this was drafted and no longer has room: {why}")
        out = [{"name": cur["name"], "title": plan["title"], "trigger": plan["trigger"], "body": plan["body"],
                "added": [o["heading"] for o in plan["ops"] if o["op"] == "add"]}]
        m = plan["moved"]
        if m:
            out.append({"name": m["name"], "title": m["title"], "trigger": m["trigger"], "body": m["body"],
                        "added": []})
        return out
    name = p.get("name") if p.get("name") and p.get("name") not in by_name else _free_name(p.get("title"), by_name)
    plan = {"body": norm_body(p.get("body")), "trigger": norm_trigger(p.get("trigger"))}
    why = _fits(plan)
    if why:
        raise ValueError(why)
    if len(by_name) >= skill_max():
        raise ValueError(f"this expert already has {skill_max()} skills")
    return [{"name": name, "title": p.get("title") or name, "trigger": plan["trigger"], "body": plan["body"],
             "added": []}]


def _stored(bot, name):
    return next((s for s in bot.skills() if s.get("name") == name), None)


def approve(bot, pid) -> dict:
    """Write the skill (two for a `move`): back each file up, save, read each back and verify the stored body and
    trigger are exactly what was intended. On a failed save or a mismatch EVERY file is restored byte for byte from
    its backup (a new skill's file is removed), the proposal becomes "failed" (lessons un-marked) and ValueError
    says why; it is never shown again. Only after the verification passes do the lessons leave memory."""
    with store.lock(bot):
        p = _decide(bot, pid)
        try:
            writes = _final(bot, p)
        except ValueError as e:
            _fail(bot, pid, str(e))
            raise ValueError(str(e)) from e
        saved_backups = [(w["name"], backup_skill(bot, w["name"])) for w in writes]

        def restore_all():
            for name, backup in saved_backups:
                try:
                    _restore(bot, name, backup)
                except Exception:  # noqa: BLE001
                    logger.warning("expert %s: skill %s not restored", getattr(bot, "id", "?"), name, exc_info=True)

        mismatch = False
        try:
            for w in writes:
                saved = bot.skill_save(w["title"], w["trigger"], w["body"], name=w["name"])
                if saved != w["name"]:
                    logger.warning("expert %s: skill saved as %s, not %s", getattr(bot, "id", "?"), saved, w["name"])
                    mismatch = True
                    break
        except Exception as e:  # noqa: BLE001
            restore_all()
            reason = f"could not save the skill: {str(e) or type(e).__name__}"
            _fail(bot, pid, reason)
            raise ValueError(reason) from e
        if not mismatch:
            for w in writes:
                got = _stored(bot, w["name"])
                if got is None or got.get("body") != w["body"] or norm_trigger(got.get("trigger")) != w["trigger"]:
                    mismatch = True
                    break
        if mismatch:
            restore_all()
            reason = "the skill could not be saved intact; it was restored from its backup"
            _fail(bot, pid, reason)
            raise ValueError(reason)
        name = writes[0]["name"]
        for h in writes[0]["added"]:
            _add_learned(bot, name, h)
        p.update(status="approved", skill=name, decided=time.time())
        if len(writes) > 1:
            p["moved_to"] = writes[1]["name"]
        _save(bot, p)
        # The skill carries these lessons now (verified): they leave memory for the archive (reason "promoted").
        try:
            memory.remove(bot, p.get("items") or [], "promoted", skill=name)
        except Exception:  # noqa: BLE001 — the skill is written; a lesson left behind is swept by tidy
            logger.warning("expert %s: promoted lessons not archived", getattr(bot, "id", "?"), exc_info=True)
            for iid in p.get("items") or []:
                memory.mark(bot, iid, promoted=True)
        _gap_settled(bot, p, True)
        return p


def reject(bot, pid) -> dict:
    with store.lock(bot):
        p = _decide(bot, pid)
        p.update(status="rejected", decided=time.time())
        _save(bot, p)
        for iid in p.get("items") or []:
            memory.mark(bot, iid, rejected=True)
        _gap_settled(bot, p, False)
        return p


def mark_shown(bot, pid, card_seq=None):
    """Not in the contract's list: cards.py records that the card is up (status
    "shown", the approval event's seq). Returns the proposal or None."""
    with store.lock(bot):
        p = get(bot, pid)
        if p is None or p.get("status") not in ("ready", "shown"):
            return p
        p.update(status="shown", card_seq=card_seq)
        _save(bot, p)
        return p


WARNING_PREFIX = "Warning: this change "


def card_text(proposal) -> tuple[str, str]:
    """(text, detail): what changes, the lessons, one "Warning: this change …" line per warning (for channels
    without the card UI), "Approve?"; detail is the unified diff."""
    p = proposal or {}
    n = len(p.get("items") or [])
    what = (f"update the skill “{p.get('title', '')}”" if p.get("action") == "update"
            else f"update the skill “{p.get('overflow_title') or p.get('overflow_from')}”" if p.get("overflow_from")
            else f"add a new skill “{p.get('title', '')}”")
    if p.get("kind") == "reorganise":
        size = len(p.get("base_body") or "")
        text = (f"The skill “{p.get('title', '')}” is nearly full ({size} of {body_cap()} characters), so I'd like "
                f"to reorganise it" + (f": {p['why']}" if p.get("why") else "."))
    elif p.get("reason") == "correction":
        text = "You corrected something a skill says. I'd like to change it"
    elif p.get("gap"):
        text = (f"Sessions asked me something I could not answer, so I looked into it and found the answer. "
                f"I'd like to {what} with it")
    else:
        text = f"I'd like to {what} from {n} lesson{'s' if n != 1 else ''} I keep seeing"
    if p.get("overflow_from"):
        text += f". {FULL_NOTE} “{p.get('title', '')}” instead"
    claims = [c for c in (p.get("claims") or []) if c]
    if claims:
        text += ":\n" + "\n".join(f"- {c}" for c in claims[:5])
        if len(claims) > 5:
            text += f"\n- …and {len(claims) - 5} more"
    elif p.get("kind") != "reorganise":
        text += "."
    for w in p.get("warnings") or []:
        text += f"\n{WARNING_PREFIX}{w}."
    text += "\nApprove?"
    return text, p.get("diff") or ""
