"""Template-management API (SPEC: TEMPLATE_MGMT_SPEC §2) — the backend for the
Templates view: edit registry bindings, inspect the template inventory across
sources, and import/export user templates as zip.

The template *resolution* engine (the suffix-pattern matcher, name resolution,
splice grammar) already lives in ``fused_render.server`` (D73). This module is
a thin management layer on top of it: it REUSES ``server._key_segments`` /
``server._match_registry`` / ``server._names_from_value`` / ``server._resolve_name``
/ ``server._icon_for`` / ``server._load_registry`` and the dir constants
``server.TEMPLATES_DIR`` / ``server.USER_TEMPLATES_DIR`` / ``server.BUILTIN_REGISTRY``
/ ``server.USER_REGISTRY`` — imported as ``server.<name>`` and read at REQUEST
time so tests can monkeypatch the dirs (test_templates.py's seam).

``server`` includes this router lazily inside ``create_app`` (no server->module
import at module top), so ``from fused_render import server`` here is acyclic:
this module only touches ``server`` attributes inside request handlers, by which
point ``server`` is fully imported.

Sources (SPEC §1): the builtin/user pair is modelled as an ordered list so a
third (org/project) can be appended later with zero UI rework — TODAY exactly
two, core (read-only) + user (editable). Effective binding for a key = the
value from the highest-precedence source that defines it (user beats core).
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shlex
import shutil
import stat as stat_mod
import subprocess
import sys
import time
import zipfile

from fastapi import APIRouter, Body, File, Header, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from fused_render import server
from fused_render.shell import storage

router = APIRouter()


# -- sources (SPEC §1) --------------------------------------------------------

# Ordered by precedence (lowest first). Modelled as a list so a third source
# can be appended later; the effective value for a key is the one from the
# highest-precedence source that defines it (user > core), matching resolution.
SOURCES = [
    {"id": "core", "label": "Core", "editable": False, "precedence": 0},
    {"id": "user", "label": "User", "editable": True, "precedence": 100},
]


def _sources_payload() -> list:
    """SOURCES with an ABSOLUTE `dir` added per source (SPEC §2.1/§2.2): core's
    templates dir = server.TEMPLATES_DIR, user's = server.USER_TEMPLATES_DIR.
    Read at request time (not baked into SOURCES) so tests that monkeypatch
    those module constants see the patched dirs."""
    dirs = {"core": server.TEMPLATES_DIR, "user": server.USER_TEMPLATES_DIR}
    return [dict(s, dir=os.path.abspath(dirs[s["id"]])) for s in SOURCES]


# -- guards / helpers ---------------------------------------------------------


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused (a custom header forces a CORS
    # preflight that fails cross-origin, blocking blind foreign writes).
    # Duplicated locally like shell/bookmarks.py / deploy.py do.
    if x_fused != "1":
        return JSONResponse({"error": "missing or invalid X-Fused header"}, status_code=403)
    return None


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _key_kind(key) -> str:
    """Classify a registry key into one of the 4 authorable shapes (SPEC §2.2):
    directory (trailing "/"), wildcard (a `*` whole-segment), compound (>=2
    literal segments), or simple (one segment). Best-effort for a malformed
    user key — resolution ignores such keys, but the view still lists them."""
    k = str(key)
    if k.endswith("/"):
        return "directory"
    body = k[1:] if k.startswith(".") else k
    segs = body.split(".")
    if any(s == "*" for s in segs):
        return "wildcard"
    return "compound" if len(segs) > 1 else "simple"


def _source_of_resolved(path: str | None) -> str | None:
    """Which source supplied a resolved template.html path: "user" if it lives
    under USER_TEMPLATES_DIR, else "core". None for an unresolved name."""
    if not path:
        return None
    p = os.path.abspath(path)
    user_root = os.path.abspath(server.USER_TEMPLATES_DIR)
    if p == user_root or p.startswith(user_root + os.sep):
        return "user"
    return "core"


def _template_obj(name: str) -> dict:
    """One effective-list entry resolved to {name, source, exists, hasIcon}
    (SPEC §2.2). A known sentinel (e.g. "_render") has no folder — source null,
    exists true, no icon. A name that resolves to no folder is kept but marked
    exists:false so the UI can surface it as broken."""
    if name in server.KNOWN_SENTINELS:
        return {"name": name, "source": None, "exists": True, "hasIcon": False}
    path, _err = server._resolve_name(name)
    if path is None:
        return {"name": name, "source": None, "exists": False, "hasIcon": False}
    return {
        "name": name,
        "source": _source_of_resolved(path),
        "exists": True,
        "hasIcon": server._icon_for(path) is not None,
    }


def _load_registries():
    """Both registries as dicts (missing/corrupt -> {}) plus the read errors."""
    builtin_reg, builtin_err = server._load_registry(
        server.BUILTIN_REGISTRY, "built-in registry.json"
    )
    user_reg, user_err = server._load_registry(server.USER_REGISTRY, "registry.json")
    builtin_reg = builtin_reg if isinstance(builtin_reg, dict) else {}
    user_reg = user_reg if isinstance(user_reg, dict) else {}
    return builtin_reg, user_reg, builtin_err, user_err


def _builtin_names(builtin_key, builtin_reg) -> list:
    """The builtin registry's name list for a key, or []."""
    if builtin_key is None:
        return []
    names, disabled, _err = server._names_from_value(builtin_key, builtin_reg[builtin_key], [])
    return names if (names and not disabled) else []


def _compute_entry(display_key, builtin_reg, user_reg, builtin_by_lower, user_by_lower) -> dict:
    """One entries[] object (SPEC §2.2), keyed off a display key. Override
    detection is case-insensitive (matches resolution: _key_segments lowercases)
    — a user ".CSV" overrides a builtin ".csv"; the row uses the user's casing."""
    kl = str(display_key).lower()
    builtin_key = builtin_by_lower.get(kl)
    user_key = user_by_lower.get(kl)

    builtin_names = _builtin_names(builtin_key, builtin_reg)

    if user_key is not None:
        raw_user_value = user_reg[user_key]
        names, disabled, error = server._names_from_value(user_key, raw_user_value, builtin_names)
        effective = [] if disabled else (names or [])
        resolved_source = "user"
        overrides_core = True
    else:
        # No user override -> the builtin decides. Builtin values are lists in
        # practice, but honour a null (disabled) shape generally.
        b_names, b_disabled, error = (
            server._names_from_value(builtin_key, builtin_reg[builtin_key], [])
            if builtin_key is not None
            else ([], False, None)
        )
        disabled = b_disabled
        effective = [] if b_disabled else (b_names or [])
        resolved_source = "core"
        overrides_core = False

    entry = {
        "key": str(display_key),
        "keyKind": _key_kind(display_key),
        "templates": [_template_obj(n) for n in effective],
        "resolvedSource": resolved_source,
        "overridesCore": overrides_core,
        "disabled": disabled,
        # what a "reset to core" would give (names), or null if core has no key.
        "coreTemplates": (builtin_names if builtin_key is not None else None),
        # Shape-level problem with the EFFECTIVE value (a non-list/string/null
        # value) surfaced from _names_from_value, so an invalid binding renders
        # explained rather than as a silent empty row. `disabled` (value is
        # null or an empty list) stays semantically distinct from `error`
        # (value is invalid): a disabled row has error=null.
        "error": error,
    }
    # userValue: the RAW user-registry value for this key (array | null),
    # included only when a user key exists (SPEC §2.2: undefined-as-omitted).
    if user_key is not None:
        entry["userValue"] = user_reg[user_key]
    return entry


def _registry_payload() -> dict:
    """The full GET /api/templates/registry response (SPEC §2.2)."""
    builtin_reg, user_reg, builtin_err, user_err = _load_registries()
    builtin_by_lower = {str(k).lower(): k for k in builtin_reg}
    user_by_lower = {str(k).lower(): k for k in user_reg}

    entries = []
    for key in builtin_reg:
        if str(key).lower() in user_by_lower:
            continue  # replaced by the user's row below (override)
        entries.append(_compute_entry(key, builtin_reg, user_reg, builtin_by_lower, user_by_lower))
    for key in user_reg:
        entries.append(_compute_entry(key, builtin_reg, user_reg, builtin_by_lower, user_by_lower))
    # File keys first, then directory keys (trailing "/"), alpha within — a
    # stable order keyed on the key string (SPEC §2.2: sort by key).
    entries.sort(key=lambda e: (e["key"].endswith("/"), e["key"]))

    return {
        "sources": _sources_payload(),
        "entries": entries,
        # Back-compat fields so old Preferences code keeps working (SPEC §2.2).
        "builtin_registry": server.BUILTIN_REGISTRY,
        "user_registry": server.USER_REGISTRY,
        "error": builtin_err or user_err,
    }


def _single_entry(key) -> dict | None:
    """Recompute one entry from the live registries; None if neither registry
    defines the key (case-insensitive)."""
    builtin_reg, user_reg, _be, _ue = _load_registries()
    builtin_by_lower = {str(k).lower(): k for k in builtin_reg}
    user_by_lower = {str(k).lower(): k for k in user_reg}
    kl = str(key).lower()
    if kl not in builtin_by_lower and kl not in user_by_lower:
        return None
    # Prefer the user's casing when the key exists there (the row's display key).
    display_key = user_by_lower.get(kl) or builtin_by_lower.get(kl) or key
    return _compute_entry(display_key, builtin_reg, user_reg, builtin_by_lower, user_by_lower)


def _apply_binding(reg: dict, key: str, value) -> None:
    """Set one user-registry key on `reg` in place (the per-key RMW body shared
    by PUT /api/templates/registry and POST /api/templates/new). Drops any
    case-colliding key first so the registry never holds both ".csv" and ".CSV"
    (resolution is case-insensitive; two rows would be ambiguous), then assigns.
    The caller owns loading `reg` and persisting it with storage.write_json."""
    for k in list(reg):
        if k != key and str(k).lower() == key.lower():
            del reg[k]
    reg[key] = value


def _sweep_registry_name(reg: dict, name: str) -> list:
    """Drop every reference to template `name` from the USER registry `reg` in
    place; returns the keys that changed (D109). A list value loses the name; a
    bare-string value equal to it matches too. A key whose value the sweep
    EMPTIES is removed entirely (revert to core) — leaving `[]` would mean
    *disabled* (D95), silently converting a prune into a disable the user never
    picked. Matching is exact: names are folder identities, not lowercased like
    keys. null/`[]`/non-referencing values are left byte-for-byte as-is."""
    cleaned = []
    for key in list(reg):
        value = reg[key]
        if isinstance(value, str):
            if value == name:
                del reg[key]
                cleaned.append(key)
        elif isinstance(value, list):
            kept = [n for n in value if n != name]
            if len(kept) != len(value):
                if kept:
                    reg[key] = kept
                else:
                    del reg[key]
                cleaned.append(key)
    return cleaned


# -- template folder inventory ------------------------------------------------


def _folders_with_template(base: str) -> dict:
    """name -> {hasIcon, hasCondition} for every immediate subdir of `base` that
    contains a template.html (a template folder; SPEC §0 — folder name =
    identity). Dirs without template.html (vendor/, shared/) are naturally
    excluded. hasCondition reports the optional condition.py gate (SPEC CT-12)
    so the management UI can flag templates that only show for some files."""
    out = {}
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        if not os.path.isfile(os.path.join(folder, "template.html")):
            continue
        out[name] = {
            "hasIcon": os.path.isfile(os.path.join(folder, "icon.svg")),
            "hasCondition": os.path.isfile(os.path.join(folder, "condition.py")),
        }
    return out


def _effective_bindings() -> dict:
    """key -> effective ordered name list, for the whole merged registry — used
    to compute each template's `usedBy`. Sentinels stay in the lists (harmless;
    they never match a template folder name)."""
    builtin_reg, user_reg, _be, _ue = _load_registries()
    builtin_by_lower = {str(k).lower(): k for k in builtin_reg}
    user_by_lower = {str(k).lower(): k for k in user_reg}
    bindings = {}

    def add(display_key):
        entry = _compute_entry(display_key, builtin_reg, user_reg, builtin_by_lower, user_by_lower)
        bindings[entry["key"]] = [] if entry["disabled"] else [t["name"] for t in entry["templates"]]

    for key in builtin_reg:
        if str(key).lower() in user_by_lower:
            continue
        add(key)
    for key in user_reg:
        add(key)
    return bindings


def _inventory_payload() -> dict:
    """GET /api/templates/inventory (SPEC §2.1) — the resolved template pool
    across sources. A user folder shadowing a core folder of the same name is
    emitted ONCE as source=user, shadowsCore=true (the hidden core one is not
    emitted)."""
    core = _folders_with_template(server.TEMPLATES_DIR)
    user = _folders_with_template(server.USER_TEMPLATES_DIR)
    core_dir = os.path.abspath(server.TEMPLATES_DIR)
    user_dir = os.path.abspath(server.USER_TEMPLATES_DIR)

    bindings = _effective_bindings()
    used_by = {}
    for key, names in bindings.items():
        for name in names:
            used_by.setdefault(name, []).append(key)

    templates = []
    for name in sorted(set(core) | set(user)):
        if name in user:
            src, editable, meta = "user", True, user[name]
            shadows = name in core
            folder_path = os.path.join(user_dir, name)
        else:
            src, editable, meta = "core", False, core[name]
            shadows = False
            folder_path = os.path.join(core_dir, name)
        templates.append(
            {
                "name": name,
                "source": src,
                "editable": editable,
                "hasIcon": meta["hasIcon"],
                "hasCondition": meta["hasCondition"],
                "usedBy": sorted(used_by.get(name, [])),
                "shadowsCore": shadows,
                "path": folder_path,
            }
        )

    return {"sources": _sources_payload(), "templates": templates}


# -- routes: registry read + bindings edit ------------------------------------


@router.get("/api/templates/inventory")
def api_templates_inventory():
    return _inventory_payload()


@router.get("/api/templates/registry")
def api_templates_registry():
    # SPEC §2.2: the merged extension->templates binding view. Read-only GET
    # (no X-Fused): a foreign page can't read the response cross-origin anyway.
    return _registry_payload()


@router.put("/api/templates/registry")
def api_put_registry(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    # SPEC §2.3: upsert ONE user key. Read-modify-write the USER registry only.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    key = body.get("key")
    value = body.get("value")
    if not isinstance(key, str) or not key:
        return _error("'key' must be a non-empty string")
    # Grammar: a valid pattern for its population (dir key iff trailing "/").
    is_dir = key.endswith("/")
    if server._key_segments(key, is_dir) is None:
        return _error(
            f"invalid registry key {key!r}: must be a dot-anchored suffix pattern "
            "(e.g. '.csv', '.xyz.json', '.*.json', or a directory key '.zarr/')"
        )
    if value is not None and not isinstance(value, list):
        return _error("'value' must be an array of template names or null (to disable)")
    if isinstance(value, list):
        # Names need only be non-empty strings — a name that resolves to no
        # folder is allowed and saved as a dangling ref (surfaced broken in the
        # UI, dropped at render), so a user can keep an in-progress or
        # not-yet-created template bound. Only structurally invalid entries are
        # rejected here.
        bad = [n for n in value if not isinstance(n, str) or not n.strip()]
        if bad:
            return _error("each template name must be a non-empty string")

    # Load the user registry for writing; refuse to clobber a corrupt file (a
    # whole-file rewrite would otherwise drop every other binding).
    reg, err = server._load_registry(server.USER_REGISTRY, "registry.json")
    if err:
        return _error(
            f"refusing to overwrite the user registry: {err}. Move "
            f"{server.USER_REGISTRY} aside and retry.",
        )
    reg = reg if isinstance(reg, dict) else {}
    _apply_binding(reg, key, value)
    storage.write_json(server.USER_REGISTRY, reg)

    return _single_entry(key)


@router.post("/api/templates/registry/reset")
def api_reset_registry(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    # SPEC §2.4: remove the user override for a key (revert to core).
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    key = body.get("key")
    if not isinstance(key, str) or not key:
        return _error("'key' must be a non-empty string")

    reg, err = server._load_registry(server.USER_REGISTRY, "registry.json")
    if err:
        return _error(
            f"refusing to overwrite the user registry: {err}. Move "
            f"{server.USER_REGISTRY} aside and retry.",
        )
    reg = reg if isinstance(reg, dict) else {}
    removed = False
    for k in list(reg):
        if str(k).lower() == key.lower():
            del reg[k]
            removed = True
    if removed:
        storage.write_json(server.USER_REGISTRY, reg)

    entry = _single_entry(key)
    if entry is None:
        # No builtin key either -> nothing left to show for this key.
        return {"key": key, "removed": True}
    return entry


# -- routes: export -----------------------------------------------------------

# Name of the sidecar written at the zip root by export and read back by import
# staging: the exported templates' registry bindings, carried as
# RECOMMENDATIONS (the importer chooses which to apply at commit) — never as
# registry rows to merge blindly.
_REC_FILENAME = "recommendation.json"


def _recommendations_for(names: list) -> dict:
    """template name -> sorted registry keys whose EFFECTIVE (merged,
    user-over-core) binding list contains it. Names with zero bindings are
    omitted; both levels are sorted so the sidecar is deterministic."""
    bindings = _effective_bindings()
    recs = {}
    for name in sorted(names):
        keys = sorted(k for k, ns in bindings.items() if name in ns)
        if keys:
            recs[name] = keys
    return recs


def _resolve_export_folder(name: str) -> str | None:
    """Resolve `name` to its template folder for export (SPEC §2.5): user
    shadow wins, else core. None if `name` is unsafe (path separators/`.`/`..`)
    or resolves to no folder in either source."""
    if not isinstance(name, str) or not name:
        return None
    if "/" in name or "\\" in name or name in (".", ".."):
        return None
    user_folder = os.path.join(server.USER_TEMPLATES_DIR, name)
    if os.path.isdir(user_folder):
        return user_folder
    core_folder = os.path.join(server.TEMPLATES_DIR, name)
    if os.path.isdir(core_folder):
        return core_folder
    return None


@router.get("/api/templates/export")
def api_export_templates(names: list[str] = Query(default=[])):
    # SPEC §2.5: stream a zip of the named templates — core OR user, resolved
    # with user-shadow-wins (folders only, no registry.json). A GET so the
    # browser can trigger it. Names arrive as repeated `?names=a&names=b` params
    # (not comma-joined) so a template folder whose name contains a comma still
    # round-trips.
    requested = [n.strip() for n in names if n and n.strip()]
    if not requested:
        return _error("provide 'names' — one or more template names")
    folders = {n: _resolve_export_folder(n) for n in requested}
    bad = [n for n, f in folders.items() if f is None]
    if bad:
        return _error(
            "no such template: "
            + ", ".join(repr(n) for n in bad)
            + f" (looked in {server.USER_TEMPLATES_DIR} and {server.TEMPLATES_DIR})"
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in requested:
            folder = folders[name]
            for root, dirs, files in os.walk(folder, followlinks=False):
                # Don't follow symlinked subdirs; skip symlinked files too.
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for fname in files:
                    full = os.path.join(root, fname)
                    if os.path.islink(full):
                        continue
                    rel = os.path.relpath(full, folder).replace(os.sep, "/")
                    zf.write(full, arcname=f"{name}/{rel}")
        # Sidecar at the zip root: each exported template's current merged
        # bindings, so an import elsewhere can OFFER them (never auto-apply).
        zf.writestr(
            _REC_FILENAME,
            json.dumps({"version": 1, "recommendations": _recommendations_for(requested)}, indent=2),
        )
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="fused-render-templates.zip"'},
    )


# -- routes: delete -----------------------------------------------------------


@router.post("/api/templates/delete")
def api_delete_template(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    # SPEC §2.8 / TV-19: delete ONE user template folder. Only USER_TEMPLATES_DIR
    # is touched — **core templates are read-only and never deletable**. With
    # `cleanRegistry: true` (D109) the USER registry is also swept of bindings
    # that referenced the name; without it they are left as-is (they resolve
    # broken until rebound), matching export/import being folder-only.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    name = body.get("name")
    if not isinstance(name, str) or not name:
        return _error("'name' must be a non-empty string")
    if "/" in name or "\\" in name or name in (".", ".."):
        return _error("invalid template name")
    clean_registry = body.get("cleanRegistry") is True

    folder = os.path.join(server.USER_TEMPLATES_DIR, name)
    # Reject symlinks (never follow one out of the user dir) and anything that
    # is not a real user directory — a core-only template resolves here to a
    # path that does not exist under USER_TEMPLATES_DIR, so it 404s.
    if os.path.islink(folder) or not os.path.isdir(folder):
        return _error(f"no user template named {name!r} to delete", status=404)

    if clean_registry:
        # Refuse a corrupt user registry BEFORE the destructive rmtree (same
        # posture as PUT) — a refusal must leave the folder intact so the user
        # can fix the registry and retry the whole gesture.
        _, err = server._load_registry(server.USER_REGISTRY, "registry.json")
        if err:
            return _error(
                f"refusing to overwrite the user registry: {err}. Move "
                f"{server.USER_REGISTRY} aside and retry.",
            )

    shutil.rmtree(folder)
    if not clean_registry:
        return {"deleted": name}

    # Re-read AFTER the rmtree so the sweep rewrites the registry as it is
    # now, not the pre-check's snapshot — a binding edited concurrently while
    # the gesture was in flight must survive the write below.
    reg, err = server._load_registry(server.USER_REGISTRY, "registry.json")
    if err:
        # The folder is already gone; nothing destructive left to refuse. Skip
        # the sweep rather than overwrite a registry we can no longer parse.
        return {"deleted": name, "registryKeysCleaned": [], "registryError": err}
    reg = reg if isinstance(reg, dict) else {}

    cleaned = _sweep_registry_name(reg, name)
    if cleaned:
        storage.write_json(server.USER_REGISTRY, reg)
    return {"deleted": name, "registryKeysCleaned": cleaned}


# -- routes: import (stage -> commit) -----------------------------------------

IMPORT_TTL_SEC = 900  # staged imports expire after 15 minutes (SPEC §2.6/2.7)
MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB across the whole zip
MAX_ENTRY_UNCOMPRESSED = 25 * 1024 * 1024  # 25 MB per single entry
MAX_ENTRIES = 2000  # zip-bomb entry-count guard
_COPY_CHUNK = 64 * 1024  # bounded read size during extraction (zip-bomb guard)
_IMPORT_ID_RE = re.compile(r"^[0-9a-f]{8,}$")  # secrets.token_hex output shape


class _ImportTooLarge(Exception):
    """Raised mid-extraction when a zip entry's ACTUAL decompressed bytes breach
    a size cap. The declared ``file_size`` metadata is untrustworthy (a crafted
    zip can understate it), so the caps are enforced on bytes actually written."""


def _staging_root() -> str:
    return os.path.join(storage.home_dir(), ".import-staging")


def _sweep_stale_staging() -> None:
    """Remove staging dirs older than the TTL (opportunistic, best-effort)."""
    root = _staging_root()
    try:
        names = os.listdir(root)
    except OSError:
        return
    now = time.time()
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and (now - os.path.getmtime(path)) > IMPORT_TTL_SEC:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    return stat_mod.S_ISLNK(info.external_attr >> 16)


def _reject_reason(info: zipfile.ZipInfo, staging_dir: str) -> str | None:
    """Why this zip entry is unsafe to extract, or None if it is safe. Guards
    zip-slip (absolute paths, `..` escapes, out-of-root targets) and symlink
    entries (SPEC §2.6)."""
    name = info.filename
    if _is_symlink_entry(info):
        return f"symlink entry not allowed: {name!r}"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or os.path.isabs(name):
        return f"absolute path not allowed: {name!r}"
    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        return f"path escape ('..') not allowed: {name!r}"
    # Defence in depth: the resolved target must stay under the staging root.
    target = os.path.normpath(os.path.join(staging_dir, normalized))
    root = os.path.normpath(staging_dir)
    if target != root and not target.startswith(root + os.sep):
        return f"path escapes the staging directory: {name!r}"
    return None


def _parse_recommendations(staging_dir: str, warnings: list) -> dict:
    """Read the staged root recommendation.json → {template: [keys]}. A missing
    file is a clean no-op ({}); a broken one (bad JSON, wrong shape) is a
    NON-FATAL warning — the import proceeds without recommendations, they are
    never worth failing a stage over. An unknown version is ignored silently
    (a future exporter's sidecar, not an error). Keys that fail the CT-3
    grammar are dropped per-key with a warning so commit never has to reject
    a recommendation the user only ticked."""
    path = os.path.join(staging_dir, _REC_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        warnings.append(f"{_REC_FILENAME} is not valid JSON; ignoring recommendations")
        return {}
    if not isinstance(data, dict):
        warnings.append(f"{_REC_FILENAME} must be a JSON object; ignoring recommendations")
        return {}
    if data.get("version") != 1:
        return {}
    recs = data.get("recommendations")
    if not isinstance(recs, dict):
        warnings.append(f"{_REC_FILENAME} has no 'recommendations' object; ignoring recommendations")
        return {}
    out = {}
    for name, keys in recs.items():
        if not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys):
            warnings.append(f"{_REC_FILENAME}: recommendations for {name!r} must be an array of registry keys; ignored")
            continue
        kept = []
        for key in keys:
            if server._key_segments(key, key.endswith("/")) is None:
                warnings.append(f"{_REC_FILENAME}: ignored invalid registry key {key!r} for {name!r}")
                continue
            kept.append(key)
        if kept:
            out[str(name)] = kept
    return out


def _recommended_key_status(key, name, builtin_reg, user_reg, builtin_by_lower, user_by_lower) -> str:
    """How a recommended key -> name binding relates to the CURRENT merged
    registry: "already-bound" (name already in the key's effective list — a
    no-op if applied), "disabled" (the user explicitly disabled the key with
    null/[]; applying would re-enable it, so the UI must warn), else "new"."""
    entry = _compute_entry(key, builtin_reg, user_reg, builtin_by_lower, user_by_lower)
    if any(t["name"] == name for t in entry["templates"]):
        return "already-bound"
    if entry["disabled"] and "userValue" in entry:
        return "disabled"
    return "new"


@router.post("/api/templates/import")
async def api_import_templates(
    file: UploadFile = File(...), x_fused: str | None = Header(default=None)
):
    # SPEC §2.6: multipart step 1 — validate + unpack into staging, DO NOT
    # commit. importId (secrets.token_hex, no Math.random/Date) names the stage.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    _sweep_stale_staging()

    data = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return _error("the uploaded file is not a valid .zip")

    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        return _error(f"zip has too many entries ({len(infos)} > {MAX_ENTRIES})")
    # NOTE: the per-entry (25 MB) and total (50 MB) size caps are NOT enforced
    # from each entry's declared `file_size` here — that metadata is attacker
    # controlled and a crafted zip can understate it. The caps are enforced
    # below on the bytes ACTUALLY decompressed during extraction.

    import_id = secrets.token_hex(16)
    staging_dir = os.path.join(_staging_root(), import_id)

    # Validate EVERY entry before writing anything, so a rejected zip never
    # leaves a partial staging dir behind.
    for info in infos:
        reason = _reject_reason(info, staging_dir)
        if reason is not None:
            return _error(f"rejected zip: {reason}")

    os.makedirs(staging_dir, exist_ok=True)
    total_written = 0
    try:
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            target = os.path.normpath(os.path.join(staging_dir, normalized))
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Bounded, chunked copy: enforce the caps on the bytes ACTUALLY
            # decompressed (per-entry, then cumulative-total), so a crafted zip
            # that understates file_size cannot expand past the caps. On the
            # first breach we abort and clean up the whole staging dir.
            entry_written = 0
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    entry_written += len(chunk)
                    total_written += len(chunk)
                    if entry_written > MAX_ENTRY_UNCOMPRESSED:
                        raise _ImportTooLarge(
                            f"entry {info.filename!r} is too large "
                            f"(> {MAX_ENTRY_UNCOMPRESSED} bytes uncompressed)"
                        )
                    if total_written > MAX_TOTAL_UNCOMPRESSED:
                        raise _ImportTooLarge(
                            f"zip uncompressed size too large "
                            f"(> {MAX_TOTAL_UNCOMPRESSED} bytes)"
                        )
                    dst.write(chunk)
    except _ImportTooLarge as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return _error(str(e))
    except (OSError, zipfile.BadZipFile) as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return _error(f"could not unpack the zip: {e}")

    # A candidate template = a top-level directory; valid iff it holds a
    # template.html. Top-level files are ignored (warned) — except the
    # recommendation.json sidecar, which is export metadata, not content.
    items = []
    warnings = []
    recommendations = _parse_recommendations(staging_dir, warnings)
    builtin_reg, user_reg, _be, _ue = _load_registries()
    builtin_by_lower = {str(k).lower(): k for k in builtin_reg}
    user_by_lower = {str(k).lower(): k for k in user_reg}
    for name in sorted(os.listdir(staging_dir)):
        path = os.path.join(staging_dir, name)
        if os.path.isfile(path):
            if name == _REC_FILENAME:
                continue  # parsed above; never a "not a template folder" warning
            warnings.append(f"ignored top-level file {name!r} (not a template folder)")
            continue
        if not os.path.isdir(path):
            continue
        has_html = os.path.isfile(os.path.join(path, "template.html"))
        file_count = sum(len(files) for _r, _d, files in os.walk(path))
        item = {
            "name": name,
            "valid": has_html,
            "hasTemplateHtml": has_html,
            "conflictsExisting": os.path.isdir(os.path.join(server.USER_TEMPLATES_DIR, name)),
            "fileCount": file_count,
        }
        rec_keys = recommendations.get(name)
        # Only valid items carry recommendations (an invalid folder can never
        # be committed); items without any omit the field (SPEC §2.2:
        # undefined-as-omitted).
        if has_html and rec_keys:
            item["recommendedKeys"] = [
                {
                    "key": key,
                    "status": _recommended_key_status(
                        key, name, builtin_reg, user_reg, builtin_by_lower, user_by_lower
                    ),
                }
                for key in rec_keys
            ]
        items.append(item)

    return {
        "importId": import_id,
        "expiresInSec": IMPORT_TTL_SEC,
        "items": items,
        "warnings": warnings,
    }


def _unique_name(base: str) -> str:
    """First of base, base-2, base-3, … that is not an existing user template
    folder — keep-both must never clobber (SPEC §2.7)."""
    candidate = base
    n = 2
    while os.path.exists(os.path.join(server.USER_TEMPLATES_DIR, candidate)):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


@router.post("/api/templates/import/{import_id}/commit")
def api_commit_import(
    import_id: str, body: dict = Body(default={}), x_fused: str | None = Header(default=None)
):
    # SPEC §2.7: step 2 — resolve conflicts and MOVE staged folders into the
    # user templates dir, then delete the stage.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    # No opportunistic sweep here (that runs on import, SPEC §2.6): sweeping
    # first would delete an expired stage before its own check, collapsing the
    # 410 (expired) signal into a 404 (unknown).
    if not _IMPORT_ID_RE.match(import_id or ""):
        return _error(f"unknown importId: {import_id!r}", status=404)
    staging_dir = os.path.join(_staging_root(), import_id)
    if not os.path.isdir(staging_dir):
        return _error(f"unknown importId: {import_id!r}", status=404)
    if (time.time() - os.path.getmtime(staging_dir)) > IMPORT_TTL_SEC:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return _error(f"import {import_id!r} has expired; re-upload the zip", status=410)

    resolutions = body.get("resolutions") if isinstance(body, dict) else None
    resolutions = resolutions if isinstance(resolutions, dict) else {}

    # Optional bindings to apply after the moves: ORIGINAL staged name ->
    # registry keys. Validate the whole map up front (same CT-3 grammar as
    # PUT /api/templates/registry) so a bad request never half-applies moves.
    bindings = body.get("bindings") if isinstance(body, dict) else None
    bindings = bindings if isinstance(bindings, dict) else {}
    for orig, keys in bindings.items():
        if not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys):
            return _error(f"'bindings' for {orig!r} must be an array of registry keys")
        for key in keys:
            if server._key_segments(key, key.endswith("/")) is None:
                return _error(
                    f"invalid registry key {key!r}: must be a dot-anchored suffix pattern "
                    "(e.g. '.csv', '.xyz.json', '.*.json', or a directory key '.zarr/')"
                )
    user_reg = {}
    if any(bindings.values()):
        # Refuse to touch a corrupt user registry (a rewrite would drop every
        # other binding) BEFORE moving anything — same posture as PUT.
        user_reg, reg_err = server._load_registry(server.USER_REGISTRY, "registry.json")
        if reg_err:
            return _error(
                f"refusing to overwrite the user registry: {reg_err}. Move "
                f"{server.USER_REGISTRY} aside and retry.",
            )
        user_reg = user_reg if isinstance(user_reg, dict) else {}

    os.makedirs(server.USER_TEMPLATES_DIR, exist_ok=True)

    imported, skipped, overwritten, renamed = [], [], [], {}
    landed = {}  # ORIGINAL staged name -> final folder name, for bindings below
    # Make the commit all-or-nothing over USER_TEMPLATES_DIR: track every applied
    # move (and every displaced original) so that if a later os.rename raises we
    # can undo the earlier ones instead of returning 500 with a half-applied
    # import and inconsistent staging (single local user, D3).
    applied: list[tuple[str, str]] = []  # (final_dest, staged_source) to reverse
    backups: list[tuple[str, str]] = []  # (backup_path, original_target) to restore
    try:
        for name in sorted(os.listdir(staging_dir)):
            staged = os.path.join(staging_dir, name)
            if not os.path.isdir(staged):
                continue  # top-level files were only ever warnings
            if not os.path.isfile(os.path.join(staged, "template.html")):
                continue  # invalid item -> dropped
            resolution = resolutions.get(name, "skip")
            if resolution not in ("overwrite", "skip", "keep-both"):
                resolution = "skip"
            if resolution == "skip":
                skipped.append(name)
                continue

            if resolution == "overwrite":
                target = os.path.join(server.USER_TEMPLATES_DIR, name)
                if os.path.exists(target):
                    # Displace the original to a sibling backup first; keep the
                    # backup until the whole commit succeeds so it can be
                    # restored on a later failure.
                    backup = target + f".bak.{secrets.token_hex(4)}"
                    os.rename(target, backup)
                    backups.append((backup, target))
                    os.rename(staged, target)
                    applied.append((target, staged))
                    overwritten.append(name)
                else:
                    os.rename(staged, target)
                    applied.append((target, staged))
                imported.append(name)
                landed[name] = name
            else:  # keep-both
                final = _unique_name(name)
                dest = os.path.join(server.USER_TEMPLATES_DIR, final)
                os.rename(staged, dest)
                applied.append((dest, staged))
                if final != name:
                    renamed[name] = final
                imported.append(final)
                landed[name] = final
    except OSError as exc:
        # Undo in reverse: move committed folders back to staging, then restore
        # any displaced originals. Staging is dropped either way.
        for dest, src in reversed(applied):
            try:
                os.rename(dest, src)
            except OSError:
                pass
        for backup, target in reversed(backups):
            try:
                if not os.path.exists(target):
                    os.rename(backup, target)
                else:
                    shutil.rmtree(backup, ignore_errors=True)
            except OSError:
                pass
        shutil.rmtree(staging_dir, ignore_errors=True)
        return _error(f"import failed while applying changes: {exc}", status=500)

    # All moves succeeded — drop the retained backups and the staging dir.
    for backup, _ in backups:
        shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(staging_dir, ignore_errors=True)

    # Apply the requested bindings, keyed by ORIGINAL staged name: a skipped
    # (or invalid) template's bindings are dropped, a keep-both rename binds
    # the FINAL name. Same additive posture as POST /api/templates/new —
    # append to whatever a key already resolves to, never replace it.
    bindings_applied = []
    if bindings:
        builtin_reg, _builtin_err = server._load_registry(
            server.BUILTIN_REGISTRY, "built-in registry.json"
        )
        builtin_reg = builtin_reg if isinstance(builtin_reg, dict) else {}
        builtin_by_lower = {str(k).lower(): k for k in builtin_reg}
        for orig in sorted(bindings):
            final = landed.get(orig)
            if final is None:
                continue  # skipped / dropped-invalid / unknown -> bindings ignored
            for key in bindings[orig]:
                user_by_lower = {str(k).lower(): k for k in user_reg}
                entry = _compute_entry(key, builtin_reg, user_reg, builtin_by_lower, user_by_lower)
                current_names = [t["name"] for t in entry["templates"]]
                if final in current_names:
                    continue  # already effectively bound -> no-op
                if entry["disabled"]:
                    # The user disabled the key (null/[]); a requested binding
                    # re-enables it as core's list + the new name (the frontend
                    # warned the user before sending this).
                    base = entry["coreTemplates"] or []
                else:
                    # Includes the key-only-in-core case: current_names IS the
                    # full core list, so the new user entry starts as a copy of
                    # it — never a shorter shadow over core.
                    base = current_names
                # Re-enabling a disabled key whose core list already holds the
                # name must not duplicate it.
                _apply_binding(user_reg, key, base if final in base else base + [final])
                bindings_applied.append({"key": key, "template": final})
        if bindings_applied:
            storage.write_json(server.USER_REGISTRY, user_reg)

    return {
        "imported": imported,
        "skipped": skipped,
        "overwritten": overwritten,
        "renamed": renamed,
        "bindingsApplied": bindings_applied,
    }


# -- routes: new template (scaffold + bind) + open in Claude ------------------

# The starter kit ships inside the package but DELIBERATELY OUTSIDE
# fused_render/templates/, so it never resolves as a template or shows up in the
# inventory (SPEC §23) — it is a scaffold source, copied into a user folder by
# POST /api/templates/new.
_STARTER_KIT_DIR = os.path.join(os.path.dirname(__file__), "template_starter")

# The two canonical authoring skills copied into every new template's
# .claude/skills/ so a scaffolded (or later exported) folder carries its own
# guidance. Single source is the repo-level skills/<name>/ (D106).
_STARTER_SKILLS = ("fused-render-authoring", "fused-render-custom-templates")
_REPO_SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")


def _ensure_starter_skills(dest: str) -> None:
    """Make sure a freshly-scaffolded template `dest` has the authoring skills
    under .claude/skills/, refreshed from the live repo skills/ dir whenever
    it's resolvable (editable/dev installs — this always wins, since the
    starter kit's own .claude/skills/ is gitignored and may be a stale copy
    left over from a previous local wheel build). Only a true wheel install,
    where the repo skills/ dir isn't reachable at all, relies on whatever
    copytree already brought in from the packaged starter kit. If neither
    source exists, proceed without skills — a missing skill must never fail
    template creation (D106).
    """
    skills_dir = os.path.join(dest, ".claude", "skills")
    for name in _STARTER_SKILLS:
        target = os.path.join(skills_dir, name)
        src = os.path.join(_REPO_SKILLS_DIR, name)
        if os.path.isdir(src):
            try:
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(src, target)
            except OSError:
                pass  # best-effort; the template is still usable without the skill
            continue
        if os.path.isdir(target):
            continue  # wheel install: copytree already brought the packaged skill
        # neither packaged nor resolvable from source — skip, don't fail


def _template_name_error(name) -> str | None:
    """Why `name` is not usable as a template folder/name, or None if it is.
    Matches server._resolve_name's rule (SPEC CT-6): one plain path segment, no
    '/', '\\', or '.', and no leading '_' (reserved for shell sentinels) — so a
    created template's name always resolves by the PT-6 rule."""
    if not isinstance(name, str) or not name:
        return "'name' must be a non-empty string"
    if "/" in name or "\\" in name or "." in name:
        return "invalid template name: use a single folder segment with no '/', '\\' or '.'"
    if name.startswith("_"):
        return "invalid template name: the '_' prefix is reserved for shell sentinel modes"
    return None


@router.post("/api/templates/new")
def api_new_template(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    # Scaffold a new user template from the starter kit and bind the given
    # extensions to it (D87 per-key RMW, reused via _apply_binding). Validate
    # everything up front so a bad request never leaves a half-created folder.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    name = body.get("name")
    name_err = _template_name_error(name)
    if name_err is not None:
        return _error(name_err)

    extensions = body.get("extensions", [])
    if not isinstance(extensions, list):
        return _error("'extensions' must be an array of registry keys")
    # Validate every extension key against the CT-3 grammar (the same check PUT
    # applies) before creating anything.
    keys: list[str] = []
    for ext in extensions:
        if not isinstance(ext, str) or not ext:
            return _error("each extension must be a non-empty registry key string")
        if server._key_segments(ext, ext.endswith("/")) is None:
            return _error(
                f"invalid registry key {ext!r}: must be a dot-anchored suffix pattern "
                "(e.g. '.csv', '.xyz.json', '.*.json', or a directory key '.zarr/')"
            )
        keys.append(ext)

    dest = os.path.join(server.USER_TEMPLATES_DIR, name)
    if os.path.exists(dest):
        return _error(f"a user template named {name!r} already exists", status=409)

    # Refuse to touch a corrupt user registry (a rewrite would drop every other
    # binding) BEFORE creating the folder — but only when a write will actually
    # happen; a no-extensions draft create must not be blocked by a registry
    # problem it never touches.
    reg = {}
    if keys:
        reg, err = server._load_registry(server.USER_REGISTRY, "registry.json")
        if err:
            return _error(
                f"refusing to overwrite the user registry: {err}. Move "
                f"{server.USER_REGISTRY} aside and retry.",
            )
        reg = reg if isinstance(reg, dict) else {}

    os.makedirs(server.USER_TEMPLATES_DIR, exist_ok=True)
    try:
        shutil.copytree(_STARTER_KIT_DIR, dest)
    except FileExistsError:
        # TOCTOU: dest was created between the exists-check above and here.
        return _error(f"a user template named {name!r} already exists", status=409)
    except Exception as exc:
        # A partial copy leaves a half-created folder behind; remove it so a
        # retry sees a clean slate and the exists-check stays meaningful.
        shutil.rmtree(dest, ignore_errors=True)
        return _error(f"failed to create template {name!r}: {exc}")

    # Editable installs have no packaged skills in the starter kit; resolve them
    # from the repo skills/ dir so the scaffolded folder still carries guidance.
    _ensure_starter_skills(dest)

    if keys:
        # Additive only: append the new template to whatever list a key
        # already resolves to (its user override, or the core default if the
        # user has no override yet) — never replace an existing multi-mode
        # binding with just this one name.
        builtin_reg, _builtin_err = server._load_registry(
            server.BUILTIN_REGISTRY, "built-in registry.json"
        )
        builtin_reg = builtin_reg if isinstance(builtin_reg, dict) else {}
        builtin_by_lower = {str(k).lower(): k for k in builtin_reg}
        for key in keys:
            user_by_lower = {str(k).lower(): k for k in reg}
            entry = _compute_entry(key, builtin_reg, reg, builtin_by_lower, user_by_lower)
            current_names = [t["name"] for t in entry["templates"]]
            _apply_binding(reg, key, current_names + [name])
        try:
            storage.write_json(server.USER_REGISTRY, reg)
        except Exception as exc:
            # The folder is otherwise complete and usable, but leaving it
            # behind after a reported failure means a retry always 409s. Clean
            # up so the error is honest: nothing was created.
            shutil.rmtree(dest, ignore_errors=True)
            return _error(f"failed to bind template {name!r}: {exc}")

    return {"ok": True, "name": name, "path": dest, "bindings": keys}


def _claude_bin() -> str:
    """Locate the `claude` CLI. Mirrors templates/claude/agent.py:_claude_bin —
    replicated here rather than imported, since a template folder is not an
    import root (and templates_api must not depend on a template's internals)."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        candidate = os.path.expanduser(candidate)
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        "claude CLI not found — install Claude Code or put `claude` on the PATH "
        "of the environment that launched fused-render"
    )


def _applescript_str(s: str) -> str:
    """Quote a Python string as an AppleScript double-quoted literal (escape
    backslash then double-quote)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@router.post("/api/templates/open-in-claude")
def api_open_in_claude(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    # Open Terminal.app in a user template's folder and start `claude` there so
    # the author can iterate on the template with the CLI. macOS-only for now.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    if sys.platform != "darwin":
        return _error(
            "Open in Claude is only supported on macOS (it spawns Terminal.app).",
            status=400,
        )

    name = body.get("name")
    if not isinstance(name, str) or not name:
        return _error("'name' must be a non-empty string")
    if "/" in name or "\\" in name or name in (".", ".."):
        return _error("invalid template name")

    # User templates only — a core-only name resolves to no user folder and 404s
    # (core folders live in the package, never opened for editing here).
    folder = os.path.join(server.USER_TEMPLATES_DIR, name)
    if os.path.islink(folder) or not os.path.isdir(folder):
        return _error(f"no user template named {name!r}", status=404)

    try:
        claude_bin = _claude_bin()
    except FileNotFoundError as exc:
        return _error(str(exc))

    # shlex.quote makes the paths safe for the shell that `do script` runs;
    # _applescript_str then escapes that command for the AppleScript literal.
    shell_cmd = f"cd {shlex.quote(folder)} && {shlex.quote(claude_bin)}"
    open_script = 'tell application "Terminal" to do script ' + _applescript_str(shell_cmd)
    activate_script = 'tell application "Terminal" to activate'
    try:
        subprocess.run(["osascript", "-e", open_script, "-e", activate_script], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return _error(f"failed to open Terminal: {exc}", status=500)

    return {"ok": True}
