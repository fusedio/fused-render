"""The shipped specialists, installed the first time the flag is on (docs/specialists.md §2 "Create, seed, delete").

One today: Canvas. Its manifest and subject section live in `shipped/canvas/`; its skills are COPIED from the
experts' shipped Canvas material (`fused_render/experts/shipped/canvas-reference/skills/*.md`), never moved, so
experts keep theirs. Each experts skill file (`# Title` then a `trigger:` line) becomes a Claude Code skill
`.claude/skills/<stem>/SKILL.md` whose frontmatter description is the title plus the trigger words, so Claude
Code's own skill loading finds it.

The witness `<root>/.state/seeded.json` lists the keys installed so far, so a specialist the user deletes stays
deleted. Never raises; the next start retries what failed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from fused_render.specialists import home as hm
from fused_render.specialists import paths

logger = logging.getLogger(__name__)

SHIPPED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipped")
EXPERTS_SHIPPED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experts", "shipped")
WITNESS = "seeded.json"


def witness_path() -> str:
    return os.path.join(paths.state_root(), WITNESS)


def shipped_keys() -> list[str]:
    try:
        names = sorted(os.listdir(SHIPPED_DIR))
    except OSError:
        return []
    return [n for n in names if os.path.isfile(os.path.join(SHIPPED_DIR, n, "specialist.json"))]


def skill_from_expert(text: str, stem: str) -> str:
    """An experts skill file as a Claude Code SKILL.md: `# Title` and `trigger: words` become the description."""
    lines = text.splitlines()
    title, trigger, i = stem.replace("-", " "), "", 0
    if i < len(lines) and lines[i].startswith("# "):
        title, i = lines[i][2:].strip(), i + 1
    if i < len(lines) and lines[i].lower().startswith("trigger:"):
        trigger, i = lines[i].split(":", 1)[1].strip(), i + 1
    desc = title + (f". Use when the work involves: {trigger}" if trigger else "")
    # The experts' files call themselves "this expert"; here it is a specialist (PRODUCT: never "expert").
    body = re.sub(r"\b(this|the) expert\b", r"\1 specialist", "\n".join(lines[i:]).strip())
    return hm.skill_text(stem, desc[:1000], f"# {title}\n\n{body}")


def seed_one(key: str):
    from fused_render.specialists import history, store

    folder = os.path.join(SHIPPED_DIR, key)
    with open(os.path.join(folder, "specialist.json"), encoding="utf-8") as f:
        man = json.load(f)
    existing = next((s for s in store.all() if s.shipped == key), None)
    if existing:
        return existing
    name, subject = man.get("name") or key, man.get("subject") or ""
    charter = hm.render_charter(name, subject, hm.read_text(os.path.join(folder, "subject.md")))
    spec = store.create(name, subject, triggers=man.get("triggers"), shipped=key, charter=charter,
                        face=man.get("face") if isinstance(man.get("face"), dict) else None)
    src = os.path.join(EXPERTS_SHIPPED, str(man.get("from") or "").rsplit("/", 1)[-1], "skills")
    try:
        names = sorted(n for n in os.listdir(src) if n.endswith(".md"))
    except OSError:
        names = []
    for n in names:
        stem = n[:-3]
        if not hm.skill_name_ok(stem):
            continue
        hm.write_text(hm.skill_path(spec.home, stem), skill_from_expert(hm.read_text(os.path.join(src, n)), stem))
    history.commit(spec.id, f"seed the shipped {name} specialist")
    return spec


def seed() -> list[str]:
    """Install every shipped specialist not installed before. Returns the keys installed now."""
    w = hm.read_json(witness_path(), {})
    done = dict(w.get("keys") or {}) if isinstance(w, dict) else {}
    out = []
    for key in shipped_keys():
        if key in done:
            continue
        try:
            seed_one(key)
        except Exception:  # noqa: BLE001
            logger.warning("specialists seed: %s failed", key, exc_info=True)
            continue
        done[key] = time.time()
        out.append(key)
    if out:
        hm.write_json(witness_path(), {"keys": done})
    return out
