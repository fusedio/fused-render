"""Skills feature (skills.md).

Read-only viewer of the user's non-plugin (local) skills under
skills/*/SKILL.md: name + description from YAML frontmatter, symlink status,
and a `bunx skills add` share command when the skill's origin is recorded.
Plugin-bundled skills are excluded (they aren't under CLAUDE_DIR/skills).

main(action=...):
  list -> {skills: [{slug, name, description, linked, source, shareCommand}]}
  open -> {ok}   params: slug  (reveal folder in OS explorer)
"""
import os
from typing import Optional

from . import lib

SKILLS_DIR = os.path.join(lib.CLAUDE_DIR, "skills")
# The `skills` CLI lockfile records each managed skill's origin. It lives in the
# .agents sibling of CLAUDE_DIR (what skills/<slug> symlinks target), so a scratch
# CLAUDE_DIR isolates tests (sharing.md §4).
SKILL_LOCK_PATH = os.path.join(lib.CLAUDE_DIR, "..", ".agents", ".skill-lock.json")


def _parse_frontmatter(text: str) -> dict:
    """Extract name/description scalars from a leading --- ... --- YAML block.
    Minimal line parser, no YAML dep (skills.md §3)."""
    out = {"name": "", "description": ""}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    for line in text[3:end].split("\n"):
        key, sep, val = line.partition(":")
        key = key.strip()
        if not sep or key not in ("name", "description"):
            continue
        v = val.strip()
        if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
            v = v[1:-1]
        out[key] = v
    return out


def _skill_sources() -> dict:
    lock = lib.read_json(SKILL_LOCK_PATH, {})
    out = {}
    for slug, meta in (lock.get("skills") or {}).items():
        out[slug] = meta.get("source") or meta.get("sourceUrl")
    return out


def _share_command(slug: str, source: Optional[str]) -> Optional[str]:
    return f"bunx skills add {source} -s {slug} -g -y" if source else None


def _list() -> dict:
    if not os.path.isdir(SKILLS_DIR):
        return {"skills": []}
    sources = _skill_sources()
    skills = []
    for name in os.listdir(SKILLS_DIR):
        entry = os.path.join(SKILLS_DIR, name)
        if not os.path.isdir(entry):  # follows symlinks; skips files
            continue
        skill_md = os.path.join(entry, "SKILL.md")
        if not os.path.isfile(skill_md):  # dangling link or non-skill dir
            continue
        with open(skill_md, "r", encoding="utf-8") as f:
            fm = _parse_frontmatter(f.read())
        source = sources.get(name)
        skills.append({
            "slug": name,
            "name": fm["name"] or name,
            "description": fm["description"],
            "linked": os.path.islink(entry),
            "source": source,
            "shareCommand": _share_command(name, source),
        })
    skills.sort(key=lambda s: s["name"].lower())
    return {"skills": skills}


def main(action: str = "list", slug: str = "") -> dict:
    if action == "list":
        return _list()

    if action == "open":
        try:
            lib.reveal(lib.safe_subdir(SKILLS_DIR, slug))
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown action: {action}"}
