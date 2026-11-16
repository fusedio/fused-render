"""Skill proposals (docs/experts.md §4 `proposals.py`).

Lessons seen often enough become a proposed skill change: one `model.text`
call drafts it (updating an existing skill is preferred), the gate scores the
expert before and after (a what-if run that writes nothing), and the draft is
saved as "ready" for an approval card. Only `approve` writes a skill.

`request` is the asynchronous entry point (chat, routes, scheduler): it saves
the record as "preparing" and marks its lessons `proposed` at once, then one
daemon thread runs `_draft` (the draft and both gate runs) and turns it
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

from fused_render.experts import gate, memory, model, store
from fused_render.experts.consolidate import parse_json

logger = logging.getLogger(__name__)

STATUSES = ("preparing", "ready", "shown", "approved", "rejected", "failed")
OPEN_STATUSES = ("preparing", "ready", "shown")
BODY_CAP = 6000
BUSY = "a skill proposal is already being prepared"
#: A "preparing" record this process is not working on, older than this, was cut short (the app quit).
PREPARE_STALE_S = 3600

_PREPARING: set[str] = set()
_PREPARING_GUARD = threading.Lock()

DRAFT_PROMPT = """You maintain an expert's skills (playbooks). These lessons keep recurring and should become part of
a skill. PREFER updating an existing skill whose subject fits; create a new skill only when none fits.

Lessons:
{lessons}

Existing skills:
{skills}

Answer with ONLY a JSON object:
{{"action": "update" | "create", "skill": "<existing skill name when updating, else null>",
  "title": "<skill title>", "trigger": "<comma-separated trigger words>",
  "body": "<the COMPLETE new skill body in markdown (for an update: the whole body with the lessons merged in)>"}}

`body` is ONLY the skill's markdown content (its steps and advice). It must NOT contain name, title or trigger
lines, nor the "### name: …" header used in the listing above: those go in their own JSON fields."""

_META_LINE = re.compile(r"^(#+\s*)?(name|title|trigger)\s*:", re.I)


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
    rows = [i for i in memory.items(bot)
            if int(i.get("count") or 0) >= at and i.get("trust") != "gap" and i.get("id") not in busy
            and not i.get("proposed") and not i.get("promoted") and not i.get("rejected")]
    rows.sort(key=lambda i: -int(i.get("count") or 0))
    return rows


def _slug(name) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60] or "playbook"


def _fallback_draft(lessons) -> dict:
    first = lessons[0].get("claim", "") if lessons else ""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", " ".join(i.get("claim", "") for i in lessons))]
    about = [a for i in lessons for a in (i.get("about") or [])]
    trigger = ", ".join(dict.fromkeys((about or words)[:5])) or "lessons"
    return {"action": "create", "skill": None, "title": ("Lessons: " + first)[:80],
            "trigger": trigger, "body": "\n".join(f"- {i.get('claim', '')}" for i in lessons)}


def _resolve_items(bot, items) -> list:
    by_id = {i["id"]: i for i in memory.items(bot)}
    out = []
    for it in items or []:
        iid = it.get("id") if isinstance(it, dict) else str(it)
        if iid in by_id and by_id[iid] not in out:
            out.append(by_id[iid])
    return out


def _override(skills, name, title, trigger, body) -> list:
    out, replaced = [], False
    for s in skills:
        if s.get("name") == name:
            out.append({"name": name, "title": title, "trigger": trigger, "body": body})
            replaced = True
        else:
            out.append(dict(s))
    if not replaced:
        out.append({"name": name, "title": title, "trigger": trigger, "body": body})
    return out


def _score(bot, skills_override):
    try:
        r = gate.run(bot, skills_override=skills_override)
        return {"score": r["score"], "total": r["total"]}
    except Exception:  # noqa: BLE001 — a failed gate is an unknown score, not a failed proposal
        return None


def _draft(bot, lessons) -> dict:
    """The draft and both gate runs for `lessons`: the proposal's fields (no id, no status). Slow: model calls."""
    skills = list(bot.skills())
    by_name = {s["name"]: s for s in skills}
    lessons_txt = "\n".join(f"- [{i['id']}] {i.get('claim', '')} (seen {i.get('count', 0)}×)" for i in lessons)
    skills_txt = "\n\n".join(f"### name: {s['name']}\ntitle: {s.get('title', '')}\ntrigger: {s.get('trigger', '')}\n"
                             f"{(s.get('body') or '')[:BODY_CAP]}" for s in skills) or "(none)"
    draft = None
    try:
        draft = parse_json(model.text(bot, DRAFT_PROMPT.format(lessons=lessons_txt, skills=skills_txt),
                                      system="You answer with JSON only."), dict)
    except Exception:  # noqa: BLE001
        draft = None
    if not isinstance(draft, dict) or not str(draft.get("body") or "").strip():
        draft = _fallback_draft(lessons)

    name = draft.get("skill") if isinstance(draft.get("skill"), str) else None
    title = " ".join(str(draft.get("title") or "").split())[:80]
    trigger = str(draft.get("trigger") or "").strip()
    if isinstance(draft.get("trigger"), list):
        trigger = ", ".join(str(t) for t in draft["trigger"])
    body = str(draft.get("body") or "").strip()[:BODY_CAP]
    action = "update" if str(draft.get("action") or "").lower() == "update" and name in by_name else "create"
    if action == "create" and _slug(title or name) in by_name:
        # A "new" skill whose name is taken would overwrite that skill: it is an update.
        action, name = "update", _slug(title or name)
    if action == "update":
        old = by_name[name]
        title = title or old.get("title") or name
        trigger = trigger or old.get("trigger") or ""
        old_body, skill, eff_name = old.get("body") or "", name, name
    else:
        fb = _fallback_draft(lessons)
        title = title or fb["title"]
        trigger = trigger or fb["trigger"]
        old_body, skill, eff_name = "", None, _slug(title)
    body = _clean_body(body, title)[:BODY_CAP] or _fallback_draft(lessons)["body"]
    diff = "".join(difflib.unified_diff(
        [ln + "\n" for ln in old_body.splitlines()],
        [ln + "\n" for ln in body.splitlines()],
        fromfile=f"skills/{eff_name}.md" if action == "update" else "/dev/null",
        tofile=f"skills/{eff_name}.md"))

    score_before = score_after = None
    if gate.questions(bot):
        score_before = _score(bot, skills)
        score_after = _score(bot, _override(skills, eff_name, title, trigger, body))
    return {"action": action, "skill": skill, "title": title, "trigger": trigger, "body": body, "diff": diff,
            "items": [i["id"] for i in lessons], "claims": [i.get("claim", "") for i in lessons],
            "score_before": score_before, "score_after": score_after}


def prepare(bot, items) -> dict:
    """`request`'s work, synchronously: draft, gate, save as "ready", mark the lessons proposed."""
    lessons = _resolve_items(bot, items)
    if not lessons:
        raise ValueError("no lessons to propose")
    fields = _draft(bot, lessons)
    p = {"id": store.new_id("p"), "ts": time.time(), "status": "ready", "topic": "", **fields, "card_seq": None,
         "error": ""}
    with store.lock(bot):
        _save(bot, p)
        for i in lessons:
            memory.mark(bot, i["id"], proposed=True)
    return p


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


def request(bot, items, *, topic="") -> dict:
    """Start a proposal for `items` in the background and return its record at once (status "preparing"; its
    lessons are marked `proposed` now). ValueError when one is already being prepared for this expert, or no
    lesson is left to propose."""
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
        _save(bot, p)
        for i in lessons:
            memory.mark(bot, i["id"], proposed=True)
        with _PREPARING_GUARD:
            _PREPARING.add(p["id"])
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


def approve(bot, pid) -> dict:
    """Write the skill. When that fails the proposal becomes "failed" (lessons un-marked) and ValueError says
    why; it is never shown again."""
    with store.lock(bot):
        p = _decide(bot, pid)
        try:
            name = bot.skill_save(p["title"], p["trigger"], p["body"], name=p.get("skill") or None)
        except Exception as e:  # noqa: BLE001
            reason = f"could not save the skill: {str(e) or type(e).__name__}"
            _fail(bot, pid, reason)
            raise ValueError(reason) from e
        p.update(status="approved", skill=name, decided=time.time())
        _save(bot, p)
        for iid in p.get("items") or []:
            memory.mark(bot, iid, promoted=True)
        return p


def reject(bot, pid) -> dict:
    with store.lock(bot):
        p = _decide(bot, pid)
        p.update(status="rejected", decided=time.time())
        _save(bot, p)
        for iid in p.get("items") or []:
            memory.mark(bot, iid, rejected=True)
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
            else f"add a new skill “{p.get('title', '')}”")
    text = f"I'd like to {what} from {n} lesson{'s' if n != 1 else ''} I keep seeing"
    claims = [c for c in (p.get("claims") or []) if c]
    if claims:
        text += ":\n" + "\n".join(f"- {c}" for c in claims[:5])
        if len(claims) > 5:
            text += f"\n- …and {len(claims) - 5} more"
    else:
        text += "."
    b, a = p.get("score_before"), p.get("score_after")
    if b and a:
        text += f"\nChecks: {b['score']}/{b['total']} now → {a['score']}/{a['total']} with this change."
    text += "\nApprove?"
    return text, p.get("diff") or ""
