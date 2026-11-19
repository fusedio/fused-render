"""The shipped specialists (`shipped/<key>/`: `specialist.json` and `subject.md`), the counterparts of the shipped
experts (`experts/shipped/<expert>/`, named by `specialist.json`'s `expert`), for comparing the two.

Each is installed once, while Specialists and Canvases are both on: their subjects are canvases and the bucket the
S3 tools read, which the Canvases switch gates. `<root>/.state/seeded.json` lists the keys installed, so one the
user deletes stays deleted and one shipped later is still installed; one whose name the user already gave a
specialist of their own is marked done and not installed. Its skills are the expert's (`# Title`, then a
`trigger:` line) turned into Claude Code skills whose description is the title and the trigger words. `"folder":
"canvases"` gives it the canvas copies folder, the one the shipped experts are granted, so a task in a canvas copy
gets its guidance by itself.
"""
from __future__ import annotations

import os
import re

from fused_render.specialists import home as hm
from fused_render.specialists import paths, store

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipped")
EXPERTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experts", "shipped")


def _skill(text: str, stem: str) -> str:
    title, _, rest = text.partition("\n")
    trigger, _, body = rest.partition("\n")
    title = title.removeprefix("# ").strip()
    words = trigger.removeprefix("trigger:").strip()
    # The experts' files call themselves "this expert".
    body = re.sub(r"\b(this|the) expert\b", r"\1 specialist", body.strip())
    return hm.skill_text(stem, f"{title}. Use when the work involves: {words}", f"# {title}\n\n{body}")


def seed() -> None:
    from fused_render import canvases
    from fused_render.shell import prefs

    if not (prefs.specialists_enabled() and prefs.canvases_enabled()):
        return
    marker = os.path.join(paths.state_root(), "seeded.json")
    done = hm.read_json(marker, [])
    for key in sorted(set(os.listdir(SHIPPED)) - set(done)):
        man = hm.read_json(os.path.join(SHIPPED, key, "specialist.json"), {})
        done.append(key)
        if store.find(man["name"]):  # the user has one by that name: theirs stands
            hm.write_json(marker, done)
            continue
        skills_dir = os.path.join(EXPERTS, man["expert"], "skills")
        skills = {n[:-3]: _skill(hm.read_text(os.path.join(skills_dir, n)), n[:-3])
                  for n in sorted(os.listdir(skills_dir)) if n.endswith(".md")}
        folder = canvases.canvases_base() if man.get("folder") == "canvases" else ""
        if folder:
            os.makedirs(folder, exist_ok=True)  # where the canvas copies go; none may be cloned yet
        store.create(man["name"], man["about"], folder, man["face"],
                     subject=hm.read_text(os.path.join(SHIPPED, key, "subject.md")), skills=skills)
        hm.write_json(marker, done)
