"""Shipped experts (docs/experts.md "Shipped expert").

Each folder under `SHIPPED_DIR` is one expert: `expert.json` (`{"name", "ask_me_about", "face"}`),
`instructions.md` and `skills/*.md`. `seed()` installs each ONCE: it creates the expert, copies the folder's files
into the expert's folder and grants it the canvas copies (`~/Fused/canvas`, canvases.canvases_base) through the
bot's `folders` setting. The witness `<bots root>/experts-seeded.json` lists the keys installed so far, so an
expert the user deletes stays deleted. A skill file ADDED to the shipped folder later is copied once into an
installed expert (`_new_skills`; the witness lists the shipped skill names each key has been offered, so one the
user deletes stays deleted). A shipped file that CHANGED replaces the installed copy only while that copy is still
what we shipped (`_resync`; the witness records the hash of each file as given): instructions.md or a skill the
user or anyone edited is theirs and is left alone.
Called by the bots owner after the Super Bot seed (registry._seed_experts).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid

from fused_render.experts import store

logger = logging.getLogger(__name__)

SHIPPED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipped")
WITNESS = "experts-seeded.json"


def witness_path() -> str:
    from fused_render.bots import paths as bpaths
    return os.path.join(bpaths.root(), WITNESS)


#: The skills each key shipped with before the witness recorded them: an install from then was offered exactly these.
FIRST_SKILLS = {"canvas-reference": ["canvas-folder-layout.md", "edges-and-folders.md", "finding-a-reference-canvas.md",
                                     "fused-cli-canvas-commands.md", "live-clone-vs-reference-copy.md"]}


#: Hashes (`_sha`) of every earlier shipped version of a file, for installs from before the witness recorded what
#: it gave: a copy matching one of these was never edited. Add a file's old hash here when you change the file.
PAST = {"canvas-reference": {
    "instructions.md": ["62c58da29d602c8f", "b4d386a0270a1a1a", "b189182d1246ad34", "1864272239c734c0",
                        "58f5ce3cce43c7b5", "705d28bc7c5754ef"],
    "skills/canvas-folder-layout.md": ["dcc8a9606ee6d28d", "38953af0627ba825"],
    "skills/finding-a-reference-canvas.md": ["2c24bd024c045947"],
    "skills/live-clone-vs-reference-copy.md": ["0a7634eb8be02203", "ffbeda78861daf1a"],
    "skills/running-udfs.md": ["ee874333d2b5d028", "7d46c614a4e8a72a", "05172db74852c1cc"]}}


def _witness() -> tuple:
    """`(keys, skills, given)`: `{key: when installed}`, `{key: [shipped skill names it has been offered]}` and
    `{key: {file: hash of that file as we last gave it}}`."""
    d = store.read_json(witness_path(), {})
    d = d if isinstance(d, dict) else {}
    return tuple(dict(d[k]) if isinstance(d.get(k), dict) else {} for k in ("keys", "skills", "given"))


def _seeded() -> dict:
    return _witness()[0]


def _sha(path: str) -> str:
    """A short content hash of the file, "" when it is not there."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def _put(src: str, to: str) -> None:
    """Copy through a temp sibling and a rename: a copy cut short never leaves a partial `to`."""
    os.makedirs(os.path.dirname(to), exist_ok=True)
    tmp = f"{to}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, to)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _files(key: str) -> list:
    return ["instructions.md"] + ["skills/" + n for n in _skill_names(key)]


def _resync(key: str, given: dict) -> dict:
    """Replace each installed file of `key` whose shipped version changed, when the installed copy is still what
    we shipped (its hash is the one recorded in `given`, or a known earlier version, `PAST`). An edited copy is
    left alone, and so is a missing one (a deleted skill stays deleted). Returns `given` brought up to date."""
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
            if have not in {given.get(rel), *PAST.get(key, {}).get(rel, [])}:
                continue  # edited: theirs
            _put(src, to)
            logger.info("experts seed: %s/%s updated to the shipped version", key, rel)
        given[rel] = new
    return given


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
            _put(os.path.join(src, rel), to)  # never a partial `to` a later non-overwriting pass would keep


def _canvas_folder() -> str:
    from fused_render import canvases
    return canvases.canvases_base()


def seed_one(key: str):
    """Install shipped expert `key`, or finish one a cut-short seed left (an expert with this shipped key but no
    witness entry): files it lacks are copied, an instructions.md still the default one is replaced, and a face
    or folders it has none of are set. Nothing the user set is overwritten. Returns the Bot."""
    from fused_render.bots import bot as bm
    src = os.path.join(SHIPPED_DIR, key)
    with open(os.path.join(src, "expert.json"), encoding="utf-8") as f:
        spec = json.load(f)
    b = _existing(key)
    if b is not None:
        d = store.expert_dir(b)
        if store.read_text(os.path.join(d, store.INSTRUCTIONS)) == store.DEFAULT_INSTRUCTIONS:
            os.remove(os.path.join(d, store.INSTRUCTIONS))  # create() laid it out; the copy was cut short
        _copy(src, d, overwrite=False)
    else:
        b = bm.create(name=store.line(spec.get("name") or key, 80), kind="expert", greet=False,
                      expert={"ask_me_about": store.line(spec.get("ask_me_about")), "shipped": key})
        _copy(src, store.expert_dir(b), overwrite=True)  # over the default instructions.md create() laid out
    if isinstance(spec.get("face"), dict) and not b.meta.get("face"):
        b.meta["face"] = bm.check_face(b.meta, spec["face"])
    if not b.meta.get("folders"):
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
            if key in done:
                offered = skills.get(key) if isinstance(skills.get(key), list) else FIRST_SKILLS.get(key, [])
                _new_skills(key, offered)
            else:
                seed_one(key)
                done[key] = time.time()
                out.append(key)
            given[key] = _resync(key, given.get(key) if isinstance(given.get(key), dict) else {})
        except Exception:  # noqa: BLE001 — the next start tries again
            logger.warning("experts seed: %s failed", key, exc_info=True)
            continue
        skills[key] = _skill_names(key)
    if json.dumps([done, skills, given], sort_keys=True) != before:
        store.write_json(witness_path(), {"keys": done, "skills": skills, "given": given})
    return out
