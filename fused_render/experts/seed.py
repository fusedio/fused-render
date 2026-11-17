"""Shipped experts (docs/experts.md "Shipped expert").

Each folder under `SHIPPED_DIR` is one expert: `expert.json` (`{"name", "ask_me_about", "face"}`),
`instructions.md` and `skills/*.md`. `seed()` installs each ONCE: it creates the expert, copies the folder's files
into the expert's folder and grants it the canvas copies (`~/Fused/canvas`, canvases.canvases_base) through the
bot's `folders` setting. The witness `<bots root>/experts-seeded.json` lists the keys installed so far, so an
expert the user deletes stays deleted. Skills are never synced afterwards: the expert's copies are its own.
Called by the bots owner after the Super Bot seed (registry._seed_experts).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time

from fused_render.experts import store

logger = logging.getLogger(__name__)

SHIPPED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipped")
WITNESS = "experts-seeded.json"


def witness_path() -> str:
    from fused_render.bots import paths as bpaths
    return os.path.join(bpaths.root(), WITNESS)


def _seeded() -> dict:
    d = store.read_json(witness_path(), {})
    keys = d.get("keys") if isinstance(d, dict) else None
    return dict(keys) if isinstance(keys, dict) else {}


def shipped_keys() -> list:
    try:
        names = sorted(os.listdir(SHIPPED_DIR))
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and os.path.isfile(os.path.join(SHIPPED_DIR, n, "expert.json"))]


def _existing(key: str):
    """An expert already carrying this shipped key (a seed whose witness write did not happen)."""
    return next((b for b in store.all_experts() if store.manifest(b)["shipped"] == key), None)


def _copy(src: str, dst: str, overwrite: bool) -> None:
    """instructions.md and skills/*.md from `src` into `dst`; `overwrite` False copies only what is missing."""
    try:
        skills = sorted(n for n in os.listdir(os.path.join(src, "skills")) if n.endswith(".md"))
    except OSError:
        skills = []
    for rel in ["instructions.md"] + [os.path.join("skills", n) for n in skills]:
        to = os.path.join(dst, rel)
        if overwrite or not os.path.exists(to):
            os.makedirs(os.path.dirname(to), exist_ok=True)
            shutil.copyfile(os.path.join(src, rel), to)


def _canvas_folder() -> str:
    from fused_render import canvases
    return canvases.canvases_base()


def seed_one(key: str):
    """Install shipped expert `key` (or finish one a cut-short seed left). Returns the Bot."""
    from fused_render.bots import bot as bm
    src = os.path.join(SHIPPED_DIR, key)
    with open(os.path.join(src, "expert.json"), encoding="utf-8") as f:
        spec = json.load(f)
    b = _existing(key)
    if b is not None:
        _copy(src, store.expert_dir(b), overwrite=False)
        return b
    b = bm.create(name=store.line(spec.get("name") or key, 80), kind="expert", greet=False,
                  expert={"ask_me_about": store.line(spec.get("ask_me_about")), "shipped": key})
    _copy(src, store.expert_dir(b), overwrite=True)  # over the default instructions.md create() laid out
    if isinstance(spec.get("face"), dict):
        b.meta["face"] = bm.check_face(b.meta, spec["face"])
    b.meta["folders"] = bm.clean_folders([_canvas_folder()])
    b.save()
    return b


def seed() -> list:
    """Install every shipped expert not installed before. Returns the keys installed now. Never raises."""
    done = _seeded()
    out = []
    for key in shipped_keys():
        if key in done:
            continue
        try:
            seed_one(key)
        except Exception:  # noqa: BLE001 — the next start tries again
            logger.warning("experts seed: %s failed", key, exc_info=True)
            continue
        done[key] = time.time()
        store.write_json(witness_path(), {"keys": done})
        out.append(key)
    return out
