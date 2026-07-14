"""Reader backing canvas/template.html — parses a Fused `canvas.toml` (v2) into
a JSON-native layout the viewer draws (SPEC §26).

One `tomllib` pass turns the canvas definition into
`{name, nodes, folders, edges, viewport, viewportBounds, siblings}`:

  * nodes   — UDF nodes (positioned rects), optional fields defaulted per the
              §26 table (title = udfName, visible = true).
  * folders — `type = "udf-folder"` group boxes (folderName, folderColor,
              childUdfOrder, isLocked).
  * edges   — [[srcUdfName, dstUdfName], …] pairs, malformed ones dropped.
  * viewport / viewportBounds — the stored camera, when present.
  * siblings — udfName → the sibling file extensions (`.py`/`.json`/`.md`/
              `.html`) present next to the toml, from one os.listdir of its dir.

Malformed node/edge entries are skipped, never fatal — a hand-edited canvas
with one broken node still previews the rest. A whole-file parse failure is
allowed to propagate so the page's traceback overlay shows it (§26).

Called by `fused.runPython("./reader.py", {file})`.
"""


# ENGINE ISOLATION (SPEC PY / §26): under the fused engine the UDF body runs
# re-exec'd in isolation from this module's globals, so EVERY helper and
# constant lives INSIDE main() and every import is done inside main(). Nothing
# is referenced at module level except the entrypoint and its registration shim.
def main(file: str = "") -> dict:
    import os
    import tomllib

    SIBLING_EXTS = (".py", ".json", ".md", ".html")

    def is_num(v):
        # bool is an int subclass in Python — reject it as a coordinate.
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def num(v, default=0):
        return v if is_num(v) else default

    def text(v, default=""):
        return v if isinstance(v, str) else default

    def flag(v, default):
        return v if isinstance(v, bool) else default

    def node_common(entry):
        # Fields shared by UDF nodes and folder nodes; position/size defaulted
        # so a node missing them still lands somewhere paintable (§26).
        name = text(entry.get("udfName"))
        return {
            "udfName": name,
            "x": num(entry.get("x")),
            "y": num(entry.get("y")),
            "zIndex": int(num(entry.get("zIndex"))),
            "width": num(entry.get("width"), 240),
            "height": num(entry.get("height"), 140),
        }

    if not file:
        # Raise (don't return an error dict the viewer would render as an
        # empty canvas) — a reader failure surfaces via the page's traceback
        # overlay (§26), same as a whole-file parse failure.
        raise ValueError("no file (missing _file param)")

    # A whole-file parse failure propagates -> the page's traceback overlay.
    with open(file, "rb") as f:
        doc = tomllib.load(f)
    if not isinstance(doc, dict):
        raise ValueError("canvas.toml top level is not a table")

    canvas = doc.get("canvas")
    if not isinstance(canvas, dict):
        canvas = {}

    # ---- nodes / folders --------------------------------------------------
    nodes = []
    folders = []
    raw_nodes = canvas.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, dict):
                continue  # skip a malformed node entry, never crash
            base = node_common(entry)
            if not base["udfName"] and entry.get("type") != "udf-folder":
                # A UDF node with no name can't badge siblings or wire edges.
                continue
            if entry.get("type") == "udf-folder":
                child_order = entry.get("childUdfOrder")
                base.update({
                    "folderName": text(entry.get("folderName"),
                                       base["udfName"] or "folder"),
                    "folderColor": text(entry.get("folderColor")),
                    "childUdfOrder": [text(c) for c in child_order
                                      if isinstance(c, str)]
                    if isinstance(child_order, list) else [],
                    "isLocked": flag(entry.get("isLocked"), False),
                })
                folders.append(base)
            else:
                base.update({
                    "title": text(entry.get("title"), base["udfName"]),
                    "description": text(entry.get("description")),
                    "visible": flag(entry.get("visible"), True),
                    "type": text(entry.get("type")),
                    "textBoxColor": text(entry.get("textBoxColor")),
                })
                nodes.append(base)

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
    raw_vb = canvas.get("viewportBounds")
    viewport_bounds = raw_vb if isinstance(raw_vb, dict) else None

    # ---- siblings ---------------------------------------------------------
    # One listdir of the toml's dir; for each UDF node report which of the
    # known sibling extensions exist as `{udfName}<ext>` next to the toml.
    present = set()
    try:
        for name in os.listdir(os.path.dirname(os.path.abspath(file))):
            present.add(name)
    except OSError:
        pass
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

