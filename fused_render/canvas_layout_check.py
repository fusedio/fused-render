"""What is wrong with a canvas's LAYOUT, read off its `canvas.toml` (SPEC §28
CV-9, D1512).

An agent that adds, deletes, moves or resizes nodes edits numbers in a file;
it cannot see that two boxes now sit on top of each other. fused-render
runs this after every tool call that changed the layout and tells the
session what it found (`canvases.api_canvases_agent_hook`), so the check is
not something the agent has to remember to ask for.

It reads the file the way the canvas view's reader does (templates/canvas/
reader.py): a folder's children are the nodes its `childUdfOrder` names, and
a child's `x`/`y` is relative to its folder. What it reports:

  * two boxes that overlap: top-level nodes and folders against each other,
    and the children of one folder against each other;
  * a folder child that is not inside its folder's box;
  * a `childUdfOrder` name that is no node, a node two folders list, a
    folder listed inside a folder;
  * an edge naming something that is not on the canvas.

Pure functions over the parsed file; standard library only.

    state(toml_path)  -> (fingerprint, doc | None, error | None)
    problems(doc, present) -> [sentence, ...]
    note(doc, present, before) -> the text handed to the session
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tomllib

#: Same guard as the canvas view's own gate (templates/canvas/condition.py).
MAX_TOML_BYTES = 2 * 1024 * 1024
#: The workbench's default sizes (reader.py's DEFAULT_SIZE).
NODE_SIZE = (700, 500)
NOTE_SIZE = (350, 250)
FOLDER_SIZE = (1500, 115)
#: Boxes that only touch, or overlap by less than this in either direction,
#: are not a problem: the workbench snaps to whole units and so do people.
TOLERANCE = 1.0
#: How many problems a note spells out before it counts the rest.
MAX_LINES = 12


def _num(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value if math.isfinite(value) else default


def _rows(doc) -> list:
    canvas = doc.get("canvas") if isinstance(doc, dict) else None
    rows = canvas.get("nodes") if isinstance(canvas, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _edges(doc) -> list:
    canvas = doc.get("canvas") if isinstance(doc, dict) else None
    edges = canvas.get("edges") if isinstance(canvas, dict) else None
    return [e for e in edges if isinstance(e, list) and len(e) == 2
            and all(isinstance(x, str) for x in e)] if isinstance(edges, list) else []


def fingerprint(doc) -> str:
    """What the layout IS: every row's name, kind, place, size and children,
    and the edges. A title, a description or a colour changing does not move
    it, so those edits raise no layout note."""
    rows = [[r.get("udfName"), r.get("type"), r.get("x"), r.get("y"), r.get("width"),
             r.get("height"), r.get("childUdfOrder"), r.get("folderName")] for r in _rows(doc)]
    blob = json.dumps([rows, _edges(doc)], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def state(toml_path: str) -> tuple:
    """`(fingerprint, doc, error)` for the file as it is now. A file that
    does not parse has a fingerprint of its own (of its bytes) and an
    `error` sentence instead of a `doc`; a file that cannot be read at all is
    `("", None, None)` — nothing to say."""
    try:
        if os.path.getsize(toml_path) > MAX_TOML_BYTES:
            return "", None, None
        with open(toml_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return "", None, None
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return "bad:" + hashlib.sha1(raw).hexdigest(), None, str(exc)
    if not isinstance(doc, dict):
        return "", None, None
    return fingerprint(doc), doc, None


def _boxes(doc, present=()):
    """`(folders, nodes)`: each a list of `{name, x, y, w, h, ...}` in file
    order, a node carrying `parent` (its folder's index, or None) and its
    absolute `x`/`y`. `present` is the folder's file names: a node whose
    source is a `.md` file, or whose row is styled as one, is a note."""
    present = set(present or ())
    folders, nodes = [], []
    for row in _rows(doc):
        name = row.get("udfName") if isinstance(row.get("udfName"), str) else ""
        if row.get("type") == "udf-folder":
            order = row.get("childUdfOrder")
            folders.append({
                "name": name or (row.get("folderName") if isinstance(row.get("folderName"), str) else "") or "folder",
                "x": _num(row.get("x")), "y": _num(row.get("y")),
                "w": _num(row.get("width"), FOLDER_SIZE[0]),
                "h": _num(row.get("height"), FOLDER_SIZE[1]),
                "children": [c for c in order if isinstance(c, str)] if isinstance(order, list) else [],
            })
        elif name:
            note = (name + ".md") in present and (name + ".py") not in present \
                and (name + ".json") not in present and (name + ".html") not in present
            note = note or (not any((name + ext) in present for ext in (".py", ".json", ".html", ".md"))
                            and isinstance(row.get("textBoxColor"), str))
            size = NOTE_SIZE if note else NODE_SIZE
            nodes.append({"name": name, "x": _num(row.get("x")), "y": _num(row.get("y")),
                          "w": _num(row.get("width"), size[0]), "h": _num(row.get("height"), size[1]),
                          "parent": None})
    by_name = {}
    for node in nodes:
        by_name.setdefault(node["name"], []).append(node)
    for index, folder in enumerate(folders):
        for child_name in folder["children"]:
            child = next((c for c in by_name.get(child_name, ()) if c["parent"] is None), None)
            if child is None:
                continue
            child["parent"] = index
            child["x"] += folder["x"]
            child["y"] += folder["y"]
    return folders, nodes


def _overlap(a, b):
    """`(width, height)` of the area two boxes share, or None when they only
    touch or miss each other."""
    w = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    if w <= TOLERANCE or h <= TOLERANCE:
        return None
    return w, h


def _px(value) -> str:
    return "%d" % round(value)


def problems(doc, present=()) -> list:
    """Every layout problem as one sentence, the worst kind first."""
    folders, nodes = _boxes(doc, present)
    out = []
    node_names = {n["name"] for n in nodes}
    folder_names = {f["name"] for f in folders}

    # Names that point at nothing: what a deleted or renamed node leaves behind.
    listed = {}
    for folder in folders:
        for child in folder["children"]:
            if child in folder_names and child not in node_names:
                out.append("folder `%s` lists the folder `%s` as a child: a folder cannot be "
                           "inside a folder" % (folder["name"], child))
            elif child not in node_names:
                out.append("folder `%s` lists `%s` in childUdfOrder, but no node has that name"
                           % (folder["name"], child))
            else:
                listed.setdefault(child, []).append(folder["name"])
    for child, owners in listed.items():
        free = sum(1 for n in nodes if n["name"] == child)
        if len(owners) > free:
            out.append("`%s` is listed by %d folders (%s): a node belongs to one folder"
                       % (child, len(owners), ", ".join("`%s`" % o for o in owners)))
    known = node_names | folder_names
    for source, target in _edges(doc):
        missing = [n for n in (source, target) if n not in known]
        if missing:
            out.append("edge `%s` -> `%s` names %s, which is not on the canvas"
                       % (source, target, " and ".join("`%s`" % m for m in missing)))

    # A child outside its folder's box.
    for node in nodes:
        if node["parent"] is None:
            continue
        folder = folders[node["parent"]]
        sides = []
        for label, over in (("left", folder["x"] - node["x"]),
                            ("top", folder["y"] - node["y"]),
                            ("right", node["x"] + node["w"] - (folder["x"] + folder["w"])),
                            ("bottom", node["y"] + node["h"] - (folder["y"] + folder["h"]))):
            if over > TOLERANCE:
                sides.append("%s px past its %s edge" % (_px(over), label))
        if sides:
            out.append("`%s` is not inside its folder `%s`: %s (a child's x/y is relative "
                       "to the folder; the folder may need to be larger)"
                       % (node["name"], folder["name"], ", ".join(sides)))

    # Boxes on top of each other: the top level, then inside each folder.
    top = [dict(f, what="folder") for f in folders] \
        + [dict(n, what="node") for n in nodes if n["parent"] is None]
    groups = [top] + [[dict(n, what="node") for n in nodes if n["parent"] == i]
                      for i in range(len(folders))]
    for index, group in enumerate(groups):
        where = "" if index == 0 else " inside folder `%s`" % folders[index - 1]["name"]
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                shared = _overlap(a, b)
                if shared:
                    out.append("%s `%s` and %s `%s` overlap%s by %s x %s px"
                               % (a["what"], a["name"], b["what"], b["name"], where,
                                  _px(shared[0]), _px(shared[1])))
    return out


def counts(doc) -> tuple:
    rows = _rows(doc)
    folders = sum(1 for r in rows if r.get("type") == "udf-folder")
    return len(rows) - folders, folders, len(_edges(doc))


LOOK = 'canvas_screenshot with what: "layout"'


def note(doc, present=(), before=()) -> str:
    """What the session is told after a layout change. `before` is the
    problems the canvas already had when the turn began: those are not the
    session's to fix unasked, so they are counted, never listed."""
    found = problems(doc, present)
    before = set(before or ())
    fresh = [line for line in found if line not in before]
    older = len(found) - len(fresh)
    nodes, folders, edges = counts(doc)
    size = "%d node%s, %d folder%s, %d edge%s" % (
        nodes, "" if nodes == 1 else "s", folders, "" if folders == 1 else "s",
        edges, "" if edges == 1 else "s")
    head = "Layout check (fused-render ran it because canvas.toml's layout changed; %s)" % size
    kept = ("" if not older else
            " %d problem%s the canvas already had before this turn %s unchanged and not "
            "listed: leave %s unless the user asked."
            % (older, "" if older == 1 else "s", "is" if older == 1 else "are",
               "it" if older == 1 else "them"))
    if not fresh:
        said = ("nothing overlaps, every folder child is inside its folder, and every edge "
                "and folder child names something on the canvas." if not older else
                "your changes this turn added no problem.")
        return (head + ": " + said + kept + " When you are done changing the layout, look "
                "at it once before you report: " + LOOK + ".")
    lines = ["- " + line for line in fresh[:MAX_LINES]]
    if len(fresh) > MAX_LINES:
        lines.append("- (+%d more)" % (len(fresh) - MAX_LINES))
    return (head + " found %d problem%s%s:\n" % (
                len(fresh), "" if len(fresh) == 1 else "s",
                " that came in this turn" if older else "")
            + "\n".join(lines)
            + "\nFix them (Read canvas.toml again first), then look at the result: " + LOOK + "."
            + kept)


def error_note(error: str) -> str:
    return ("Layout check (fused-render ran it because canvas.toml changed): the file no "
            "longer parses, so the canvas cannot be drawn or published: %s. Fix it before "
            "anything else." % error)
