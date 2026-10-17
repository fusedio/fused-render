"""Embedded-workbench canvas snapshots → `canvas.toml` on disk.

The canvas sync's one writer to the server is the clone watcher (canvases.py).
Under embed v3 the workbench inside /canvases/<name> never saves to the server:
on its usual autosave cadence it posts the body it WOULD have saved — minus UDF
code — over the postMessage bridge, the workspace page relays it to
`POST /api/canvases/snapshot`, and this module folds it into the clone. The
watcher then publishes the folder exactly as it publishes a Claude edit.

Two halves, both pure except for the final write:

  `compact_state(body)`   — the studio JSON the workbench sends → the compact
                            shape `canvas.toml` holds (one row per node keyed
                            by udfName, edges as name pairs, viewport). This
                            mirrors the application server's
                            `toml_translator._studio_node_to_compact`; the
                            key table below is the contract, pinned by a test
                            against the translator's own list.
  `apply_snapshot(...)`   — merge that compact state into the toml on disk at
                            NODE granularity, given the previous snapshot from
                            the same embed:

      node in new, row differs from disk    → upsert the row (keys the embed
                                              does not send are kept)
      node in prev but not in new           → the embed deleted it: drop the
                                              row and its edges, move its
                                              source file to trash
      node on disk, unknown to the embed    → kept untouched (Claude added it
                                              since the embed last hydrated)
      edges                                 → the embed's set, plus every disk
                                              edge touching a node the embed
                                              does not know
      viewport / viewportBounds / comments  → the embed's, always
      a new UDF (no backend id, udf_body present) → its source file is CREATED
                                              from the body; an existing file
                                              is never overwritten (code is
                                              disk-owned, S10)

  `prev is None` (the first snapshot after a hydration, or after a server
  restart) means "the embed's base is whatever it just loaded": nothing is
  deleted and only rows that actually differ are written, so the first
  snapshot of a generation is a no-op on a clean clone. That no-op matters:
  a write that changed nothing would still bump mtimes, the watcher would
  push, the push would ping the embed, the embed would re-hydrate and send
  another first snapshot — a loop. Every write here is content-gated.

No import of fused_render.server (canvases.py includes this; keep it
acyclic). `rtoml` comes from the bundled fused-py; a missing module is a
clear error, not a silent skip.
"""
from __future__ import annotations

import os
import time
from typing import Any

# -- the studio → compact key table ---------------------------------------
#
# Mirrors toml_translator._studio_node_to_compact in fusedlabs/application.
# Geometry comes from `position`/`style`/top-level, chrome from `data`, the
# styling keys straight off `data`. A new key the workbench starts emitting
# must be added HERE (and to the test that pins this list against the
# translator), or it silently stops persisting from the embed — which is the
# one failure mode an ops-shaped sync has, and why the list is explicit.
NODE_DATA_KEYS = (
    "textBoxColor",
    "textBoxGradient",
    "textBoxScaleFactor",
    "textBoxAlignment",
    "nodeDescriptionScaleFactor",
    "widgetBackgroundColor",
    "folderName",
    "folderColor",
    "childUdfOrder",
)
# Catalog keys the translator copies off `data` for non-folder nodes.
NODE_CATALOG_KEYS = (
    "assetUrl",
    "tags",
    "filePreview",
    "filePreviewExtensions",
    "filePreviewFileName",
    "filePreviewRegex",
    "filePreviewMenuOrder",
)
# Every key a compact row may carry from the embed. Keys on a disk row that
# are NOT in this set are never touched by an upsert (they are the server's
# or Claude's business).
EMBED_ROW_KEYS = frozenset(
    ("udfName", "x", "y", "zIndex", "width", "height", "type", "title",
     "description", "visible", "isLocked")
    + NODE_DATA_KEYS + NODE_CATALOG_KEYS
)
# `[canvas]` scalars the embed owns outright.
CANVAS_KEYS = ("viewport", "viewportBounds", "comments")

_SYNTHETIC_DESCRIPTION = "Exported from Fused UDF Workbench"


def _trimmed(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def node_name(node: dict, index: int) -> str:
    data = node.get("data") or {}
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    for candidate in (data.get("udfName"), data.get("folderName"), config.get("title")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return f"udf_{index}"


def compact_node(node: dict, index: int) -> dict:
    """One `[[canvas.nodes]]` row from a studio node, the translator's way."""
    data = node.get("data") or {}
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    position = node.get("position") or {}
    style = node.get("style") or {}
    name = node_name(node, index)
    row: dict[str, Any] = {
        "udfName": name,
        "x": position.get("x"),
        "y": position.get("y"),
        "zIndex": node.get("zIndex"),
        "width": node.get("width", style.get("width")),
        "height": node.get("height", style.get("height")),
    }
    node_type = str(node.get("type") or "simple-udf")
    if node_type != "simple-udf":
        row["type"] = node_type
    # Title / description, resolved as toml_text.resolve_node_title_and_description.
    description = _trimmed(data.get("nodeDescription"))
    if not description:
        cfg_desc = config.get("description")
        if isinstance(cfg_desc, str) and cfg_desc.strip():
            desc = cfg_desc.strip()
            if not (desc == _SYNTHETIC_DESCRIPTION or desc == f"UDF {name}"):
                description = desc
    title = _trimmed(data.get("nodeTitle"))
    if not title:
        cfg_title = _trimmed(config.get("title"))
        if cfg_title and cfg_title != name:
            title = cfg_title
    row["title"] = title or name
    if description:
        row["description"] = description
    visible = config.get("visible")
    if visible is not None:
        row["visible"] = visible
    for key in NODE_DATA_KEYS:
        if key in data:
            row[key] = data.get(key)
    if node_type == "udf-folder":
        locked = data.get("isLocked")
        if locked is not None and locked is not False:
            row["isLocked"] = locked
    else:
        for key in NODE_CATALOG_KEYS:
            if key in data and data.get(key) is not None:
                row[key] = data.get(key)
    # None geometry is "unknown", never a value to write.
    return {k: v for k, v in row.items() if v is not None}


def compact_state(body: dict) -> dict:
    """The embed's snapshot body → `{nodes: {name: row}, edges: [[a,b]],
    canvas: {viewport, ...}, new_files: [{file, body}], files: {name: file}}`."""
    dashboard = body.get("dashboard_body") or {}
    # The workbench serializes dashboard_body as a JSON STRING (that is what
    # the server's collection save takes); an object is accepted too.
    if isinstance(dashboard, str):
        import json
        try:
            dashboard = json.loads(dashboard)
        except ValueError as exc:
            raise ValueError(f"dashboard_body is not JSON: {exc}") from exc
    studio = dashboard.get("studioState") if isinstance(dashboard, dict) else None
    studio = studio if isinstance(studio, dict) else {}
    raw_nodes = [n for n in (studio.get("nodes") or []) if isinstance(n, dict)]
    nodes: dict[str, dict] = {}
    name_by_id: dict[str, str] = {}
    for index, node in enumerate(raw_nodes):
        row = compact_node(node, index)
        nodes[row["udfName"]] = row
        if node.get("id") is not None:
            name_by_id[str(node.get("id"))] = row["udfName"]
    edges: list[list[str]] = []
    for edge in studio.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src = name_by_id.get(str(edge.get("source")), edge.get("source"))
        dst = name_by_id.get(str(edge.get("target")), edge.get("target"))
        if isinstance(src, str) and src and isinstance(dst, str) and dst:
            edges.append([src, dst])
    canvas = {k: studio[k] for k in CANVAS_KEYS if k in studio}
    files: dict[str, str] = {}
    new_files: list[dict] = []
    for udf in body.get("udfs") or []:
        if not isinstance(udf, dict):
            continue
        slug = udf.get("slug")
        file = udf.get("file")
        if not isinstance(slug, str) or not isinstance(file, str) or "/" in file:
            continue
        files[slug] = file
        # Canvas visibility lives on the UDF row (`canvasVisible` →
        # `is_visible`); `data.config.visible` is its serialization-only
        # mirror, which the server's import reads. Take the mirror when the
        # node carries it, the row otherwise — never leave it unset when the
        # workbench said something.
        row = nodes.get(slug)
        if row is not None and "visible" not in row and isinstance(udf.get("is_visible"), bool):
            row["visible"] = udf["is_visible"]
        udf_body = udf.get("udf_body")
        if udf.get("id") in (None, "") and isinstance(udf_body, str):
            new_files.append({"file": file, "body": udf_body})
    return {"nodes": nodes, "edges": edges, "canvas": canvas,
            "files": files, "new_files": new_files}


# -- applying to disk -----------------------------------------------------

def _load_toml(path: str) -> dict:
    import rtoml
    with open(path, encoding="utf-8") as fh:
        return rtoml.loads(fh.read())


def _dump_toml(data: dict) -> str:
    import rtoml
    return rtoml.dumps(data)


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _udf_code_from_body(udf_body: str) -> str | None:
    """The source text inside a serialized `udf_body` (the workbench's
    `toJobJson(...).udf` JSON: `code` is the file's content)."""
    import json
    try:
        parsed = json.loads(udf_body)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, str):
            return code
    return None


def apply_snapshot(
    clone_dir: str,
    new: dict,
    prev: dict | None,
    *,
    backup: Any = None,
) -> dict:
    """Fold `new` (a `compact_state`) into `<clone_dir>/canvas.toml`.

    `prev` is the previous compact state from the same embed generation, or
    None. `backup(rel, data)` receives every file about to be deleted.
    Returns `{changed: bool, upserted: [...], deleted: [...], created: [...]}`.
    Raises FileNotFoundError when the clone has no canvas.toml, and whatever
    `rtoml` raises on a malformed one (the caller reports both as rejections).
    """
    path = os.path.join(clone_dir, "canvas.toml")
    doc = _load_toml(path)
    canvas = doc.setdefault("canvas", {})
    if not isinstance(canvas, dict):
        raise ValueError("canvas.toml: [canvas] is not a table")
    rows = canvas.get("nodes")
    if not isinstance(rows, list):
        rows = []
        canvas["nodes"] = rows
    by_name: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("udfName"), str):
            by_name[row["udfName"]] = row

    changed = False
    upserted: list[str] = []
    deleted: list[str] = []
    created: list[str] = []

    # Deletions: only nodes the embed KNEW (in prev) and dropped.
    known_before = set(prev["nodes"]) if prev else set()
    for name in sorted(known_before - set(new["nodes"])):
        row = by_name.pop(name, None)
        if row is None:
            continue
        rows.remove(row)
        changed = True
        deleted.append(name)
        file = (prev or {}).get("files", {}).get(name) or new["files"].get(name)
        if file:
            fpath = os.path.join(clone_dir, file)
            try:
                with open(fpath, "rb") as fh:
                    data = fh.read()
                if backup is not None:
                    backup(file, data)
                os.remove(fpath)
            except OSError:
                pass

    # Upserts: rows that differ on the keys the embed owns.
    for name, row in new["nodes"].items():
        disk = by_name.get(name)
        if disk is None:
            rows.append(dict(row))
            by_name[name] = rows[-1]
            changed = True
            upserted.append(name)
            continue
        diff = False
        for key in EMBED_ROW_KEYS:
            if key in row:
                if disk.get(key) != row[key]:
                    disk[key] = row[key]
                    diff = True
            elif key in disk and prev and key in prev["nodes"].get(name, {}):
                # The embed used to send this key and stopped: it cleared it.
                del disk[key]
                diff = True
        if diff:
            changed = True
            upserted.append(name)

    # Edges: the embed's set, plus disk edges touching nodes it does not know.
    known = set(new["nodes"])
    disk_edges = canvas.get("edges") if isinstance(canvas.get("edges"), list) else []
    kept = [e for e in disk_edges
            if isinstance(e, list) and len(e) == 2
            and (e[0] not in known or e[1] not in known)]
    merged = [list(e) for e in new["edges"]]
    for e in kept:
        if list(e) not in merged:
            merged.append(list(e))
    if merged != disk_edges:
        canvas["edges"] = merged
        changed = True

    # Canvas scalars the embed owns.
    for key in CANVAS_KEYS:
        if key in new["canvas"] and canvas.get(key) != new["canvas"][key]:
            canvas[key] = new["canvas"][key]
            changed = True

    # New UDFs: create their source file, never overwrite one.
    for entry in new["new_files"]:
        fpath = os.path.join(clone_dir, entry["file"])
        if os.path.exists(fpath):
            continue
        code = _udf_code_from_body(entry["body"])
        if code is None:
            continue
        try:
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(code)
            created.append(entry["file"])
        except OSError:
            pass

    if changed:
        _write_atomic(path, _dump_toml(doc))
    return {"changed": changed or bool(created), "upserted": upserted,
            "deleted": deleted, "created": created, "at": time.time()}
