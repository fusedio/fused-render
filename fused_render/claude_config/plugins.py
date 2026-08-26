"""Plugins feature (plugins.md).

Enable/disable toggles over settings.json -> enabledPlugins, grouped by
marketplace, enriched (read-only) from plugins/installed_plugins.json and
plugins/known_marketplaces.json. shareCommand strings are computed here
(sharing.md; there is no dedicated sharing module).

main(action=...):
  list      -> {plugins: [...]}
  available -> {plugins: [...], skipped: [...]}   the marketplace CATALOGS
  contents  -> {ok, id, root, skills, commands, agents, hooks, mcpServers}
                                                 params: id   what one INSTALLED
                                                 plugin puts in a session
  toggle    -> {ok, id, enabled}   params: id, enabled ("true"/"false")
  update    -> {ok, id, stdout} | {ok:False, error}  best-effort `claude` CLI
  install   -> {ok, id, stdout} | {ok:False, error}  best-effort `claude` CLI
"""
import json
import os
from typing import Optional

from . import lib


def _marketplace_ref(src: Optional[dict]) -> Optional[str]:
    """owner/repo (github) or git url -> the bare ref share commands accept."""
    if not isinstance(src, dict):
        return None
    return src.get("repo") or src.get("url")


def _plugin_share_command(plugin_id: str, mkt_src: Optional[dict]) -> str:
    ref = _marketplace_ref(mkt_src)
    install = f"claude plugin install {plugin_id}"
    if ref:
        return f"claude plugin marketplace add {ref}\n{install}"
    return install


def _list() -> dict:
    s = lib.read_settings()
    enabled = s.get("enabledPlugins") or {}
    installed = lib.read_json(lib.INSTALLED_PLUGINS_PATH, {})
    installed_plugins = installed.get("plugins") or {}
    extra = s.get("extraKnownMarketplaces") or {}
    known = lib.read_json(lib.KNOWN_MARKETPLACES_PATH, {})

    ids = sorted(set(installed_plugins) | set(enabled))
    plugins = []
    for pid in ids:
        name, _, marketplace = pid.partition("@")
        marketplace = marketplace or "unknown"
        rec = (installed_plugins.get(pid) or [{}])[0]
        mkt_src = (extra.get(marketplace) or {}).get("source") or (
            known.get(marketplace) or {}
        ).get("source")
        plugins.append({
            "id": pid,
            "name": name,
            "marketplace": marketplace,
            "enabled": bool(enabled.get(pid, False)),
            "installed": pid in installed_plugins,
            "version": rec.get("version"),
            "gitSourced": bool(rec.get("gitCommitSha")),
            "shareCommand": _plugin_share_command(pid, mkt_src),
        })
    return {"plugins": plugins}


# --- the marketplace catalogs: plugins you could install --------------------
# `list` above answers "what is on this machine". These answer "what is on
# offer", which is a different source entirely: each cloned marketplace ships a
# catalog at <mkt>/.claude-plugin/marketplace.json (older ones put it at
# <mkt>/marketplace.json) listing every plugin that marketplace publishes.
#
# The marketplace's DIRECTORY name is what identifies it, not the `name` field
# inside the catalog: the id the CLI and installed_plugins.json speak is
# "<plugin>@<directory>", and joining installed/enabled state onto the catalog
# only works if both sides spell the marketplace the same way.


def _catalog_path(mkt_dir: str) -> Optional[str]:
    for rel in (os.path.join(".claude-plugin", "marketplace.json"), "marketplace.json"):
        cand = os.path.join(mkt_dir, rel)
        if os.path.isfile(cand):
            return cand
    return None


def _author(value: object) -> Optional[str]:
    """Catalogs write `author` as either a bare string or {name, email, …}."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) and name else None
    return None


def _available() -> dict:
    """Every plugin every cloned marketplace publishes, with installed/enabled
    joined on so the UI can mark or hide what is already here.

    A marketplace whose catalog is missing, unreadable or not shaped like a
    catalog is SKIPPED rather than fatal — one hand-edited marketplace.json
    must not blank the whole Discover list. It is not swallowed either: its
    name comes back in `skipped` so the page can say which one it could not
    read, instead of quietly showing a short list.
    """
    s = lib.read_settings()
    enabled = s.get("enabledPlugins") or {}
    installed_plugins = lib.read_json(lib.INSTALLED_PLUGINS_PATH, {}).get("plugins") or {}

    plugins, skipped = [], []
    try:
        marketplaces = sorted(os.listdir(lib.MARKETPLACES_DIR))
    except OSError:
        # No marketplaces dir at all is the honest empty answer, not an error:
        # `claude plugin marketplace add` is what creates it.
        return {"plugins": [], "skipped": []}

    for mkt in marketplaces:
        mkt_dir = os.path.join(lib.MARKETPLACES_DIR, mkt)
        if not os.path.isdir(mkt_dir):
            continue
        path = _catalog_path(mkt_dir)
        if path is None:
            skipped.append(mkt)
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                catalog = json.load(f)
        except (OSError, ValueError):
            skipped.append(mkt)
            continue
        entries = catalog.get("plugins") if isinstance(catalog, dict) else None
        if not isinstance(entries, list):
            skipped.append(mkt)
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            # A catalog is third-party content, and its `name` becomes argv for
            # the CLI further down. Nothing legitimate starts with a dash; an
            # entry that does is dropped HERE, at the boundary, so no downstream
            # action has to remember (see lib.option_shaped).
            if lib.option_shaped(name) or lib.option_shaped(mkt):
                continue
            pid = f"{name}@{mkt}"
            keywords = entry.get("keywords")
            plugins.append({
                "id": pid,
                "name": name,
                "marketplace": mkt,
                "description": entry.get("description") or "",
                "version": entry.get("version"),
                "author": _author(entry.get("author")),
                "category": entry.get("category"),
                "keywords": [k for k in keywords if isinstance(k, str)]
                if isinstance(keywords, list) else [],
                "installed": pid in installed_plugins,
                "enabled": bool(enabled.get(pid, False)),
            })
    return {"plugins": plugins, "skipped": skipped}


# --- what a plugin PUTS IN A SESSION ---------------------------------------
# `list` and `available` describe a plugin from the outside — its id, its
# version, whether it is on. Neither answers the question you actually have in
# front of a list of twelve of them: what does this one give a session? That
# answer is on disk and nowhere else. installed_plugins.json records an
# `installPath` per plugin, and under it the components sit at conventional
# paths (skills/, commands/, agents/, hooks/hooks.json, .mcp.json) that
# .claude-plugin/plugin.json may relocate.
#
# Read-only, and READ ON DEMAND — one plugin per call, when its row is
# expanded. Reading all twelve up front would walk a dozen trees (context-mode
# ships node_modules) to fill in a panel most visits never open.

# The conventional home of each component, used when the manifest names none.
_DEFAULT_DIRS = {"skills": "skills", "commands": "commands", "agents": "agents"}

# Walk caps. A plugin root is third-party content of unbounded size, and this
# runs on a UI request: a component dir that is somehow enormous (or a symlink
# loop) must cost a bounded panel, not a hung request.
_MAX_DEPTH = 4
_MAX_ENTRIES = 200

# Directories that never hold a component and can hold a hundred thousand
# files. Skipped by name rather than by size — cheaper, and it is the same list
# every tool that walks a package tree keeps.
_SKIP_DIRS = {"node_modules", "__pycache__", ".git", "dist", "build"}


def _within(root: str, path: str) -> bool:
    """Lexical containment, matching lib.safe_subdir's boundary. A manifest is
    third-party content and its path values become directories we walk, so
    `"skills": "../../../.."` must resolve to nothing rather than to a tour of
    the user's disk."""
    root = os.path.normpath(root)
    return path == root or path.startswith(root + os.sep)


def _manifest_dirs(manifest: dict, key: str, root: str) -> list:
    """The dirs a manifest names for `key`, else the conventional one.

    The value is a string or a list of strings, each relative to the plugin
    root and free to spell it as ${CLAUDE_PLUGIN_ROOT} — the same substitution
    the CLI does when it reads these paths."""
    val = manifest.get(key)
    if isinstance(val, str):
        raw = [val]
    elif isinstance(val, list):
        raw = [v for v in val if isinstance(v, str)]
    else:
        raw = [_DEFAULT_DIRS[key]]
    out = []
    for v in raw:
        v = v.replace("${CLAUDE_PLUGIN_ROOT}", root)
        p = os.path.normpath(v if os.path.isabs(v) else os.path.join(root, v))
        if _within(root, p) and p not in out:
            out.append(p)
    return out


def _walk(top: str):
    """Depth- and count-bounded os.walk over one component dir, skipping the
    dirs that never hold components. Yields (dirpath, filenames)."""
    if not os.path.isdir(top):
        return
    seen = 0
    base_depth = top.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [
            d for d in sorted(dirnames) if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        if dirpath.count(os.sep) - base_depth >= _MAX_DEPTH:
            dirnames[:] = []
        yield dirpath, sorted(filenames)
        seen += 1
        if seen >= _MAX_ENTRIES:
            return


def _read_frontmatter(path: str) -> dict:
    """Frontmatter, or empty on any read failure. A component whose file cannot
    be read is still a component the plugin ships — it keeps its row (and its
    path, which is what makes the row clickable) and simply says nothing."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            # The block is at the top; a skill body can run to tens of KB and
            # none of it is frontmatter.
            return lib.parse_frontmatter(f.read(4096))
    except OSError:
        return {}


def _skills(root: str, manifest: dict) -> list:
    """A skill is a directory with a SKILL.md in it (D490's rule, applied to a
    plugin's tree rather than the repo's). Nested, because a plugin is free to
    group them in subdirectories."""
    out = []
    for top in _manifest_dirs(manifest, "skills", root):
        # `_walk` yields `top` itself first, so a manifest pointing at ONE skill
        # rather than at a dir of them needs no special case.
        for dirpath, filenames in _walk(top):
            if "SKILL.md" not in filenames:
                continue
            path = os.path.join(dirpath, "SKILL.md")
            fm = _read_frontmatter(path)
            out.append({
                "name": fm.get("name") or os.path.basename(dirpath),
                "description": fm.get("description") or "",
                "path": path,
            })
    return out


def _markdown(root: str, manifest: dict, key: str) -> list:
    """Commands and agents are both "every .md under a dir", differing only in
    what names them: an agent declares its own `name` in frontmatter, while a
    command is INVOKED by its path (`/plugin:sub:name`), so the path is the
    honest label and a frontmatter `name` would be a second one."""
    out = []
    for top in _manifest_dirs(manifest, key, root):
        for dirpath, filenames in _walk(top):
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, top)[: -len(".md")].replace(os.sep, ":")
                fm = _read_frontmatter(path)
                name = rel if key == "commands" else (fm.get("name") or rel)
                out.append({
                    "name": name,
                    "description": fm.get("description") or "",
                    "path": path,
                })
    return out


def _hooks(root: str, manifest: dict) -> list:
    """One row per EVENT, not per hook command: what a reader wants from a hook
    listing is "this plugin runs something on SessionStart", and the command
    itself is a ${CLAUDE_PLUGIN_ROOT} string that says nothing on one line.

    `hooks` in the manifest is a path to the json; the conventional
    hooks/hooks.json is used when it names none. An INLINE hooks object is also
    accepted — the file it lives in is then plugin.json, which is where the
    click lands."""
    val = manifest.get("hooks")
    path, config = None, None
    if isinstance(val, dict):
        path, config = os.path.join(root, ".claude-plugin", "plugin.json"), val
    else:
        candidates = []
        if isinstance(val, str):
            v = val.replace("${CLAUDE_PLUGIN_ROOT}", root)
            candidates = [os.path.normpath(v if os.path.isabs(v) else os.path.join(root, v))]
        else:
            candidates = [os.path.join(root, "hooks", "hooks.json")]
        for cand in candidates:
            if _within(root, cand) and os.path.isfile(cand):
                path = cand
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        config = json.load(f)
                except (OSError, ValueError):
                    # A hooks.json we cannot parse is still a hooks.json the
                    # plugin ships: the row stays, says so, and opens the file
                    # that needs fixing.
                    return [{"name": "hooks.json", "description": "could not be read",
                             "path": cand}]
                break
    if not isinstance(config, dict):
        return []
    events = config.get("hooks") if isinstance(config.get("hooks"), dict) else config
    out = []
    for event, entries in (events or {}).items():
        if not isinstance(event, str) or not isinstance(entries, list):
            continue
        matchers = [
            e.get("matcher") for e in entries
            if isinstance(e, dict) and isinstance(e.get("matcher"), str) and e.get("matcher")
        ]
        n = sum(len(e.get("hooks") or []) for e in entries if isinstance(e, dict))
        desc = f"{n} hook{'' if n == 1 else 's'}"
        if matchers:
            desc += " on " + ", ".join(matchers)
        out.append({"name": event, "description": desc, "path": path})
    return out


def _mcp_servers(root: str, manifest: dict) -> list:
    """Declared inline in plugin.json (sentry, context-mode) or in a sibling
    .mcp.json (github, circleback) — both are in the wild, so both are read.
    The description is the transport, because that is the one fact about an MCP
    server that changes what you expect of it: a URL is remote, a command is a
    process this machine will spawn."""
    sources = []
    val = manifest.get("mcpServers")
    if isinstance(val, dict):
        sources.append((os.path.join(root, ".claude-plugin", "plugin.json"), val))
    elif isinstance(val, str):
        v = val.replace("${CLAUDE_PLUGIN_ROOT}", root)
        p = os.path.normpath(v if os.path.isabs(v) else os.path.join(root, v))
        if _within(root, p):
            sources.append((p, None))
    else:
        sources.append((os.path.join(root, ".mcp.json"), None))
    out = []
    for path, servers in sources:
        if servers is None:
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                continue
            servers = doc.get("mcpServers") if isinstance(doc, dict) else None
        if not isinstance(servers, dict):
            continue
        for name, cfg in servers.items():
            if not isinstance(name, str):
                continue
            desc = ""
            if isinstance(cfg, dict):
                desc = cfg.get("url") or cfg.get("command") or cfg.get("type") or ""
            out.append({"name": name, "description": str(desc), "path": path})
    return out


def _contents(pid: str) -> dict:
    """Everything one INSTALLED plugin contributes to a session.

    Keyed by the plugin id rather than by a path from the browser: the path is
    resolved here, out of installed_plugins.json, so this action cannot be
    pointed at an arbitrary directory. An id that is not installed has no
    installPath and is refused, which is the same membership guard `update`
    applies for the same reason."""
    if not pid:
        return {"ok": False, "error": "id required"}
    installed = lib.read_json(lib.INSTALLED_PLUGINS_PATH, {}).get("plugins") or {}
    rec = installed.get(pid)
    if not rec:
        return {"ok": False, "error": "plugin is not installed"}
    root = (rec[0] or {}).get("installPath") if isinstance(rec, list) and rec else None
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "plugin files are missing — reinstall it"}
    root = os.path.normpath(root)
    # Tolerant, unlike lib.read_json: that helper lets malformed JSON raise
    # because the user's OWN config being corrupt must surface. A plugin's
    # manifest is somebody else's file, and a broken one costs this panel its
    # non-default paths — not a 500 on a row the user merely expanded.
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    return {
        "ok": True,
        "id": pid,
        "root": root,
        "description": manifest.get("description") or "",
        "skills": _skills(root, manifest),
        "commands": _markdown(root, manifest, "commands"),
        "agents": _markdown(root, manifest, "agents"),
        "hooks": _hooks(root, manifest),
        "mcpServers": _mcp_servers(root, manifest),
    }


def main(action: str = "list", id: str = "", enabled: bool = False) -> dict:
    if action == "list":
        return _list()

    if action == "available":
        return _available()

    if action == "contents":
        return _contents(id)

    if action == "toggle":
        if not id:
            return {"ok": False, "error": "id required"}
        want = lib.as_bool(enabled)
        with lib.config_lock():
            s = lib.read_settings()
            s["enabledPlugins"] = {**(s.get("enabledPlugins") or {}), id: want}
            lib.write_json(lib.SETTINGS_PATH, s)
            lib.commit(f"{'Enable' if want else 'Disable'} plugin {id}")
        return {"ok": True, "id": id, "enabled": want}

    if action == "update":
        # Two guards, because membership alone is not safety: settings.json is
        # hand-editable, so an id being "known" says only that it is in a file
        # the user can write — not that it is a plugin name rather than a flag.
        if lib.option_shaped(id):
            return {"ok": False, "error": "unknown plugin"}
        s = lib.read_settings()
        installed = lib.read_json(lib.INSTALLED_PLUGINS_PATH, {})
        known = set(installed.get("plugins") or {}) | set(s.get("enabledPlugins") or {})
        if id not in known:
            return {"ok": False, "error": "unknown plugin"}
        # No git commit — plugins/ is ignored; restart applies it. Best-effort
        # (the claude CLI may be absent, or exceed claude_cli's timeout; plugins.md §5).
        #
        # -y for the same reason `install` below passes it, and the CLI documents
        # it on `update` too: a marketplace whose plugin manifest has CHANGED its
        # declared install command re-prompts for consent on update, and the flag
        # is required outright when stdout is not a TTY — which it never is here,
        # `claude_cli` captures it. Without it such an update sits on a prompt
        # nobody can answer until the timeout, and the user is told it timed out
        # rather than that consent was wanted.
        res = lib.claude_cli("plugin", "update", id, "--scope", "user", "-y")
        if not res["ok"]:
            return {"ok": False, "error": res["stderr"] or "update failed"}
        return {"ok": True, "id": id, "stdout": res["stdout"]}

    if action == "install":
        if not id:
            return {"ok": False, "error": "id required"}
        # Same guard as `update`, against the other source of truth: the id must
        # be one a cloned marketplace actually publishes.
        #
        # What that buys, stated precisely, because the previous wording claimed
        # more than it delivered: the catalog is THIRD-PARTY content, so passing
        # this check does not make a string ours. It makes it a string some
        # marketplace published. The thing that stops it being read as a flag is
        # _available() dropping option-shaped names as it builds the catalog
        # (see lib.option_shaped) — an entry named "--force" never reaches this
        # set, so it can never match here.
        if id not in {p["id"] for p in _available()["plugins"]}:
            return {"ok": False, "error": "unknown plugin"}
        # -y: this runs headless, and an install that stops on a prompt would
        # just hit the timeout. Generous timeout because an install can clone a
        # repo over the network — claude_cli's 25s default is a local-command
        # budget and would report a working install as a failure.
        # No git commit — plugins/ is ignored — but the CLI may write
        # settings.json's enabledPlugins, so the caller still signals onChanged.
        res = lib.claude_cli("plugin", "install", id, "--scope", "user", "-y", timeout=120)
        if not res["ok"]:
            return {"ok": False, "error": res["stderr"] or "install failed"}
        return {"ok": True, "id": id, "stdout": res["stdout"]}

    return {"ok": False, "error": f"unknown action: {action}"}
