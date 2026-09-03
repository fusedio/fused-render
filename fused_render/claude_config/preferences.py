"""Preferences feature (preferences.md).

Surfaces a curated catalog of scalar settings.json keys as form controls, each
showing its documented default when unset. The catalog is settings_catalog.json
— curated fields (label/group/control/options/optionLabels/unsetLabel) always
from the copy that shipped with the package, doc/default/minVersion overlaid
from the user-writable override when refresh_catalog.py has ever written one;
see lib.load_catalog() for why the merge is per-field rather than per-file
(a whole-file override going stale on the curated half — a new option, a new
row — the moment anyone ever refreshes was PR #968's bug, live on a real
server: an old override shadowed a catalog update forever).

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
    # mid-process, and a module-level constant would keep serving a stale
    # merge until the app restarted.
    return lib.load_catalog()


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
