"""Skills feature (skills.md).

Read-only viewer of the user's non-plugin (local) skills under
skills/*/SKILL.md: name + description from YAML frontmatter, symlink status,
and a `bunx skills add` share command when the skill's origin is recorded.
Plugin-bundled skills are excluded (they aren't under CLAUDE_DIR/skills).

main(action=...):
  list -> {skills: [{slug, name, description, linked, source, shareCommand}]}
  open -> {ok}   params: slug  (reveal folder in OS explorer)
  create -> {ok, slug}  params: slug  (scaffolds skills/<slug>/SKILL.md)
  read   -> {ok, text, files}  params: slug, path (default SKILL.md)
            files: every file under the skill dir, relative paths
  write  -> {ok}  params: slug, path, text
  delete -> {ok}  params: slug  (removes the skill dir, or just the link)
"""
import os
import re
import shutil
from typing import Optional

from . import lib

SKILLS_DIR = os.path.join(lib.CLAUDE_DIR, "skills")
# The `skills` CLI lockfile records each managed skill's origin. It lives in the
# .agents sibling of CLAUDE_DIR (what skills/<slug> symlinks target), so a scratch
# CLAUDE_DIR isolates tests (sharing.md §4).
SKILL_LOCK_PATH = os.path.join(lib.CLAUDE_DIR, "..", ".agents", ".skill-lock.json")


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
            fm = lib.parse_frontmatter(f.read())
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


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _files(root: str) -> list:
    out = []
    for dirpath, _dirs, names in os.walk(root, followlinks=True):
        for n in names:
            out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return sorted(out)


def _skill_file(slug: str, path: str) -> str:
    """skills/<slug>/<path>, guarded against escaping the skill dir."""
    root = lib.safe_subdir(SKILLS_DIR, slug)
    target = os.path.normpath(os.path.join(root, path or "SKILL.md"))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"invalid path: {path!r}")
    return target


def main(action: str = "list", slug: str = "", path: str = "", text: str = "") -> dict:
    if action == "list":
        return _list()

    if action == "open":
        try:
            lib.reveal(lib.safe_subdir(SKILLS_DIR, slug))
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    try:
        if action == "create":
            if not _SLUG_RE.match(slug):
                return {"ok": False, "error": "use lowercase letters, digits, - and _"}
            entry = lib.safe_subdir(SKILLS_DIR, slug)
            if os.path.exists(entry):
                return {"ok": False, "error": f"skill {slug} already exists"}
            with lib.config_lock():
                lib.write_text(
                    os.path.join(entry, "SKILL.md"),
                    f"---\nname: {slug}\ndescription: Describe when to use this skill.\n---\n\n# {slug}\n",
                )
            return {"ok": True, "slug": slug}

        if action == "read":
            target = _skill_file(slug, path)
            with open(target, encoding="utf-8") as f:
                body = f.read()
            return {"ok": True, "text": body,
                    "files": _files(lib.safe_subdir(SKILLS_DIR, slug))}

        if action == "write":
            target = _skill_file(slug, path)
            with lib.config_lock():
                lib.write_text(target, text)
            return {"ok": True}

        if action == "delete":
            entry = lib.safe_subdir(SKILLS_DIR, slug)
            with lib.config_lock():
                if os.path.islink(entry):
                    os.remove(entry)
                else:
                    shutil.rmtree(entry)
            return {"ok": True}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown action: {action}"}
