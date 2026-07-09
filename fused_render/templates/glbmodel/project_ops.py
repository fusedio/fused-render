"""Project backend for the glbmodel editor — scaffold / import-part / delete.

Ported from the reference model-editor's core/project_ops.py, stripped of the
bpy authoring tier and the workspace/repo-root/build.py model of a "project".
Here a model project is a single self-contained `*.glbproj/` directory (the
editable unit fused-render routes to this template), holding:

    <name>.glbproj/
        parts/manifest.json     {"parts": ["Head", "Torso", ...]}
        parts/<Name>.glb        one frozen part GLB per entry
        placements.json         saved placement/material tier (viewer-owned)
        overrides.json          unsaved tier (viewer-owned, created lazily)
        .trash/                 soft-deleted parts

Stdlib only (no external deps) — the packaged app ships no pip. Called by the
viewer as fused.runPython("./project_ops.py", {action, model_dir, ...}); the
project dir arrives as an absolute path (the template's `_file` param).
Expected failures return {"error": str} for inline display; tracebacks are
reserved for bugs.
"""

import base64
import json
import os
import re

try:
    import fused
    _udf = fused.udf
except ImportError:  # headless / plain-python testing
    def _udf(f):
        return f

_MAX_IMPORT = 25 * 2 ** 20  # decoded bytes; base64 rides one 30s runPython


def _check_proj(model_dir: str) -> str | None:
    """Guard the trust boundary: writes only ever land inside a `*.glbproj/`
    directory, never an arbitrary path handed in via the param."""
    if not model_dir:
        return "no model_dir given"
    if not model_dir.rstrip("/").endswith(".glbproj"):
        return f"not a .glbproj directory: {model_dir}"
    return None


def _parts_dir(model_dir: str) -> str:
    return os.path.join(model_dir, "parts")


def _read_manifest(model_dir: str) -> list:
    manifest = os.path.join(_parts_dir(model_dir), "manifest.json")
    if not os.path.exists(manifest):
        return []
    with open(manifest) as fh:
        return json.load(fh).get("parts", [])


def _write_manifest(model_dir: str, parts: list) -> None:
    with open(os.path.join(_parts_dir(model_dir), "manifest.json"), "w") as fh:
        json.dump({"parts": parts}, fh, indent=2)


def _scaffold(model_dir: str) -> None:
    """Create the empty-project skeleton if missing. Idempotent — an existing
    project keeps its parts/manifest untouched."""
    os.makedirs(_parts_dir(model_dir), exist_ok=True)
    manifest = os.path.join(_parts_dir(model_dir), "manifest.json")
    if not os.path.exists(manifest):
        _write_manifest(model_dir, [])
    placements = os.path.join(model_dir, "placements.json")
    if not os.path.exists(placements):
        with open(placements, "w") as fh:
            fh.write("{}")


def _create(model_dir: str) -> dict:
    if os.path.exists(os.path.join(_parts_dir(model_dir), "manifest.json")):
        return {"error": f"{os.path.basename(model_dir)} already exists"}
    _scaffold(model_dir)
    return {"model_dir": model_dir, "parts": []}


def _info(model_dir: str) -> dict:
    _scaffold(model_dir)
    return {"model_dir": model_dir, "parts": _read_manifest(model_dir)}


def _write_part(model_dir: str, name: str, data_b64: str) -> dict:
    """Validated writer for a client-transformed part GLB. The browser's
    import_part.mjs performs the glTF transformation (material split,
    fit-to-height/pivot normalization, node-tree flatten); this action
    re-checks only the trust-boundary invariants (name sanitization, base64
    decode, size cap, glTF magic bytes) and commits the already-transformed
    bytes to parts/<name>.glb + manifest.json.

    The caller (import_part.mjs) already resolved unique names against the
    current manifest, so a name that collides with an on-disk <name>.glb NOT
    in the manifest is rejected (a stale/partial write); a name already in the
    manifest is accepted as an overwrite (re-importing the same part)."""
    pname = re.sub(r"[^A-Za-z0-9_-]", "", name or "")
    if not pname:
        return {"error": f"can't derive a part name from {name!r}"}
    try:
        data = base64.b64decode(data_b64, validate=True)
    except Exception:
        return {"error": "invalid base64 payload"}
    if len(data) > _MAX_IMPORT:
        return {"error": f"file too large ({len(data) // 2 ** 20} MB > "
                         f"{_MAX_IMPORT // 2 ** 20} MB)"}
    if data[:4] != b"glTF":
        return {"error": "not a binary glTF (.glb) file"}

    _scaffold(model_dir)
    manifest = _read_manifest(model_dir)
    part_path = os.path.join(_parts_dir(model_dir), f"{pname}.glb")
    if pname not in manifest and os.path.exists(part_path):
        return {"error": f"{pname}.glb already exists and isn't in the manifest"}
    with open(part_path, "wb") as fh:
        fh.write(data)
    if pname not in manifest:
        _write_manifest(model_dir, manifest + [pname])
    return {"part": pname, "path": os.path.join("parts", f"{pname}.glb")}


def _strip_part_key(path: str, part: str) -> None:
    """Remove ONLY this part's entry from a placement file — other parts'
    entries (possibly unsaved user nudges) must survive untouched."""
    if not os.path.exists(path):
        return
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(data, dict) and part in data:
        del data[part]
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)


def _delete_part(model_dir: str, name: str) -> dict:
    """Soft-delete a part: move its GLB into <model_dir>/.trash/, drop it from
    the manifest, and strip its placement/override entries."""
    manifest = _read_manifest(model_dir)
    if name not in manifest:
        return {"error": f"unknown part: {name}"}
    trash = os.path.join(model_dir, ".trash")
    os.makedirs(trash, exist_ok=True)
    src = os.path.join(_parts_dir(model_dir), f"{name}.glb")
    dst, n = os.path.join(trash, f"{name}.glb"), 2
    while os.path.exists(dst):
        dst = os.path.join(trash, f"{name}-{n}.glb")
        n += 1
    if os.path.exists(src):
        os.rename(src, dst)
    _write_manifest(model_dir, [p for p in manifest if p != name])
    for fname in ("placements.json", "overrides.json"):
        _strip_part_key(os.path.join(model_dir, fname), name)
    return {"part": name, "trash": os.path.relpath(dst, model_dir)}


@_udf
def main(action: str, model_dir: str = "", name: str = "",
         data_b64: str = "") -> dict:
    err = _check_proj(model_dir)
    if err:
        return {"error": err}
    model_dir = model_dir.rstrip("/")
    if action == "create":
        return _create(model_dir)
    if action == "info":
        return _info(model_dir)
    if action == "write_part":
        return _write_part(model_dir, name, data_b64)
    if action == "delete_part":
        return _delete_part(model_dir, name)
    return {"error": f"unknown action {action!r}"}
