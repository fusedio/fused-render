"""Plugins feature (plugins.md).

Enable/disable toggles over settings.json -> enabledPlugins, grouped by
marketplace, enriched (read-only) from plugins/installed_plugins.json and
plugins/known_marketplaces.json. shareCommand strings are computed here
(sharing.md; there is no dedicated sharing module).

main(action=...):
  list   -> {plugins: [...]}
  toggle -> {ok, id, enabled}   params: id, enabled ("true"/"false")
  update -> {ok, id, stdout} | {ok:False, error}  best-effort `claude` CLI
"""
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


def main(action: str = "list", id: str = "", enabled: bool = False) -> dict:
    if action == "list":
        return _list()

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
        # Guard: only ids we know about are ever handed to the CLI.
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

    return {"ok": False, "error": f"unknown action: {action}"}
