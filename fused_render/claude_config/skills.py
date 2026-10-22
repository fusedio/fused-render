"""Skills feature (skills.md).

Every skill available to the user: their own under skills/*/SKILL.md
(source "user") and those of each enabled installed plugin (source "plugin",
read-only). name + description come from YAML frontmatter.

A skill is addressed by source + plugin + slug: slugs collide across plugins
and between a user skill and a plugin skill. Plugin skills resolve through
installed_plugins.json's installPath, never a path from the caller.

main(action=...):
  list -> {skills: [{slug, name, description, source, plugin?, marketplace?,
                     linked, path}]}
  open -> {ok}   params: slug [, source, plugin, marketplace]  (reveal folder)
  create -> {ok, slug}  params: slug  (scaffolds skills/<slug>/SKILL.md)
  read   -> {ok, text, files}  params: slug, path (default SKILL.md) [, source, plugin, marketplace]
            files: every file under the skill dir, relative paths
  write  -> {ok}  params: slug, path, text  (user skills only)
  delete -> {ok}  params: slug  (user skills only; removes the dir, or just the link)
"""
import os
import re
import shutil

from . import lib, plugins

SKILLS_DIR = os.path.join(lib.CLAUDE_DIR, "skills")


# A plugin ships its skills across at most a few dozen dirs; the walk budget
# only has to stop a runaway tree, so it is far above the Plugins panel's.
_PLUGIN_WALK_CAP = 5000


def _user_skills() -> list:
    if not os.path.isdir(SKILLS_DIR):
        return []
    out = []
    for name in os.listdir(SKILLS_DIR):
        entry = os.path.join(SKILLS_DIR, name)
        if not os.path.isdir(entry):  # follows symlinks; skips files
            continue
        skill_md = os.path.join(entry, "SKILL.md")
        if not os.path.isfile(skill_md):  # dangling link or non-skill dir
            continue
        fm = plugins._read_frontmatter(skill_md)
        out.append({
            "slug": name,
            "name": fm.get("name") or name,
            "description": fm.get("description") or "",
            "source": "user",
            "linked": os.path.islink(entry),
            "path": entry,
        })
    out.sort(key=lambda s: s["name"].lower())
    return out


def _enabled_plugin_roots() -> list:
    """[(display name, marketplace, installPath)] for each installed plugin
    switched on in settings.json, current install only."""
    enabled = lib.read_settings().get("enabledPlugins") or {}
    installed = lib.read_json(lib.INSTALLED_PLUGINS_PATH, {}).get("plugins") or {}
    out = []
    for pid in sorted(installed):
        rec = installed[pid]
        if not enabled.get(pid) or not isinstance(rec, list) or not rec:
            continue
        root = (rec[0] or {}).get("installPath")
        if not root or not os.path.isdir(root):
            continue
        name, _, marketplace = pid.partition("@")
        out.append((name, marketplace, os.path.normpath(root)))
    return out


def _plugin_skills(name: str, marketplace: str, root: str) -> list:
    manifest = plugins._manifest(root)
    budget = plugins._WalkBudget(_PLUGIN_WALK_CAP)
    out = []
    for item in plugins._skills(root, manifest, budget):
        skill_dir = os.path.dirname(item["path"])
        out.append({
            "slug": os.path.basename(skill_dir),
            "name": item["name"],
            "description": item["description"],
            "source": "plugin",
            "plugin": name,
            "marketplace": marketplace,
            "linked": False,
            "path": skill_dir,
        })
    return out


def _list() -> dict:
    skills = _user_skills()
    for name, marketplace, root in _enabled_plugin_roots():
        skills.extend(_plugin_skills(name, marketplace, root))
    return {"skills": skills}


def _skill_dir(slug: str, source: str, plugin: str, marketplace: str) -> str:
    """The directory of one skill. A plugin skill is looked up in that
    plugin's own listing, so the path always lives under its installPath."""
    if source == "user":
        return lib.safe_subdir(SKILLS_DIR, slug)
    if source != "plugin":
        raise ValueError(f"unknown skill source: {source!r}")
    for name, mkt, root in _enabled_plugin_roots():
        if name != plugin or (marketplace and mkt != marketplace):
            continue
        for s in _plugin_skills(name, mkt, root):
            if s["slug"] == slug and plugins._within(root, s["path"]):
                return s["path"]
    raise ValueError(f"no skill {slug!r} in plugin {plugin!r}")


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _files(root: str) -> list:
    out = []
    for dirpath, _dirs, names in os.walk(root, followlinks=True):
        for n in names:
            out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return sorted(out)


def _skill_file(root: str, path: str) -> str:
    """<skill dir>/<path>, guarded against escaping the skill dir."""
    target = os.path.normpath(os.path.join(root, path or "SKILL.md"))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"invalid path: {path!r}")
    return target


def main(action: str = "list", slug: str = "", path: str = "", text: str = "",
         source: str = "user", plugin: str = "", marketplace: str = "") -> dict:
    if action == "list":
        return _list()

    try:
        if action == "open":
            lib.reveal(_skill_dir(slug, source, plugin, marketplace))
            return {"ok": True}

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
            root = _skill_dir(slug, source, plugin, marketplace)
            with open(_skill_file(root, path), encoding="utf-8") as f:
                body = f.read()
            return {"ok": True, "text": body, "files": _files(root)}

        if action in ("write", "delete"):
            if source != "user":
                return {"ok": False,
                        "error": f"{plugin or 'plugin'} skills are read-only; the plugin owns them"}
            root = _skill_dir(slug, source, plugin, marketplace)
            with lib.config_lock():
                if action == "write":
                    lib.write_text(_skill_file(root, path), text)
                elif os.path.islink(root):
                    os.remove(root)
                else:
                    shutil.rmtree(root)
            return {"ok": True}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown action: {action}"}
