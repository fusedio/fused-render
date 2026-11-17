"""Shipped experts (docs/experts.md §4 `seed.py`).

Each folder under `SHIPPED_DIR` is one expert: `expert.json` (`{"name",
"manifest", "face"?}`), `instructions.md`, `skills/*.md` (the skills format:
`# Title`, `trigger: a, b`, blank line, body) and `questions.md` (the quality
gate's questions). `seed()` creates each one ONCE: the witness
`<home>/bots/experts-seeded.json` lists the keys seeded so far, so an expert the
user deletes stays deleted. An install cut short (the app quit after the create,
a skill that did not copy) is marked `seed_pending` on the expert and `pending`
in the witness; the next start copies only what is missing, then marks it done. Called by the bots owner right after the Super Bot
seed (registry `_become_owner`); never raises.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)

SHIPPED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipped")
WITNESS = "experts-seeded.json"


def witness_path() -> str:
    from fused_render.bots import paths as bpaths
    return os.path.join(bpaths.root(), WITNESS)


def _read_witness() -> dict:
    try:
        with open(witness_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    keys = d.get("keys") if isinstance(d, dict) else None
    return dict(keys) if isinstance(keys, dict) else {}


def _write_witness(keys: dict) -> None:
    p = witness_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"keys": keys}, f, indent=1)
    os.replace(tmp, p)


def shipped_keys() -> list[str]:
    try:
        names = sorted(os.listdir(SHIPPED_DIR))
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and os.path.isfile(os.path.join(SHIPPED_DIR, n, "expert.json"))]


def _read_text(path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


#: Set in the expert's manifest (`meta["expert"]`) while its seed is still being installed; cleared once every
#: skill and `questions.md` is in place. A start that finds it set finishes the install.
PENDING = "seed_pending"


def _existing(key):
    """An expert already carrying this shipped key (an import, or a seed whose witness write failed)."""
    try:
        from fused_render.bots import bot as bm
        from fused_render.bots import store as bstore
        for bid in bm._list_ids():
            try:
                m = bstore.read_meta(bid)
            except Exception:  # noqa: BLE001
                continue
            if m.get("kind") == "expert" and isinstance(m.get("expert"), dict) and m["expert"].get("shipped") == key:
                return bid
    except Exception:  # noqa: BLE001
        return None
    return None


def _spec(key):
    src = os.path.join(SHIPPED_DIR, key)
    with open(os.path.join(src, "expert.json"), encoding="utf-8") as f:
        spec = json.load(f)
    if not isinstance(spec, dict):
        raise ValueError(f"{key}/expert.json is not an object")
    return src, spec


def _seed_one(key: str):
    from fused_render.bots import bot as bm
    src, spec = _spec(key)
    manifest = dict(spec.get("manifest") or {}) if isinstance(spec.get("manifest"), dict) else {}
    manifest["shipped"] = key
    manifest[PENDING] = True  # until `_install` has put every skill in place
    name = " ".join(str(spec.get("name") or key).split())[:80]
    instructions = _read_text(os.path.join(src, "instructions.md")).strip()
    return bm.create(name=name, instructions=instructions, kind="expert", greet=False, expert=manifest)


def _pending(b) -> bool:
    raw = b.meta.get("expert")
    return isinstance(raw, dict) and bool(raw.get(PENDING))


def _install(b, key: str) -> bool:
    """Put the shipped face, skills and `questions.md` on expert `b` (created by `_seed_one`): what is already
    there is kept, only what is missing is copied, so an interrupted install resumes. True when everything is in
    place: the pending mark is then cleared. False when a copy failed (the next start tries again)."""
    from fused_render.bots import bot as bm
    src, spec = _spec(key)
    ok = True
    face = spec.get("face")
    if isinstance(face, dict) and not isinstance(b.meta.get("face"), dict):
        with b.lock:
            b.meta["face"] = {k: face[k] for k in ("shape", "color", "icon") if k in face}
            b.save()
    skills_dir = os.path.join(src, "skills")
    try:
        names = sorted(n for n in os.listdir(skills_dir) if n.endswith(".md"))
    except OSError:
        names = []
    for n in names:
        if os.path.isfile(os.path.join(b.skills_dir, n)):
            continue
        try:
            title, trigger, body = bm.Bot._parse_skill(_read_text(os.path.join(skills_dir, n)))
            b.skill_save(title or n[:-3], trigger, body, name=n[:-3])
        except Exception:  # noqa: BLE001 — one bad skill file never loses the expert
            logger.warning("expert seed %s: skill %s not copied", key, n, exc_info=True)
            ok = False
    q = os.path.join(src, "questions.md")
    dst = os.path.join(b.public_dir, "questions.md")
    if os.path.isfile(q) and not os.path.isfile(dst):
        try:
            shutil.copyfile(q, dst)
        except OSError:
            logger.warning("expert seed %s: questions.md not copied", key, exc_info=True)
            ok = False
    if ok:
        with b.lock:
            raw = b.meta.get("expert")
            if isinstance(raw, dict) and PENDING in raw:
                raw = dict(raw)
                raw.pop(PENDING, None)
                b.meta["expert"] = raw
                b.save()
    return ok


def _finish(bid, key) -> bool:
    """Finish the install of an existing expert whose seed was interrupted. True when it is complete (or was
    never a pending seed: an import, a seed done before)."""
    from fused_render.bots import registry
    b = registry.get(bid)
    return _install(b, key) if _pending(b) else True


def seed() -> list[str]:
    """Create every shipped expert not seeded before, and finish any whose install was interrupted (the witness
    row stays `pending` until it is complete). Returns the ids created now."""
    created: list[str] = []
    keys = shipped_keys()
    if not keys:
        return created
    seeded = _read_witness()

    def witness(key, bid, done):
        seeded[key] = {"id": bid, "at": time.time(), **({} if done else {"pending": True})}
        try:
            _write_witness(seeded)
        except OSError:
            logger.warning("could not write %s", witness_path(), exc_info=True)

    for key in keys:
        row = seeded.get(key)
        if isinstance(row, dict) and not row.get("pending"):
            continue  # seeded and complete: the user's edits (and a delete) stand
        bid = _existing(key)
        if bid is None and row is not None:
            witness(key, row.get("id") if isinstance(row, dict) else None, True)  # deleted mid-install: stays so
            continue
        try:
            if bid is None:
                b = _seed_one(key)
                bid = b.id
                created.append(bid)
                witness(key, bid, False)
                done = _install(b, key)
            else:
                done = _finish(bid, key)
        except Exception:  # noqa: BLE001 — the next start tries again (no witness row, or a pending one)
            logger.warning("expert seed %s failed", key, exc_info=True)
            continue
        witness(key, bid, done)
    return created


# ------------------------------------------------------- repairing a skill ---
def shipped_skill_path(bot, name) -> str | None:
    """The shipped file of skill `name` for an expert seeded from a shipped package (manifest `shipped`), or
    None (not a shipped expert, or no such shipped skill)."""
    from fused_render.experts import store
    key = str(store.manifest(bot).get("shipped") or "")
    name = str(name or "")
    if not key or not name or os.path.basename(name) != name or name.startswith(".") \
            or os.path.basename(key) != key or key.startswith("."):
        return None
    path = os.path.join(SHIPPED_DIR, key, "skills", name + ".md")
    return path if os.path.isfile(path) else None


def restore_shipped_skill(bot, name) -> bool:
    """Copy skill `name`'s shipped file over the bot's skill (the current file is backed up first, as an approve
    does) and clear that skill's entries from `learned-sections.json`. False when `name` is not a shipped skill of
    this expert."""
    from fused_render.experts import proposals, store
    src = shipped_skill_path(bot, name)
    if src is None:
        return False
    with open(src, "rb") as f:
        data = f.read()
    with store.lock(bot):
        proposals.backup_skill(bot, name)
        os.makedirs(bot.skills_dir, exist_ok=True)
        dst = os.path.join(bot.skills_dir, name + ".md")
        with open(dst + ".restore.tmp", "wb") as f:
            f.write(data)
        os.replace(dst + ".restore.tmp", dst)
        proposals.forget_learned(bot, name)
    return True


def skills_health(bot) -> list:
    """`[{name, shipped, body_chars, limit, damaged, reasons}]` for every skill of `bot`, no model call. A shipped
    skill is damaged when its current file has fewer fenced code blocks than the shipped original or lacks one of
    its `## ` headings; any skill whose body is at the body limit is damaged too (it was most likely cut there)."""
    from fused_render.bots.bot import Bot
    from fused_render.experts import proposals
    limit = proposals.body_cap()
    out = []
    for s in bot.skills():
        body = s.get("body") or ""
        src = shipped_skill_path(bot, s["name"])
        reasons = []
        if src is not None:
            _t, _tr, orig = Bot._parse_skill(_read_text(src))
            have, had = proposals.fenced_blocks(body), proposals.fenced_blocks(orig)
            if have < had:
                reasons.append(f"{had - have} of the shipped skill's {had} fenced code blocks are missing")
            now = {h.casefold() for h in proposals.headings(body)}
            missing = [h for h in proposals.headings(orig) if h.casefold() not in now]
            if missing:
                reasons.append("missing sections: " + ", ".join(f"“{h}”" for h in missing))
        if len(body) >= limit:
            reasons.append(f"the body is at the {limit}-character limit, so it was probably cut short")
        out.append({"name": s["name"], "shipped": src is not None, "body_chars": len(body), "limit": limit,
                    "damaged": bool(reasons), "reasons": reasons})
    return out
