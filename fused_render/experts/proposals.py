"""Skill proposals (docs/experts.md §4 `proposals.py`).

Lessons seen often enough become a proposed skill change: one `model.text`
call drafts it (adding to an existing skill is preferred) and the draft is
saved as "ready" for an approval card. Only `approve` writes a skill. A
proposal never runs the quality gate (`gate.py` is the Checks tab's, run by
the user); `score_before` / `score_after` stay on the record, always None.

A proposal PATCHES a skill; it never rewrites one. For an update the model
writes only the new material (`heading`, `section`, extra `triggers`); code
builds the new body deterministically: the existing body unchanged with
`\n\n## <heading>\n\n<section>` appended (or, when an earlier proposal
added a section with that heading, that section's content replaced), and the
new trigger words appended to the existing trigger list. Nothing is ever
truncated: a patch that would not fit the limits `Bot.skill_save` enforces
becomes a separate new skill, and one that does not fit even there fails.
`approve` backs the skill file up, writes, reads the skill back and restores
the backup byte for byte when what was stored is not what was intended.

`request` is the asynchronous entry point (chat, routes, scheduler): it saves
the record as "preparing" and marks its lessons `proposed` at once, then one
daemon thread runs `_draft` (the draft) and turns it
"ready" (and calls `cards.show`) or "failed" with an `error` (its lessons are
un-marked). One proposal is prepared per expert at a time. `prepare` is the
same work done synchronously.

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
#: How much of each existing skill's body the draft prompt shows (for choosing where material belongs only).
EXCERPT_CHARS = 600
LEARNED_FILE = "learned-sections.json"
BACKUP_DIR = "skill-backups"
BACKUPS_KEPT = 20
DEFAULT_HEADING = "Learned lessons"
FULL_NOTE = "That skill is full, so this goes into a new skill"

_PREPARING: set[str] = set()
_PREPARING_GUARD = threading.Lock()

DRAFT_PROMPT = """You maintain an expert's skills (playbooks). These lessons keep recurring and should become part of
a skill. PREFER adding them to an existing skill whose subject fits; create a new skill only when none fits.

Lessons:
{lessons}

Existing skills (shown ONLY so you can choose where the lessons belong; never copy or rewrite them):
{skills}

You never rewrite an existing skill. To add to one, write ONLY the new material as one new section: it is appended
to the end of that skill and everything already in the skill stays exactly as it is.

Answer with ONLY a JSON object, one of:
{{"action": "update", "skill": "<existing skill name>", "heading": "<short section heading>",
  "section": "<markdown for the new section only>", "triggers": ["extra trigger words", ...]}}
{{"action": "create", "title": "<skill title>", "trigger": "<comma-separated trigger words>",
  "body": "<the new skill's markdown body>"}}

`section` and `body` are ONLY markdown content (steps and advice). They must NOT contain name, title or trigger
lines, the section heading, nor the "### name: …" header used in the listing above: those go in their own JSON
fields. `triggers` lists only NEW trigger words the skill does not have yet (it may be empty)."""

_META_LINE = re.compile(r"^(#+\s*)?(name|title|trigger)\s*:", re.I)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class Unfit(ValueError):
    """The material cannot be saved without cutting it, not even as a new skill."""


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


def _sections(body) -> list:
    """The `## ` headings of `body` outside fenced code: [(line index, heading text)]."""
    out, fence = [], None
    for n, line in enumerate(str(body or "").split("\n")):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
            elif line.startswith("## "):
                out.append((n, line[3:].strip()))
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and \
                not line.strip()[len(m.group(1)):].strip():
            fence = None
    return out


def headings(body) -> list:
    """The `## ` headings of a skill body (outside fenced code), in order."""
    return [h for _n, h in _sections(body)]


def fenced_blocks(body) -> int:
    """How many fenced code blocks `body` opens."""
    count, fence = 0, None
    for line in str(body or "").split("\n"):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence, count = m.group(1), count + 1
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and \
                not line.strip()[len(m.group(1)):].strip():
            fence = None
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


def _clean_section(section, heading, title="") -> str:
    """The new section's markdown: copied metadata lines, a copied "# <skill title>" and a repeated heading
    dropped, and its own `#` / `##` headings (outside fenced code) demoted to `###` so the section stays one
    section of the skill."""
    lines = _clean_body(section, title).split("\n")
    want = heading.casefold()
    while lines and (not lines[0].strip() or (re.match(r"^#{1,6}\s", lines[0])
                                              and _clean_heading(lines[0]).casefold() == want)):
        lines = lines[1:]
    fence = None
    for n, line in enumerate(lines):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
            elif re.match(r"^#{1,2}\s", line):
                lines[n] = "### " + line.lstrip("#").strip()
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and \
                not line.strip()[len(m.group(1)):].strip():
            fence = None
    return norm_body("\n".join(lines))


def patch_body(old_body, heading, section, *, replace=False) -> str:
    """`old_body` with the section `## <heading>` + `section` appended at the end, or (`replace`, and a section
    with that heading exists) the LAST such section's content replaced. Nothing else in `old_body` changes."""
    old = norm_body(old_body)
    if replace:
        found = [(n, h) for n, h in _sections(old) if h.casefold() == heading.casefold()]
        if found:
            lines = old.split("\n")
            start = found[-1][0]
            nxt = [n for n, _h in _sections(old) if n > start]
            end = nxt[0] if nxt else len(lines)
            new = lines[:start] + [lines[start], ""] + section.split("\n")
            if end < len(lines):
                new += [""] + lines[end:]
            return norm_body("\n".join(new))
    return norm_body((old + "\n\n" if old else "") + f"## {heading}\n\n{section}")


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
    """`[{"skill", "heading"}]`: the sections approved proposals added (`learned-sections.json`)."""
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


def _update_plan(bot, old, heading, section, words) -> dict:
    """The patched skill: `old` (a `bot.skills()` row) + the section, the new trigger words appended."""
    name = old["name"]
    replaced = is_learned(bot, name, heading) and any(
        h.casefold() == heading.casefold() for h in headings(old.get("body") or ""))
    body = patch_body(old.get("body") or "", heading, section, replace=replaced)
    old_trigger = norm_trigger(old.get("trigger"))
    added = _dedupe(words, trigger_words(old_trigger))
    trigger = norm_trigger([old_trigger] + added)
    return {"action": "update", "skill": name, "name": name, "title": old.get("title") or name,
            "trigger": trigger, "body": body, "heading": heading, "section": section, "triggers_added": added,
            "replaced": replaced, "_old_body": norm_body(old.get("body")), "_old_trigger": old_trigger}


def _fits(plan) -> str:
    """"" when `plan` fits `skill_save`'s limits, else why not."""
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
            "trigger": trigger, "body": body, "heading": "", "section": "", "triggers_added": [],
            "replaced": False, "_old_body": "", "_old_trigger": ""}
    why = _fits(plan)
    if why:
        raise Unfit(f"the material does not fit even a new skill: {why}")
    return plan


def _skills_listing(skills) -> str:
    rows = []
    for s in skills:
        body = s.get("body") or ""
        rows.append(f"### name: {s['name']}\ntitle: {s.get('title', '')}\ntrigger: {s.get('trigger', '')}\n"
                    f"sections: {'; '.join(headings(body)) or '(none)'}\n"
                    f"beginning:\n{body[:EXCERPT_CHARS]}{' …' if len(body) > EXCERPT_CHARS else ''}")
    return "\n\n".join(rows) or "(none)"


def _draft(bot, lessons) -> dict:
    """The draft for `lessons`: the proposal's fields (no id, no status). One model call. Raises `Unfit` when the
    material cannot be saved uncut, not even as a new skill."""
    skills = list(bot.skills())
    by_name = {s["name"]: s for s in skills}
    lessons_txt = "\n".join(f"- [{i['id']}] {i.get('claim', '')} (seen {i.get('count', 0)}×)" for i in lessons)
    draft = None
    try:
        draft = parse_json(model.text(bot, DRAFT_PROMPT.format(lessons=lessons_txt, skills=_skills_listing(skills)),
                                      system="You answer with JSON only."), dict)
    except Exception:  # noqa: BLE001
        draft = None
    if not isinstance(draft, dict):
        draft = _fallback_draft(lessons)
    claims_md = "\n".join(f"- {i.get('claim', '')}" for i in lessons)
    name = draft.get("skill") if isinstance(draft.get("skill"), str) else None
    action = str(draft.get("action") or "").strip().lower()
    note = ""
    if action == "update" and name in by_name:
        heading = _clean_heading(draft.get("heading")) or DEFAULT_HEADING
        section = _clean_section(draft.get("section"), heading, by_name[name].get("title")) or claims_md
        words = trigger_words(draft.get("triggers")) or trigger_words(draft.get("trigger"))
        plan = _update_plan(bot, by_name[name], heading, section, words)
        if _slug(name) != name:
            raise Unfit(f"the skill file “{name}” cannot be saved under its own name")
        if _fits(plan):
            # Never cut: the material goes into a separate new skill instead.
            note = f"{FULL_NOTE} “{_short_title(heading)}” instead."
            new_words = _dedupe(plan["triggers_added"] or words) or _fallback_trigger(lessons, heading)
            full = by_name[name]
            plan = _create_plan(bot, heading, new_words, section, by_name, lessons)
            plan["overflow_from"] = name
            plan["overflow_title"] = full.get("title") or name
    else:
        title = (draft.get("title") if isinstance(draft.get("title"), str) else "") or \
            _clean_heading(draft.get("heading")) or (name or "")
        body = _clean_body(draft.get("body") or draft.get("section") or "", title) or claims_md
        trigger = draft.get("trigger") if draft.get("trigger") else draft.get("triggers")
        plan = _create_plan(bot, title, trigger_words(trigger), body, by_name, lessons)
    diff = _diff(plan["_old_body"], plan["body"], plan["_old_trigger"], plan["trigger"], plan["name"],
                 plan["action"] == "create")
    out = {k: v for k, v in plan.items() if not k.startswith("_")}
    out.update(diff=diff, note=note, items=[i["id"] for i in lessons], claims=[i.get("claim", "") for i in lessons],
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


def request(bot, items, *, topic="", gap="") -> dict:
    """Start a proposal for `items` in the background and return its record at once (status "preparing"; its
    lessons are marked `proposed` now). `gap` (or the `gap` link of a `research` lesson among the items) records
    the gap the proposal fixes; those gaps turn "proposed". ValueError when one is already being prepared for this
    expert, or no lesson is left to propose."""
    with store.lock(bot):
        if _preparing(bot):
            raise ValueError(BUSY)
        busy = open_item_ids(bot)
        lessons = [i for i in _resolve_items(bot, items) if i["id"] not in busy]
        if not lessons:
            raise ValueError("no lessons to propose")
        p = {"id": store.new_id("p"), "ts": time.time(), "status": "preparing", "topic": str(topic or "")[:200],
             "action": None, "skill": None, "title": "", "trigger": "", "body": "", "diff": "",
             "items": [i["id"] for i in lessons], "claims": [i.get("claim", "") for i in lessons],
             "score_before": None, "score_after": None, "card_seq": None, "error": ""}
        linked = list(dict.fromkeys([str(gap)] * bool(gap) + [str(i["gap"]) for i in lessons if i.get("gap")]))
        if linked:
            p["gap"], p["gaps"] = linked[0], linked
        _save(bot, p)
        for i in lessons:
            memory.mark(bot, i["id"], proposed=True)
        with _PREPARING_GUARD:
            _PREPARING.add(p["id"])
        if linked:
            from fused_render.experts import gaps
            for gid in linked:
                gaps.mark_proposed(bot, gid, p["id"])
    t = threading.Thread(target=_prepare_bg, args=(bot, p["id"]), daemon=True,
                         name=f"expert-proposal-{getattr(bot, 'id', '?')}")
    t.start()
    return dict(p)


def _prepare_bg(bot, pid) -> None:
    ready = None
    try:
        p = get(bot, pid)
        if p is None:
            return
        lessons = _resolve_items(bot, p.get("items"))
        if not lessons:
            raise ValueError("its lessons were forgotten")
        fields = _draft(bot, lessons)
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


def _final(bot, p) -> dict:
    """What `approve` writes for `p`, rebuilt against the skill as it is NOW (another approval may have patched it
    since the draft): `{"name","title","trigger","body","heading"}`. ValueError when it cannot be written uncut."""
    skills = list(bot.skills())
    by_name = {s["name"]: s for s in skills}
    if p.get("action") == "update":
        if not p.get("heading") or not p.get("section"):
            # A record drafted before proposals became patches carries a whole rewritten body: never written.
            raise ValueError("this proposal rewrote the whole skill (an older draft) and is not applied; "
                             "its lessons will be proposed again")
        cur = by_name.get(p.get("skill"))
        if cur is None:
            raise ValueError(f"the skill “{p.get('skill')}” no longer exists")
        plan = _update_plan(bot, cur, p["heading"], p["section"], list(p.get("triggers_added") or []))
        why = _fits(plan)
        if why:
            raise ValueError(f"the skill changed since this was drafted and no longer has room: {why}")
        return {"name": cur["name"], "title": plan["title"], "trigger": plan["trigger"], "body": plan["body"],
                "heading": p["heading"]}
    name = p.get("name") if p.get("name") and p.get("name") not in by_name else _free_name(p.get("title"), by_name)
    plan = {"body": norm_body(p.get("body")), "trigger": norm_trigger(p.get("trigger"))}
    why = _fits(plan)
    if why:
        raise ValueError(why)
    if len(by_name) >= skill_max():
        raise ValueError(f"this expert already has {skill_max()} skills")
    return {"name": name, "title": p.get("title") or name, "trigger": plan["trigger"], "body": plan["body"],
            "heading": ""}


def _stored(bot, name):
    return next((s for s in bot.skills() if s.get("name") == name), None)


def approve(bot, pid) -> dict:
    """Write the skill: back its file up, save, read it back and verify the stored body and trigger are exactly
    what was intended. On a failed save or a mismatch the file is restored byte for byte from the backup, the
    proposal becomes "failed" (lessons un-marked) and ValueError says why; it is never shown again. Only after the
    verification passes do the lessons leave memory."""
    with store.lock(bot):
        p = _decide(bot, pid)
        try:
            want = _final(bot, p)
        except ValueError as e:
            _fail(bot, pid, str(e))
            raise ValueError(str(e)) from e
        name = want["name"]
        backup = backup_skill(bot, name)
        try:
            saved = bot.skill_save(want["title"], want["trigger"], want["body"], name=name)
        except Exception as e:  # noqa: BLE001
            _restore(bot, name, backup)
            reason = f"could not save the skill: {str(e) or type(e).__name__}"
            _fail(bot, pid, reason)
            raise ValueError(reason) from e
        got = _stored(bot, name)
        if saved != name or got is None or got.get("body") != want["body"] \
                or norm_trigger(got.get("trigger")) != want["trigger"]:
            _restore(bot, name, backup)
            if saved and saved != name:
                logger.warning("expert %s: skill saved as %s, not %s", getattr(bot, "id", "?"), saved, name)
            reason = "the skill could not be saved intact; it was restored from its backup"
            _fail(bot, pid, reason)
            raise ValueError(reason)
        if want["heading"]:
            _add_learned(bot, name, want["heading"])
        p.update(status="approved", skill=name, decided=time.time())
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


def card_text(proposal) -> tuple[str, str]:
    p = proposal or {}
    n = len(p.get("items") or [])
    what = (f"update the skill “{p.get('title', '')}”" if p.get("action") == "update"
            else f"update the skill “{p.get('overflow_title') or p.get('overflow_from')}”" if p.get("overflow_from")
            else f"add a new skill “{p.get('title', '')}”")
    if p.get("gap"):
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
    else:
        text += "."
    text += "\nApprove?"
    return text, p.get("diff") or ""
