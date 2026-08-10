"""Marketplaces feature (marketplaces.md).

Add/remove user marketplaces in settings.json -> extraKnownMarketplaces.
Official/resolved marketplaces (from plugins/known_marketplaces.json) are
read-only. shareCommand strings computed here (sharing.md).

main(action=...):
  list   -> {marketplaces: [...]}
  add    -> {ok, name}     params: name, kind ("github"|"git"), value, auto_update
  remove -> {ok, name}     params: name (only user-added are removable)
"""
from typing import Optional

from . import lib


def _marketplace_ref(src: Optional[dict]) -> Optional[str]:
    if not isinstance(src, dict):
        return None
    return src.get("repo") or src.get("url")


def _add_command(src: Optional[dict]) -> Optional[str]:
    ref = _marketplace_ref(src)
    return f"claude plugin marketplace add {ref}" if ref else None


def _list() -> dict:
    s = lib.read_settings()
    extra = s.get("extraKnownMarketplaces") or {}
    known = lib.read_json(lib.KNOWN_MARKETPLACES_PATH, {})
    names = sorted(set(extra) | set(known))
    marketplaces = []
    for name in names:
        src = (extra.get(name) or {}).get("source") or (known.get(name) or {}).get("source") or {}
        marketplaces.append({
            "name": name,
            "source": src,
            # only user-added (in settings) are editable/removable
            "editable": name in extra,
            "autoUpdate": (extra.get(name) or {}).get("autoUpdate")
            or (known.get(name) or {}).get("autoUpdate")
            or False,
            "shareCommand": _add_command(src),
        })
    return {"marketplaces": marketplaces}


def main(action: str = "list", name: str = "", kind: str = "github",
         value: str = "", auto_update: bool = False) -> dict:
    if action == "list":
        return _list()

    if action == "add":
        if not name or not value:
            return {"ok": False, "error": "name and value required"}
        source = {"source": "github", "repo": value} if kind == "github" else {"source": "git", "url": value}
        entry = {"source": source, "autoUpdate": True} if lib.as_bool(auto_update) else {"source": source}
        with lib.config_lock():
            s = lib.read_settings()
            s["extraKnownMarketplaces"] = {**(s.get("extraKnownMarketplaces") or {}), name: entry}
            lib.write_json(lib.SETTINGS_PATH, s)
            lib.commit(f"Add marketplace {name}")
        return {"ok": True, "name": name}

    if action == "remove":
        with lib.config_lock():
            s = lib.read_settings()
            extra = s.get("extraKnownMarketplaces") or {}
            if name not in extra:
                return {"ok": False, "error": "not a user-added marketplace"}
            del extra[name]
            s["extraKnownMarketplaces"] = extra
            lib.write_json(lib.SETTINGS_PATH, s)
            lib.commit(f"Remove marketplace {name}")
        return {"ok": True, "name": name}

    return {"ok": False, "error": f"unknown action: {action}"}
