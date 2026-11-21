"""The specialists the user keeps: one home folder each under `paths.root()`, its manifest `specialist.json` the
record. Nothing is cached: every read is a file read.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass

from fused_render.specialists import history, paths
from fused_render.specialists import home as hm

NAME_MAX = 80
ABOUT_MAX = 300
_LOCK = threading.Lock()


@dataclass
class Specialist:
    id: str
    name: str
    about: str
    folder: str
    face: dict | None
    home: str

    def manifest(self) -> dict:
        return {"id": self.id, "name": self.name, "about": self.about, "folder": self.folder, "face": self.face}


class Invalid(ValueError):
    """A create/update argument the user must fix; its message is the sentence the route answers."""


class Taken(Invalid):
    pass


def _load(folder: str) -> Specialist | None:
    m = hm.read_json(os.path.join(folder, paths.MANIFEST), None)
    if not isinstance(m, dict) or not m.get("id"):
        return None
    return Specialist(id=m["id"], name=m.get("name") or m["id"], about=m.get("about") or "",
                      folder=m.get("folder") or "", face=m.get("face") or None, home=folder)


def all() -> list[Specialist]:  # noqa: A001
    found = (_load(f) for f in paths.scan().values())
    return sorted((s for s in found if s), key=lambda s: s.name.lower())


def get(spec_id: str) -> Specialist | None:
    s = _load(paths.home(spec_id)) if spec_id else None
    return s if s and s.id == spec_id else None


def find(key: str) -> Specialist | None:
    """By id, then by name (case-insensitive, whitespace folded)."""
    key = " ".join((key or "").split())
    return get(key) or next((s for s in all() if s.name.lower() == key.lower()), None) if key else None


def for_folder(path: str) -> list[str]:
    """Names of the specialists whose folder holds `path`."""
    p = os.path.normpath(os.path.abspath(path or ""))
    return [s.name for s in all() if s.folder and (p == s.folder or p.startswith(s.folder.rstrip(os.sep) + os.sep))]


def _clean(fields: dict, own_id: str = "") -> dict:
    out = {}
    if "name" in fields:
        out["name"] = " ".join(str(fields["name"] or "").split())[:NAME_MAX]
        if not out["name"]:
            raise Invalid("A specialist needs a name")
        other = find(out["name"])
        if other and other.id != own_id:
            raise Taken(f"A specialist named {out['name']} already exists")
    if "about" in fields:
        out["about"] = " ".join(str(fields["about"] or "").split())[:ABOUT_MAX]
        if not out["about"]:
            raise Invalid("Say what the specialist is an expert on")
    if "folder" in fields:
        f = os.path.expanduser(str(fields["folder"] or "").strip())
        if f and not os.path.isdir(f):
            raise Invalid(f"{f} is not a folder")
        out["folder"] = os.path.normpath(os.path.abspath(f)) if f else ""
    if "face" in fields:
        if fields["face"] is not None and not isinstance(fields["face"], dict):
            raise Invalid("'face' must be an object")
        out["face"] = fields["face"] or None
    return out


def create(name: str, about: str, folder: str = "", face: dict | None = None, *, subject: str = "",
           skills: dict[str, str] | None = None) -> Specialist:
    """A new home with its first commit. `subject` (a charter section) and `skills` (`{name: SKILL.md text}`) are
    the shipped Canvas specialist's content."""
    with _LOCK:
        f = _clean({"name": name, "about": about, "folder": folder, "face": face})
        slug = re.sub(r"[^a-z0-9]+", "-", f["name"].lower()).strip("-")[:48] or "specialist"
        home, n = os.path.join(paths.root(), slug), 2
        while os.path.exists(home):
            home, n = os.path.join(paths.root(), f"{slug}-{n}"), n + 1
        s = Specialist(id=uuid.uuid4().hex[:8], home=home, **f)
        os.makedirs(home)
        hm.lay_out(home, s.manifest(), subject)
        for skill, text in (skills or {}).items():
            hm.write_text(hm.skill_path(home, skill), text)
        paths._reset()
    history.init(home)
    return s


def update(spec_id: str, **fields) -> Specialist:
    s = get(spec_id)
    if s is None:
        raise KeyError(spec_id)
    for k, v in _clean(fields, s.id).items():
        setattr(s, k, v)
    hm.write_json(os.path.join(s.home, paths.MANIFEST), s.manifest())
    hm.rewrite_charter(s.home, s.name, s.about)
    history.commit(s.home, "Settings", [paths.MANIFEST, "CLAUDE.md"])
    return s


def live_runs(home: str) -> list[dict]:
    """Agent runs with a turn open in the home (main chat, breakouts, an update)."""
    from fused_render.claude_agent import agent_module

    return list(agent_module()._live_runs_in(home))


def delete(spec_id: str) -> None:
    """End every session in the home, then move it to `<root>/.state/trash/<id>-<ts>/` (recoverable by hand)."""
    s = get(spec_id)
    if s is None:
        raise KeyError(spec_id)
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    for run in agent._live_runs_in(s.home, idle=True):
        agent._cancel_run(run["run_id"], interrupt_first=False)
    trash = os.path.join(paths.state_root(), "trash")
    os.makedirs(trash, exist_ok=True)
    shutil.move(s.home, os.path.join(trash, f"{s.id}-{int(time.time())}"))
    paths._reset()
