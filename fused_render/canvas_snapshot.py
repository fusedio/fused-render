"""Embedded-workbench canvas snapshots → `canvas.toml` on disk.

The canvas sync's one writer to the server is the clone watcher (canvases.py).
Under embed v3 the workbench inside /canvases/<name> never saves to the server:
on its usual autosave cadence it posts the body it WOULD have saved — minus UDF
code — over the postMessage bridge, the workspace page relays it to
`POST /api/canvases/snapshot`, and this module folds it into the clone. The
watcher then publishes the folder exactly as it publishes a Claude edit.

Three entry points for the embed → disk leg, all pure except for the final
writes (the disk → embed leg, `compact_state_from_disk` / `diff_views`, is at
the bottom of the module):

  `compact_state(body)`   — the studio JSON the workbench sends → the compact
                            shape `canvas.toml` holds (one row per node keyed
                            by udfName, edges as name pairs, viewport), plus
                            what the rows alone cannot say: each node's server
                            id, the bodies of text notes, and the names the
                            embed held when it last hydrated. The node key
                            table mirrors the application server's
                            `toml_translator._studio_node_to_compact`.
  `apply_snapshot(...)`   — merge that compact state into the toml on disk at
                            NODE granularity, given the previous snapshot from
                            the same embed generation:

      node in new, row differs from disk    → upsert the row (keys the embed
                                              does not send are kept)
      node known before but not in new,
        and its id reappears under a new
        name                                → a RENAME the hint missed: row,
                                              edges and file follow the name
      node known before but not in new      → the embed deleted it: drop the
                                              row and its edges, move its
                                              source file to trash
      node on disk, unknown to the embed    → kept untouched (Claude added it
                                              since the embed last hydrated)
      edges                                 → the embed's set, plus every disk
                                              edge touching a node the embed
                                              does not know
      viewport / viewportBounds / comments  → the embed's, always
      a text note's body (`.md`, the one
        kind editable on the canvas itself) → written when it CHANGED since
                                              the previous snapshot — never
                                              compared against disk, so a note
                                              Claude edited meanwhile is not
                                              overwritten by a stale re-send
      a new UDF (no server id, body given)  → its source file is CREATED; an
                                              existing file is never
                                              overwritten (code is disk-owned)

  `apply_rename(...)`     — the rename hint the workbench posts the moment a
                            UDF is renamed, before any snapshot carries the
                            new name: file, row, edges, in one write.

  "Known before" is the previous snapshot's node set when there is one, else
  the embed's own `base_nodes` — the names it held right after hydrating. So
  a node deleted in the first moments of a generation is still a deletion,
  and a node Claude added on disk meanwhile (in neither set) survives.

  Code files (`.py`), JSON-UI (`.json`) and HTML (`.html`) bodies are
  DISK-OWNED: with the workbench's code editor hidden under v3 they cannot be
  edited in the embed, so they are never written from this leg.

  Every write is content-gated. A write that changed nothing would still bump
  mtimes, the watcher would push, the push would ping the embed, the embed
  would re-hydrate and send another first snapshot — a loop.

No import of fused_render.server (canvases.py includes this; keep it
acyclic). `rtoml` comes from the bundled fused-py; a missing module is a
clear error, not a silent skip.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

# -- the studio → compact key table ---------------------------------------
#
# Mirrors toml_translator._studio_node_to_compact in fusedlabs/application.
# A new key the workbench starts emitting must be added HERE (and to the test
# that pins this list against the translator), or it silently stops
# persisting from the embed — the one failure mode an ops-shaped sync has.
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
NODE_CATALOG_KEYS = (
    "assetUrl",
    "tags",
    "filePreview",
    "filePreviewExtensions",
    "filePreviewFileName",
    "filePreviewRegex",
    "filePreviewMenuOrder",
)
EMBED_ROW_KEYS = frozenset(
    ("udfName", "x", "y", "zIndex", "width", "height", "type", "title",
     "description", "visible", "isLocked")
    + NODE_DATA_KEYS + NODE_CATALOG_KEYS
)
CANVAS_KEYS = ("viewport", "viewportBounds", "comments")

# The one file kind whose body the embed can change without its code editor.
EMBED_BODY_SUFFIXES = (".md",)

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
    return {k: v for k, v in row.items() if v is not None}


def _udf_code_from_body(udf_body: Any) -> str | None:
    """The source text inside a serialized `udf_body` (the workbench's
    `toJobJson(...).udf` JSON: `code` is the file's content)."""
    if not isinstance(udf_body, str):
        return None
    try:
        parsed = json.loads(udf_body)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, str):
            return code
    return None


def compact_state(body: dict) -> dict:
    """The embed's snapshot body → ::

        {nodes: {name: row}, edges: [[a, b]], canvas: {viewport, ...},
         files: {name: basename}, ids: {name: server id},
         bodies: {name: file text}   (text notes only),
         new_files: [{file, body}]   (UDFs with no server id yet),
         base_nodes: set[name] | None}
    """
    dashboard = body.get("dashboard_body") or {}
    if isinstance(dashboard, str):
        try:
            dashboard = json.loads(dashboard)
        except ValueError as exc:
            raise ValueError(f"dashboard_body is not JSON: {exc}") from exc
    studio = dashboard.get("studioState") if isinstance(dashboard, dict) else None
    # No studioState, or no nodes list, is a MALFORMED body, not an empty
    # canvas: read as empty it would delete every node the previous snapshot
    # knew. The workbench clears a canvas with `nodes: []`, which still passes.
    if not isinstance(studio, dict) or not isinstance(studio.get("nodes"), list):
        raise ValueError("snapshot has no dashboard_body.studioState.nodes")
    raw_nodes = [n for n in studio["nodes"] if isinstance(n, dict)]
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
    ids: dict[str, str] = {}
    bodies: dict[str, str] = {}
    new_files: list[dict] = []
    for udf in body.get("udfs") or []:
        if not isinstance(udf, dict):
            continue
        slug = udf.get("slug")
        file = udf.get("file")
        if not isinstance(slug, str) or not isinstance(file, str) or "/" in file:
            continue
        files[slug] = file
        server_id = udf.get("id")
        if isinstance(server_id, str) and server_id:
            ids[slug] = server_id
        # Canvas visibility lives on the UDF row (`canvasVisible` →
        # `is_visible`); `data.config.visible` is its serialization-only
        # mirror. Take the mirror when the node carries it, the row otherwise.
        row = nodes.get(slug)
        if row is not None and "visible" not in row and isinstance(udf.get("is_visible"), bool):
            row["visible"] = udf["is_visible"]
        # `file_body` is the exact file text as `fused canvas pull` would
        # write it (a note's README-shaped markdown, not its raw text);
        # `udf_body.code` is the file for a never-saved code UDF.
        text = udf.get("file_body")
        if not isinstance(text, str):
            text = _udf_code_from_body(udf.get("udf_body"))
        if text is None:
            continue
        if not server_id:
            new_files.append({"file": file, "body": text})
        elif file.endswith(EMBED_BODY_SUFFIXES):
            bodies[slug] = text
    base_raw = body.get("base_nodes")
    base_nodes = (
        {n for n in base_raw if isinstance(n, str)} if isinstance(base_raw, list) else None
    )
    return {"nodes": nodes, "edges": edges, "canvas": canvas, "files": files,
            "ids": ids, "bodies": bodies, "new_files": new_files,
            "base_nodes": base_nodes}


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


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


# Every file kind a node's source may be, as the clone names them.
_SOURCE_SUFFIXES = (".py", ".json", ".md", ".html")


def _find_source(clone_dir: str, name: str) -> str | None:
    """The clone's source file for a node named `name`, when nothing else
    names it (a deletion in the first snapshot of a generation, where there
    is no previous file map and the node is no longer in the snapshot)."""
    for suffix in _SOURCE_SUFFIXES:
        candidate = name + suffix
        if os.path.isfile(os.path.join(clone_dir, candidate)):
            return candidate
    return None


def _rename_in_doc(canvas: dict, frm: str, to: str) -> bool:
    """Rename a node in a parsed `[canvas]` table: its row's `udfName` (and a
    title that was just the name), every edge endpoint. True if anything
    changed."""
    changed = False
    for row in canvas.get("nodes") or []:
        if isinstance(row, dict) and row.get("udfName") == frm:
            row["udfName"] = to
            if row.get("title") == frm:
                row["title"] = to
            changed = True
    edges = canvas.get("edges")
    if isinstance(edges, list):
        for edge in edges:
            if isinstance(edge, list):
                for i, end in enumerate(edge):
                    if end == frm:
                        edge[i] = to
                        changed = True
    return changed


def _move_file(clone_dir: str, file_from: str | None, file_to: str | None) -> str | None:
    """Move a node's source file with its rename; None when nothing moved
    (no source yet, or the target already exists — then the target is kept,
    since code is disk-owned and never overwritten from this leg)."""
    if not file_from or not file_to or file_from == file_to:
        return None
    src = os.path.join(clone_dir, file_from)
    dst = os.path.join(clone_dir, file_to)
    if not os.path.isfile(src) or os.path.exists(dst):
        return None
    try:
        os.replace(src, dst)
    except OSError:
        return None
    return file_to


def apply_rename(clone_dir: str, frm: str, to: str,
                 file_from: str | None, file_to: str | None) -> dict:
    """The rename hint: `frm` → `to` on disk, file included. Raises on a
    clone with no canvas.toml; a `to` that already names a row is rejected
    (ValueError) rather than merged into."""
    path = os.path.join(clone_dir, "canvas.toml")
    doc = _load_toml(path)
    canvas = doc.setdefault("canvas", {})
    if not isinstance(canvas, dict):
        raise ValueError("canvas.toml: [canvas] is not a table")
    rows = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
    names = {r.get("udfName") for r in rows if isinstance(r, dict)}
    if frm == to or (to in names and frm not in names):
        # Already on disk: a snapshot carrying the new name landed first, or
        # the embed re-posted the hint after a restart emptied the dedupe.
        return {"changed": False, "renamed": [frm, to], "moved": None, "at": time.time()}
    if to in names:
        raise ValueError(f"a node named {to!r} already exists")
    changed = _rename_in_doc(canvas, frm, to)
    moved = _move_file(clone_dir, file_from, file_to)
    if changed:
        _write_atomic(path, _dump_toml(doc))
    return {"changed": changed or moved is not None, "renamed": [frm, to],
            "moved": moved, "at": time.time()}


def apply_snapshot(
    clone_dir: str,
    new: dict,
    prev: dict | None,
    *,
    backup: Callable[[str, bytes], None] | None = None,
) -> dict:
    """Fold `new` (a `compact_state`) into `<clone_dir>/canvas.toml`.

    `prev` is the previous compact state from the same embed generation, or
    None. `backup(rel, data)` receives every file about to be deleted or
    overwritten. Returns `{changed, upserted, deleted, renamed, created,
    written}`. Raises FileNotFoundError when the clone has no canvas.toml, and
    whatever `rtoml` raises on a malformed one (the caller reports both as
    rejections).
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
    renamed: list[list[str]] = []
    created: list[str] = []
    written: list[str] = []

    def trash(rel: str) -> None:
        if backup is None:
            return
        data = _read_bytes(os.path.join(clone_dir, rel))
        if data is not None:
            backup(rel, data)

    # What the embed KNEW: the previous snapshot, else its own hydration base.
    if prev is not None:
        known_before = set(prev["nodes"])
        files_before = dict(prev.get("files", {}))
        ids_before = dict(prev.get("ids", {}))
    else:
        known_before = set(new.get("base_nodes") or ())
        files_before = {}
        ids_before = {}
    vanished = sorted(known_before - set(new["nodes"]))

    # Renames the hint missed: a vanished name whose server id reappears under
    # a new name that is not on disk yet.
    id_to_new_name = {sid: name for name, sid in new["ids"].items()}
    for name in list(vanished):
        sid = ids_before.get(name)
        to = id_to_new_name.get(sid) if sid else None
        if not to or to == name or to in by_name or name not in by_name:
            continue
        _rename_in_doc(canvas, name, to)
        by_name[to] = by_name.pop(name)
        _move_file(clone_dir, files_before.get(name) or _find_source(clone_dir, name),
                   new["files"].get(to))
        renamed.append([name, to])
        vanished.remove(name)
        changed = True

    # Deletions.
    for name in vanished:
        row = by_name.pop(name, None)
        if row is None:
            continue
        rows.remove(row)
        changed = True
        deleted.append(name)
        file = files_before.get(name) or new["files"].get(name) or _find_source(clone_dir, name)
        if file:
            fpath = os.path.join(clone_dir, file)
            if os.path.isfile(fpath):
                trash(file)
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    # Upserts: rows that differ on the keys the embed owns.
    prev_nodes = prev["nodes"] if prev else {}
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
            elif key in disk and key in prev_nodes.get(name, {}):
                del disk[key]
                diff = True
        if diff:
            changed = True
            upserted.append(name)

    # Edges: the embed's set, plus disk edges touching nodes it does not know.
    # A node deleted just above is NOT "unknown": its edges go with it, or
    # they would be copied back dangling and fail validation on the push.
    known = set(new["nodes"])
    gone = set(deleted)
    disk_edges = canvas.get("edges") if isinstance(canvas.get("edges"), list) else []
    kept = [e for e in disk_edges
            if isinstance(e, list) and len(e) == 2
            and e[0] not in gone and e[1] not in gone
            and (e[0] not in known or e[1] not in known)]
    merged = [list(e) for e in new["edges"]]
    for e in kept:
        if list(e) not in merged:
            merged.append(list(e))
    if merged != disk_edges:
        canvas["edges"] = merged
        changed = True

    for key in CANVAS_KEYS:
        if key in new["canvas"] and canvas.get(key) != new["canvas"][key]:
            canvas[key] = new["canvas"][key]
            changed = True

    # Note bodies: written only when the EMBED changed them since its
    # previous snapshot. The first snapshot of a generation (prev None)
    # writes nothing — its bodies are what it just hydrated.
    prev_bodies = prev.get("bodies", {}) if prev else {}
    for name, text in new["bodies"].items():
        if prev is None or prev_bodies.get(name) == text:
            continue
        file = new["files"].get(name)
        if not file or not file.endswith(EMBED_BODY_SUFFIXES):
            continue
        fpath = os.path.join(clone_dir, file)
        current = _read_bytes(fpath)
        if current is not None and current.decode("utf-8", "replace") == text:
            continue
        if current is not None:
            trash(file)
        # Atomic, and an OSError PROPAGATES (the route answers 409 so the
        # embed keeps the snapshot queued): swallowed, the toml below would
        # still be written and the ack say ok for an edit that never landed.
        _write_atomic(fpath, text)
        written.append(file)

    # New UDFs: create their source file, never overwrite one. A failure
    # propagates for the same reason — a row whose file is missing fails the
    # canvas validation on the push.
    for entry in new["new_files"]:
        fpath = os.path.join(clone_dir, entry["file"])
        if os.path.exists(fpath):
            continue
        _write_atomic(fpath, entry["body"])
        created.append(entry["file"])

    if changed:
        _write_atomic(path, _dump_toml(doc))
    return {"changed": changed or bool(created) or bool(written),
            "upserted": upserted, "deleted": deleted, "renamed": renamed,
            "created": created, "written": written, "at": time.time()}


def rename_prev(prev: dict | None, frm: str, to: str, file_to: str | None) -> dict | None:
    """The previous snapshot with one node renamed, so the snapshot that
    follows a rename hint is diffed against the name it will carry."""
    if prev is None:
        return None
    out = dict(prev)
    for key in ("nodes", "files", "ids", "bodies"):
        table = dict(prev.get(key, {}))
        if frm in table:
            table[to] = table.pop(frm)
            if key == "nodes" and isinstance(table[to], dict):
                table[to] = dict(table[to], udfName=to)
        out[key] = table
    if file_to:
        out["files"] = dict(out.get("files", {}), **{to: file_to})
    base = prev.get("base_nodes")
    if base is not None and frm in base:
        out["base_nodes"] = (set(base) - {frm}) | {to}
    out["edges"] = [[to if a == frm else a, to if b == frm else b] for a, b in prev.get("edges", [])]
    return out


# -- the disk → embed leg: deltas -------------------------------------------
#
# The mirror image of the snapshot leg above. The host keeps `embed_view`, the
# compact state it believes the embed is showing (canvases._SyncManager). When
# the clone moves on disk — Claude wrote a file, the watcher merged a cloud
# edit down — the page asks for diff(disk, embed_view) and posts it to the
# frame as `fused-embed-delta`; the frame patches its atoms in place (no
# re-hydrate) and acks, and the host sets embed_view := disk. After the host
# applied the embed's own snapshot it re-reads disk into embed_view, so the
# embed's own edits diff to nothing and are never echoed back.
#
# `embed_view` is ONLY ever a disk reading (`compact_state_from_disk`), never
# a `compact_state(snapshot)`: the snapshot's rows carry EMBED_ROW_KEYS only
# and no code, so a view built from one would diff non-empty against disk
# forever. The one field the snapshot overrides is `ids` — the server ids the
# embed truly holds — so a node that gained an id on the last push is still
# told about it (an `ids` op).

# Source files, by suffix, whose text the embed holds as the UDF's code
# verbatim. A text note's `.md` is README-shaped (`Udf.makeZipReadme`), NOT
# the note's text, so it is sent as `body` instead and the frame decides.
CODE_SUFFIXES = (".py", ".json", ".html")


def compact_state_from_disk(clone_dir: str, remote_ids: dict[str, str] | None = None) -> dict:
    """The clone as the embed should hold it::

        {nodes: {name: row}, order: [name], edges: [[a, b]],
         canvas: {viewport, viewportBounds, comments}, files: {name: basename},
         ids: {name: server id} (from the remote manifest, when known),
         bodies: {name: text} (every node's source file, all four kinds),
         missing: [name]} (rows whose source file is not on disk)

    Same shape as `compact_state` where the keys overlap, so it serves as
    `prev` for `apply_snapshot` too. Raises FileNotFoundError when there is
    no canvas.toml and whatever rtoml raises on a malformed one.
    """
    doc = _load_toml(os.path.join(clone_dir, "canvas.toml"))
    canvas = doc.get("canvas") or {}
    if not isinstance(canvas, dict):
        raise ValueError("canvas.toml: [canvas] is not a table")
    rows = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
    nodes: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("udfName"), str) and row["udfName"]:
            nodes[row["udfName"]] = dict(row)
            order.append(row["udfName"])
    edges: list[list[str]] = []
    for e in canvas.get("edges") or []:
        if (isinstance(e, list) and len(e) == 2
                and isinstance(e[0], str) and isinstance(e[1], str)):
            edges.append([e[0], e[1]])
    scalars = {k: canvas[k] for k in CANVAS_KEYS if k in canvas}
    files: dict[str, str] = {}
    bodies: dict[str, str] = {}
    missing: list[str] = []
    for name, row in nodes.items():
        file = _find_source(clone_dir, name)
        if file is None:
            # A folder is a row with no source file of its own.
            if row.get("type") == "udf-folder":
                continue
            missing.append(name)
            continue
        files[name] = file
        data = _read_bytes(os.path.join(clone_dir, file))
        bodies[name] = data.decode("utf-8", "replace") if data is not None else ""
    ids = {n: remote_ids[n] for n in nodes if remote_ids and isinstance(remote_ids.get(n), str)}
    return {"nodes": nodes, "order": order, "edges": edges, "canvas": scalars,
            "files": files, "ids": ids, "bodies": bodies, "missing": missing}


def studio_patch_from_row(row: dict) -> dict:
    """The studio-node fragment a compact row stands for — the inverse of
    `compact_node`, shaped like the application server's
    `_compact_node_to_studio` but PARTIAL: only the keys the row carries, so
    the frame merges it over the node it has (id, measured size, selection
    and every runtime field untouched). Keys the embed cannot show are left
    out rather than guessed at."""
    data: dict[str, Any] = {}
    if "title" in row:
        data["nodeTitle"] = row["title"]
    if "description" in row:
        data["nodeDescription"] = row["description"]
    for key in NODE_DATA_KEYS + NODE_CATALOG_KEYS:
        if key in row:
            data[key] = row[key]
    if "isLocked" in row:
        data["isLocked"] = row["isLocked"]
    if "visible" in row:
        data["config"] = {"visible": row["visible"]}
    patch: dict[str, Any] = {"data": data}
    if "x" in row or "y" in row:
        patch["position"] = {"x": row.get("x"), "y": row.get("y")}
    for key in ("zIndex", "width", "height"):
        if key in row:
            patch[key] = row[key]
    if row.get("type"):
        patch["type"] = row["type"]
    return patch


def _upsert_op(disk: dict, name: str, *, row: bool, body: bool, sid: bool) -> dict:
    op: dict[str, Any] = {"op": "upsert", "name": name}
    file = disk["files"].get(name)
    if file:
        op["file"] = file
    if row:
        op["row"] = disk["nodes"][name]
        op["node"] = studio_patch_from_row(disk["nodes"][name])
    if body and name in disk["bodies"]:
        if file and file.endswith(CODE_SUFFIXES):
            op["code"] = disk["bodies"][name]
        else:
            op["body"] = disk["bodies"][name]
    if sid and name in disk["ids"]:
        op["id"] = disk["ids"][name]
    return op


def diff_views(disk: dict, view: dict | None) -> list[dict]:
    """The ops that take `view` (what the embed holds; None = nothing known,
    a full state) to `disk`. Node-level, by UDF name:

        upsert {name, file, row?, node?, code?|body?, id?}  — new or changed;
                                           an absent field means "unchanged",
                                           never "cleared"
        rename {from, to, file}           — a name vanished whose server id
                                           reappears under a name the view
                                           lacks (then diffed as `to`)
        delete {name}
        edges  {set: [[a, b]]}            — the whole set, when it differs
        canvas {comments?, viewportBounds?} — the scalars that differ; the
                                           frame never moves its own camera
    A full state carries every node as an upsert plus `edges` and `canvas`;
    the frame drops what is not listed.
    """
    ops: list[dict] = []
    if view is None:
        for name in disk["order"]:
            ops.append(_upsert_op(disk, name, row=True, body=True, sid=True))
        ops.append({"op": "edges", "set": [list(e) for e in disk["edges"]]})
        if disk["canvas"]:
            ops.append({"op": "canvas", **{k: v for k, v in disk["canvas"].items()
                                           if k != "viewport"}})
        return ops
    vnodes, dnodes = view["nodes"], disk["nodes"]
    vids, dids = view.get("ids", {}), disk["ids"]
    vbodies = view.get("bodies", {})
    renamed_to: dict[str, str] = {}
    id_to_disk = {sid: n for n, sid in dids.items()}
    for name in vnodes:
        if name in dnodes:
            continue
        sid = vids.get(name)
        to = id_to_disk.get(sid) if sid else None
        if to and to not in vnodes:
            renamed_to[to] = name
            ops.append({"op": "rename", "from": name, "to": to,
                        "file": disk["files"].get(to)})
        else:
            ops.append({"op": "delete", "name": name})
    for name in disk["order"]:
        was = renamed_to.get(name, name)
        if was not in vnodes:
            ops.append(_upsert_op(disk, name, row=True, body=True, sid=True))
            continue
        row = dnodes[name] != vnodes[was]
        body = disk["bodies"].get(name) != vbodies.get(was)
        sid = name in dids and dids[name] != vids.get(was)
        if row or body or sid:
            ops.append(_upsert_op(disk, name, row=row, body=body, sid=sid))
    if sorted(map(tuple, disk["edges"])) != sorted(map(tuple, view.get("edges", []))):
        ops.append({"op": "edges", "set": [list(e) for e in disk["edges"]]})
    vcanvas = view.get("canvas", {})
    scalars = {k: v for k, v in disk["canvas"].items()
               if k != "viewport" and vcanvas.get(k) != v}
    if scalars:
        ops.append({"op": "canvas", **scalars})
    return ops
