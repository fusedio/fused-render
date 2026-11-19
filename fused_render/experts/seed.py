"""Shipped experts (docs/experts.md "Shipped expert").

Each folder under `SHIPPED_DIR` is one expert: `expert.json` (`{"name", "ask_me_about", "face"}`),
`instructions.md` and `skills/*.md`. `seed()` installs each ONCE: it creates the expert, copies the folder's files
into the expert's folder and grants it the canvas copies (`~/Fused/canvas`, canvases.canvases_base) through the
bot's `folders` setting. The witness `<bots root>/experts-seeded.json` lists the keys installed so far, so an
expert the user deletes stays deleted. A skill file ADDED to the shipped folder later is copied once into an
installed expert (`_new_skills`; the witness lists the shipped skill names each key has been offered, so one the
user deletes stays deleted). A shipped file that CHANGED replaces the installed copy only while that copy is still
what we gave (`_resync`; the witness records the hash of each file, and the description, as given): what the
user or anyone edited is theirs and is left alone, and so is anything the witness has no record of.
Called by the bots owner after the Super Bot seed (registry._seed_experts).
"""
from __future__ import annotations

import hashlib
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


ABOUT = "ask_me_about"  # in the witness's `given`, beside the files: the description as we last gave it


def _witness() -> tuple:
    """`(keys, skills, given)`: `{key: when installed}`, `{key: [shipped skill names it has been offered]}` and
    `{key: {file: hash of that file as we last gave it, "ask_me_about": the description as we last gave it}}`."""
    d = store.read_json(witness_path(), {})
    d = d if isinstance(d, dict) else {}
    return tuple(dict(d[k]) if isinstance(d.get(k), dict) else {} for k in ("keys", "skills", "given"))


def _sha(path: str) -> str:
    """A short content hash of the file, "" when it is not there."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def _put(src: str, to: str) -> None:
    os.makedirs(os.path.dirname(to), exist_ok=True)
    shutil.copyfile(src, to)


def _files(key: str) -> list:
    return ["instructions.md"] + ["skills/" + n for n in _skill_names(key)]


def _resync(key: str, given: dict) -> dict:
    """Replace each installed file of `key` whose shipped version changed, when the installed copy is still what
    we gave (its hash is the one recorded in `given`). An edited copy is left alone, and so is a missing one (a
    deleted skill stays deleted). The description (`ask_me_about` in the shipped expert.json) follows the same
    rule. Returns `given` brought up to date."""
    b = _existing(key)
    if b is None:
        return given
    given = dict(given)
    for rel in _files(key):
        src, to = os.path.join(SHIPPED_DIR, key, rel), os.path.join(store.expert_dir(b), rel)
        new, have = _sha(src), _sha(to)
        if not have or not new:
            continue
        if have != new:
            if have != given.get(rel):
                continue  # edited, or never recorded: theirs
            _put(src, to)
            logger.info("experts seed: %s/%s updated to the shipped version", key, rel)
        given[rel] = new
    with open(os.path.join(SHIPPED_DIR, key, "expert.json"), encoding="utf-8") as f:
        new = store.line(json.load(f).get(ABOUT))
    have = store.manifest(b)[ABOUT]
    if new and have != new and have == given.get(ABOUT):
        b.meta["expert"] = {**(b.meta.get("expert") or {}), ABOUT: new}
        b.save()
        have = new
    if have == new:
        given[ABOUT] = new
    return given


def follows_shipped(bot, rel: str = "instructions.md") -> bool:
    """The installed `rel` of a shipped expert is still the copy we gave, so `_resync` replaces it when the
    shipped file changes. False once it was edited, and for an expert that is not a shipped one."""
    key = store.manifest(bot)["shipped"]
    have = _sha(os.path.join(store.expert_dir(bot), rel))
    if not key or not have:
        return False
    return have in {_sha(os.path.join(SHIPPED_DIR, key, rel)), _witness()[2].get(key, {}).get(rel)}


def _skill_names(key: str) -> list:
    try:
        return sorted(n for n in os.listdir(os.path.join(SHIPPED_DIR, key, "skills")) if n.endswith(".md"))
    except OSError:
        return []


def _new_skills(key: str, offered: list) -> list:
    """Copy the shipped skills of `key` that were added since it was installed (not in `offered`) into the
    installed expert, never over a file it has. Returns their names; nothing when the expert was deleted."""
    new = [n for n in _skill_names(key) if n not in offered]
    b = _existing(key) if new else None
    if b is None:
        return []
    for n in new:
        to = os.path.join(store.expert_dir(b), "skills", n)
        if not os.path.exists(to):
            _put(os.path.join(SHIPPED_DIR, key, "skills", n), to)
    return new


def shipped_keys() -> list:
    try:
        names = sorted(os.listdir(SHIPPED_DIR))
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and os.path.isfile(os.path.join(SHIPPED_DIR, n, "expert.json"))]


def _existing(key: str):
    """The installed expert carrying this shipped key, or None."""
    return next((b for b in store.all_experts() if store.manifest(b)["shipped"] == key), None)


def _canvas_folder() -> str:
    from fused_render import canvases
    return canvases.canvases_base()


def seed_one(key: str):
    """Install shipped expert `key` and return the Bot. An expert that already carries the key (a seed that ended
    before the witness was written) is returned as it is: never a second one."""
    from fused_render.bots import bot as bm
    b = _existing(key)
    if b is not None:
        return b
    with open(os.path.join(SHIPPED_DIR, key, "expert.json"), encoding="utf-8") as f:
        spec = json.load(f)
    b = bm.create(name=store.line(spec.get("name") or key, 80), kind="expert", greet=False,
                  expert={"ask_me_about": store.line(spec.get("ask_me_about")), "shipped": key})
    for rel in _files(key):  # instructions.md over the default one create() laid out
        _put(os.path.join(SHIPPED_DIR, key, rel), os.path.join(store.expert_dir(b), rel))
    if isinstance(spec.get("face"), dict):
        b.meta["face"] = bm.check_face(b.meta, spec["face"])
    b.meta["folders"] = bm.clean_folders([_canvas_folder()])
    b.save()
    return b


def seed() -> list:
    """Install every shipped expert not installed before; hand an installed one the shipped skills added since
    (`_new_skills`) and the new version of each shipped file it has not edited (`_resync`). Returns the keys
    installed now. Never raises."""
    done, skills, given = _witness()
    before = json.dumps([done, skills, given], sort_keys=True)
    out = []
    for key in shipped_keys():
        try:
            if key not in done:
                seed_one(key)
                done[key] = time.time()
                out.append(key)
            elif isinstance(skills.get(key), list):  # no record of what was offered: nothing is offered
                _new_skills(key, skills[key])
            given[key] = _resync(key, given.get(key) if isinstance(given.get(key), dict) else {})
        except Exception:  # noqa: BLE001 — the next start tries again
            logger.warning("experts seed: %s failed", key, exc_info=True)
            continue
        skills[key] = _skill_names(key)
    if json.dumps([done, skills, given], sort_keys=True) != before:
        store.write_json(witness_path(), {"keys": done, "skills": skills, "given": given})
    return out
