"""Shipped experts (docs/experts.md §4 `seed.py`).

Each folder under `SHIPPED_DIR` is one expert: `expert.json` (`{"name",
"manifest", "face"?}`), `instructions.md`, `skills/*.md` (the skills format:
`# Title`, `trigger: a, b`, blank line, body) and `questions.md` (the quality
gate's questions). `seed()` creates each one ONCE: the witness
`<home>/bots/experts-seeded.json` lists the keys seeded so far, so an expert the
user deletes stays deleted. Called by the bots owner right after the Super Bot
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


def _seed_one(key: str):
    from fused_render.bots import bot as bm
    src = os.path.join(SHIPPED_DIR, key)
    with open(os.path.join(src, "expert.json"), encoding="utf-8") as f:
        spec = json.load(f)
    if not isinstance(spec, dict):
        raise ValueError(f"{key}/expert.json is not an object")
    manifest = dict(spec.get("manifest") or {}) if isinstance(spec.get("manifest"), dict) else {}
    manifest["shipped"] = key
    name = " ".join(str(spec.get("name") or key).split())[:80]
    instructions = _read_text(os.path.join(src, "instructions.md")).strip()
    b = bm.create(name=name, instructions=instructions, kind="expert", greet=False, expert=manifest)
    face = spec.get("face")
    if isinstance(face, dict):
        with b.lock:
            b.meta["face"] = {k: face[k] for k in ("shape", "color", "icon") if k in face}
            b.save()
    skills_dir = os.path.join(src, "skills")
    try:
        names = sorted(n for n in os.listdir(skills_dir) if n.endswith(".md"))
    except OSError:
        names = []
    for n in names:
        try:
            title, trigger, body = bm.Bot._parse_skill(_read_text(os.path.join(skills_dir, n)))
            b.skill_save(title or n[:-3], trigger, body, name=n[:-3])
        except Exception:  # noqa: BLE001 — one bad skill file never loses the expert
            logger.warning("expert seed %s: skill %s not copied", key, n, exc_info=True)
    q = os.path.join(src, "questions.md")
    if os.path.isfile(q):
        try:
            shutil.copyfile(q, os.path.join(b.public_dir, "questions.md"))
        except OSError:
            logger.warning("expert seed %s: questions.md not copied", key, exc_info=True)
    return b


def seed() -> list[str]:
    """Create every shipped expert not seeded before. Returns the ids created now."""
    created: list[str] = []
    keys = shipped_keys()
    if not keys:
        return created
    seeded = _read_witness()
    for key in keys:
        if key in seeded:
            continue
        bid = _existing(key)
        if bid is None:
            try:
                b = _seed_one(key)
            except Exception:  # noqa: BLE001 — the next start tries again (no witness row)
                logger.warning("expert seed %s failed", key, exc_info=True)
                continue
            bid = b.id
            created.append(bid)
        seeded[key] = {"id": bid, "at": time.time()}
        try:
            _write_witness(seeded)
        except OSError:
            logger.warning("could not write %s", witness_path(), exc_info=True)
    return created
