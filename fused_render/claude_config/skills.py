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
  ~/.gemini/skills            Gemini    (GEMINI_CLI_HOME moves it)

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
          readers: ["claude" | "copilot" | "codex" | "gemini"]; paths: every entry (copy
          and link) that is this skill
  available -> {skills: [{slug, name, description, plugin, pluginDescription,
                          marketplace, path, pluginInstalled, added}], skipped}
          the skills of every registered marketplace's plugins that live inside
          its clone; skipped counts the plugins whose source is elsewhere.
          pluginInstalled: the plugin is installed and enabled; added: the slug
          is already in a user skills dir
  add -> {ok, slug, path}  params: marketplace, plugin, slug  (copies a
          marketplace skill to where the enabled CLIs read it)
  preview -> {ok, text, files}  params: marketplace, plugin, slug  (a marketplace
            skill's SKILL.md and file list, resolved like add; read-only)
  search -> {ok, skills: [{id, source, skillId, name, installs, added}]}
            params: query  (skills.sh directory search, 30 hits; < 2 chars is an
            empty result with no request; network trouble is {ok: False, error})
  remote_preview -> {ok, text, files}  params: source (GitHub owner/repo), skill_id
            (the skill's SKILL.md and file list, located in the repo's tree)
  remote_add -> {ok, slug, path}  params: source, skill_id  (downloads that skill
            dir from GitHub and places it like add; the file paths come from the
            repo's tree, never from the caller)
  draft_check -> {ok, root, name, files}  params: path  (a folder holding SKILL.md or
            the SKILL.md itself, `~` allowed; {ok: False, error} when it is neither)
  draft_start -> {ok, staging}  params: [path]  (a fresh dir under the state dir's
            skill-drafts/ for Claude to write one skill into; `path` is copied in
            first as the starting point)
  draft_status -> {ok, files, text, name, description, problems}  params: staging
  draft_save -> {ok, slug, path, key}  params: staging, placement ("all" | "claude")
            (validates, places like create, then removes the staging dir; a
            failed validation is {ok: False, error, problems} and keeps it)
  draft_discard -> {ok}  params: staging
  draft_prompt -> {ok, prompt}  params: text [, adapting]  (the task prompt)
  open -> {ok}   params: slug [, source, plugin, marketplace, location]  (reveal folder)
  create -> {ok, slug}  params: slug  (scaffolds SKILL.md where add would put it)
  read   -> {ok, text, files}  params: slug, path (default SKILL.md) [, source, plugin, marketplace, location]
            files: every file under the skill dir, relative paths
  write  -> {ok}  params: slug, path, text [, location]  (user skills only)
  delete -> {ok, removed}  params: slug [, location]  (user skills only; removes
            every copy and link of it across the user dirs)
"""
import json
import os
import re
import secrets
import shutil
import time
import urllib.parse
import urllib.request

from fused_render import codex_health, copilot_health, gemini_health
from fused_render.shell import prefs, storage

from . import lib, marketplaces, plugins

SKILLS_DIR = os.path.join(lib.CLAUDE_DIR, "skills")

_CLIS = ("claude", "copilot", "codex", "gemini")


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
        # Gemini too: `discoverSkills` loads `~/.gemini/skills` AND the
        # shared `~/.agents/skills` (0.65.0 source, `getUserAgentSkillsDir`).
        (_agents_dir(), ["copilot", "codex", "gemini"]),
        (os.path.join(copilot_health.config_dir(), "skills"), ["copilot"]),
        (os.path.join(codex_health.config_dir(), "skills"), ["codex"]),
        (os.path.join(gemini_health.config_dir(), "skills"), ["gemini"]),
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


def _placement(slug: str, placement: str = "all") -> tuple:
    """(home, link): where the one copy of a new skill lives, and the link
    that lets Claude read it from there (None when none is needed).
    placement "claude" skips the shared dir: ~/.claude/skills alone."""
    if placement == "claude":
        return lib.safe_subdir(SKILLS_DIR, slug), None
    on = _enabled_clis()
    if "copilot" in on or "codex" in on or "gemini" in on:
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


def _place(slug: str, fill, placement: str = "all") -> dict:
    """Create the skill `slug` where the enabled CLIs read it; `fill(dir)`
    writes its files into a fresh dir that is moved into place whole."""
    home, link = _placement(slug, placement)
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


# -- skills.sh + GitHub: skills that are not on this machine -----------------------

_HTTP_TIMEOUT = 10
_SEARCH_LIMIT = 30
_TREE_TTL = 600
_FRONTMATTER_PROBES = 40
_SOURCE_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_trees: dict = {}  # source -> (fetched at, [(path, size)])


def _http_get(url: str) -> bytes:
    """GET `url`; network and HTTP errors surface as OSError."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "fused-render", "Accept": "application/json, text/plain, */*"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return r.read()


def _search(query: str) -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        return {"ok": True, "skills": []}
    url = "https://skills.sh/api/search?" + urllib.parse.urlencode({"q": query, "limit": _SEARCH_LIMIT})
    try:
        rows = json.loads(_http_get(url)).get("skills") or []
    except (OSError, ValueError, AttributeError) as e:
        return {"ok": False, "error": f"skills.sh search failed: {e}"}
    taken = {slug for d, _ in _user_dirs() for slug, _p in _entries(d)}
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        source, sid = r.get("source"), r.get("skillId")
        if not isinstance(source, str) or not isinstance(sid, str):
            continue
        sid = sid.lower()
        if not _SOURCE_RE.match(source) or not _SLUG_RE.match(sid):
            continue
        installs = r.get("installs")
        out.append({
            "id": f"{source}/{sid}",
            "source": source,
            "skillId": sid,
            "name": r.get("name") if isinstance(r.get("name"), str) and r.get("name") else sid,
            "installs": installs if isinstance(installs, int) else 0,
            "added": sid in taken,
        })
    return {"ok": True, "skills": out}


def _repo_tree(source: str) -> list:
    """[(path, size)] of every blob in the repo's default branch. One request
    per source per _TREE_TTL: the unauthenticated GitHub limit is 60/hour."""
    hit = _trees.get(source)
    if hit and time.time() - hit[0] < _TREE_TTL:
        return hit[1]
    try:
        raw = _http_get(f"https://api.github.com/repos/{source}/git/trees/HEAD?recursive=1")
    except OSError as e:
        raise ValueError(f"could not read {source} on GitHub: {e}")
    blobs = [(n["path"], n.get("size") or 0) for n in (json.loads(raw).get("tree") or [])
             if isinstance(n, dict) and n.get("type") == "blob" and isinstance(n.get("path"), str)]
    _trees[source] = (time.time(), blobs)
    return blobs


def _raw(source: str, path: str) -> bytes:
    return _http_get(f"https://raw.githubusercontent.com/{source}/HEAD/{urllib.parse.quote(path)}")


def _locate(source: str, skill_id: str) -> tuple:
    """(skill dir inside the repo, [(path, relpath, size)]) for the skill named
    `skill_id`: the SKILL.md whose dir is called that, else whose frontmatter
    `name` is. The paths come from the repo's tree."""
    if not _SOURCE_RE.match(source or "") or not _SLUG_RE.match(skill_id or ""):
        raise ValueError(f"invalid skill: {source!r} {skill_id!r}")
    tree = _repo_tree(source)
    cands = sorted((p for p, _n in tree if p == "SKILL.md" or p.endswith("/SKILL.md")),
                   key=lambda p: (p.count("/"), p))
    found = next((p for p in cands if p.rpartition("/")[0].rpartition("/")[2].lower() == skill_id), None)
    if found is None:
        for p in cands[:_FRONTMATTER_PROBES]:
            try:
                text = _raw(source, p).decode("utf-8", errors="replace")
            except OSError:
                continue
            if (lib.parse_frontmatter(text[:4096]).get("name") or "").lower() == skill_id:
                found = p
                break
    if found is None:
        raise ValueError(f"no skill {skill_id} in {source}")
    root = found.rpartition("/")[0]
    prefix = root + "/" if root else ""
    files, size = [], 0
    for p, n in tree:
        if not p.startswith(prefix):
            continue
        rel = p[len(prefix):]
        parts = rel.split("/")
        if any(x in ("", ".", "..") for x in parts) or any(x in _COPY_SKIP_DIRS for x in parts[:-1]):
            continue
        files.append((p, rel, n))
        size += n
        if len(files) > _COPY_MAX_FILES or size > _COPY_MAX_BYTES:
            raise ValueError("skill is too large to add")
    files.sort(key=lambda f: f[1])
    return root, files


def _remote_preview(source: str, skill_id: str) -> dict:
    root, files = _locate(source, skill_id)
    skill_md = next(p for p, rel, _n in files if rel == "SKILL.md")
    text = _raw(source, skill_md).decode("utf-8", errors="replace")
    return {"ok": True, "text": text, "files": [rel for _p, rel, _n in files]}


def _remote_add(source: str, skill_id: str) -> dict:
    _root, files = _locate(source, skill_id)

    def fill(dst: str) -> None:
        for p, rel, _n in files:
            target = os.path.normpath(os.path.join(dst, rel))
            if not target.startswith(dst + os.sep):
                raise ValueError(f"invalid path: {rel!r}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(_raw(source, p))

    return _place(skill_id, fill)


def _preview(marketplace: str, plugin: str, slug: str) -> dict:
    found, _ = _marketplace_plugins()
    for mkt, name, root, _d in found:
        if mkt != marketplace or name != plugin:
            continue
        for s, skill_dir, _n, _d in _marketplace_skills(root):
            if s != slug:
                continue
            md = os.path.join(skill_dir, "SKILL.md")
            if not plugins._within(skill_dir, os.path.realpath(md)):
                raise ValueError("SKILL.md points outside the skill")
            with open(md, encoding="utf-8", errors="replace") as f:
                text = f.read()
            files = []
            for dirpath, dirnames, names in os.walk(skill_dir):
                dirnames[:] = [d for d in dirnames if d not in _COPY_SKIP_DIRS]
                files.extend(os.path.relpath(os.path.join(dirpath, n), skill_dir) for n in names)
            return {"ok": True, "text": text, "files": sorted(files)}
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


# -- Draft with Claude: a staging dir the task writes one skill into ----------------

_DRAFT_NAME_MAX = 64
_DRAFT_DESC_MAX = 1024


def _drafts_root() -> str:
    return os.path.join(storage.home_dir(), "skill-drafts")


def _staging_dir(arg: str, must_exist: bool = True) -> str:
    """`arg` if it is a direct child of the drafts root (never a link, never
    a path that merely normalises into one), else ValueError."""
    root = os.path.normpath(_drafts_root())
    if not arg or not os.path.isabs(arg) or os.path.normpath(arg) != arg \
            or os.path.dirname(arg) != root or os.path.basename(arg).startswith("."):
        raise ValueError("not a skill draft directory")
    if os.path.islink(arg) or (must_exist and not os.path.isdir(arg)):
        raise ValueError("not a skill draft directory")
    return arg


def _draft_files(root: str) -> list:
    out = []
    for dirpath, dirnames, names in os.walk(root):
        top = os.path.normpath(dirpath) == os.path.normpath(root)
        dirnames[:] = [d for d in dirnames
                       if d not in _COPY_SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
                       and not (top and d == ".claude")]
        out.extend(os.path.relpath(os.path.join(dirpath, n), root) for n in names
                   if not os.path.islink(os.path.join(dirpath, n)))
    return sorted(out)


def _draft_check(path: str) -> dict:
    path = os.path.expanduser((path or "").strip())
    if not path:
        return {"ok": False, "error": "enter the path of a skill folder or its SKILL.md"}
    if os.path.isfile(path):
        if os.path.basename(path) != "SKILL.md":
            return {"ok": False, "error": "that file is not a SKILL.md"}
        path = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(path):
        return {"ok": False, "error": f"{path} was not found"}
    md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(md):
        return {"ok": False, "error": "no SKILL.md in that folder"}
    with open(md, encoding="utf-8", errors="replace") as f:
        fm = lib.parse_frontmatter(f.read(4096))
    return {"ok": True, "root": path, "name": fm.get("name") or os.path.basename(path),
            "files": _draft_files(path)}


def _draft_start(path: str) -> dict:
    found = _draft_check(path) if (path or "").strip() else None
    if found is not None and not found["ok"]:
        return found
    root = _drafts_root()
    os.makedirs(root, exist_ok=True)
    staging = os.path.join(root, f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}")
    os.mkdir(staging)
    if found:
        try:
            _copy_skill(found["root"], staging)
            shutil.rmtree(os.path.join(staging, ".claude"), ignore_errors=True)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return {"ok": True, "staging": staging}


def _draft_problems(name: str, description: str) -> list:
    problems = []
    if not name:
        problems.append("name is missing from the SKILL.md frontmatter")
    elif not _SLUG_RE.match(name):
        problems.append("name must be lowercase letters, digits, - and _, starting with a letter or digit")
    if len(name) > _DRAFT_NAME_MAX:
        problems.append(f"name must be at most {_DRAFT_NAME_MAX} characters")
    if not description:
        problems.append("description is missing from the SKILL.md frontmatter")
    elif len(description) > _DRAFT_DESC_MAX:
        problems.append(f"description must be at most {_DRAFT_DESC_MAX} characters")
    if _SLUG_RE.match(name) and any(os.path.lexists(os.path.join(d, name)) for d, _ in _user_dirs()):
        problems.append(f"a skill named {name} already exists; pick a different name")
    return problems


def _draft_status(staging: str) -> dict:
    staging = _staging_dir(staging)
    files = _draft_files(staging)
    md = os.path.join(staging, "SKILL.md")
    if not os.path.isfile(md) or os.path.islink(md):
        return {"ok": True, "files": files, "text": None, "name": "", "description": "", "problems": []}
    with open(md, encoding="utf-8", errors="replace") as f:
        text = f.read()
    fm = lib.parse_frontmatter(text)
    name, description = fm.get("name") or "", fm.get("description") or ""
    return {"ok": True, "files": files, "text": text, "name": name, "description": description,
            "problems": _draft_problems(name, description)}


def _draft_save(staging: str, placement: str) -> dict:
    if placement not in ("all", "claude"):
        return {"ok": False, "error": f"unknown placement: {placement!r}"}
    status = _draft_status(staging)
    if status["text"] is None:
        return {"ok": False, "error": "the draft has no SKILL.md yet", "problems": []}
    if status["problems"]:
        return {"ok": False, "error": "; ".join(status["problems"]), "problems": status["problems"]}

    def fill(dst: str) -> None:
        _copy_skill(staging, dst)
        shutil.rmtree(os.path.join(dst, ".claude"), ignore_errors=True)

    res = _place(status["name"], fill, placement)
    if res["ok"]:
        res["key"] = res["path"]
        shutil.rmtree(staging, ignore_errors=True)
    return res


def draft_prompt(text: str, adapting: bool = False) -> str:
    """The prompt of the Claude task that drafts a skill in its cwd."""
    lines = [
        "Write exactly one agent skill as a folder in the current directory, from this description:",
        "",
        text,
        "",
        "Rules:",
        "- Put a SKILL.md at the top of the current directory. It starts with YAML frontmatter "
        "holding `name` (lowercase letters, digits, - and _, at most 64 characters) and "
        "`description` (at most 1024 characters, starting \"Use when\" and saying when the "
        "skill applies, not what it does).",
        "- After the frontmatter, write the body: the steps to follow, concrete and short.",
        "- Add files under references/ or scripts/ only if the skill needs them.",
        "- Write only inside the current directory. Do not read or change anything outside it.",
    ]
    if adapting:
        lines += ["", "The current directory already contains a copy of an existing skill. "
                  "Adapt that copy to the description rather than starting from nothing."]
    return "\n".join(lines)


def main(action: str = "list", slug: str = "", path: str = "", text: str = "",
         source: str = "user", plugin: str = "", marketplace: str = "",
         location: str = "", query: str = "", skill_id: str = "",
         staging: str = "", placement: str = "all", adapting: bool = False) -> dict:
    if action == "list":
        return _list()

    if action == "search":
        return _search(query)

    if action == "available":
        return _available()

    try:
        if action == "draft_check":
            return _draft_check(path)

        if action == "draft_start":
            return _draft_start(path)

        if action == "draft_status":
            return _draft_status(staging)

        if action == "draft_save":
            with lib.config_lock():
                return _draft_save(staging, placement)

        if action == "draft_discard":
            shutil.rmtree(_staging_dir(staging, must_exist=False), ignore_errors=True)
            return {"ok": True}

        if action == "draft_prompt":
            return {"ok": True, "prompt": draft_prompt(text, adapting)}

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

        if action == "preview":
            return _preview(marketplace, plugin, slug)

        if action == "remote_preview":
            return _remote_preview(source, skill_id)

        if action == "remote_add":
            with lib.config_lock():
                return _remote_add(source, skill_id)

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
