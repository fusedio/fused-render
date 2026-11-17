"""The only writer of an expert's memory and skills (docs/experts.md `knowledge.py`).

`apply(bot, change)` makes one change, saves what `undo` needs under `history/`, and appends one line to
`activity.jsonl`. It never raises for a bad change: the line says `ok: false` and why. `undo` puts a lesson back as
it was (item level) or a skill file back as it was (whole file, refused when the file changed since).

A skill is edited by section: the body is split at its `## ` headings, the ops `add` / `replace` / `remove` touch
only the sections they name, and every other section and the preamble stay byte for byte. Every skill write is read
back and verified; on a mismatch the file is put back as it was.
"""
from __future__ import annotations

import json
import os
import re
import time

from fused_render.experts import memory, store

ACTIVITY_FILE = "activity.jsonl"
SEEN_FILE = "activity-seen.json"
HISTORY_DIR = "history"
SHIPPED_DIR = "skills-shipped"
NOTE_ACTIONS = ("noticed", "researched", "could_not_find", "skipped")
#: Lines the UI hides unless asked.
QUIET_ACTIONS = ("counted", "noticed")
EVIDENCE_KEYS = ("session", "quote", "source", "item")
TRIGGER_CAP = 300
TITLE_CAP = 80
DEFAULT_HEADING = "Learned lessons"
SECTION_OPS = ("add", "replace", "remove")
CHANGED_SINCE = "changed since; undo the later change first"
UNDO_WHY = "You undid this."
LINKED_UNDO_WHY = "Undone together with the skill change."

_META_LINE = re.compile(r"^(#+\s*)?(name|title|trigger)\s*:", re.I)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")


class Refused(Exception):
    """`undo` cannot be done; the message says why."""


# ------------------------------------------------------------------ limits ---
def _bot_class():
    from fused_render.bots.bot import Bot
    return Bot


def body_cap() -> int:
    return int(_bot_class().SKILL_BODY_CAP)


def trigger_cap() -> int:
    return int(getattr(_bot_class(), "SKILL_TRIGGER_CAP", TRIGGER_CAP))


def skill_max() -> int:
    return int(_bot_class().SKILL_MAX)


# ------------------------------------------------------------- normalising ---
def norm_body(body) -> str:
    """A body as a skill file stores it and `Bot.skills()` reads it back (stripped, lines joined with "\n")."""
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
    next `## ` heading or the end. A `## ` line inside fenced code is not a heading. Joining the preamble and every
    `text` gives `body` back for every string."""
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


def _join(preamble, sections) -> str:
    """The pieces joined as they are; a piece that does not end with a newline (the old last section, followed now
    by another) gets a blank line after it."""
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
    """Apply section `ops` (`{"op": "add"|"replace"|"remove", "heading", "section"?, "new_heading"?}`) to `body`,
    in order. Returns `{"body", "applied": [ops as applied], "dropped": [{"op", "reason"}]}`. A replace / remove
    must name one existing section (trimmed, case-insensitive), else it is dropped with a reason; an add whose
    heading exists replaces that section. Sections no op names and the preamble stay byte for byte."""
    pre, secs = parse_sections(norm_body(body))
    applied, dropped = [], []

    def drop(op, reason):
        dropped.append({"op": op, "reason": reason})

    for op in (ops if isinstance(ops, list) else []):
        if not isinstance(op, dict):
            drop(op, "an operation must be an object")
            continue
        kind = str(op.get("op") or "").strip().lower()
        if kind not in SECTION_OPS:
            drop(op, f"unknown operation “{op.get('op')}”")
            continue
        heading = _clean_heading(op.get("heading"))
        hits, why = _find(secs, heading)
        if kind == "remove":
            if why:
                drop(op, why)
                continue
            applied.append({"op": "remove", "heading": secs.pop(hits[0])["heading"]})
            continue
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
            continue
        if why:  # a replace naming nothing, or an add / replace naming several sections
            drop(op, why)
            continue
        old = secs[hits[0]]
        new_heading = _clean_heading(op.get("new_heading")) if kind == "replace" else ""
        if new_heading and _hkey(new_heading) != _hkey(old["heading"]) and _find(secs, new_heading)[0]:
            drop(op, f"a section “{new_heading}” already exists")
            continue
        renamed = bool(new_heading) and new_heading != old["heading"]
        shown = new_heading or old["heading"]
        content = _clean_section(op.get("section"), shown, title)
        if not content:
            drop(op, "the section's new content is empty (use remove to drop a section)")
            continue
        line = f"## {new_heading}" if renamed else _LINE.findall(old["text"])[0]
        secs[hits[0]] = {"heading": shown, "text": _section_text(line, content)}
        applied.append({"op": "replace", "heading": old["heading"], "section": content,
                        **({"new_heading": new_heading} if renamed else {})})
    return {"body": norm_body(_join(pre, secs)), "applied": applied, "dropped": dropped}


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


# ------------------------------------------------------------- skill files ---
def _skill_name(name) -> str:
    name = str(name or "")
    if not name or os.path.basename(name) != name or name.startswith("."):
        raise ValueError("no such skill")
    return name


def _skill_file(bot, name) -> str:
    return os.path.join(bot.skills_dir, name + ".md")


def shipped_path(bot, name) -> str | None:
    """The untouched shipped version of skill `name` (`skills-shipped/<name>.md`), or None."""
    try:
        path = os.path.join(store.expert_dir(bot), SHIPPED_DIR, _skill_name(name) + ".md")
    except ValueError:
        return None
    return path if os.path.isfile(path) else None


def _read(path) -> str | None:
    """A file's bytes as text that survives JSON and encodes back to the same bytes; None when there is no file."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        return None


def _write(path, text) -> None:
    """Make the file hold exactly `text` (`_read`'s form); None removes it."""
    if text is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(text.encode("utf-8", "surrogateescape"))
    os.replace(path + ".tmp", path)


def _stored(bot, name):
    return next((s for s in bot.skills() if s.get("name") == name), None)


def _save_skill(bot, name, title, trigger, body) -> dict:
    """Write one skill file, read it back and verify it; on a mismatch the file is put back as it was. Returns
    the history record."""
    if len(body) > body_cap():
        raise ValueError(f"the skill body would be {len(body)} characters (the limit is {body_cap()})")
    if len(trigger) > trigger_cap():
        raise ValueError(f"the trigger line would be {len(trigger)} characters (the limit is {trigger_cap()})")
    if not body:
        raise ValueError("the skill would have no content")
    if not trigger:
        raise ValueError("the skill would have no trigger words")
    path = _skill_file(bot, name)
    before = _read(path)
    try:
        _write(path, f"# {title}\ntrigger: {trigger}\n\n{body}\n")
        got = _stored(bot, name)
        if got is None or got.get("body") != body or norm_trigger(got.get("trigger")) != trigger:
            raise ValueError("the skill could not be saved intact; it was put back as it was")
    except Exception:
        _write(path, before)
        raise
    return {"kind": "skill", "name": name, "before": before, "after": _read(path)}


def _skill_edit(bot, c):
    name = _skill_name(c.get("skill"))
    s = _stored(bot, name)
    if s is None:
        raise ValueError(f"no such skill: {name}")
    res = apply_ops(s["body"], c.get("ops"), title=s["title"])
    trig = apply_triggers(s["trigger"], c.get("triggers_add"), c.get("triggers_remove"))
    dropped = res["dropped"] + trig["dropped"]
    if dropped:  # all or nothing: a change that does not fit the skill as it is now is not half applied
        raise ValueError("; ".join(d["reason"] for d in dropped))
    did = [{"add": "added", "replace": "rewrote", "remove": "removed"}[o["op"]] + f" “{o['heading']}”"
           for o in res["applied"]] + (["changed its trigger words"] if trig["added"] or trig["removed"] else [])
    if not did:
        raise ValueError("the change names no section and no trigger word")
    hist = _save_skill(bot, name, s["title"], trig["trigger"], res["body"])
    return "skill_updated", name, f"Updated “{s['title']}”: " + ", ".join(did), hist


def _skill_create(bot, c):
    title = " ".join(_clean_heading(c.get("title")).split())[:TITLE_CAP]
    if not title:
        raise ValueError("a new skill needs a title")
    taken = {s["name"] for s in bot.skills()}
    if len(taken) >= skill_max():
        raise ValueError(f"this expert already has {skill_max()} skills")
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "skill"
    name, k = base, 2
    while name in taken:
        name, k = f"{base[:55].rstrip('-')}-{k}", k + 1
    hist = _save_skill(bot, name, title, norm_trigger(c.get("trigger")), norm_body(_clean_body(c.get("body"), title)))
    return "skill_created", name, f"New skill “{title}”", hist


def _skill_restore(bot, c):
    name = _skill_name(c.get("skill"))
    src = shipped_path(bot, name)
    if src is None:
        raise ValueError(f"Fused shipped no skill named {name}")
    path = _skill_file(bot, name)
    before, text = _read(path), _read(src)
    _write(path, text)
    return "skill_restored", name, f"Put “{name}” back to the version Fused shipped", \
        {"kind": "skill", "name": name, "before": before, "after": text}


# ------------------------------------------------------------------ lessons ---
def _memory_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), "memory.json")


def _lesson(bot, fn):
    """Run one `memory` mutator; returns (its result, the history record: every lesson it touched, as it was)."""
    before = {i["id"]: i for i in memory.items(bot)}
    out = fn()
    after = {i["id"]: i for i in memory.items(bot)}
    was = {k: before.get(k) for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return out, ({"kind": "lesson", "before": was} if was else None)


def _restore_lessons(bot, was: dict) -> None:
    """Item-level undo: each lesson id back to the item it was (None: it did not exist, so it goes)."""
    data = store.read_json(_memory_path(bot), {})
    items = [i for i in (data.get("items") if isinstance(data, dict) else None) or [] if isinstance(i, dict)]
    for lid, item in was.items():
        at = next((n for n, i in enumerate(items) if i.get("id") == lid), None)
        if item is None:
            if at is not None:
                items.pop(at)
        elif at is None:
            items.append(item)
        else:
            items[at] = item
    store.write_json(_memory_path(bot), {"version": 1, "items": items})


def _need(item, what="no such lesson"):
    if not item:
        raise ValueError(what)
    return item


def _lesson_add(bot, c):
    known = {i["id"] for i in memory.items(bot)}
    kw = {k: c[k] for k in ("kind", "trust", "via", "session", "about", "counted") if c.get(k) is not None}
    item, hist = _lesson(bot, lambda: memory.add(bot, c.get("claim"), **kw))
    return ("counted" if item["id"] in known else "learned"), item["id"], item.get("claim", ""), hist


def _lesson_bump(bot, c):
    item, hist = _lesson(bot, lambda: _need(memory.bump(bot, c.get("id"), session=c.get("session") or "")))
    return "counted", item["id"], item.get("claim", ""), hist


def _lesson_confirm(bot, c):
    item, hist = _lesson(bot, lambda: _need(memory.confirm(bot, c.get("id"), session=c.get("session") or "")))
    return "counted", item["id"], item.get("claim", ""), hist


def _lesson_contradict(bot, c):
    item, hist = _lesson(bot, lambda: _need(memory.contradict(bot, c.get("id"), session=c.get("session") or "")))
    return "counted", item["id"], item.get("claim", ""), hist


def _lesson_edit(bot, c):
    item, hist = _lesson(bot, lambda: _need(memory.narrow(bot, c.get("id"), c.get("claim")),
                                            "no such lesson, or the new claim is empty"))
    return "corrected", item["id"], item.get("claim", ""), hist


def _lesson_remove(bot, c):
    reason = str(c.get("reason") or "forgotten")
    gone, hist = _lesson(bot, lambda: _need(memory.remove(
        bot, [c.get("id")], reason, skill=str(c.get("skill") or ""), into=str(c.get("into") or ""))))
    return ("forgot" if reason == "forgotten" else "removed"), gone[0]["id"], gone[0].get("claim", ""), hist


def _lesson_merge(bot, c):
    res, hist = _lesson(bot, lambda: _need(memory.merge(bot, c.get("ids"), c.get("claim")),
                                           "these lessons cannot be merged"))
    return "merged", res[0]["id"], res[0].get("claim", ""), hist


def _note(bot, c):
    action = c.get("action")
    if action not in NOTE_ACTIONS:
        raise ValueError("a note's action must be one of " + ", ".join(NOTE_ACTIONS))
    return action, str(c.get("target") or ""), " ".join(str(c.get("summary") or "").split()), None


#: op -> (its function, the action a failed line carries).
_OPS = {
    "lesson_add": (_lesson_add, "learned"), "lesson_bump": (_lesson_bump, "counted"),
    "lesson_confirm": (_lesson_confirm, "counted"), "lesson_contradict": (_lesson_contradict, "counted"),
    "lesson_edit": (_lesson_edit, "corrected"),
    "lesson_remove": (_lesson_remove, "removed"), "lesson_merge": (_lesson_merge, "merged"),
    "skill_edit": (_skill_edit, "skill_updated"), "skill_create": (_skill_create, "skill_created"),
    "skill_restore": (_skill_restore, "skill_restored"), "note": (_note, "skipped"),
}


# ----------------------------------------------------------------- activity ---
def _activity_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), ACTIVITY_FILE)


def _history_path(bot, aid) -> str:
    return os.path.join(store.expert_dir(bot), HISTORY_DIR, os.path.basename(str(aid)) + ".json")


def _lines(bot) -> list:
    """Every activity line, oldest first, with `undone` filled in from the undo lines."""
    rows = [r for r in store.read_jsonl(_activity_path(bot)) if isinstance(r, dict) and r.get("id")]
    undone = {r.get("target") for r in rows if r.get("action") == "undo" and r.get("ok")}
    for r in rows:
        r["undone"] = r["id"] in undone
    return rows


def _log(bot, line) -> dict:
    if line["action"] in QUIET_ACTIONS:
        line["quiet"] = True
    store.append_jsonl(_activity_path(bot), line)
    return line


def apply(bot, change: dict) -> dict:
    """Make one change (docs/experts.md, the ops table) and return the activity line written for it.
    Never raises for a bad change: the line has `ok: false` and `error`. A change may carry `summary` (replaces the
    line's own) and `quiet: true` (the UI hides the line unless asked)."""
    c = change if isinstance(change, dict) else {}
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    fn, action = _OPS.get(c.get("op"), (None, "skipped"))
    line = {"id": store.new_id("a"), "ts": time.time(), "action": action,
            "target": str(c.get("id") or c.get("skill") or ""),
            "summary": " ".join(str(c.get("summary") or c.get("claim") or c.get("title") or "").split()),
            "why": " ".join(str(c.get("why") or "").split()),
            "evidence": {k: ev[k] for k in EVIDENCE_KEYS if ev.get(k)}, "stage": str(c.get("stage") or ""),
            "ok": True, "undoable": False, "undone": False, "error": ""}
    if c.get("quiet") is True:
        line["quiet"] = True
    if c.get("because"):  # the activity line this change follows: undoing that one undoes this one too
        line["because"] = str(c["because"])
    hist = None
    with store.lock(bot):
        try:
            if fn is None:
                raise ValueError(f"unknown op: {c.get('op')}")
            action, target, summary, hist = fn(bot, c)
            line.update(action=action, target=target, summary=c.get("summary") and line["summary"] or summary)
            if hist:
                # ASCII JSON: a skill file's stray bytes (kept as lone surrogates) survive it.
                path = _history_path(bot, line["id"])
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="ascii") as f:
                    json.dump(hist, f)
                line["undoable"] = True
        except OSError as e:
            if not hist:  # (a change made whose history could not be saved stays made; it just cannot be undone)
                line.update(ok=False, error=str(e) or type(e).__name__)
        except Exception as e:  # noqa: BLE001 — a bad change is logged, never raised
            line.update(ok=False, error=str(e) or type(e).__name__)
        return _log(bot, line)


def _undo_line(bot, orig, why) -> dict:
    return _log(bot, {"id": store.new_id("a"), "ts": time.time(), "action": "undo", "target": orig["id"],
                      "summary": "Undid: " + str(orig.get("summary") or orig.get("action") or ""), "why": why,
                      "evidence": {}, "stage": "", "ok": True, "undoable": False, "undone": False, "error": ""})


def undo(bot, activity_id: str, why: str = UNDO_WHY) -> dict:
    """Undo one applied change and return the undo's own activity line. Raises `Refused` when there is nothing to
    undo, it was undone already, or a skill's file is no longer what that change left. Every not yet undone line
    whose `because` is this line's id (the lessons a skill change took out of memory) is undone with it, each
    logged as its own undo line."""
    with store.lock(bot):
        rows = _lines(bot)
        orig = next((r for r in rows if r["id"] == activity_id), None)
        if orig is None:
            raise Refused("no such activity")
        if orig["undone"]:
            raise Refused("this was already undone")
        hist = store.read_json(_history_path(bot, activity_id), None) if orig.get("undoable") else None
        if not isinstance(hist, dict):
            raise Refused("this cannot be undone")
        if hist.get("kind") == "skill":
            path = _skill_file(bot, _skill_name(hist.get("name")))
            if _read(path) != hist.get("after"):
                raise Refused(f"“{hist.get('name')}” {CHANGED_SINCE}")
            _write(path, hist.get("before"))
        else:
            _restore_lessons(bot, hist.get("before") or {})
        line = _undo_line(bot, orig, why)
        for r in rows:
            if r.get("because") != activity_id or r["undone"] or not r.get("undoable"):
                continue
            linked = store.read_json(_history_path(bot, r["id"]), None)
            if isinstance(linked, dict) and linked.get("kind") == "lesson":
                _restore_lessons(bot, linked.get("before") or {})
                _undo_line(bot, r, LINKED_UNDO_WHY)
        return line


def activity(bot, limit=100, before=None, quiet=True) -> list:
    """The activity lines, newest first: at most `limit`, only those older than the line with id `before` when it
    is given, without the quiet lines when `quiet` is False."""
    rows = list(reversed(_lines(bot)))
    if before:
        at = next((n for n, r in enumerate(rows) if r["id"] == before), None)
        rows = rows[at + 1:] if at is not None else rows
    if not quiet:
        rows = [r for r in rows if not r.get("quiet")]
    return rows[:max(0, int(limit))]


def _seen_path(bot) -> str:
    return os.path.join(store.expert_dir(bot), SEEN_FILE)


def unread(bot) -> int:
    """How many lines the user has not seen: those after the last `seen`, not quiet and not the user's own undo."""
    rows = _lines(bot)
    last = (store.read_json(_seen_path(bot), {}) or {}).get("id")
    at = next((n for n, r in enumerate(rows) if r["id"] == last), -1)
    return sum(1 for r in rows[at + 1:] if not r.get("quiet") and r.get("action") != "undo")


def seen(bot) -> None:
    rows = _lines(bot)
    store.write_json(_seen_path(bot), {"id": rows[-1]["id"] if rows else ""})
