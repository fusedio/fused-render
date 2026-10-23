"""Skills feature (skills.md).

Every skill available to the user: their own (source "user") and those of each
enabled installed plugin (source "plugin", read-only), plus the skills the
cloned marketplaces offer (`available`). name + description come from YAML
frontmatter.

The user's own skills live in the dirs the three CLIs read:

  ~/.claude/skills            Claude
  ~/.agents/skills            Copilot and Codex
  ~/.copilot/skills           Copilot   (COPILOT_HOME moves it)
  ~/.codex/skills             Codex     (CODEX_HOME moves it)

A skill reachable from several of them by symlink is ONE row whose `readers`
are the union. Dot-dirs (Codex's bundled .system) are not the user's.

Adding or creating a skill puts ONE copy where every enabled CLI reads it:
~/.agents/skills when Copilot or Codex is on, with a link from ~/.claude/skills
when Claude is on too (Claude does not read .agents; Copilot does not read
.claude), and ~/.claude/skills alone when only Claude is on.

A skill is addressed by source + plugin + slug (+ location for a user skill,
the dir it lives in): slugs collide across plugins, between a user skill and a
plugin skill, and between the user dirs. Plugin skills resolve through
installed_plugins.json's installPath, never a path from the caller.

main(action=...):
  list -> {skills: [{slug, name, description, source, plugin?, marketplace?,
                     linked, path, location, paths, readers}]}
          readers: ["claude" | "copilot" | "codex"]; paths: every entry (copy
          and link) that is this skill
  available -> {skills: [{slug, name, description, plugin, pluginDescription,
                          marketplace, path, pluginInstalled, added}], skipped}
          the skills of every registered marketplace's plugins that live inside
          its clone; skipped counts the plugins whose source is elsewhere.
          pluginInstalled: the plugin is installed and enabled; added: the slug
          is already in a user skills dir
  add -> {ok, slug, path}  params: marketplace, plugin, slug  (copies a
          marketplace skill to where the enabled CLIs read it)
  open -> {ok}   params: slug [, source, plugin, marketplace, location]  (reveal folder)
  create -> {ok, slug}  params: slug  (scaffolds SKILL.md where add would put it)
  read   -> {ok, text, files}  params: slug, path (default SKILL.md) [, source, plugin, marketplace, location]
            files: every file under the skill dir, relative paths
  write  -> {ok}  params: slug, path, text [, location]  (user skills only)
  delete -> {ok, removed}  params: slug [, location]  (user skills only; removes
            every copy and link of it across the user dirs)
"""
import os
import re
import shutil

from fused_render import codex_health, copilot_health
from fused_render.shell import prefs

from . import lib, marketplaces, plugins

SKILLS_DIR = os.path.join(lib.CLAUDE_DIR, "skills")

_CLIS = ("claude", "copilot", "codex")


# A plugin ships its skills across at most a few dozen dirs; the walk budget
# only has to stop a runaway tree, so it is far above the Plugins panel's.
_PLUGIN_WALK_CAP = 5000


def _agents_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".agents", "skills")


def _user_dirs() -> list:
    """[(dir, readers)] — each dir the user's own skills live in and the CLIs
    that read it, in the order a row's readers are listed."""
    dirs = [
        (SKILLS_DIR, ["claude"]),
        (_agents_dir(), ["copilot", "codex"]),
        (os.path.join(copilot_health.config_dir(), "skills"), ["copilot"]),
        (os.path.join(codex_health.config_dir(), "skills"), ["codex"]),
    ]
    merged: dict = {}
    for path, readers in dirs:
        key = os.path.realpath(path)
        have = merged.setdefault(key, (path, []))[1]
        have.extend(r for r in readers if r not in have)
    return list(merged.values())


def _entries(directory: str) -> list:
    """[(slug, path)] — each skill in one dir: a dir (or a link to one) holding
    a SKILL.md, dot-names skipped."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        (n, os.path.join(directory, n)) for n in names
        if not n.startswith(".") and os.path.isfile(os.path.join(directory, n, "SKILL.md"))
    ]


def _user_skills() -> list:
    # One skill is one real directory, however many of the dirs link to it.
    groups: dict = {}
    for directory, readers in _user_dirs():
        for _slug, entry in _entries(directory):
            groups.setdefault(os.path.realpath(entry), []).append((directory, entry, readers))
    out = []
    for members in groups.values():
        primary = next((m for m in members if not os.path.islink(m[1])), members[0])
        directory, entry, _ = primary
        fm = plugins._read_frontmatter(os.path.join(entry, "SKILL.md"))
        slug = os.path.basename(entry)
        readers = [c for c in _CLIS if any(c in m[2] for m in members)]
        out.append({
            "slug": slug,
            "name": fm.get("name") or slug,
            "description": fm.get("description") or "",
            "source": "user",
            "linked": any(os.path.islink(m[1]) for m in members),
            "path": entry,
            "location": directory,
            "paths": [m[1] for m in members],
            "readers": readers,
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
            "location": os.path.dirname(skill_dir),
            "paths": [skill_dir],
            "readers": ["claude"],
        })
    return out


def _list() -> dict:
    skills = _user_skills()
    for name, marketplace, root in _enabled_plugin_roots():
        skills.extend(_plugin_skills(name, marketplace, root))
    return {"skills": skills}


def _skill_dir(slug: str, source: str, plugin: str, marketplace: str, location: str = "") -> str:
    """The directory of one skill. A plugin skill is looked up in that
    plugin's own listing, so the path always lives under its installPath; a
    user skill lives in one of the user dirs, named by `location` or found by
    slug."""
    if source == "user":
        dirs = [d for d, _ in _user_dirs()]
        if location:
            want = os.path.realpath(location)
            for d in dirs:
                if os.path.realpath(d) == want:
                    return lib.safe_subdir(d, slug)
            raise ValueError(f"not a skills directory: {location!r}")
        for d in dirs:
            found = lib.safe_subdir(d, slug)
            if os.path.isfile(os.path.join(found, "SKILL.md")):
                return found
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


# -- the marketplaces' skills ---------------------------------------------------


def _marketplace_plugins() -> tuple:
    """([(marketplace, plugin, root, description)], skipped) over every
    registered marketplace that is cloned. A stray directory under the clones
    dir that no settings or known_marketplaces entry names is not a marketplace.

    Only a plugin whose catalog `source` is a relative path inside the clone is
    listed: a github/git source is not on this machine until it is installed,
    and counts toward `skipped`. The clone is third-party content, so the root
    must resolve inside it."""
    try:
        cloned = set(os.listdir(lib.MARKETPLACES_DIR))
    except OSError:
        return [], 0
    registered = {m["name"] for m in marketplaces._list()["marketplaces"]}
    found, skipped = [], 0
    for mkt in sorted(cloned & registered):
        mkt_dir = os.path.join(lib.MARKETPLACES_DIR, mkt)
        if lib.option_shaped(mkt) or not os.path.isdir(mkt_dir):
            continue
        for entry in plugins._read_catalog(mkt_dir) or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name or lib.option_shaped(name):
                continue
            src = entry.get("source")
            root = os.path.normpath(os.path.join(mkt_dir, src)) if (
                isinstance(src, str) and (src == "." or src.startswith("./"))) else None
            if root is None or not plugins._within(mkt_dir, root) or not os.path.isdir(root):
                skipped += 1
                continue
            desc = entry.get("description")
            found.append((mkt, name, root, desc if isinstance(desc, str) else ""))
    return found, skipped


def _marketplace_skills(root: str) -> list:
    """[(slug, dir, name, description)] — what one cloned plugin ships."""
    budget = plugins._WalkBudget(_PLUGIN_WALK_CAP)
    out = []
    for item in plugins._skills(root, plugins._manifest(root), budget):
        skill_dir = os.path.dirname(item["path"])
        if plugins._within(root, skill_dir):
            out.append((os.path.basename(skill_dir), skill_dir, item["name"], item["description"]))
    return out


def _available() -> dict:
    found, skipped = _marketplace_plugins()
    on = {(n, m) for n, m, _ in _enabled_plugin_roots()}
    taken = {slug for d, _ in _user_dirs() for slug, _p in _entries(d)}
    out = []
    for mkt, plugin, root, plugin_desc in found:
        for slug, skill_dir, name, description in _marketplace_skills(root):
            out.append({
                "slug": slug,
                "name": name,
                "description": description,
                "plugin": plugin,
                "pluginDescription": plugin_desc,
                "marketplace": mkt,
                "path": skill_dir,
                "pluginInstalled": (plugin, mkt) in on,
                "added": slug in taken,
            })
    out.sort(key=lambda s: (s["name"].lower(), s["plugin"], s["marketplace"]))
    return {"skills": out, "skipped": skipped}


# -- where a new skill goes ------------------------------------------------------

# A skill is a prompt and a few reference files; this stops a mislabelled tree
# (a whole repo as one "skill") from being copied into the user's home.
_COPY_MAX_FILES = 500
_COPY_MAX_BYTES = 25 * 1024 * 1024
_COPY_SKIP_DIRS = {".git", "node_modules", "__pycache__"}


def _enabled_clis() -> list:
    return [c for c in _CLIS if prefs.backend_enabled(c)]


def _placement(slug: str) -> tuple:
    """(home, link): where the one copy of a new skill lives, and the link
    that lets Claude read it from there (None when none is needed)."""
    on = _enabled_clis()
    if "copilot" in on or "codex" in on:
        return (lib.safe_subdir(_agents_dir(), slug),
                lib.safe_subdir(SKILLS_DIR, slug) if "claude" in on else None)
    return lib.safe_subdir(SKILLS_DIR, slug), None


def _copy_skill(src: str, dst: str) -> None:
    """Copy one skill dir. Symlinks are skipped, not followed: a skill from a
    clone must not pull files from outside it."""
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [
            d for d in dirnames
            if d not in _COPY_SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
        ]
        out = os.path.normpath(os.path.join(dst, os.path.relpath(dirpath, src)))
        os.makedirs(out, exist_ok=True)
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            files += 1
            size += os.path.getsize(path)
            if files > _COPY_MAX_FILES or size > _COPY_MAX_BYTES:
                raise ValueError("skill is too large to add")
            shutil.copy2(path, os.path.join(out, fn))


def _place(slug: str, fill) -> dict:
    """Create the skill `slug` where the enabled CLIs read it; `fill(dir)`
    writes its files into a fresh dir that is moved into place whole."""
    home, link = _placement(slug)
    if any(os.path.lexists(os.path.join(d, slug)) for d, _ in _user_dirs()):
        return {"ok": False, "error": f"skill {slug} already exists"}
    parent = os.path.dirname(home)
    os.makedirs(parent, exist_ok=True)
    staging = os.path.join(parent, f".adding-{slug}-{os.getpid()}")
    try:
        os.mkdir(staging)
        fill(staging)
        os.rename(staging, home)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if link:
        try:
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(home, link)
        except OSError:
            shutil.rmtree(home, ignore_errors=True)
            raise
    return {"ok": True, "slug": slug, "path": home}


def _add(marketplace: str, plugin: str, slug: str) -> dict:
    found, _ = _marketplace_plugins()
    for mkt, name, root, _d in found:
        if mkt != marketplace or name != plugin:
            continue
        for s, skill_dir, _n, _d in _marketplace_skills(root):
            if s == slug:
                return _place(slug, lambda dst: _copy_skill(skill_dir, dst))
    return {"ok": False, "error": f"no skill {slug!r} in {plugin}@{marketplace}"}


def _remove(root: str) -> list:
    """Remove a user skill: its dir and every link to it in the user dirs.
    A link whose target lives outside them removes only the link."""
    real = os.path.realpath(root)
    entries = {root}
    for d, _ in _user_dirs():
        entries.update(p for _s, p in _entries(d) if os.path.realpath(p) == real)
    removed = []
    for path in sorted(entries, key=lambda p: not os.path.islink(p)):
        if os.path.islink(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            continue
        removed.append(path)
    return removed


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
         source: str = "user", plugin: str = "", marketplace: str = "",
         location: str = "") -> dict:
    if action == "list":
        return _list()

    if action == "available":
        return _available()

    try:
        if action == "open":
            lib.reveal(_skill_dir(slug, source, plugin, marketplace, location))
            return {"ok": True}

        if action == "create":
            if not _SLUG_RE.match(slug):
                return {"ok": False, "error": "use lowercase letters, digits, - and _"}
            scaffold = f"---\nname: {slug}\ndescription: Describe when to use this skill.\n---\n\n# {slug}\n"
            with lib.config_lock():
                return _place(slug, lambda dst: lib.write_text(os.path.join(dst, "SKILL.md"), scaffold))

        if action == "add":
            with lib.config_lock():
                return _add(marketplace, plugin, slug)

        if action == "read":
            root = _skill_dir(slug, source, plugin, marketplace, location)
            with open(_skill_file(root, path), encoding="utf-8") as f:
                body = f.read()
            return {"ok": True, "text": body, "files": _files(root)}

        if action in ("write", "delete"):
            if source != "user":
                return {"ok": False,
                        "error": f"{plugin or 'plugin'} skills are read-only; the plugin owns them"}
            root = _skill_dir(slug, source, plugin, marketplace, location)
            with lib.config_lock():
                if action == "write":
                    lib.write_text(_skill_file(root, path), text)
                    return {"ok": True}
                return {"ok": True, "removed": _remove(root)}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown action: {action}"}
