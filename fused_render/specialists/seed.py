"""The shipped Canvas specialist, installed when the specialists root is first created (so one the user deletes
stays deleted). Its skills are copied from the experts' shipped Canvas material: each experts skill (`# Title`, then
a `trigger:` line) becomes a Claude Code skill whose description is the title and the trigger words.
"""
from __future__ import annotations

import os
import re

from fused_render.specialists import home as hm
from fused_render.specialists import paths, store

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipped", "canvas")
SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experts", "shipped",
                      "canvas-reference", "skills")


def _skill(text: str, stem: str) -> str:
    title, _, rest = text.partition("\n")
    trigger, _, body = rest.partition("\n")
    title = title.removeprefix("# ").strip()
    words = trigger.removeprefix("trigger:").strip()
    # The experts' files call themselves "this expert".
    body = re.sub(r"\b(this|the) expert\b", r"\1 specialist", body.strip())
    return hm.skill_text(stem, f"{title}. Use when the work involves: {words}", f"# {title}\n\n{body}")


def seed() -> None:
    if os.path.isdir(paths.root()):
        return
    os.makedirs(paths.root())
    man = hm.read_json(os.path.join(SHIPPED, "specialist.json"), {})
    skills = {n[:-3]: _skill(hm.read_text(os.path.join(SKILLS, n)), n[:-3])
              for n in sorted(os.listdir(SKILLS)) if n.endswith(".md")}
    store.create(man["name"], man["about"], face=man["face"], subject=hm.read_text(os.path.join(SHIPPED, "subject.md")),
                 skills=skills)
