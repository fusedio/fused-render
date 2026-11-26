"""Reader backing canvas/template.html — parses a Fused `canvas.toml` (v2) into
a JSON-native layout the viewer draws (SPEC §28).

One `tomllib` pass turns the canvas definition into
`{name, nodes, folders, edges, viewport, viewportBounds, siblings}`:

  * nodes   — UDF nodes (positioned rects), optional fields defaulted per the
              §28 table (title = udfName, visible = true), each with the
              `kind` its source file gives it (udf / widget / html / note),
              the row's display keys, and — for a note — the file's text.
  * folders — `type = "udf-folder"` group boxes (folderName, folderColor,
              childUdfOrder, isLocked).
  * edges   — [[srcUdfName, dstUdfName], …] pairs, malformed ones dropped.
  * viewport / viewportBounds — the stored camera, when present.
  * siblings — udfName → the sibling file extensions (`.py`/`.json`/`.md`/
              `.html`) present next to the toml, from one listing of its dir
              (one os.listdir).

Where the workbench puts things (CV-3): a row's `x`/`y` is the React Flow
node position, which for a node inside a folder is RELATIVE to that folder.
Every node and folder therefore also carries `ax`/`ay`, its absolute canvas
position, and `z`, the stacking level React Flow gives it (folders that hold
nodes are spread 10 apart, and a child sits at least one above its folder);
a node also carries `parent`, the index of its folder in `folders`, or None.

Malformed node/edge entries are skipped, never fatal — a hand-edited canvas
with one broken node still previews the rest. A whole-file parse failure is
allowed to propagate so the page's traceback overlay shows it (§28).

Called by `fused.runPython("./reader.py", {file})`.
"""


# ENGINE ISOLATION (SPEC PY / §28): under the fused engine the UDF body runs
# re-exec'd in isolation from this module's globals, so EVERY helper and
# constant lives INSIDE main() and every import is done inside main(). Nothing
# is referenced at module level except the entrypoint and its registration shim.
def main(file: str = "") -> dict:
    import math
    import os
    import tomllib

    SIBLING_EXTS = (".py", ".json", ".md", ".html")
    # What a node's source file makes it. A node has one source file; the
    # order only settles a folder that holds two files of one name.
    KIND_BY_EXT = ((".py", "udf"), (".json", "widget"), (".html", "html"),
                   (".md", "note"))
    # The workbench's own default sizes (NODE_DEFAULTS in its constants).
    DEFAULT_SIZE = {"udf": (700, 500), "widget": (700, 500),
                    "html": (700, 500), "note": (350, 250)}
    DEFAULT_FOLDER_SIZE = (1500, 115)
    # A note's text is drawn on its card. One note is cut at NOTE_BYTES and
    # the canvas at NOTES_BYTES, so a folder of huge files cannot stall a
    # preview.
    NOTE_BYTES = 200_000
    NOTES_BYTES = 2_000_000

    def is_num(v):
        # bool is an int subclass in Python — reject it as a coordinate. TOML
        # also writes `nan` and `inf`: neither is a place or a size, and
        # neither survives the result's JSON encoding.
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v))

    def num(v, default=0):
        return v if is_num(v) else default

    def text(v, default=""):
        return v if isinstance(v, str) else default

    def flag(v, default):
        return v if isinstance(v, bool) else default

    def node_common(entry, index):
        # Fields shared by UDF nodes and folder nodes. A row with no zIndex
        # stacks by its place in the file, as the workbench loads it
        # (`zIndex ?? index + 1`).
        name = text(entry.get("udfName"))
        return {
            "udfName": name,
            "x": num(entry.get("x")),
            "y": num(entry.get("y")),
            "zIndex": int(num(entry.get("zIndex"), index + 1)),
        }

    if not file:
        # Raise (don't return an error dict the viewer would render as an
        # empty canvas) — a reader failure surfaces via the page's traceback
        # overlay (§28), same as a whole-file parse failure.
        raise ValueError("no file (missing _file param)")

    # A whole-file parse failure propagates -> the page's traceback overlay.
    with open(file, "rb") as f:
        doc = tomllib.load(f)
    if not isinstance(doc, dict):
        raise ValueError("canvas.toml top level is not a table")

    canvas = doc.get("canvas")
    if not isinstance(canvas, dict):
        canvas = {}

    # One listing of the toml's dir: which source file each node has.
    parent_dir = os.path.dirname(os.path.abspath(file))
    try:
        present = set(os.listdir(parent_dir))
    except OSError:
        present = set()

    def kind_of(name, entry):
        for ext, kind in KIND_BY_EXT:
            if (name + ext) in present:
                return kind
        # No source file next to the toml (a toml read on its own): a row
        # carrying note styling is a note, anything else a UDF.
        return "note" if isinstance(entry.get("textBoxColor"), str) else "udf"

    # ---- nodes / folders --------------------------------------------------
    nodes = []
    folders = []
    raw_nodes = canvas.get("nodes")
    if isinstance(raw_nodes, list):
        for index, entry in enumerate(raw_nodes):
            if not isinstance(entry, dict):
                continue  # skip a malformed node entry, never crash
            base = node_common(entry, index)
            if not base["udfName"] and entry.get("type") != "udf-folder":
                # A UDF node with no name can't badge siblings or wire edges.
                continue
            if entry.get("type") == "udf-folder":
                child_order = entry.get("childUdfOrder")
                base.update({
                    "width": num(entry.get("width"), DEFAULT_FOLDER_SIZE[0]),
                    "height": num(entry.get("height"), DEFAULT_FOLDER_SIZE[1]),
                    "folderName": text(entry.get("folderName"),
                                       base["udfName"] or "folder"),
                    "folderColor": text(entry.get("folderColor")),
                    "childUdfOrder": [text(c) for c in child_order
                                      if isinstance(c, str)]
                    if isinstance(child_order, list) else [],
                    "isLocked": flag(entry.get("isLocked"), False),
                    "ax": base["x"],
                    "ay": base["y"],
                    "z": base["zIndex"],
                })
                folders.append(base)
            else:
                kind = kind_of(base["udfName"], entry)
                width, height = DEFAULT_SIZE[kind]
                scale = entry.get("textBoxScaleFactor")
                desc_scale = entry.get("nodeDescriptionScaleFactor")
                gradient = entry.get("textBoxGradient")
                base.update({
                    "width": num(entry.get("width"), width),
                    "height": num(entry.get("height"), height),
                    "kind": kind,
                    "title": text(entry.get("title"), base["udfName"]),
                    "description": text(entry.get("description")),
                    "visible": flag(entry.get("visible"), True),
                    "type": text(entry.get("type")),
                    "textBoxColor": text(entry.get("textBoxColor")),
                    "textBoxGradient": gradient
                    if isinstance(gradient, bool) else None,
                    "textBoxScaleFactor": scale if is_num(scale) else None,
                    "textBoxAlignment": text(entry.get("textBoxAlignment")),
                    "nodeDescriptionScaleFactor": desc_scale
                    if is_num(desc_scale) else None,
                    "widgetBackgroundColor":
                        text(entry.get("widgetBackgroundColor")),
                    "ax": base["x"],
                    "ay": base["y"],
                    "z": base["zIndex"],
                    "parent": None,
                })
                nodes.append(base)

    # ---- folder children ---------------------------------------------------
    # A folder lists its children by udfName (`childUdfOrder`); each name
    # takes the first node of that name no folder has taken yet — the
    # workbench's own rule (`hydrateFolderParentage`). A child's x/y is
    # relative to its folder.
    by_name = {}
    for node in nodes:
        by_name.setdefault(node["udfName"], []).append(node)
    for folder_index, folder in enumerate(folders):
        for child_name in folder["childUdfOrder"]:
            child = next((c for c in by_name.get(child_name, ())
                          if c["parent"] is None), None)
            if child is None:
                continue
            child["parent"] = folder_index
            child["ax"] = folder["ax"] + child["x"]
            child["ay"] = folder["ay"] + child["y"]

    # ---- stacking ----------------------------------------------------------
    # React Flow's own two rules (`updateChildNode`). Folders that hold
    # nodes are spread apart: each is lifted by 10 times its turn, counted in
    # the order the rows of their first children come in the file. React
    # Flow keeps ONE running count and sets it back to a folder's own turn
    # whenever a later child of that folder comes up, so children rows
    # ordered A, B, A, C give A 0, B 10 and C 10 (not 20). That is what the
    # workbench shows, so it is kept here on purpose. Then a child sits one
    # above its folder unless its own zIndex is already higher.
    turn = -1
    turns = {}
    for node in nodes:
        folder_index = node["parent"]
        if folder_index is None:
            continue
        if folder_index not in turns:
            turn += 1
            turns[folder_index] = turn
            folders[folder_index]["z"] += turn * 10
        else:
            turn = turns[folder_index]
    for node in nodes:
        if node["parent"] is None:
            continue
        folder = folders[node["parent"]]
        if folder["z"] >= node["z"]:
            node["z"] = folder["z"] + 1

    # ---- note text ---------------------------------------------------------
    # A note IS its text, and the text is the node's `.md` file next to the
    # toml, so the card can be drawn here without asking Fused for anything.
    budget = NOTES_BYTES
    for node in nodes:
        if node["kind"] != "note":
            continue
        node["note"] = ""
        if budget <= 0 or (node["udfName"] + ".md") not in present:
            continue
        try:
            with open(os.path.join(parent_dir, node["udfName"] + ".md"),
                      "rb") as f:
                raw = f.read(min(NOTE_BYTES, budget))
        except OSError:
            continue
        budget -= len(raw)
        node["note"] = raw.decode("utf-8", errors="replace")

    # ---- edges ------------------------------------------------------------
    edges = []
    raw_edges = canvas.get("edges")
    if isinstance(raw_edges, list):
        for pair in raw_edges:
            if (isinstance(pair, list) and len(pair) == 2
                    and isinstance(pair[0], str) and isinstance(pair[1], str)):
                edges.append([pair[0], pair[1]])

    # ---- viewport ---------------------------------------------------------
    # A viewport only counts when x and y are actually present — an empty
    # [canvas.viewport] table must fall through to fit-to-bounds, not pin the
    # camera at a fabricated origin.
    viewport = None
    raw_vp = canvas.get("viewport")
    if (isinstance(raw_vp, dict)
            and is_num(raw_vp.get("x"))
            and is_num(raw_vp.get("y"))):
        viewport = {
            "x": num(raw_vp.get("x")),
            "y": num(raw_vp.get("y")),
            "zoom": num(raw_vp.get("zoom"), 1),
        }
    # The bounds are the part of the canvas that was on screen, in canvas
    # units; they only count when all four are numbers and enclose an area.
    viewport_bounds = None
    raw_vb = canvas.get("viewportBounds")
    if isinstance(raw_vb, dict):
        keys = ("minX", "minY", "maxX", "maxY")
        if all(is_num(raw_vb.get(k)) for k in keys):
            if (raw_vb["maxX"] > raw_vb["minX"]
                    and raw_vb["maxY"] > raw_vb["minY"]):
                viewport_bounds = {k: raw_vb[k] for k in keys}

    # ---- siblings ---------------------------------------------------------
    # For each UDF node report which known sibling extensions exist as
    # `{udfName}<ext>` next to the toml.
    siblings = {}
    for node in nodes:
        name = node["udfName"]
        if not name:
            continue
        found = [ext for ext in SIBLING_EXTS if (name + ext) in present]
        if found:
            siblings[name] = found

    return {
        "name": text(doc.get("name")) or None,
        "version": doc.get("version"),
        "previewImageUrl": text(doc.get("previewImageUrl")) or None,
        "nodes": nodes,
        "folders": folders,
        "edges": edges,
        "viewport": viewport,
        "viewportBounds": viewport_bounds,
        "siblings": siblings,
    }


# The fused-render engine / app runner only invoke a @fused.udf-registered
# entrypoint; a bare main() returns null under them. Register main via the shim
# (the house pattern — las/usd/pdf readers) so it runs under the engine, while
# `main` stays a plain callable for the built-in executor and for tests. (A
# directly-decorated `main` would sandbox its __call__ against the local
# filesystem, which reading canvas.toml needs.)
try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass
