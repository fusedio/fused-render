"""Bot presets: ready-made bots for one site each (OpenBot `agents.presets` /
`apply_preset`, docs/bots.md §5).

`bots/presets/<key>/preset.json` (name, color, order, model, instructions,
optional `apps`) plus any number of playbook `.md` files in the Skills format
(`# title`, a `trigger:` line, numbered steps). The key doubles as the brand
icon the page draws on the avatar. `apps` names starter apps
(`bots/starters/<key>`, see starters.py) installed when a bot is made from the
preset, so its APP TOOLS are there on the first task.
"""
from __future__ import annotations

import json
import os

from fused_render.bots import paths as bpaths

HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS_DIR = os.path.join(HERE, "presets")


def presets():
    """Every preset, sorted by (order, name): [{key, name, color, order, model,
    instructions, skills: [{name, title, trigger, body}], apps: [starter key]}]."""
    from fused_render.bots.bot import DEFAULT_MODEL, MODELS, Bot
    out = []
    try:
        keys = [k for k in os.listdir(PRESETS_DIR) if os.path.isfile(os.path.join(PRESETS_DIR, k, "preset.json"))]
    except OSError:
        return out
    for key in keys:
        d = os.path.join(PRESETS_DIR, key)
        try:
            with open(os.path.join(d, "preset.json"), encoding="utf-8") as f:
                p = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(p, dict):
            continue
        skills = []
        for n in sorted(x for x in os.listdir(d) if x.endswith(".md") and x != "README.md"):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    title, trigger, body = Bot._parse_skill(f.read())
            except OSError:
                continue
            if title and trigger and body:
                skills.append({"name": n[:-3], "title": title, "trigger": trigger, "body": body})
        apps = p.get("apps") if isinstance(p.get("apps"), list) else []
        try:
            order = int(p.get("order") or 99)
        except (TypeError, ValueError):
            order = 99
        out.append({"key": key, "name": str(p.get("name") or key), "color": str(p.get("color") or "#767676"),
                    "order": order, "model": p.get("model") if p.get("model") in MODELS else DEFAULT_MODEL,
                    "instructions": str(p.get("instructions") or ""), "skills": skills,
                    "apps": [str(a) for a in apps if a]})
    out.sort(key=lambda p: (p["order"], p["name"]))
    return out


def get(key):
    """The preset for `key`, else None."""
    return next((x for x in presets() if x["key"] == key), None) if key else None


def apply_preset(b, key, apps_root=None):
    """Copy a preset's playbooks into the bot, give it the brand icon, use the preset's
    standing rules as Instructions when the user typed none, and install the starter
    apps it relies on (preset.json `apps`) under the apps folder when they are missing,
    so the bot's APP TOOLS are there on its first task."""
    p = get(key)
    if not p:
        raise ValueError(f"unknown preset {key!r}")
    for sk in p["skills"]:
        b.skill_save(sk["title"], sk["trigger"], sk["body"], name=sk["name"])
    b.meta["preset"] = key
    b.meta["face"] = {"icon": key, "color": p["color"], "shape": ""}
    if not (b.meta.get("instructions") or "").strip():
        b.meta["instructions"] = p["instructions"].strip()
    b.save()
    if p["apps"]:
        from fused_render.bots import starters
        root = apps_root or bpaths.apps_root()
        names = starters.ensure(p["apps"], root)
        if names:
            b.emit("system", f"Installed the {' and '.join(names)} app under {root} so this bot has its tools. "
                             "Open it from Apps to finish its setup.")
