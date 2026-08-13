"""Plugins feature (plugins.md).

Enable/disable toggles over settings.json -> enabledPlugins, grouped by
marketplace, enriched (read-only) from plugins/installed_plugins.json and
plugins/known_marketplaces.json. shareCommand strings are computed here
(sharing.md; there is no dedicated sharing module).

main(action=...):
  list      -> {plugins: [...]}
  available -> {plugins: [...], skipped: [...]}   the marketplace CATALOGS
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


def main(action: str = "list", id: str = "", enabled: bool = False) -> dict:
    if action == "list":
        return _list()

    if action == "available":
        return _available()

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
