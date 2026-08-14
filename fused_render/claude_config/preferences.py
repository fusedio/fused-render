"""Preferences feature (preferences.md).

Surfaces a curated catalog of scalar settings.json keys as form controls, each
showing its documented default when unset. The catalog is settings_catalog.json
(merged UI overlay + docs snapshot) — the user-writable override when one exists,
else the copy that shipped with the package; refresh_catalog.py regenerates its
doc/default half from Anthropic's docs.

main(action=...) actions:
  get    -> {schema, prefs}   schema = catalog list; prefs = current value per
                              key (dotted-path read; null when unset)
  patch  -> {ok, changed}     payload = JSON object {key: value|null}; null
                              resets a key (deletes the leaf). Only catalog keys
                              are accepted. Atomic write + git commit.
"""
import json

from . import lib


def _catalog() -> list:
    # Resolved per call, not once at import: a refresh writes the override
    # mid-process, and a module-level constant would keep serving the packaged
    # copy until the app restarted.
    with open(lib.catalog_read_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def main(action: str = "get", payload: str = "") -> dict:
    catalog = _catalog()
    keys = {d["key"] for d in catalog}

    if action == "get":
        # Capture the baseline snapshot on load, before any edit, so the first
        # patch lands as its own commit rather than folding into the seed.
        lib.ensure_repo()
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
            lib.commit("Update preferences: " + ", ".join(changed))
        return {"ok": True, "changed": changed}

    return {"ok": False, "error": f"unknown action: {action}"}
