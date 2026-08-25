"""Plugins feature (plugins.md).

Enable/disable toggles over settings.json -> enabledPlugins, grouped by
marketplace, enriched (read-only) from plugins/installed_plugins.json and
plugins/known_marketplaces.json. shareCommand strings are computed here
(sharing.md; there is no dedicated sharing module).

`injected` is the odd one out and the reason to read the block above
`_injected`: it reports what FUSED-RENDER hands its own sessions, which is not
in the user's config at all.

main(action=...):
  list      -> {plugins: [...]}
  available -> {plugins: [...], skipped: [...]}   the marketplace CATALOGS
  injected  -> {plugins: [...]}    what fused-render passes with --plugin-dir
  toggle    -> {ok, id, enabled}   params: id, enabled ("true"/"false")
  update    -> {ok, id, stdout} | {ok:False, error}  best-effort `claude` CLI
  install   -> {ok, id, stdout} | {ok:False, error}  best-effort `claude` CLI
  rebuild   -> {ok, root, plugins: [...]} | {ok:False, error}  reassemble ours
"""
import json
import os
from typing import Optional

from . import lib
from .skills import parse_frontmatter


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


# --- what FUSED-RENDER injects: plugins that are not in the user's config ----
#
# D491.
# `list` and `available` both answer questions about the USER's config — what
# they installed, what their marketplaces offer. This answers a third question
# neither can: what does a session fused-render spawns actually receive?
#
# That is a different mechanism, and the difference is the whole point. The
# sessions we launch are handed our skills as `claude --plugin-dir <root>`
# (D216): session-scoped argv, additive, resolved per spawn, and by design NOT
# written into settings.json — which is what makes it reliable, and also what
# made it invisible. A user who could not get the model to know about `fused.ai`
# came to this page to look, and found nothing registered under Marketplaces,
# Plugins or Skills: all three read their config, and our delivery is not in it.
# "Nothing" was indistinguishable from "four of the five skills are being
# injected right now, and the missing one is the one you are looking for"
# (D490 — the packaged copy really was short a skill for months).
#
# So this reports the roots as the TEMPLATE resolves them: off the env contract
# skill_plugin publishes (SPEC PY-15), never by recomputing `plugin_dir()`. The
# two differ in exactly the case worth seeing — a root that was never published,
# or a server whose startup sync failed — and recomputing would paper over it
# with a path that no spawn is passing.
#
# Read-only, and rendered without a toggle: `enabledPlugins` has no say over
# `--plugin-dir`, so a switch here would be a lie in either position. The one
# action offered is `rebuild`, because the roots are assembled at server startup
# and a long-running server otherwise keeps handing out the tree it built at
# boot (which is how the D490 gap survived an upgrade in place).

# The two roots, in the order a session receives them, with the SCOPE each is
# handed on — `agent.py:_plugin_argv` gates the workbench root on the target
# being inside the canvases root, so calling it machine-wide would misdescribe
# it. Keyed off skill_plugin's own env-var constants rather than the literal
# strings, so a renamed contract cannot leave this page reading a dead variable.
def _injected_roots() -> list:
    from fused_render import skill_plugin

    return [
        (skill_plugin.PLUGIN_DIR_ENV, "fused-render", "every session"),
        (skill_plugin.WORKBENCH_PLUGIN_DIR_ENV, "workbench",
         "canvas sessions only"),
    ]


def _root_skills(root: str) -> list:
    """The skills a plugin root actually holds, newest frontmatter and all.

    Reads the assembled tree rather than any list of what SHOULD be there: the
    point of the row is to show what a session gets, and a discrepancy between
    that and the repo is precisely the thing worth seeing.
    """
    skills_dir = os.path.join(root, "skills")
    try:
        names = sorted(os.listdir(skills_dir))
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(skills_dir, name, "SKILL.md")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                fm = parse_frontmatter(fh.read())
        except OSError:
            continue  # not a skill dir, or unreadable — not this page's problem
        out.append({
            "slug": name,
            "name": fm["name"] or name,
            "description": fm["description"],
        })
    return out


def _injected_root(env: str, label: str, scope: str) -> dict:
    """One root's row. `available: False` (with `root: None`) is a real answer,
    not an error: the var is unset when there was nothing loadable to publish,
    and for the workbench root that is also just "no canvas has been opened on
    this machine yet"."""
    root = os.environ.get(env) or ""
    manifest = os.path.join(root, ".claude-plugin", "plugin.json") if root else ""
    row = {
        "name": label,
        "scope": scope,
        "env": env,
        "root": root or None,
        "available": bool(root) and os.path.isfile(manifest),
        "skills": [],
        "assembled": None,
    }
    if not row["available"]:
        return row
    row["skills"] = _root_skills(root)
    try:
        # The manifest's mtime IS the assembly time: `_build` writes it into a
        # staging dir that is then renamed into place, so the file is never
        # older than the tree around it.
        row["assembled"] = int(os.path.getmtime(manifest))
    except OSError:
        pass
    return row


def _injected() -> dict:
    return {"plugins": [_injected_root(*r) for r in _injected_roots()]}


def _rebuild() -> dict:
    """Reassemble OUR root from the installed wheel and republish it.

    Only ours. The workbench root is fetched over the network and only on a
    canvas open (`sync_workbench_plugin`), so refreshing it from a config page
    would be a surprising network call about a feature the user may not use.

    Not a git commit: nothing under the user's config changed. What changed is a
    tree this app owns, and the next spawn picks it up with no restart."""
    from fused_render.skill_plugin import export_skill_plugin_env

    root = export_skill_plugin_env()
    if root is None:
        return {"ok": False,
                "error": "no skills to assemble — this install shipped none"}
    return {"ok": True, "root": root, **_injected()}


def main(action: str = "list", id: str = "", enabled: bool = False) -> dict:
    if action == "list":
        return _list()

    if action == "available":
        return _available()

    if action == "injected":
        return _injected()

    if action == "rebuild":
        return _rebuild()

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
        res = lib.claude_cli("plugin", "update", id, "--scope", "user")
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
