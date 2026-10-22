"""Preferences feature (preferences.md).

Surfaces a curated list of 20 scalar settings.json keys, in five groups, as
form controls, each showing its documented default when unset. The list is
settings_catalog.json — the copy that ships with the package (see
lib.load_catalog()); refresh_catalog.CURATED is its source of truth.

main(action=...) actions:
  get    -> {schema, prefs}   schema = catalog list; prefs = current value per
                              key (dotted-path read; null when unset)
  patch  -> {ok, changed}     payload = JSON object {key: value|null}; null
                              resets a key (deletes the leaf). Only catalog keys
                              are accepted. Atomic write.
"""
import json

from . import lib


def _catalog() -> list:
    return lib.load_catalog()


def main(action: str = "get", payload: str = "") -> dict:
    catalog = _catalog()
    keys = {d["key"] for d in catalog}

    if action == "get":
        settings = lib.read_settings()
        prefs = {d["key"]: lib.get_path(settings, d["key"]) for d in catalog}
        return {"schema": catalog, "prefs": prefs}

    if action == "patch":
        body = json.loads(payload) if payload else {}
        unknown = [k for k in body if k not in keys]
        if unknown:
            return {"ok": False, "error": f"unmanaged keys: {unknown}"}
        with lib.config_lock():
            settings = lib.read_settings()
            changed = []
            for key, value in body.items():
                if value is None:
                    lib.delete_path(settings, key)  # reset to Claude default
                else:
                    lib.set_path(settings, key, value)
                changed.append(key)
            lib.write_json(lib.SETTINGS_PATH, settings)
        return {"ok": True, "changed": changed}

    return {"ok": False, "error": f"unknown action: {action}"}
