"""Embedded-workbench canvas snapshots → `canvas.toml` on disk.

The canvas sync's one writer to the server is the clone watcher (canvases.py).
Under embed v3 the workbench inside /canvases/<env>/<name> never saves to the server:
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
                                              own row follows), slug reported
                                              as `rowless_files`

  `apply_rename(...)`     — the rename hint the workbench posts the moment a
                            UDF is renamed, before any snapshot carries the
                            new name: file, row, edges, in one write.

  "Known before" is the previous snapshot's node set when there is one, else
  the embed's own `base_nodes` — the names it held right after hydrating. So
  a node deleted in the first moments of a generation is still a deletion,
  and a node Claude added on disk meanwhile (in neither set) survives.

  Code files (`.py`), JSON-UI (`.json`) and HTML (`.html`) bodies are never
  written from this leg. A workbench that speaks the BODY CHANNEL
  (`body_channel: true` on the snapshot) sends EVERY source text — code and
  notes, new files included — through `POST /api/canvases/body` instead
  (canvases.SyncManager.apply_body): one file per request, accepted only
  when the hash of what disk holds equals the `base_hash` the frame names,
  so a Claude edit that landed meanwhile is never overwritten; the frame
  rebases on the disk text it gets back and resends. Such a snapshot carries
  no `file_body` and no `udf_body`, and this module writes no source file
  for it. An older workbench (no flag) still ships note bodies and new
  files here, as below.

  Every write is content-gated. A write that changed nothing would still bump
  mtimes, so `disk_rev` would move, the page would cut a delta and post it,
  and the embed would answer with another snapshot — a loop.

No import of fused_render.server (canvases.py includes this; keep it
acyclic). `rtoml` comes from the bundled fused-py; a missing module is a
clear error, not a silent skip.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
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
# before writing the row binds once the row exists.
# `normalise_folders` drops only what no reader can honour: a non-string
# entry, an entry naming a folder, a name listed twice by one folder, and a
# name two folders list (the preferred folder keeps it, local's nesting in a
# merge, else the first in document order). It runs on every canvas.toml the
# snapshot leg and the pull write; nothing is ever rolled back over it.
#
# Names are not unique. A folder and a UDF may share one (a folder's
# `childUdfOrder` entry always means the UDF), and so may two folders: the
# workbench numbers new folders by count, paste copies a folderName, rename
# checks nothing, and the server hydrates each folder by its own id in
# document order, first lister winning a child. `merge_toml` keys rows by
# (is folder, name, occurrence) and `normalise_folders` tells same-named
# folders apart by occurrence, so such a canvas merges like any other.
#
# KNOWN GAP (view leg only): the snapshot and delta legs (`apply_snapshot`,
# `compact_state_from_disk`, `diff_views`) key rows by name alone
# (`rows_by_name`), so of two folders sharing a name they keep the FIRST and
# do not show the other; `folder_problems` and `normalise_folders` report it.

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


def _folder_ids(rows: Iterable[Any]) -> dict[int, tuple[str, int]]:
    """`{id(folder row): (name, occurrence)}`: two folders may share a name
    (the workbench numbers new folders by count, paste copies the name), and
    each is its own folder, counted in document order — the server hydrates
    per folder in that order, first lister winning a child."""
    out: dict[int, tuple[str, int]] = {}
    seen: dict[str, int] = {}
    for row in rows:
        name = row_name(row) if is_folder(row) else None
        if name:
            out[id(row)] = (name, seen.get(name, 0))
            seen[name] = seen.get(name, 0) + 1
    return out


def normalise_folders(rows: list, prefer: dict[str, Any] | None = None) -> list[str]:
    """Make `rows` (in place) satisfy `folder_problems` as far as dropping
    entries can: a `childUdfOrder` that is not a list becomes empty; an entry
    that is not a non-empty string, names a folder, or repeats within its
    folder is dropped; a name two folders list stays only in `prefer[name]`
    (a folder name, or a (name, occurrence) pair) when that folder lists
    it, else in the first lister in document order. Entries naming no row
    are kept. Returns one line per fix, plus one per name rows share
    (reported, not fixable here)."""
    fixes = _clash_lines(rows, " (not fixed)")
    # An entry names the UDF when one has the name, a folder only otherwise.
    leaves = {row_name(r) for r in rows if not is_folder(r)} - {None}
    folders = ({row_name(r) for r in rows if is_folder(r)} - {None}) - leaves
    ids = _folder_ids(rows)
    listers: dict[str, list[tuple[str, int]]] = {}
    for row in rows:
        fid = ids.get(id(row))
        order = row.get("childUdfOrder") if fid else None
        if not isinstance(order, list):
            continue
        for child in order:
            if isinstance(child, str) and child and child not in folders:
                found = listers.setdefault(child, [])
                if fid not in found:
                    found.append(fid)
    owner: dict[str, tuple[str, int]] = {}
    for child, found in listers.items():
        want = (prefer or {}).get(child)
        if isinstance(want, str):
            want = (want, 0)
        owner[child] = want if want in found else found[0]

    def label(fid: tuple[str, int]) -> str:
        return repr(fid[0]) if fid[1] == 0 else f"{fid[0]!r} (#{fid[1] + 1})"
    for row in rows:
        fid = ids.get(id(row))
        if fid is None or "childUdfOrder" not in row:
            continue
        order = row["childUdfOrder"]
        if not isinstance(order, list):
            row["childUdfOrder"] = []
            fixes.append(f"folder {label(fid)}: childUdfOrder was not a list")
            continue
        kept: list[str] = []
        seen: list = []           # one report per name, however often listed
        for child in order:
            again = child in seen
            seen.append(child)
            if not isinstance(child, str) or not child:
                fixes.append(f"folder {label(fid)}: dropped {child!r} (not a name)")
            elif child in folders:
                if not again:
                    fixes.append(f"folder {label(fid)}: dropped {child!r} (a folder)")
            elif owner.get(child) != fid:
                if not again:
                    fixes.append(f"folder {label(fid)}: dropped {child!r} "
                                 f"(folder {label(owner[child])} keeps it)")
            elif child in kept:
                fixes.append(f"folder {label(fid)}: dropped a second {child!r}")
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
    # The workbench sends source texts over the body channel: nothing here
    # is a body to write (a `file_body` such a frame still sent would be a
    # stale re-send racing its own body posts).
    body_channel = body.get("body_channel") is True
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
        # mirror, which the workbench does NOT refresh when a node is hidden
        # or shown (`use-udf-node-sync`: the mirror is written only by the
        # TOML export path, never by the live canvas). A snapshot taken after
        # a hide therefore carries `is_visible: false` beside a node whose
        # mirror still says `true`. The row wins whenever it is present; the
        # mirror only stands in for a UDF the snapshot lists without one.
        row = nodes.get(slug)
        if row is not None and isinstance(udf.get("is_visible"), bool):
            row["visible"] = udf["is_visible"]
        # `file_body` is the exact file text as `fused canvas pull` would
        # write it (a note's README-shaped markdown, not its raw text);
        # `udf_body.code` is the file for a never-saved code UDF.
        if body_channel:
            continue
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

IGNORED_BASENAMES = frozenset({"CLAUDE.md", ".fusedignore"})


def fusedignore_rules(clone_dir: str) -> list[tuple[str, bool, bool, bool]]:
    """The clone's `.fusedignore` as `(pattern, negated, dir_only, anchored)`
    basename rules; a rule with an inner `/` is left to the CLI."""
    data = _read_bytes(os.path.join(clone_dir, ".fusedignore")) or b""
    rules = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line[1:].strip() if negated else line
        dir_only = line.endswith("/")
        anchored = line.startswith("/")
        line = line.strip("/").removeprefix("**/")
        if line and "/" not in line:
            rules.append((line, negated, dir_only, anchored))
    return rules


def fusedignored(rules: list, name: str, *, is_dir: bool = False, at_root: bool = True) -> bool:
    """Last matching rule wins; a file matches at the clone root only, a
    directory at any depth unless the rule is anchored."""
    out = False
    for pattern, negated, dir_only, anchored in rules:
        if (dir_only and not is_dir) or (not at_root and (anchored or not is_dir)):
            continue
        if fnmatch.fnmatchcase(name, pattern):
            out = not negated
    return out


def node_files(clone_dir: str) -> dict[str, str]:
    """`{stem: basename}` of every source file at the clone root that can be
    a node; `.py` wins over `.json`, `.md`, `.html` for a shared stem."""
    rules = fusedignore_rules(clone_dir)
    present = sorted(os.listdir(clone_dir))
    out: dict[str, str] = {}
    for suffix in _SOURCE_SUFFIXES:
        for f in present:
            stem = f[:-len(suffix)]
            if (f.endswith(suffix) and stem and not f.startswith(".") and f not in IGNORED_BASENAMES
                    and not (suffix == ".py" and stem.startswith("_"))
                    and not fusedignored(rules, f)
                    and os.path.isfile(os.path.join(clone_dir, f))):
                out.setdefault(stem, f)
    return out


def rows_by_name(rows: list) -> dict[str, dict]:
    """`{name: row}`: of two UDF rows sharing a name the LAST wins; a folder
    keeps its name against any later row."""
    out: dict[str, dict] = {}
    for row in rows:
        name = row_name(row)
        if name and not (name in out and (is_folder(row) or is_folder(out[name]))):
            out[name] = row
    return out


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


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
    overwritten. `rowless_prev` are the files this embed generation created
    that still have no row (the host's `rowless_files` so far): one this
    snapshot no longer lists (no UDF, no node, no row) has its file trashed
    and is reported in `deleted`. A node the embed held before (`prev`)
    whose row left disk is not re-added. Returns `{changed, upserted,
    deleted, deleted_files, renamed, created, written, fileless,
    rowless_files, deferred_deletes, folder_fixes}` (`deleted_files`: the
    source files this snapshot removed from the clone, by basename, a
    folder in `deleted` has none; `fileless`: rows added for nodes whose
    source has not arrived; `rowless_files`: the embed's files still
    without a row; `deferred_deletes`: known nodes the snapshot omitted
    while still listing their UDF, the host keeps them in its view;
    `folder_fixes`: `normalise_folders`' lines). Raises FileNotFoundError
    when the clone has no canvas.toml, and whatever `rtoml` raises on a
    malformed one (the caller reports both as rejections).
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
    by_name = rows_by_name(rows)
    on_disk = node_files(clone_dir)
    # What the doc held before this snapshot: the toml is written only when
    # the doc ends up different (the folder normalisation below can undo an
    # upsert, and a write that changes nothing would loop through disk_rev).
    doc_before = copy.deepcopy(doc)

    changed = False
    upserted: list[str] = []
    deleted: list[str] = []
    deleted_files: list[str] = []
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
        _move_file(clone_dir, on_disk.get(name), new["files"].get(to))
        renamed.append([name, to])
        vanished.remove(name)
        changed = True

    files_changed = False
    deferred_deletes: list[str] = []

    def remove(file: str | None) -> None:
        nonlocal files_changed
        if file and os.path.isfile(os.path.join(clone_dir, file)):
            trash(file)
            os.remove(os.path.join(clone_dir, file))
            files_changed = True
            deleted_files.append(file)

    for name in vanished:
        if name in new["files"]:
            # stale closure: the embed's nodes lag its udfs, not a delete
            deferred_deletes.append(name)
            continue
        row = by_name.pop(name, None)
        if row is None:
            to = id_to_new_name.get(ids_before.get(name))
            if to and to != name:
                if _move_file(clone_dir, on_disk.get(name), new["files"].get(to)):
                    renamed.append([name, to])
                    files_changed = True
                continue
            if prev is None or is_folder(prev["nodes"].get(name)):
                continue
        else:
            rows.remove(row)
            changed = True
        deleted.append(name)
        if row is None or not is_folder(row):
            remove(on_disk.get(name))

    rowless_files: list[str] = []
    for slug in rowless_prev:
        if slug in new["nodes"] or slug in by_name or slug in deleted or slug not in on_disk:
            continue
        if slug in new["files"]:
            rowless_files.append(slug)
            continue
        remove(on_disk[slug])
        deleted.append(slug)

    # Upserts: rows that differ on the keys the embed owns. A key the embed's
    # row LACKS is deleted from disk only when the embed's own PREVIOUS row
    # carried it (`embed_rows`, recorded on the view by the host after each
    # snapshot) — never because the DISK row has it: `compact_node` emits
    # several keys conditionally (an empty description, a catalog key that is
    # None), so measured against the disk row every snapshot after a delta
    # would strip them and rewrite the toml for nothing.
    prev_nodes = (prev.get("embed_rows") or {}) if prev else {}
    fileless: list[str] = []
    for name, row in new["nodes"].items():
        disk = by_name.get(name)
        if disk is None:
            if prev is not None and name in prev["nodes"]:
                continue
            if name not in on_disk and not is_folder(row):
                fileless.append(name)
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
    for entry in new["new_files"]:
        fpath = os.path.join(clone_dir, entry["file"])
        if not os.path.exists(fpath):
            _write_atomic(fpath, entry["body"])
            created.append(entry["file"])
        slug, suffix = os.path.splitext(entry["file"])
        if (slug and suffix in _SOURCE_SUFFIXES and slug not in new["nodes"]
                and slug not in by_name and slug not in rowless_files
                and os.path.isfile(fpath)):
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
            "upserted": upserted, "deleted": deleted, "deleted_files": deleted_files,
            "renamed": renamed,
            "created": created, "written": written, "fileless": fileless,
            "rowless_files": rowless_files, "deferred_deletes": deferred_deletes,
            "folder_fixes": folder_fixes, "at": time.time()}


def held_view(disk: dict, names: Iterable[str], *, edges: list, ids: dict,
              bodies: dict, embed_rows: dict | None,
              extra_names: Iterable[str] = ()) -> dict:
    """The view the embed holds after one of its own writes landed: `disk`
    (a `compact_state_from_disk` reading taken AFTER the write) restricted
    to `names`, the nodes the embed is known to hold (a node whose file has
    not arrived yet among them), plus `extra_names`
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
    rows = {**(embed_rows or {}), **disk["rowed"]}
    known = [n for n in rows if n in keep]
    return {
        "nodes": {n: rows[n] for n in known},
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

# Every source file kind a body post may name: the four the clone holds.
BODY_SUFFIXES = CODE_SUFFIXES + EMBED_BODY_SUFFIXES


def body_hash(text: str) -> str:
    """The hash both ends of the body channel compute over a source text:
    sha256 of the TEXT re-encoded as UTF-8, never of the raw disk bytes. Disk
    is read with `decode("utf-8", "replace")`, so an invalid byte becomes
    U+FFFD on the way to the frame, and the frame hashes the string it got;
    hashing the bytes would make such a file differ forever."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def read_body(path: str) -> str | None:
    """A source file as the body channel reads it (None when missing)."""
    data = _read_bytes(path)
    return None if data is None else data.decode("utf-8", "replace")


def compact_state_from_disk(clone_dir: str, remote_ids: dict[str, str] | None = None) -> dict:
    """The clone as the embed should hold it::

        {nodes: {name: row}, order: [name], edges: [[a, b]],
         canvas: {viewport, viewportBounds, comments}, files: {name: basename},
         ids: {name: server id} (from the remote manifest, when known),
         bodies: {name: text},   (every node's source file, all four kinds)
         rowed: {name: row}}     (every row, a file-less one too)

    A row whose source file is not on disk is not a node, only a row in
    `rowed`; a folder has no file and is always a node; `files` lists every
    source file, a row-less one too. `edges` keep every edge between rowed
    names. Same shape as `compact_state` where the keys
    overlap, so it serves as `prev` for `apply_snapshot` too. Raises
    FileNotFoundError when there is no canvas.toml and whatever rtoml raises
    on a malformed one.
    """
    doc = _load_toml(os.path.join(clone_dir, "canvas.toml"))
    canvas = doc.get("canvas") or {}
    if not isinstance(canvas, dict):
        raise ValueError("canvas.toml: [canvas] is not a table")
    rows = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
    rowed = {n: _with_name(r, n) for n, r in rows_by_name(rows).items()}
    files = {s: f for s, f in node_files(clone_dir).items() if not is_folder(rowed.get(s))}
    nodes = {n: r for n, r in rowed.items() if is_folder(r) or n in files}
    order = list(nodes)
    bodies = {n: read_body(os.path.join(clone_dir, files[n])) or "" for n in nodes if n in files}
    edges: list[list[str]] = []
    for e in canvas.get("edges") or []:
        if (isinstance(e, list) and len(e) == 2
                and e[0] in rowed and e[1] in rowed):
            edges.append([e[0], e[1]])
    scalars = {k: canvas[k] for k in CANVAS_KEYS if k in canvas}
    ids = {n: remote_ids[n] for n in nodes if remote_ids and isinstance(remote_ids.get(n), str)}
    return {"nodes": nodes, "order": order, "edges": edges, "canvas": scalars,
            "files": files, "ids": ids, "bodies": bodies, "rowed": rowed}


_NODE_GAP_PX = 75
_FOLDER_PADDING_PX = 40
_DEFAULT_NODE_SIZE = (700, 500)
_DEFAULT_TEXTBOX_SIZE = (350, 250)


def _default_size(filename: str) -> tuple[int, int]:
    return _DEFAULT_TEXTBOX_SIZE if filename.endswith(".md") else _DEFAULT_NODE_SIZE


_DEFAULT_DATA: dict[str, dict[str, Any]] = {
    ".md": {"textBoxColor": "#FFF9C4", "textBoxGradient": True,
            "textBoxScaleFactor": 1.0, "textBoxAlignment": "left"},
    ".json": {"nodeDescriptionScaleFactor": 1.5, "widgetBackgroundColor": "#2e2e2e"},
    ".py": {"nodeDescriptionScaleFactor": 1.5},
    ".html": {"nodeDescriptionScaleFactor": 1.5},
}


def _default_row(stem: str, file: str, x: float, y: float, z: float) -> dict:
    width, height = _default_size(file)
    return {"udfName": stem, "x": x, "y": y, "zIndex": z, "width": width, "height": height,
            "title": stem, "visible": True, **_DEFAULT_DATA[os.path.splitext(file)[1]]}


def add_canvas_type(clone_dir: str) -> None:
    path = os.path.join(clone_dir, "canvas.toml")
    doc = _load_toml(path)
    if "type" not in doc:
        _write_atomic(path, _dump_toml({"type": "canvas", **doc}))


def reconcile_rows(clone_dir: str, *, pending: Iterable[str] = (), drop: Iterable[str] = (),
                   publish: bool = False,
                   backup: Callable[[str, bytes], None] | None = None) -> dict:
    """Rows follow files: `type` set, every UDF name's LAST row kept, a
    default row for each file none names (`pending` ones excepted, placed
    inside the folder that lists them). A UDF row with no file is dropped
    with its edges only when named in `drop` or when `publish` (the copy
    the push ships), where edges to no row go too."""
    path = os.path.join(clone_dir, "canvas.toml")
    doc = _load_toml(path)
    before = copy.deepcopy(doc)
    doc = {"type": "canvas", **doc}
    canvas = doc.get("canvas", {})
    if not isinstance(canvas, dict):
        raise ValueError("canvas.toml: [canvas] is not a table")
    files = node_files(clone_dir)
    rows_in = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
    last = {row_name(r): r for r in rows_in if not is_folder(r)}
    rows: list = []
    dropped: list[str] = []
    for row in rows_in:
        name = row_name(row)
        if not is_folder(row) and (last[name] is not row or (
                name not in files and (publish or name in drop))):
            dropped.append(str(name))
            continue
        rows.append(row)
    owners: dict[str, dict] = {}
    for r in rows:
        if is_folder(r) and isinstance(r.get("childUdfOrder"), list):
            for c in r["childUdfOrder"]:
                if isinstance(c, str):
                    owners.setdefault(c, r)
    taken = {row_name(r) for r in rows} | set(pending)
    missing = [s for s in sorted(files) if s not in taken]
    z = max((_num(r.get("zIndex"), 0) for r in rows), default=0)
    for stem in missing:
        owner = owners.get(stem)
        if owner is None:
            continue
        kids = [r for r in rows if not is_folder(r) and owners.get(row_name(r)) is owner]
        kx = min((_num(r.get("x"), 0) for r in kids), default=_FOLDER_PADDING_PX)
        ky = max((_num(r.get("y"), 0) + _num(r.get("height"), 0) for r in kids),
                 default=0) + _NODE_GAP_PX
        z += 1
        rows.append(_default_row(stem, files[stem], kx, ky, z))
        owner["height"] = max(_num(owner.get("height"), 0),
                              ky + rows[-1]["height"] + _FOLDER_PADDING_PX)
    tops = [(_num(r.get("x"), 0), _num(r.get("y"), 0) + _num(r.get("height"), 0)) for r in rows
            if is_folder(r) or row_name(r) not in owners]
    x = min((t[0] for t in tops), default=100)
    y = max((t[1] for t in tops), default=100 - _NODE_GAP_PX)
    for stem in missing:
        if stem in owners:
            continue
        z += 1
        rows.append(_default_row(stem, files[stem], x, y + _NODE_GAP_PX, z))
        y += _NODE_GAP_PX + rows[-1]["height"]
    kept = {row_name(r) for r in rows} - {None}
    gone = set(dropped) - kept
    edges = [e for e in canvas.get("edges") or []
             if isinstance(e, list) and len(e) == 2
             and all(isinstance(end, str) and end not in gone and (end in kept or not publish)
                     for end in e)]
    for key, value in (("nodes", rows), ("edges", edges)):
        if canvas.get(key, []) != value:
            canvas[key] = value
    if canvas:
        doc["canvas"] = canvas
    changed = doc != before
    if changed:
        if backup is not None:
            old = _read_bytes(path)
            if old is not None:
                backup("canvas.toml", old)
        _write_atomic(path, _dump_toml(doc))
    return {"changed": changed, "dropped": dropped, "added": missing}


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
        text = disk["bodies"][name]
        if file and file.endswith(CODE_SUFFIXES):
            op["code"] = text
        else:
            op["body"] = text
        # The frame's base for its next body post (canvases.apply_body).
        op["hash"] = body_hash(text)
    if sid and name in disk["ids"]:
        op["id"] = disk["ids"][name]
    return op


def held_edges(disk: dict, vnodes: Iterable[str]) -> list[list[str]]:
    held = disk["nodes"].keys() | ((disk["rowed"].keys() | disk["files"].keys()) & set(vnodes))
    return [list(e) for e in disk["edges"] if e[0] in held and e[1] in held]


def diff_views(disk: dict, view: dict | None) -> list[dict]:
    """The ops that take `view` (what the embed holds; None = nothing known,
    a full state) to `disk`. Node-level, by UDF name:

        upsert {name, file, row?, node?, code?|body?, hash?, id?}  — new or
                                           changed; an absent field means
                                           "unchanged", never "cleared";
                                           `hash` = body_hash of the text sent
        rename {from, to, file}           — a name vanished whose server id
                                           reappears under a name the view
                                           lacks (then diffed as `to`)
        delete {name}
        delete {name} + upsert {name, …}  — the node's source file changed
                                           KIND (`w.json` → `w.html`): the
                                           frame rebuilds the Udf from the
                                           file; `edges` is then always sent
        edges  {set: [[a, b]]}            — the whole set, when it differs
        canvas {comments?, viewportBounds?} — the scalars that differ; the
                                           frame never moves its own camera
    A full state carries every node as an upsert plus `edges` and `canvas`;
    the frame drops what is not listed. A node whose file is missing but whose
    row remains is not deleted.
    """
    ops: list[dict] = []
    if view is None:
        for name in disk["order"]:
            ops.append(_upsert_op(disk, name, row=True, body=True, sid=True))
        ops.append({"op": "edges", "set": held_edges(disk, ())})
        if disk["canvas"]:
            ops.append({"op": "canvas", **{k: v for k, v in disk["canvas"].items()
                                           if k != "viewport"}})
        return ops
    vnodes, dnodes = view["nodes"], disk["nodes"]
    vids, dids = view.get("ids", {}), disk["ids"]
    vbodies = view.get("bodies", {})
    vfiles = view.get("files", {})
    renamed_to: dict[str, str] = {}
    id_to_disk = {sid: n for n, sid in dids.items()}
    # A node whose source file changed KIND (Claude turned `w.json` into
    # `w.html`) is not an upsert: the frame's Udf carries its kind in
    # `metadata.nodeTemplateType` (a JSON-UI widget, a note, an HTML node, a
    # Python UDF), the applier never re-reads it from `file`, and so a plain
    # upsert would leave a JSON-UI widget holding HTML text — rendered wrong,
    # and written back to disk under the OLD name on the frame's next body
    # post. So the frame is told `delete` then a full `upsert` under the same
    # name: it drops the Udf and builds a fresh one of the new kind from the
    # file (the row carries the position and size the node keeps). Its
    # edges go with the deleted node, so the edge set is always re-sent.
    retyped = False

    def kind_changed(view_file: str | None, disk_file: str | None) -> bool:
        if not view_file or not disk_file:
            return False
        return os.path.splitext(view_file)[1] != os.path.splitext(disk_file)[1]

    for name in vnodes:
        if name in disk["rowed"] or name in disk["files"]:
            continue
        sid = vids.get(name)
        to = id_to_disk.get(sid) if sid else None
        if to and to not in vnodes and not kind_changed(vfiles.get(name), disk["files"].get(to)):
            renamed_to[to] = name
            ops.append({"op": "rename", "from": name, "to": to,
                        "file": disk["files"].get(to)})
        else:
            # A rename that also changed kind is a delete plus a new node.
            ops.append({"op": "delete", "name": name})
            retyped = retyped or bool(to)
    for name in disk["order"]:
        was = renamed_to.get(name, name)
        if was not in vnodes:
            ops.append(_upsert_op(disk, name, row=True, body=True, sid=True))
            continue
        if kind_changed(vfiles.get(was), disk["files"].get(name)):
            retyped = True
            ops.append({"op": "delete", "name": name})
            ops.append(_upsert_op(disk, name, row=True, body=True, sid=True))
            continue
        row = dnodes[name] != vnodes[was]
        body = disk["bodies"].get(name) != vbodies.get(was)
        sid = name in dids and dids[name] != vids.get(was)
        if row or body or sid:
            ops.append(_upsert_op(disk, name, row=row, body=body, sid=sid,
                                  view_row=vnodes[was]))
    edges = held_edges(disk, vnodes)
    if retyped or sorted(map(tuple, edges)) != sorted(map(tuple, view.get("edges", []))):
        ops.append({"op": "edges", "set": edges})
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
# the node row, the edge set, the canvas scalars.
#
# A row's merge key is (is folder, name, occurrence): a folder and a UDF may
# share a name, and so may two folders (the workbench numbers new folders by
# count, so a delete then a regroup makes a second Folder_2; paste copies a
# folderName verbatim). For a UDF the occurrence counts rows of that name in
# document order, which is how the server hydrates them, first one winning.
# A FOLDER's position in the document says nothing about which folder it is:
# the workbench PREPENDS a new folder, so a teammate's new Folder_2 pushes
# the existing one to second place. Same-named folders are matched to base's
# by their children (`_align_folders`): base's children all present first,
# then the larger share of them, one-to-one, best first; occurrence only
# breaks ties (and decides among empty folders). Two non-empty folders with
# no child in common are NOT the same folder (the old one was deleted and
# the name reused); an empty one matches. A folder no base folder matches
# is new on its side; a local-new and a remote-new folder of one name are
# the same folder when they share a child or are both empty (or one is
# empty only because the other's children are all held elsewhere on its
# side: what an earlier merge leaves), and then hold the union of both
# sides' children. A folder the remote deleted but local changed stays,
# releasing the children the deletion released.
# A `childUdfOrder` entry or an edge endpoint naming X means the first row
# of that name in the document, as on the server.
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
#   coordinates  Who MOVED a node is decided once per node, both axes
#                together. A side that kept base's frame (the same folder,
#                or none) moved it only if it changed the raw x/y — a drag
#                of the FOLDER moves no child. A side that changed the frame
#                moved it only if its position on screen (through its own
#                folder) differs from base's — ungrouping or regrouping in
#                place rewrites the x/y but moves nothing. When exactly one
#                side moved it, that side's x/y are taken, in that side's
#                frame. When both or neither did (or there is no base), the
#                field-wise merge's x/y stand, attributed per axis to the
#                side they came from (local first). Membership comes first:
#                when the merged folder is exactly one side's, and the other
#                side re-nested the node elsewhere or also moved it, the
#                winning side's x/y are taken (its frame, converted) — a
#                node is never drawn outside the folder that lists it. If that side nested the node in
#                the folder the merge puts it in (a paired rename counts as
#                the same folder), the x/y are kept: relative to that
#                folder, they ride with it wherever its own merge put it.
#                Otherwise they are converted: absolute = x/y + that side's
#                folder x/y when that side nested it (whether or not the
#                folder survived the merge), then relative = absolute − the
#                merged folder's x/y when the merge nests it.
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
#                weaker overlap is a folder the remote made, kept. Base and
#                local edge endpoints naming F follow the rename (unless a
#                UDF of that name is the endpoint). A LOCAL rename needs no
#                pairing: the remote's old-name row reads as "local deleted
#                it" and stays deleted.
#
# The sync point's rows are `rows_of_doc(...)["rows"]`. A base saved before
# `rows` existed falls back to its name-keyed `nodes` (first row per name
# only), and one saved before this branch never held a folder row written
# without `udfName` at all: on the FIRST pull after the upgrade such a folder
# reads as "local added it", so one the remote deleted comes back, once.

def rows_of_doc(doc: dict) -> dict:
    """`{nodes: {name: row}, order: [name], rows: [row], edges: [[a, b]],
    canvas: {...}}` from a parsed canvas.toml. `rows` is EVERY named row in
    document order (what `merge_toml` keys by kind, name and occurrence);
    `nodes`/`order` are keyed by name alone, keeping the FIRST row for a
    name (the view leg's reading). Rows with no `row_name` are dropped; a
    folder row is read with `udfName` = its folderName."""
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


def _keyed(src: Any) -> tuple[list, dict]:
    """`(order, rows)` of a parsed canvas.toml or a `rows_of_doc` reading,
    keyed by (is folder, name, occurrence). A reading saved before `rows`
    existed falls back to its name-keyed `nodes`."""
    if isinstance(src, dict) and isinstance(src.get("rows"), list):
        raw = src["rows"]
    else:
        raw = _rows_list(src)
    order: list = []
    rows: dict = {}
    seen: dict = {}
    for row in raw:
        name = row_name(row)
        if not name:
            continue
        kind = (is_folder(row), name)
        key = (*kind, seen.get(kind, 0))
        seen[kind] = key[2] + 1
        rows[key] = _with_name(row, name)
        order.append(key)
    return order, rows


def _child_match_order(left: list, right: list, allow: Callable[..., bool]) -> list:
    """One-to-one `(left key, right key)` pairs of same-named folders, best
    first: `left`'s children all in `right` first, then the larger share of
    `left`'s children in `right`, then the nearer occurrence. Only pairs
    `allow(lk, lset, rk, rset)` admits are considered."""
    scored = []
    for li, (lk, lset) in enumerate(left):
        for ri, (rk, rset) in enumerate(right):
            if not allow(lk, lset, rk, rset):
                continue
            common = lset & rset
            subset = lset <= rset if lset else not rset
            share = len(common) / len(lset) if lset else (1.0 if not rset else 0.0)
            scored.append(((not subset, -share, abs(li - ri), li, ri), lk, rk))
    scored.sort(key=lambda t: t[0])
    pairs, used_l, used_r = [], set(), set()
    for _score, lk, rk in scored:
        if lk in used_l or rk in used_r:
            continue
        pairs.append((lk, rk))
        used_l.add(lk)
        used_r.add(rk)
    return pairs


def _by_name(order: list, rows: dict) -> dict[str, list]:
    out: dict[str, list] = {}
    for key in order:
        if key[0]:
            out.setdefault(key[1], []).append((key, _name_set(rows[key])))
    return out


def _nested(order: list, rows: dict) -> set[str]:
    """Names some folder on this side lists."""
    out: set[str] = set()
    for key in order:
        if key[0]:
            out |= _name_set(rows[key])
    return out


def _align_folders(base: tuple, local: tuple, remote: tuple) -> tuple[tuple, tuple]:
    """`local` and `remote` (`_keyed` results) with every folder re-keyed so
    the same folder has the same key on all three sides (see the section
    comment): matched to base's folder → base's key; a local-new and a
    remote-new folder that are the same folder → a shared new key; any other
    folder → a key of its own side."""
    b_names = _by_name(*base)

    # Against base: a shared child, or an empty folder on either side (one
    # emptied or first filled is still that folder). Two non-empty folders
    # with no child in common are different folders: the old one deleted,
    # a new one made under the reused name.
    def same_as_base(_sk, sset, _bk, bset) -> bool:
        return bool(sset & bset) or not sset or not bset

    maps: list[dict] = [{}, {}]
    left: list[dict[str, list]] = []
    for i, side in enumerate((local, remote)):
        names = _by_name(*side)
        for name, folders in names.items():
            for sk, bk in _child_match_order(folders, b_names.get(name, []), same_as_base):
                maps[i][sk] = bk
        left.append({n: [f for f in fs if f[0] not in maps[i]] for n, fs in names.items()})

    # Local-new against remote-new: a shared child, or both empty — or one
    # empty only because every child of the other is held by another folder
    # on its own side (what a merge that kept those children elsewhere
    # leaves behind; the next merge must find the same folder).
    l_nested, r_nested = _nested(*local), _nested(*remote)

    def fuse(_lk, lset, _rk, rset) -> bool:
        if lset & rset or (not lset and not rset):
            return True
        if not lset:
            return rset <= l_nested
        if not rset:
            return lset <= r_nested
        return False

    for name, l_folders in left[0].items():
        pairs = _child_match_order(l_folders, left[1].get(name, []), fuse)
        for n, (lk, rk) in enumerate(pairs):
            maps[0][lk] = maps[1][rk] = (True, name, ("new", n))
    out = []
    for i, (order, rows) in enumerate((local, remote)):
        tag = ("local", "remote")[i]
        mapping = maps[i]
        new_order, new_rows = [], {}
        for key in order:
            if key[0] and key not in mapping:
                mapping[key] = (True, key[1], (tag, key[2]))
            nk = mapping.get(key, key)
            new_order.append(nk)
            new_rows[nk] = rows[key]
        out.append((new_order, new_rows))
    return out[0], out[1]


class _Side:
    """One side of the merge, as the server reads it: rows by key, the
    folder that holds each child (first lister in document order), and the
    rows a name means (occurrence 0)."""

    def __init__(self, order: list, rows: dict):
        self.order, self.rows = order, rows
        # name → the first folder of that name in document order (what an
        # edge endpoint naming a folder means).
        self.first_folder: dict[str, tuple] = {}
        for key in order:
            if key[0]:
                self.first_folder.setdefault(key[1], key)
        self.home: dict[str, tuple] = {}
        for key in order:
            row = rows[key]
            if not key[0] or not isinstance(row.get(_ORDER_KEY), list):
                continue
            for child in row[_ORDER_KEY]:
                if isinstance(child, str) and child and child not in self.home:
                    self.home[child] = key
        self.leaves = {k[1] for k in order if not k[0]}

    def home_of(self, key: tuple) -> tuple | None:
        """The folder key holding a non-folder row, or None (a folder, an
        occurrence past 0, or a node no folder lists)."""
        if key[0] or key[2] != 0:
            return None
        return self.home.get(key[1])

    def position(self, key: tuple, *, absolute: bool) -> tuple:
        """(x, y) of a row, raw or through its folder (None per axis that is
        not a number)."""
        return tuple(self.absolute(key, axis) if absolute else _number(
            (self.rows.get(key) or {}).get(axis)) for axis in ("x", "y"))

    def absolute(self, key: tuple, axis: str) -> float | None:
        row = self.rows.get(key)
        value = row.get(axis) if row else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        home = self.home_of(key)
        if home is None:
            return value
        return value + _num(self.rows[home].get(axis), 0)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


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


def _folder_renames(B: _Side, L: _Side, R: _Side) -> dict:
    """`{base folder key: remote folder key}` for the base folders the
    remote renamed (see "renames" above) — local kept or deleted."""
    local_at = {k: i for i, k in enumerate(L.order)}
    base_at = {k: i for i, k in enumerate(B.order)}
    fresh = [k for k in R.order if k[0] and k not in B.rows and k not in L.rows]
    scored = []
    for f in B.order:
        if not f[0] or f in R.rows:
            continue
        kept = f in L.rows
        was = _name_set(B.rows[f])
        if not was:
            continue
        for gi, g in enumerate(fresh):
            have = _name_set(R.rows[g])
            common = was & have
            subset = bool(have) and was <= have
            if not subset and (not kept or len(was) < 3 or 2 * len(common) < len(was)):
                continue
            at = local_at[f] if kept else len(L.order) + base_at[f]
            scored.append(((not subset, -len(common) / len(was), at, gi), f, g))
    scored.sort(key=lambda t: t[0])
    out: dict = {}
    taken: set = set()
    for _score, f, g in scored:
        if f in out or g in taken:
            continue
        out[f] = g
        taken.add(g)
    return out


def _same_frame(B: _Side, S: _Side, key: tuple, renames: dict) -> bool:
    """Whether side `S` keeps node `key` in base's folder (or its rename)."""
    fb, fs = B.home_of(key), S.home_of(key)
    return fs == fb or (fb in renames and renames[fb] == fs)


def _moved(B: _Side, S: _Side, key: tuple, renames: dict) -> bool:
    """Whether side `S` moved node `key` since base (see "coordinates")."""
    same_frame = _same_frame(B, S, key, renames)
    return S.position(key, absolute=not same_frame) != B.position(key, absolute=not same_frame)


def _reframe_children(merged: dict, B: _Side, L: _Side, R: _Side,
                      renames: dict) -> None:
    """Set, in place, the x/y of every merged non-folder row: from the side
    that moved it, in the merged folder's frame (see "coordinates" above)."""
    M = _Side(list(merged), merged)
    for key, row in merged.items():
        if key[0]:
            continue
        target = M.home_of(key)
        tpos = merged.get(target) if target else None
        l_home = L.home_of(key)
        l_home = renames.get(l_home, l_home)
        r_home = R.home_of(key)

        def put(side: _Side, home: tuple | None, axis: str) -> None:
            value = _number(side.rows[key].get(axis))
            if value is None:
                return
            if home == target:
                row[axis] = value
                return
            raw_home = side.home_of(key)
            frow = side.rows.get(raw_home) if raw_home else None
            absolute = value + (_num(frow.get(axis), 0) if frow else 0)
            row[axis] = absolute - (_num(tpos.get(axis), 0) if tpos else 0)

        if key in L.rows and key in R.rows and key in B.rows:
            # A side that re-nested the node where the merge did NOT put it
            # lost that re-nesting, and the move that came with it: the
            # coordinates follow the side whose nesting won, so the node is
            # never drawn outside the folder that lists it.
            l_same = _same_frame(B, L, key, renames)
            r_same = _same_frame(B, R, key, renames)
            l_moved, r_moved = _moved(B, L, key, renames), _moved(B, R, key, renames)
            both = l_moved and r_moved
            winner = None
            if target == l_home and r_home != target and (not r_same or both):
                winner = (L, l_home)
            elif target == r_home and l_home != target and (not l_same or both):
                winner = (R, r_home)
            if winner is not None:
                for axis in ("x", "y"):
                    put(*winner, axis)
                continue
            if l_moved != r_moved:
                side, home = (L, l_home) if l_moved else (R, r_home)
                for axis in ("x", "y"):
                    put(side, home, axis)
                continue
        # Both moved it, or neither, or a side lacks it: the field-wise x/y
        # stand, each axis attributed to the side it came from.
        for axis in ("x", "y"):
            value = _number(row.get(axis))
            if value is None:
                continue
            sides: list[tuple[_Side, tuple | None]] = []
            if key in L.rows and L.rows[key].get(axis) == value:
                sides.append((L, l_home))
            if key in R.rows and R.rows[key].get(axis) == value:
                sides.append((R, r_home))
            if not sides or any(home == target for _side, home in sides):
                continue
            put(*sides[0], axis)


def merge_toml(base: dict | None, local_doc: dict, remote_doc: dict) -> tuple[dict, dict]:
    """Three-way merge of two canvas.toml documents at node granularity.

    `base` is `rows_of_doc` of the toml at the last sync point (None when the
    sync point predates this code: then only nodes the remote has and local
    lacks come in, and local wins everything else — the old per-file rule's
    outcome for the file, but still per node). Returns `(merged_doc, info)`
    where merged_doc is `local_doc` with its [canvas] nodes/edges/scalars
    replaced, and info = {added, removed, taken, kept} by node name, plus
    `renamed` ([[base name, remote name]] for folders paired as renames)
    and `folder_fixes` (`normalise_folders`' lines).

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
                             "renamed": [], "folder_fixes": []}
    b_keyed = _keyed(base) if base else ([], {})
    l_keyed, r_keyed = _align_folders(b_keyed, _keyed(local_doc), _keyed(remote_doc))
    B, L, R = _Side(*b_keyed), _Side(*l_keyed), _Side(*r_keyed)
    bn, ln, rn = B.rows, L.rows, R.rows
    Lr, Rr = rows_of_doc(local_doc), rows_of_doc(remote_doc)
    renames = _folder_renames(B, L, R)
    # Local deleted the folder the remote renamed: the delete wins.
    dropped = {g for f, g in renames.items() if f not in ln}
    merged: dict = {}
    for k in L.order:
        if k in renames:
            to = renames[k]
            merged[to] = _merge_row(_renamed_folder(bn[k], k[1], to[1]),
                                    _renamed_folder(ln[k], k[1], to[1]), rn[to])
        elif k in rn:
            merged[k] = _merge_row(bn.get(k), ln[k], rn[k])
            if k[0] and k not in bn and base is not None:
                # One folder both sides made: its children are the union.
                lo, ro = _names(ln[k].get(_ORDER_KEY)), _names(rn[k].get(_ORDER_KEY))
                if lo is not None and ro is not None:
                    merged[k][_ORDER_KEY] = _merge_order([], lo, ro)
        elif k in bn and bn[k] == ln[k]:
            info["removed"].append(k[1])      # remote deleted an untouched node
        else:
            merged[k] = dict(ln[k])           # local added or changed it
            if k[0] and k in bn:
                # The remote deleted this folder, local changed it: it stays,
                # but the children the remote released by deleting it are
                # released (local's own additions stay in).
                bo, lo = _names(bn[k].get(_ORDER_KEY)), _names(ln[k].get(_ORDER_KEY))
                if bo is not None and lo is not None:
                    merged[k][_ORDER_KEY] = _merge_order(bo, lo, [])
    for k in R.order:
        if k in merged or k in ln or k in dropped:
            continue
        if k in bn:
            continue                          # local deleted it: stays deleted
        merged[k] = dict(rn[k])               # new on the remote

    # `normalise_folders` tells same-named folders apart by their place in
    # the MERGED document: local's nesting, in those terms.
    merged_at: dict = {}
    counts: dict[str, int] = {}
    for k in merged:
        if k[0]:
            merged_at[k] = (k[1], counts.get(k[1], 0))
            counts[k[1]] = counts.get(k[1], 0) + 1
    local_nesting = {}
    for child, home in L.home.items():
        home = renames.get(home, home)
        if home in merged_at:
            local_nesting[child] = merged_at[home]
    info["folder_fixes"] = normalise_folders(list(merged.values()), prefer=local_nesting)
    _reframe_children(merged, B, L, R, renames)

    paired = {g for f, g in renames.items() if f in ln}
    info["renamed"] = [[f[1], g[1]] for f, g in renames.items() if f in ln]
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

    # An endpoint names the first row of its name; one naming a renamed
    # folder follows it.
    by_name = {f[1]: g[1] for f, g in renames.items()
               if B.first_folder.get(f[1]) == f and R.first_folder.get(g[1]) == g}

    def follow(rows, side: _Side):
        def end(n):
            return n if n in side.leaves else by_name.get(n, n)
        return [[end(a), end(b)] for a, b in rows]
    b_edges = follow((base or {}).get("edges") or [], B)
    l_edges = follow(Lr["edges"], L)
    be, le, re_ = edge_set(b_edges), edge_set(l_edges), edge_set(Rr["edges"])
    edges = ((le | re_) - (be - le) - (be - re_))
    ordered = [e for e in l_edges if tuple(e) in edges]
    seen = {tuple(e) for e in ordered}
    for e in Rr["edges"]:
        if tuple(e) in edges and tuple(e) not in seen:
            ordered.append(e)
            seen.add(tuple(e))
    names = {k[1] for k in merged}
    ordered = [e for e in ordered if e[0] in names and e[1] in names]

    b_canvas = (base or {}).get("canvas") or {}
    scalars = dict(Lr["canvas"])
    for key, rv in Rr["canvas"].items():
        if key in _LOCAL_ONLY_SCALARS:
            continue
        lv, bv = Lr["canvas"].get(key, _ABSENT), b_canvas.get(key, _ABSENT)
        if lv == bv and rv != lv:
            scalars[key] = rv
    for key in list(scalars):
        if key not in Rr["canvas"] and key not in _LOCAL_ONLY_SCALARS \
                and scalars[key] == b_canvas.get(key, _ABSENT):
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
