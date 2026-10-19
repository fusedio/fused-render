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
      … and the snapshot has no node
        for it                              → file only, NO row (the embed's
                                              own row follows; a synthesized
                                              one would move the node), slug
                                              reported as `rowless_files`

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
  mtimes, so `disk_rev` would move, the page would cut a delta and post it,
  and the embed would answer with another snapshot — a loop.

No import of fused_render.server (canvases.py includes this; keep it
acyclic). `rtoml` comes from the bundled fused-py; a missing module is a
clear error, not a silent skip.
"""
from __future__ import annotations

import copy
import json
import os
import time
from typing import Any, Callable, Iterable

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

# -- folders ----------------------------------------------------------------
#
# A folder is a `[[canvas.nodes]]` row with `type = "udf-folder"`, a
# `folderName`, an optional `folderColor`/`isLocked` and `childUdfOrder`, the
# ordered names of the nodes inside it. Membership lives ONLY there (first
# folder in document order wins, as the frame and the server's hydration
# nest); a child row has no pointer back. A child's `x`/`y` are RELATIVE to
# its folder's `x`/`y` (the React Flow parent frame), absolute when no folder
# lists it. A folder has no source file and no server id: its name is its
# only identity.
#
# A folder is NAMED by its `folderName`, as the server's translator and the
# embed name it; the canvas-toml skill writes a folder row with no `udfName`
# at all. `row_name` is that rule (folderName for a folder row, udfName for
# every other row and for a folder row with no folderName), used at every
# site here that keys rows by name; a written folder row carries `udfName =
# folderName` (`_fill_names`), so a hand edit that sets the two apart
# converges on the folderName instead of flip-flopping.
#
# What the server tolerates, this tolerates. A `childUdfOrder` entry naming
# no row is KEPT: the workbench's deleteNode leaves one behind and the server
# round-trips it (its hydration ignores the name), and a name Claude lists
# before writing the row binds once the row exists, synthesized or not.
# `normalise_folders` drops only what no reader can honour: a non-string
# entry, an entry naming a folder, a name listed twice by one folder, and a
# name two folders list (the preferred folder keeps it, local's nesting in a
# merge, else the first in document order). It runs on every canvas.toml the
# snapshot leg and the pull write; nothing is ever rolled back over it.
#
# A folder and a UDF may share a name (the server keys them apart: a
# folder's `childUdfOrder` entry always means the UDF). `merge_toml` keys its
# rows by (is folder, name), so the two merge independently. Two rows of the
# SAME kind sharing a name are broken data no reader resolves alike: the
# pull refuses to merge such a canvas.toml and leaves the sync point and the
# remote baseline where they were.
#
# KNOWN GAP (view leg only): the snapshot and delta legs (`apply_snapshot`,
# `compact_state_from_disk`, `diff_views`) key rows by name alone, so of a
# folder and a UDF sharing a name they keep the FIRST row and do not show
# the other; `folder_problems` and `normalise_folders` report the clash.

FOLDER_TYPE = "udf-folder"


def is_folder(row: Any) -> bool:
    return isinstance(row, dict) and row.get("type") == FOLDER_TYPE


def row_name(row: Any) -> str | None:
    """A row's key: a folder row's `folderName`, else its `udfName`. None
    for a row with neither."""
    if not isinstance(row, dict):
        return None
    if row.get("type") == FOLDER_TYPE:
        name = row.get("folderName")
        if isinstance(name, str) and name:
            return name
    name = row.get("udfName")
    if isinstance(name, str) and name:
        return name
    return None


def _with_name(row: dict, name: str) -> dict:
    """A copy of `row` carrying `udfName = name` (first, when it was missing,
    so the dumped row reads like every other)."""
    if row.get("udfName") == name:
        return dict(row)
    if "udfName" in row:
        return dict(row, udfName=name)
    out = {"udfName": name}
    out.update(row)
    return out


def folder_of(rows: Iterable[Any]) -> dict[str, str]:
    """`{child name: folder name}` from the folders' `childUdfOrder` — the
    first folder in document order that lists a name wins."""
    out: dict[str, str] = {}
    for row in rows:
        if not is_folder(row):
            continue
        fname = row_name(row)
        order = row.get("childUdfOrder")
        if not fname or not isinstance(order, list):
            continue
        for child in order:
            if isinstance(child, str) and child and child not in out:
                out[child] = fname
    return out


def _rows_list(doc_or_rows: Any) -> list:
    """The row list of a parsed canvas.toml, a `[canvas]` table, a
    `rows_of_doc` reading, or a plain list of rows."""
    if isinstance(doc_or_rows, list):
        return doc_or_rows
    if not isinstance(doc_or_rows, dict):
        return []
    if isinstance(doc_or_rows.get("rows"), list):     # a `rows_of_doc` reading
        return doc_or_rows["rows"]
    nodes = doc_or_rows.get("nodes")
    if isinstance(nodes, dict):           # a reading: {nodes: {name: row}, order}
        order = doc_or_rows.get("order")
        if isinstance(order, list):
            return [nodes[n] for n in order if n in nodes]
        return list(nodes.values())
    if isinstance(nodes, list):           # a [canvas] table
        return nodes
    canvas = doc_or_rows.get("canvas")    # a whole parsed canvas.toml
    if isinstance(canvas, dict) and isinstance(canvas.get("nodes"), list):
        return canvas["nodes"]
    return []


def shared_names(doc_or_rows: Any, *, same_kind: bool = False) -> list[str]:
    """Names more than one row carries, in first-seen order: any two rows
    (a folder and a UDF named alike included), or with `same_kind` only two
    folders or two non-folder rows."""
    seen: set = set()
    out: list[str] = []
    for row in _rows_list(doc_or_rows):
        name = row_name(row)
        if name is None:
            continue
        key = (is_folder(row), name) if same_kind else name
        if key in seen and name not in out:
            out.append(name)
        seen.add(key)
    return out


def _clash_lines(rows: Any, suffix: str = "") -> list[str]:
    same = shared_names(rows, same_kind=True)
    out = [f"two rows of one kind share the name {n!r}{suffix}" for n in same]
    out += [f"a folder and a node share the name {n!r}{suffix}"
            for n in shared_names(rows) if n not in same]
    return out


def folder_problems(doc_or_rows: Any) -> list[str]:
    """What is wrong with a canvas's folder grammar, one line per problem
    (empty = fine): two rows sharing a name, a folder row with no name, a
    `childUdfOrder` that is not a list, an entry that is not a name or names
    a folder, a name listed twice by one folder or by two. An entry naming
    no row is NOT a problem (see the section comment). Pure, and never a
    reason to refuse a write: `normalise_folders` fixes what can be fixed."""
    rows = [r for r in _rows_list(doc_or_rows) if isinstance(r, dict)]
    problems = _clash_lines(rows)
    leaves = {row_name(r) for r in rows if not is_folder(r)} - {None}
    folders = ({row_name(r) for r in rows if is_folder(r)} - {None}) - leaves
    listed_by: dict[str, str] = {}
    for row in rows:
        if not is_folder(row):
            continue
        fname = row_name(row)
        if fname is None:
            problems.append("a udf-folder row has neither folderName nor udfName")
            continue
        order = row.get("childUdfOrder")
        if order is None:
            continue
        if not isinstance(order, list):
            problems.append(f"folder {fname!r}: childUdfOrder is not a list")
            continue
        seen: set[str] = set()
        for child in order:
            if not isinstance(child, str) or not child:
                problems.append(f"folder {fname!r} lists {child!r}, which is not a name")
            elif child in folders:
                problems.append(f"folder {fname!r} lists {child!r}, which is a folder")
            elif child in seen:
                problems.append(f"folder {fname!r} lists {child!r} twice")
            elif child in listed_by:
                problems.append(f"{child!r} is listed by folders "
                                f"{listed_by[child]!r} and {fname!r}")
                seen.add(child)
            else:
                listed_by[child] = fname
                seen.add(child)
    return problems


def normalise_folders(rows: list, prefer: dict[str, str] | None = None) -> list[str]:
    """Make `rows` (in place) satisfy `folder_problems` as far as dropping
    entries can: a `childUdfOrder` that is not a list becomes empty; an entry
    that is not a non-empty string, names a folder, or repeats within its
    folder is dropped; a name two folders list stays only in `prefer[name]`
    when that folder lists it, else in the first lister in document order.
    Entries naming no row are kept. Returns one line per fix, plus one per
    name two rows share (reported, not fixable here)."""
    fixes = _clash_lines(rows, " (not fixed)")
    # An entry names the UDF when one has the name, a folder only otherwise.
    leaves = {row_name(r) for r in rows if not is_folder(r)} - {None}
    folders = ({row_name(r) for r in rows if is_folder(r)} - {None}) - leaves
    listers: dict[str, list[str]] = {}
    for row in rows:
        fname = row_name(row) if is_folder(row) else None
        order = row.get("childUdfOrder") if fname else None
        if not isinstance(order, list):
            continue
        for child in order:
            if isinstance(child, str) and child and child not in folders:
                names = listers.setdefault(child, [])
                if fname not in names:
                    names.append(fname)
    owner: dict[str, str] = {}
    for child, names in listers.items():
        want = (prefer or {}).get(child)
        owner[child] = want if want in names else names[0]
    for row in rows:
        fname = row_name(row) if is_folder(row) else None
        if fname is None or "childUdfOrder" not in row:
            continue
        order = row["childUdfOrder"]
        if not isinstance(order, list):
            row["childUdfOrder"] = []
            fixes.append(f"folder {fname!r}: childUdfOrder was not a list")
            continue
        kept: list[str] = []
        seen: list = []           # one report per name, however often listed
        for child in order:
            again = child in seen
            seen.append(child)
            if not isinstance(child, str) or not child:
                fixes.append(f"folder {fname!r}: dropped {child!r} (not a name)")
            elif child in folders:
                if not again:
                    fixes.append(f"folder {fname!r}: dropped {child!r} (a folder)")
            elif owner.get(child) != fname:
                if not again:
                    fixes.append(f"folder {fname!r}: dropped {child!r} "
                                 f"(folder {owner.get(child)!r} keeps it)")
            elif child in kept:
                fixes.append(f"folder {fname!r}: dropped a second {child!r}")
            else:
                kept.append(child)
        if kept != order:
            row["childUdfOrder"] = kept
    return fixes


def _trimmed(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def node_name(node: dict, index: int) -> str:
    data = node.get("data") or {}
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    first = (data.get("folderName"), data.get("udfName")) if node.get("type") == FOLDER_TYPE \
        else (data.get("udfName"), data.get("folderName"))
    for candidate in (*first, config.get("title")):
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
        # Both ends must be nodes IN THIS SNAPSHOT. The studio keeps an edge
        # whose node was just deleted until its next reconcile, and an id that
        # resolves to no node would otherwise be written to canvas.toml as the
        # name — a dangling `udf-node-…` target the push's validation rejects.
        if src in nodes and dst in nodes:
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


# -- every source file has a row --------------------------------------------
#
# A `.py/.json/.md/.html` in the clone root with no `[[canvas.nodes]]` row is
# an ORPHAN: a UDF the embed created whose node never reached a snapshot (the
# workbench sent `udfs` before `studioState.nodes` caught up), a file Claude
# wrote without a row. Ignored, it is invisible to the delta leg (rows only)
# and the push ships a node-less UDF. So it gets a SYNTHESIZED row: after the
# last node, titled by its slug, visible. The same rule everywhere a row is
# synthesized (`compact_state_from_disk`, `persist_synthesized`,
# `apply_snapshot`), byte-identical, so a reading that synthesized a row and
# the toml that later persists it diff to nothing.

# Mirrors canvases._SYNC_IGNORED_BASENAMES (not imported: canvases imports
# this module). Files the app seeds into a clone that are never nodes.
_IGNORED_BASENAMES = frozenset({"CLAUDE.md", ".fusedignore"})

# Synthesized node geometry: the workbench's NODE_DEFAULTS.UDF size, and
# NODE_DEFAULTS.TEXTBOX for a note (`.md`).
_SYNTH_WIDTH = 700
_SYNTH_HEIGHT = 500
_SYNTH_SIZE_BY_SUFFIX = {".md": (350, 250)}
_SYNTH_GAP = 40
_SYNTH_ORIGIN = (100, 100)

# Per-kind data defaults, as the embed's `buildNode` (embed-delta-apply.ts)
# gives a node it creates for a UDF: a node's KIND comes from its file suffix
# (the server's NODE_TEMPLATE_TYPE_BY_EXTENSION), never from the row's
# `type` — every non-folder node is `simple-udf`. Carrying the same defaults
# keeps the embed's first snapshot of the node from re-upserting them.
_SYNTH_DATA_DEFAULTS: dict[str, dict[str, Any]] = {
    ".md": {"textBoxColor": "#FFF9C4", "textBoxGradient": True,
            "textBoxScaleFactor": 1.0, "textBoxAlignment": "left"},
    ".json": {"nodeDescriptionScaleFactor": 1.5, "widgetBackgroundColor": "#2e2e2e"},
    ".py": {"nodeDescriptionScaleFactor": 1.5},
    ".html": {"nodeDescriptionScaleFactor": 1.5},
}


def _fusedignore_rules(clone_dir: str) -> list[tuple[str, bool, bool]]:
    """The clone's `.fusedignore` as `[(pattern, negated, directory_only)]`,
    parsed as the CLI's `gitignore_filter.parse_ignore_line` does: blank and
    `#` lines skipped, `!` negates, a trailing `/` is directory-only, a
    leading `/` anchors to the clone root."""
    data = _read_bytes(os.path.join(clone_dir, ".fusedignore"))
    if data is None:
        return []
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        directory_only = line.endswith("/")
        line = line.rstrip("/").lstrip("/")
        if line:
            out.append((line, negated, directory_only))
    return out


def _fusedignored(base: str, rules: list[tuple[str, bool, bool]]) -> bool:
    """Whether the CLI's push skips the root-level FILE `base`
    (`gitignore_filter.resolve_ignore_source`): the last matching rule wins.
    For a root file the root-relative path IS the basename, so an anchored
    pattern matches it the same way an unanchored one does, a pattern with a
    `/` in it never does, and a directory-only rule never matches a file."""
    import fnmatch
    ignored = False
    for pattern, negated, directory_only in rules:
        if not directory_only and fnmatch.fnmatchcase(base, pattern):
            ignored = not negated
    return ignored


def orphan_sources(clone_dir: str, names: Iterable[str],
                   exclude: Iterable[str] = ()) -> list[tuple[str, str]]:
    """`[(slug, basename)]`, sorted by slug, of the root-level source files
    whose slug is not in `names` (the rows on disk) nor in `exclude` (the
    host's still-in-grace `rowless_files`: a file the embed just created,
    whose row its next snapshot brings). Skips canvas.toml, dotfiles, the
    app's seeded files and whatever `.fusedignore` lists. Two files sharing
    a slug resolve as `_find_source` does (`.py` first)."""
    have = set(names) | set(exclude)
    rules = _fusedignore_rules(clone_dir)
    try:
        entries = os.listdir(clone_dir)
    except OSError:
        return []
    slugs: set[str] = set()
    for base in entries:
        if base.startswith(".") or base == "canvas.toml" or base in _IGNORED_BASENAMES:
            continue
        stem, suffix = os.path.splitext(base)
        if suffix not in _SOURCE_SUFFIXES or not stem or stem in have:
            continue
        if _fusedignored(base, rules):
            continue
        if not os.path.isfile(os.path.join(clone_dir, base)):
            continue
        slugs.add(stem)
    out = []
    for slug in sorted(slugs):
        file = _find_source(clone_dir, slug)
        if file is not None:
            out.append((slug, file))
    return out


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def _absolute_xy(rows: list, row: dict) -> tuple[float, float]:
    """`row`'s position on screen: its own x/y, plus its folder's when a
    folder in `rows` lists it (child coordinates are folder-relative)."""
    x, y = _num(row.get("x"), 0), _num(row.get("y"), 0)
    if is_folder(row):
        return x, y
    home = folder_of(rows).get(row_name(row) or "")
    if home is None:
        return x, y
    frow = next((r for r in rows if is_folder(r) and row_name(r) == home), None)
    if frow is None:
        return x, y
    return _num(frow.get("x"), 0) + x, _num(frow.get("y"), 0) + y


def synthesize_row(rows: list[dict], slug: str, file: str) -> dict:
    """The row an orphan source file gets, above every node, titled by its
    slug, visible. Placed:

      - when a folder in `rows` already lists the slug (a name Claude listed
        before writing the row, or one the workbench left behind): INSIDE
        that folder, in coordinates RELATIVE to it, as every reader nests
        it — below the lowest row the folder holds (x = its x, y = its y +
        height + 40), or at (40, 40) in a folder that holds none;
      - otherwise after the last row in `rows`, in ABSOLUTE coordinates
        (x = its x, y = its y + height + 40; a last row inside a folder is
        resolved through the folder, a last row that IS a folder is cleared
        by its full height), at a fixed origin on an empty canvas.

    Chained (each synthesized row appended to `rows` before the next) by
    every caller alike, so the rows match byte for byte."""
    homes = folder_of(rows)
    home = homes.get(slug)
    last = next((r for r in reversed(rows) if isinstance(r, dict)), None)
    if home is not None:
        held = [r for r in rows if isinstance(r, dict) and not is_folder(r)
                and homes.get(row_name(r) or "") == home]
        if held:
            low = max(held, key=lambda r: _num(r.get("y"), 0)
                      + _num(r.get("height"), _SYNTH_HEIGHT))
            x = _num(low.get("x"), 0)
            y = _num(low.get("y"), 0) + _num(low.get("height"), _SYNTH_HEIGHT) + _SYNTH_GAP
        else:
            x, y = _SYNTH_GAP, _SYNTH_GAP
    elif last is None:
        x, y = _SYNTH_ORIGIN
    else:
        x, y = _absolute_xy(rows, last)
        y = y + _num(last.get("height"), _SYNTH_HEIGHT) + _SYNTH_GAP
    z = max((_num(r.get("zIndex"), 0) for r in rows if isinstance(r, dict)), default=0)
    suffix = os.path.splitext(file)[1]
    width, height = _SYNTH_SIZE_BY_SUFFIX.get(suffix, (_SYNTH_WIDTH, _SYNTH_HEIGHT))
    row: dict[str, Any] = {
        "udfName": slug, "x": x, "y": y, "zIndex": z + 1,
        "width": width, "height": height,
        "title": slug, "visible": True,
    }
    row.update(_SYNTH_DATA_DEFAULTS.get(suffix, {}))
    return row


def persist_synthesized(clone_dir: str, names: Iterable[str]) -> list[str]:
    """Write the rows `compact_state_from_disk` synthesized for `names` into
    canvas.toml (one atomic write), so the next push ships them. Re-derives
    from disk: a name that has a row by now, or whose file is gone, is
    skipped. Chained over `names` alone, in slug order — the same rows the
    reading that reported them computed. Returns the names written."""
    wanted = set(names)
    if not wanted:
        return []
    path = os.path.join(clone_dir, "canvas.toml")
    doc = _load_toml(path)
    canvas = doc.setdefault("canvas", {})
    if not isinstance(canvas, dict):
        raise ValueError("canvas.toml: [canvas] is not a table")
    rows = canvas.get("nodes")
    if not isinstance(rows, list):
        rows = []
        canvas["nodes"] = rows
    have = [n for n in map(row_name, rows) if n]
    written: list[str] = []
    for slug, file in orphan_sources(clone_dir, have):
        if slug not in wanted:
            continue
        rows.append(synthesize_row(rows, slug, file))
        written.append(slug)
    if written:
        _fill_names(rows)
        _write_atomic(path, _dump_toml(doc))
    return written


def _fill_names(rows: list) -> None:
    """Give every named row without `udfName` (a folder row written with
    `folderName` only) its `udfName`, in place — called right before a doc
    is written, so the toml converges on both keys."""
    for i, row in enumerate(rows):
        name = row_name(row)
        if name and row.get("udfName") != name:
            rows[i] = _with_name(row, name)


def _rename_in_doc(canvas: dict, frm: str, to: str) -> bool:
    """Rename a node in a parsed `[canvas]` table: its row's `udfName` (and a
    title that was just the name; a folder's `folderName` too), every edge
    endpoint, every folder's `childUdfOrder` entry. True if anything
    changed."""
    changed = False
    rows = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row_name(row) == frm:
            row["udfName"] = to
            if row.get("title") == frm:
                row["title"] = to
            if is_folder(row) and row.get("folderName") == frm:
                row["folderName"] = to
            changed = True
        order = row.get("childUdfOrder") if is_folder(row) else None
        if isinstance(order, list) and frm in order:
            row["childUdfOrder"] = [to if n == frm else n for n in order]
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
    names = {row_name(r) for r in rows} - {None}
    if frm == to or (to in names and frm not in names):
        # Already on disk: a snapshot carrying the new name landed first, or
        # the embed re-posted the hint after a restart emptied the dedupe.
        return {"changed": False, "renamed": [frm, to], "moved": None, "at": time.time()}
    if to in names:
        raise ValueError(f"a node named {to!r} already exists")
    changed = _rename_in_doc(canvas, frm, to)
    moved = _move_file(clone_dir, file_from, file_to)
    if changed:
        _fill_names(rows)
        _write_atomic(path, _dump_toml(doc))
    return {"changed": changed or moved is not None, "renamed": [frm, to],
            "moved": moved, "at": time.time()}


def apply_snapshot(
    clone_dir: str,
    new: dict,
    prev: dict | None,
    *,
    backup: Callable[[str, bytes], None] | None = None,
    rowless_prev: Iterable[str] = (),
) -> dict:
    """Fold `new` (a `compact_state`) into `<clone_dir>/canvas.toml`.

    `prev` is the previous compact state from the same embed generation, or
    None. `backup(rel, data)` receives every file about to be deleted or
    overwritten. `rowless_prev` are slugs earlier snapshots reported as
    `rowless_files` and the host still holds in grace: one this snapshot
    no longer lists (no UDF, no node, no row) has its file trashed and is
    reported in `deleted`. Returns `{changed, upserted, deleted, renamed, created,
    written, synthesized, rowless_files, deferred_deletes, folder_fixes}`
    (`rowless_files`:
    slugs of new UDFs whose file came without a node — file created, no row;
    `synthesized`: always empty here, kept for the ack's shape;
    `deferred_deletes`: known nodes the snapshot omitted while still listing
    their UDF — the host keeps them in its view; `folder_fixes`:
    `normalise_folders`' lines). Raises FileNotFoundError when the clone has no canvas.toml, and
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
    # Keyed by `row_name`: a folder row gets `udfName` = its folderName
    # here, so it matches the embed's row (which always has one) and is
    # written with both keys whenever this doc is written.
    _fill_names(rows)
    by_name: dict[str, dict] = {}
    for row in rows:
        name = row_name(row)
        if name:
            by_name.setdefault(name, row)     # two rows, one name: the first
    # What the doc held before this snapshot: the toml is written only when
    # the doc ends up different (the folder normalisation below can undo an
    # upsert, and a write that changes nothing would loop through disk_rev).
    doc_before = copy.deepcopy(doc)

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
        # KNOWN GAP (frame side): the embed's `base_nodes` lists UDF slugs
        # only, no folders, so a folder deleted in the first snapshot of a
        # generation is not "known before" and survives on disk; the next
        # delta then re-sends it. Fixed where base_nodes is built.
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

    # Deletions. A name with no row is still a deletion when its file is
    # there: a synthesized node (an orphan file the frame received as a
    # delta before its row was persisted) the embed then deleted — leaving
    # the file would re-synthesize it on the next reading.
    #
    # A vanished name the snapshot still lists among its UDFs is NOT a
    # deletion: the workbench drops a UDF with its node, so "UDF listed, node
    # absent" is its `udfs` running ahead of `studioState.nodes` — the very
    # case a synthesized row rescues. Deleting there would trash the UDF the
    # previous snapshot or delta just delivered. Such a name is DEFERRED, not
    # forgotten: reported so the host keeps it in the view (`held_view`'s
    # `extra_names`), it stays "known before" for the next snapshot, which
    # deletes it if that one lists neither its node nor its UDF. Dropped from
    # the view instead, the next delta would re-add it and the delete be lost.
    files_changed = False
    deferred_deletes: list[str] = []
    for name in vanished:
        if name in new["files"]:
            # stale closure: the embed's nodes lag its udfs, not a delete
            deferred_deletes.append(name)
            continue

        row = by_name.pop(name, None)
        if row is None:
            if prev is not None and is_folder(prev["nodes"].get(name)):
                continue    # a folder already gone from disk: no file of its own
            file = files_before.get(name) or _find_source(clone_dir, name)
            if not file or os.path.splitext(file)[0] != name \
                    or not os.path.isfile(os.path.join(clone_dir, file)):
                continue
            sid = ids_before.get(name)
            to = id_to_new_name.get(sid) if sid else None
            if to and to != name:
                # A rename of a row-less node: its file is the new name's
                # source, never trash. The new name's row comes from the
                # upserts below.
                if _move_file(clone_dir, file, new["files"].get(to)):
                    renamed.append([name, to])
                    files_changed = True
                continue
            files_changed = True     # no row: only the file goes
        else:
            rows.remove(row)
            changed = True
            if is_folder(row):
                # A folder has no source file: a file sharing its name is
                # another node's (or nobody's), never this deletion's.
                deleted.append(name)
                continue
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

    # Row-less files still in the host's grace window (`rowless_prev`, the
    # earlier snapshots' `rowless_files`) are in no view, so the loop above
    # never sees them vanish. One whose UDF this snapshot no longer lists,
    # and that still has no node or row, was deleted in the embed: trash it,
    # or it outlives the grace window and is synthesized back.
    for slug in rowless_prev:
        if slug in new["files"] or slug in new["nodes"] or slug in by_name \
                or slug in deleted:
            continue
        file = _find_source(clone_dir, slug)
        if file is None:
            continue
        trash(file)
        try:
            os.remove(os.path.join(clone_dir, file))
        except OSError:
            continue
        files_changed = True
        deleted.append(slug)

    # Upserts: rows that differ on the keys the embed owns. A key the embed's
    # row LACKS is deleted from disk only when the embed's own PREVIOUS row
    # carried it (`embed_rows`, recorded on the view by the host after each
    # snapshot) — never because the DISK row has it: `compact_node` emits
    # several keys conditionally (an empty description, a catalog key that is
    # None), so measured against the disk row every snapshot after a delta
    # would strip them and rewrite the toml for nothing.
    prev_nodes = (prev.get("embed_rows") or {}) if prev else {}
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
    # … and only edges whose both ends are rows on disk: a dangling edge (a
    # node id written as a name by an older build, a row removed by hand)
    # would otherwise be preserved forever as "touching a node the embed does
    # not know" and fail every push.
    kept = [e for e in disk_edges
            if isinstance(e, list) and len(e) == 2
            and e[0] in by_name and e[1] in by_name
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
    # A new UDF whose node the snapshot does not carry (the workbench's
    # `udfs` ran ahead of its `studioState.nodes`) gets its file but NO row:
    # the embed HAS that node, placed where the user dropped it, and its next
    # snapshot brings the row. A row synthesized here would be "after the
    # last node", and the delta would move the user's node there. Reported
    # as `rowless_files` (slug = the file's stem, the name the push validates
    # rows against) so the host exempts them from `compact_state_from_disk`'s
    # synthesis for a grace window (`exclude_orphans`); one still row-less
    # after it is a true orphan and is synthesized then.
    synthesized: list[str] = []
    rowless_files: list[str] = []
    for entry in new["new_files"]:
        fpath = os.path.join(clone_dir, entry["file"])
        if not os.path.exists(fpath):
            _write_atomic(fpath, entry["body"])
            created.append(entry["file"])
        slug, suffix = os.path.splitext(entry["file"])
        if (slug and suffix in _SOURCE_SUFFIXES and slug not in new["nodes"]
                and slug not in by_name and os.path.isfile(fpath)):
            rowless_files.append(slug)

    # Folder grammar: normalised, never rejected (see "folders" above). A
    # row the normalisation put back as it was is not reported upserted,
    # and a doc that ends where it began is not written.
    folder_fixes = normalise_folders(rows)
    if folder_fixes:
        changed = True
    if changed and doc == doc_before:
        changed = False
    if upserted:
        rows_before: dict[str, dict] = {}
        for row in _rows_list(doc_before):
            rows_before.setdefault(row_name(row) or "", row)
        upserted = [n for n in upserted if by_name.get(n) != rows_before.get(n)]
    if changed:
        _write_atomic(path, _dump_toml(doc))
    return {"changed": changed or files_changed or bool(created) or bool(written),
            "upserted": upserted, "deleted": deleted, "renamed": renamed,
            "created": created, "written": written, "synthesized": synthesized,
            "rowless_files": rowless_files, "deferred_deletes": deferred_deletes,
            "folder_fixes": folder_fixes, "at": time.time()}


def held_view(disk: dict, names: Iterable[str], *, edges: list, ids: dict,
              bodies: dict, embed_rows: dict | None,
              extra_names: Iterable[str] = ()) -> dict:
    """The view the embed holds after one of its own writes landed: `disk`
    (a `compact_state_from_disk` reading taken AFTER the write) restricted
    to `names`, the nodes the embed is known to hold, plus `extra_names`
    (`apply_snapshot`'s `deferred_deletes`: nodes its snapshot omitted while
    still listing their UDF, kept so the next snapshot can still delete
    them and no delta re-adds them meanwhile). `edges` are the embed's
    own set, kept as given (disk may carry more: edges touching nodes it
    does not know; the embed's may name a raw id `compact_state` could not
    resolve, which disk carries too), `ids` the server ids it holds, `bodies` the texts it holds — a node with
    no entry is a body the host cannot vouch for, which the next delta then
    re-sends (`diff_views` reads a missing view body as "differs"). Canvas
    scalars are disk's: a snapshot always carries the embed's and the write
    took them. See "the disk → embed leg" above for why this is not simply
    `disk`."""
    keep = set(names) | set(extra_names)
    known = [n for n in disk["order"] if n in keep]
    return {
        "nodes": {n: disk["nodes"][n] for n in known},
        "order": known,
        "edges": [list(e) for e in edges],
        "canvas": dict(disk["canvas"]),
        "files": {n: disk["files"][n] for n in known if n in disk["files"]},
        "ids": {n: ids[n] for n in known if n in ids},
        "bodies": {n: bodies[n] for n in known if n in bodies},
        "embed_rows": {k: dict(v) for k, v in (embed_rows or {}).items()},
    }


def rename_prev(prev: dict | None, frm: str, to: str, file_to: str | None) -> dict | None:
    """The previous snapshot with one node renamed, so the snapshot that
    follows a rename hint is diffed against the name it will carry."""
    if prev is None:
        return None
    out = dict(prev)
    for key in ("nodes", "files", "ids", "bodies", "embed_rows"):
        if key not in prev:
            continue
        table = dict(prev.get(key) or {})
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
# re-hydrate) and acks, and only then does the host set embed_view := that
# disk reading. After the host applied the embed's own snapshot it re-reads
# disk into embed_view RESTRICTED to the snapshot's nodes (`held_view`, next
# paragraph), so the embed's own edits diff to nothing and are never echoed
# back while a disk-only node still is.
#
# `embed_view` means WHAT THE FRAME HOLDS, nothing wider. Its rows are always
# disk rows (`compact_state_from_disk`), never `compact_state(snapshot)` rows:
# the snapshot's rows carry EMBED_ROW_KEYS only and no code, so a view built
# from one would diff non-empty against disk forever. But after the embed's own
# snapshot landed the view is NOT the whole disk reading either: it is disk
# RESTRICTED to the nodes the snapshot carried (`held_view`). A snapshot is the
# embed's full state, so those are exactly the nodes it knows; a node Claude
# added on disk meanwhile stays OUT of the view, so the next delta carries it
# to the frame — folded into the view, it would diff to nothing, never reach
# the frame, and be read as a deletion by the embed's next snapshot. The same
# goes for a note Claude edited: the view keeps the text the embed SENT, so the
# disk text diffs against it and is delivered instead of overwritten. `ids` are
# the snapshot's own (the server ids the embed truly holds), so a node that
# gained an id on the last push is still told about it (an `ids` op).

# Source files, by suffix, whose text the embed holds as the UDF's code
# verbatim. A text note's `.md` is README-shaped (`Udf.makeZipReadme`), NOT
# the note's text, so it is sent as `body` instead and the frame decides.
CODE_SUFFIXES = (".py", ".json", ".html")


def compact_state_from_disk(clone_dir: str, remote_ids: dict[str, str] | None = None,
                            exclude_orphans: Iterable[str] = ()) -> dict:
    """The clone as the embed should hold it::

        {nodes: {name: row}, order: [name], edges: [[a, b]],
         canvas: {viewport, viewportBounds, comments}, files: {name: basename},
         ids: {name: server id} (from the remote manifest, when known),
         bodies: {name: text} (every node's source file, all four kinds),
         missing: [name],     (rows whose source file is not on disk)
         synthesized: [name]} (orphan source files given a row here, after
                               every real row — NOT on disk until the caller
                               `persist_synthesized`s them; slugs in
                               `exclude_orphans` are never synthesized)

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
        name = row_name(row)
        if name and name not in nodes:        # two rows, one name: the first
            nodes[name] = _with_name(row, name)
            order.append(name)
    # Orphan source files: synthesized rows, chained after the real ones
    # exactly as `persist_synthesized` will write them. `exclude_orphans`
    # (files the embed just created, still in the host's grace window) are
    # left out entirely: no row, no entry anywhere in the reading.
    synthesized: list[str] = []
    placed = [r for r in rows if isinstance(r, dict)]
    for slug, file in orphan_sources(clone_dir, nodes, exclude_orphans):
        row = synthesize_row(placed, slug, file)
        placed.append(row)
        nodes[slug] = dict(row)
        order.append(slug)
        synthesized.append(slug)
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
            "files": files, "ids": ids, "bodies": bodies, "missing": missing,
            "synthesized": synthesized}


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


def _upsert_op(disk: dict, name: str, *, row: bool, body: bool, sid: bool,
               view_row: dict | None = None) -> dict:
    """One upsert. `view_row` is the row the frame holds for this node (None
    on a full state or a node new to the frame): when the disk row's x AND y
    equal it, `node` carries NO `position`. The frame reads a patch position
    as "the host placed this node, in the frame it means" and then skips its
    own frame conversion for a node whose folder membership changed in the
    same delta; an unchanged x/y shipped on every title/colour change would
    pin a node that only moved between folders to stale coordinates."""
    op: dict[str, Any] = {"op": "upsert", "name": name}
    file = disk["files"].get(name)
    if file:
        op["file"] = file
    if row:
        drow = disk["nodes"][name]
        op["row"] = drow
        node = studio_patch_from_row(drow)
        if (view_row is not None and "position" in node
                and drow.get("x") == view_row.get("x")
                and drow.get("y") == view_row.get("y")):
            del node["position"]
        op["node"] = node
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
            ops.append(_upsert_op(disk, name, row=row, body=body, sid=sid,
                                  view_row=vnodes[was]))
    if sorted(map(tuple, disk["edges"])) != sorted(map(tuple, view.get("edges", []))):
        ops.append({"op": "edges", "set": [list(e) for e in disk["edges"]]})
    vcanvas = view.get("canvas", {})
    scalars = {k: v for k, v in disk["canvas"].items()
               if k != "viewport" and vcanvas.get(k) != v}
    if scalars:
        ops.append({"op": "canvas", **scalars})
    return ops


# -- canvas.toml, merged per node -----------------------------------------
#
# The watcher's remote merge is per FILE (canvases._merge_remote). canvas.toml
# is the one file every writer touches — a drag in the embed, a node added in
# a hosted tab, Claude renaming a UDF all rewrite it — so a per-file "local
# changed since the sync point → local wins" there drops a teammate's new
# node row whenever the clone has any unpushed layout edit. This is the
# three-way merge of that one file at the granularity it is actually edited:
# the node row (keyed by kind and `row_name`: a folder and a UDF may share a
# name), the edge set, the canvas scalars.
#
# Folders merge as a UNIT with their children: a child's x/y mean nothing
# without the folder that lists it (relative inside one, absolute outside),
# and membership and coordinates live in different rows, so merged row by
# row a node can end up with one side's membership and the other side's
# coordinates — and jump across the canvas. After the per-row merge:
#
#   membership   A `childUdfOrder` both sides changed merges as a list:
#                local's order minus every name either side removed since
#                base, then the remote's additions in its order. Then
#                `normalise_folders` keeps every child in ONE folder: where
#                two merged folders list it, the folder LOCAL nests it in
#                keeps it (local wins a both-sides move, as for any field);
#                when local nests it in neither, the first in document
#                order. Names with no row are kept (see "folders" above).
#   coordinates  Per axis, the merged value came from local, the remote, or
#                both (base-equal included). If a side it came from had the
#                node in the folder the merge puts it in (a paired rename
#                counts as the same folder), it is kept: relative to that
#                folder, it rides with the folder wherever the folder's own
#                merge put it. Otherwise it is converted: absolute = value +
#                that side's folder x/y when that side nested it (whether or
#                not the folder survived the merge), then relative =
#                absolute − the merged folder's x/y when the merge nests
#                it. The node stays where that side had it on screen.
#   renames      Folders have no id, so a rename reads as a delete plus an
#                add. Every (base folder F the remote no longer has, folder
#                G new on the remote) pair is scored: F's children all in G
#                (subset) first, then the larger share of F's children in G;
#                a pair below the subset needs F to have 3+ children and G
#                to hold at least half (half of 2 is one shared child: too
#                weak to call a rename). Pairs are assigned one-to-one,
#                best first, ties in local document order (then base order
#                for folders local deleted). If local still has F, the
#                merged row takes G's name and merges against base F:
#                field-wise, members three-way, so local's restyle and
#                local's added children both survive. If local deleted F,
#                only a SUBSET pair counts (local's delete beats the
#                remote's rename: G is dropped, its children released); a
#                weaker overlap is a new folder the remote made, kept.
#                Base and local edge endpoints naming F follow the rename
#                (unless a UDF of that name is the endpoint). A LOCAL rename
#                needs no pairing: the remote's old-name row reads as "local
#                deleted it" and stays deleted.
#   refusal      An input in which two rows of the SAME kind share a name is
#                not merged: local is returned as is, `info["collisions"]`
#                naming them; the caller treats it as an unmergeable file.

def rows_of_doc(doc: dict) -> dict:
    """`{nodes: {name: row}, order: [name], rows: [row], edges: [[a, b]],
    canvas: {...}}` from a parsed canvas.toml. `rows` is every named row in
    document order (what `merge_toml` keys by kind and name); `nodes`/`order`
    are keyed by name alone, keeping the FIRST row for a name. Rows with no
    `row_name` are dropped; a folder row is read with `udfName` = its
    folderName."""
    canvas = doc.get("canvas") if isinstance(doc.get("canvas"), dict) else {}
    nodes: dict[str, dict] = {}
    order: list[str] = []
    named: list[dict] = []
    for row in canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []:
        name = row_name(row)
        if not name:
            continue
        named.append(_with_name(row, name))
        if name not in nodes:
            nodes[name] = named[-1]
            order.append(name)
    edges = [[e[0], e[1]] for e in canvas.get("edges") or []
             if isinstance(e, list) and len(e) == 2
             and isinstance(e[0], str) and isinstance(e[1], str)]
    scalars = {k: v for k, v in canvas.items() if k not in ("nodes", "edges")}
    return {"nodes": nodes, "order": order, "rows": named, "edges": edges,
            "canvas": scalars}


_ORDER_KEY = "childUdfOrder"

# A merge key: (is folder, name).
_Key = tuple


def _keyed(src: Any) -> tuple[list, dict, list[str]]:
    """`(order, rows, duplicates)` of a parsed canvas.toml or a `rows_of_doc`
    reading, keyed by (is folder, name), first row winning; `duplicates`
    names two rows of one kind sharing a name. A reading saved before
    `rows` existed falls back to its name-keyed `nodes`."""
    if isinstance(src, dict) and isinstance(src.get("rows"), list):
        raw = src["rows"]
    else:
        raw = _rows_list(src)
    order: list = []
    rows: dict = {}
    dups: list[str] = []
    for row in raw:
        name = row_name(row)
        if not name:
            continue
        key = (is_folder(row), name)
        if key in rows:
            if name not in dups:
                dups.append(name)
            continue
        rows[key] = _with_name(row, name)
        order.append(key)
    return order, rows, dups


def _names(order: Any) -> list[str] | None:
    """`order` when it is a list of names only, else None."""
    if isinstance(order, list) and all(isinstance(n, str) for n in order):
        return order
    return None


def _name_set(row: dict) -> set[str]:
    order = row.get(_ORDER_KEY)
    return {n for n in order if isinstance(n, str) and n} if isinstance(order, list) else set()


def _merge_order(base: list, local: list, remote: list) -> list:
    """Three-way merge of a `childUdfOrder` both sides changed: local's
    order minus what either side removed since base, then the remote's
    additions in its order. Names only (see `_names`)."""
    removed = {n for n in base if n not in local or n not in remote}
    out: list = []
    for n in list(local) + list(remote):
        if n not in removed and n not in out:
            out.append(n)
    return out


def _merge_row(base: dict | None, local: dict, remote: dict) -> dict:
    """Field-wise three-way merge of one row, local winning a same-field
    tie — except a folder's `childUdfOrder`, which merges as a list."""
    if local == remote:
        return dict(local)
    if base is not None:
        if local == base:
            return dict(remote)
        if remote == base:
            return dict(local)
    had_base = base is not None
    base = base or {}
    out: dict = {}
    for key in list(local) + [k for k in remote if k not in local]:
        lv, rv, bv = local.get(key, _ABSENT), remote.get(key, _ABSENT), base.get(key, _ABSENT)
        if lv == rv:
            val = lv
        elif lv == bv:
            val = rv          # only the remote changed this field
        elif rv == bv:
            val = lv          # only local changed it
        elif (key == _ORDER_KEY and had_base and _names(lv) is not None
              and _names(rv) is not None and (bv is _ABSENT or _names(bv) is not None)):
            val = _merge_order(bv if bv is not _ABSENT else [], lv, rv)
        else:
            val = lv          # both changed it: local wins
        if val is not _ABSENT:
            out[key] = val
    return out


_ABSENT = object()

# Scalars that are a view's own, never taken from the other side.
_LOCAL_ONLY_SCALARS = ("viewport", "viewportBounds")


def _renamed_folder(row: dict, frm: str, to: str) -> dict:
    out = dict(row, udfName=to)
    for key in ("folderName", "title"):
        if out.get(key) == frm:
            out[key] = to
    return out


def _folder_renames(bo: list, bn: dict, lo: list, ln: dict,
                    ro: list, rn: dict) -> dict[str, str]:
    """`{base folder name: remote folder name}` for the base folders the
    remote renamed (see "renames" above) — local kept or deleted."""
    local_at = {k: i for i, k in enumerate(lo)}
    base_at = {k: i for i, k in enumerate(bo)}
    fresh = [k for k in ro if k[0] and k not in bn and k not in ln]
    scored = []
    for f in bo:
        if not f[0] or f in rn:
            continue
        kept = f in ln
        was = _name_set(bn[f])
        if not was:
            continue
        for gi, g in enumerate(fresh):
            have = _name_set(rn[g])
            common = was & have
            subset = bool(have) and was <= have
            if not subset and (not kept or len(was) < 3 or 2 * len(common) < len(was)):
                continue
            at = local_at[f] if kept else len(lo) + base_at[f]
            scored.append(((not subset, -len(common) / len(was), at, gi), f, g))
    scored.sort(key=lambda t: t[0])
    out: dict = {}
    taken: set = set()
    for _score, f, g in scored:
        if f in out or g in taken:
            continue
        out[f] = g
        taken.add(g)
    return {f[1]: g[1] for f, g in out.items()}


def _side(order: list, rows: dict) -> tuple[dict, dict, list]:
    """(non-folder rows by name, folder rows by name, rows in order)."""
    leaves = {k[1]: rows[k] for k in order if not k[0]}
    folders = {k[1]: rows[k] for k in order if k[0]}
    return leaves, folders, [rows[k] for k in order]


def _reframe_children(merged: dict, lside: tuple, rside: tuple,
                      renames: dict[str, str]) -> None:
    """Convert, in place, the coordinates of every merged non-folder row
    whose source side nested it in a different folder than the merge does
    (see "coordinates" above)."""
    l_leaves, l_folders, l_rows = lside
    r_leaves, r_folders, r_rows = rside
    home_m = folder_of(merged.values())
    raw_l, home_r = folder_of(l_rows), folder_of(r_rows)
    for (isf, name), row in merged.items():
        if isf:
            continue
        target = home_m.get(name)
        tpos = merged.get((True, target)) if target else None
        for axis in ("x", "y"):
            value = row.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            # (the side's folder as the merge names it, the side's folder row)
            sides: list[tuple[str | None, dict | None]] = []
            if name in l_leaves and l_leaves[name].get(axis) == value:
                lf = raw_l.get(name)
                sides.append((renames.get(lf, lf) if lf else None,
                              l_folders.get(lf) if lf else None))
            if name in r_leaves and r_leaves[name].get(axis) == value:
                rf = home_r.get(name)
                sides.append((rf, r_folders.get(rf) if rf else None))
            if not sides or any(home == target for home, _ in sides):
                continue
            _home, frow = sides[0]
            absolute = value + (_num(frow.get(axis), 0) if frow else 0)
            row[axis] = absolute - (_num(tpos.get(axis), 0) if tpos else 0)


def merge_toml(base: dict | None, local_doc: dict, remote_doc: dict) -> tuple[dict, dict]:
    """Three-way merge of two canvas.toml documents at node granularity.

    `base` is `rows_of_doc` of the toml at the last sync point (None when the
    sync point predates this code: then only nodes the remote has and local
    lacks come in, and local wins everything else — the old per-file rule's
    outcome for the file, but still per node). Returns `(merged_doc, info)`
    where merged_doc is `local_doc` with its [canvas] nodes/edges/scalars
    replaced, and info = {added, removed, taken, kept} by node name, plus
    `renamed` ([[base name, remote name]] for folders paired as renames),
    `folder_fixes` (`normalise_folders`' lines) and `collisions` (names two
    rows of one kind share in an input; then merged_doc IS local_doc,
    unmerged, and the caller must not treat the remote as taken).

    Per node: one side changed → that side; both → field-wise, local wins a
    same-field tie; deleted on one side and untouched on the other → deleted;
    deleted on one side and changed on the other → the change wins (local
    deletes beat remote changes, as the file rule did: the push propagates
    the delete). Folders and their children: membership, coordinates and
    renames as the section comment above says. Edges: union minus what
    either side deleted since base, minus edges touching a dropped node.
    Scalars: viewport/viewportBounds local; others local unless only the
    remote moved them.
    """
    info: dict[str, list] = {"added": [], "removed": [], "taken": [], "kept": [],
                             "renamed": [], "folder_fixes": [], "collisions": []}
    lo, ln, ldups = _keyed(local_doc)
    ro, rn, rdups = _keyed(remote_doc)
    if ldups or rdups:
        info["collisions"] = ldups + [n for n in rdups if n not in ldups]
        return local_doc, info
    L, R = rows_of_doc(local_doc), rows_of_doc(remote_doc)
    B = base or {"edges": [], "canvas": {}}
    bo, bn, _bdups = _keyed(base) if base else ([], {}, [])
    renames = _folder_renames(bo, bn, lo, ln, ro, rn)
    # Local deleted the folder the remote renamed: the delete wins.
    dropped = {(True, g) for f, g in renames.items() if (True, f) not in ln}
    merged: dict = {}
    for k in lo:
        if k[0] and k[1] in renames:
            to = renames[k[1]]
            merged[(True, to)] = _merge_row(_renamed_folder(bn[k], k[1], to),
                                            _renamed_folder(ln[k], k[1], to), rn[(True, to)])
        elif k in rn:
            merged[k] = _merge_row(bn.get(k), ln[k], rn[k])
        elif k in bn and bn[k] == ln[k]:
            info["removed"].append(k[1])      # remote deleted an untouched node
        else:
            merged[k] = dict(ln[k])           # local added or changed it
    for k in ro:
        if k in merged or k in ln or k in dropped:
            continue
        if k in bn:
            continue                          # local deleted it: stays deleted
        merged[k] = dict(rn[k])               # new on the remote

    lside, rside = _side(lo, ln), _side(ro, rn)
    local_nesting = {c: renames.get(f, f) for c, f in folder_of(lside[2]).items()}
    info["folder_fixes"] = normalise_folders(list(merged.values()), prefer=local_nesting)
    _reframe_children(merged, lside, rside, renames)

    paired = {(True, g) for f, g in renames.items() if (True, f) in ln}
    info["renamed"] = [[f, g] for f, g in renames.items() if (True, f) in ln]
    for k, row in merged.items():
        if k in paired:
            info["taken"].append(k[1])
        elif k not in ln:
            info["added"].append(k[1])
        elif k not in rn or row == ln[k]:
            info["kept"].append(k[1])
        else:
            info["taken"].append(k[1])

    def edge_set(rows):
        return {(a, b) for a, b in rows}

    def follow(rows, leaves):                 # base/local edges, renames applied
        def end(n):
            return n if n in leaves else renames.get(n, n)
        return [[end(a), end(b)] for a, b in rows]
    b_leaves = {k[1] for k in bo if not k[0]}
    b_edges, l_edges = follow(B["edges"], b_leaves), follow(L["edges"], lside[0])
    be, le, re_ = edge_set(b_edges), edge_set(l_edges), edge_set(R["edges"])
    edges = ((le | re_) - (be - le) - (be - re_))
    ordered = [e for e in l_edges if tuple(e) in edges]
    seen = {tuple(e) for e in ordered}
    for e in R["edges"]:
        if tuple(e) in edges and tuple(e) not in seen:
            ordered.append(e)
            seen.add(tuple(e))
    names = {k[1] for k in merged}
    ordered = [e for e in ordered if e[0] in names and e[1] in names]

    scalars = dict(L["canvas"])
    for key, rv in R["canvas"].items():
        if key in _LOCAL_ONLY_SCALARS:
            continue
        lv, bv = L["canvas"].get(key, _ABSENT), B["canvas"].get(key, _ABSENT)
        if lv == bv and rv != lv:
            scalars[key] = rv
    for key in list(scalars):
        if key not in R["canvas"] and key not in _LOCAL_ONLY_SCALARS \
                and scalars[key] == B["canvas"].get(key, _ABSENT):
            del scalars[key]                  # remote removed an untouched scalar

    # Keep the local document's key order (rtoml writes keys in dict order):
    # an unchanged merge must dump to the same bytes it read.
    out = dict(local_doc)
    canvas = dict(local_doc.get("canvas") or {})
    for key in list(canvas):
        if key not in ("nodes", "edges") and key not in scalars:
            del canvas[key]
    for key, val in scalars.items():
        canvas[key] = val
    canvas["nodes"] = list(merged.values())
    canvas["edges"] = ordered
    out["canvas"] = canvas
    return out, info
