"""The shipped experts (`shipped/<key>/`: `expert.json`, `subject.md` and `skills/*.md`).

Each is installed once, while Experts and Canvases are both on: their subjects are canvases and the bucket the
S3 tools read, which the Canvases switch gates. `<root>/.state/seeded.json` lists the keys installed, so one the
user deletes stays deleted and one shipped later is still installed; one whose name the user already gave an
expert of their own is marked done and not installed. Its skills are the files in `skills/` (`# Title`, then a
`trigger:` line) turned into Claude Code skills whose description is the title and the trigger words. `"folder":
"canvases"` gives it the canvas copies folder, so a task in a canvas copy gets its guidance by itself.
"""
from __future__ import annotations

import os

from fused_render.experts import home as hm
from fused_render.experts import paths, store

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipped")


def _skill(text: str, stem: str) -> str:
    title, _, rest = text.partition("\n")
    trigger, _, body = rest.partition("\n")
    title = title.removeprefix("# ").strip()
    words = trigger.removeprefix("trigger:").strip()
    return hm.skill_text(stem, f"{title}. Use when the work involves: {words}", f"# {title}\n\n{body.strip()}")


def seed() -> None:
    from fused_render import canvases
    from fused_render.shell import prefs

    if not (prefs.experts_enabled() and prefs.canvases_enabled()):
        return
    marker = os.path.join(paths.state_root(), "seeded.json")
    done = hm.read_json(marker, [])
    for key in sorted(set(os.listdir(SHIPPED)) - set(done)):
        man = hm.read_json(os.path.join(SHIPPED, key, "expert.json"), {})
        done.append(key)
        if store.find(man["name"]):  # the user has one by that name: theirs stands
            hm.write_json(marker, done)
            continue
        skills_dir = os.path.join(SHIPPED, key, "skills")
        skills = {n[:-3]: _skill(hm.read_text(os.path.join(skills_dir, n)), n[:-3])
                  for n in sorted(os.listdir(skills_dir)) if n.endswith(".md")}
        folder = canvases.canvases_base() if man.get("folder") == "canvases" else ""
        if folder:
            os.makedirs(folder, exist_ok=True)  # where the canvas copies go; none may be cloned yet
        store.create(man["name"], man["about"], folder, man["face"],
                     subject=hm.read_text(os.path.join(SHIPPED, key, "subject.md")), skills=skills)
        hm.write_json(marker, done)
