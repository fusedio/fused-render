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
import os
import re
import secrets
import shutil
import stat as stat_mod
import time
import zipfile

from fastapi import APIRouter, Body, File, Header, UploadFile
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
    """The builtin registry's (splice-expanded) name list for a key, or []."""
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
        # Shape-level problem with the EFFECTIVE value (e.g. >1 "..." splice or
        # a non-list/string/null value) surfaced from _names_from_value, so an
        # invalid binding renders explained rather than as a silent empty row.
        # `disabled` (value is null) stays semantically distinct from `error`
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


# -- template folder inventory ------------------------------------------------


def _folders_with_template(base: str) -> dict:
    """name -> {hasIcon} for every immediate subdir of `base` that contains a
    template.html (a template folder; SPEC §0 — folder name = identity). Dirs
    without template.html (vendor/, shared/) are naturally excluded."""
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
        out[name] = {"hasIcon": os.path.isfile(os.path.join(folder, "icon.svg"))}
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
            src, editable, has_icon = "user", True, user[name]["hasIcon"]
            shadows = name in core
            folder_path = os.path.join(user_dir, name)
        else:
            src, editable, has_icon = "core", False, core[name]["hasIcon"]
            shadows = False
            folder_path = os.path.join(core_dir, name)
        templates.append(
            {
                "name": name,
                "source": src,
                "editable": editable,
                "hasIcon": has_icon,
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


def _validate_binding_names(value: list) -> list:
    """Return the list of names in `value` that don't resolve — a name is OK if
    it is a known sentinel, the "..." splice token, or resolves to an existing
    template folder (core or user)."""
    unknown = []
    for name in value:
        if name in server.KNOWN_SENTINELS or name == "...":
            continue
        path, _err = server._resolve_name(name)
        if path is None:
            unknown.append(name)
    return unknown


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
        unknown = _validate_binding_names(value)
        if unknown:
            return _error(
                "unknown template name(s): "
                + ", ".join(repr(n) for n in unknown)
                + " — each name must be an existing template folder (core or user)"
            )

    # Load the user registry for writing; refuse to clobber a corrupt file (a
    # whole-file rewrite would otherwise drop every other binding).
    reg, err = server._load_registry(server.USER_REGISTRY, "registry.json")
    if err:
        return _error(
            f"refusing to overwrite the user registry: {err}. Move "
            f"{server.USER_REGISTRY} aside and retry.",
        )
    reg = reg if isinstance(reg, dict) else {}
    # Drop any case-colliding key so the registry never holds both ".csv" and
    # ".CSV" (resolution is case-insensitive; two rows would be ambiguous).
    for k in list(reg):
        if k != key and str(k).lower() == key.lower():
            del reg[k]
    reg[key] = value
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
def api_export_templates(names: str = ""):
    # SPEC §2.5: stream a zip of the named templates — core OR user, resolved
    # with user-shadow-wins (folders only, no registry.json). A GET so the
    # browser can trigger it via <a href download>.
    requested = [n for n in (names.split(",") if names else []) if n.strip()]
    requested = [n.strip() for n in requested]
    if not requested:
        return _error("provide 'names' — a comma-separated list of template names")
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
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="fused-render-templates.zip"'},
    )


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
    # template.html. Top-level files are ignored (warned).
    items = []
    warnings = []
    for name in sorted(os.listdir(staging_dir)):
        path = os.path.join(staging_dir, name)
        if os.path.isfile(path):
            warnings.append(f"ignored top-level file {name!r} (not a template folder)")
            continue
        if not os.path.isdir(path):
            continue
        has_html = os.path.isfile(os.path.join(path, "template.html"))
        file_count = sum(len(files) for _r, _d, files in os.walk(path))
        items.append(
            {
                "name": name,
                "valid": has_html,
                "hasTemplateHtml": has_html,
                "conflictsExisting": os.path.isdir(os.path.join(server.USER_TEMPLATES_DIR, name)),
                "fileCount": file_count,
            }
        )

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

    os.makedirs(server.USER_TEMPLATES_DIR, exist_ok=True)

    imported, skipped, overwritten, renamed = [], [], [], {}
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
                # Swap via a sibling backup so the live folder is missing only
                # for the duration of two renames (single local user, D3).
                backup = target + f".bak.{secrets.token_hex(4)}"
                os.rename(target, backup)
                try:
                    os.rename(staged, target)
                except OSError:
                    os.rename(backup, target)  # restore on failure
                    raise
                shutil.rmtree(backup, ignore_errors=True)
                overwritten.append(name)
            else:
                os.rename(staged, target)
            imported.append(name)
        else:  # keep-both
            final = _unique_name(name)
            os.rename(staged, os.path.join(server.USER_TEMPLATES_DIR, final))
            if final != name:
                renamed[name] = final
            imported.append(final)

    shutil.rmtree(staging_dir, ignore_errors=True)
    return {
        "imported": imported,
        "skipped": skipped,
        "overwritten": overwritten,
        "renamed": renamed,
    }
