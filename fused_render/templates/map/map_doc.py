"""The map document: the Map Viewer's whole state as one JSON file, and the
command vocabulary that edits it.

A map is a `.fmap` file (JSON) holding the camera, the basemap and an ordered
list of layers, each a data source plus how to draw it. Everything that changes
a map — a click in the page, a call from `window.fusedMap`, an MCP tool an agent
runs while the page is closed — is one of the COMMANDS below applied to that
document. The page reconciles the live map against the document, so the file
is the single source of truth and an edit from any of those three places lands
the same way.

The page carries a line-for-line JavaScript twin of `apply` (the
`map-doc-core` script block in template.html). `tests/test_map_doc_parity.py`
runs both over the same cases, so the two cannot drift.

stdlib only, and nothing here imports fused_render (SPEC PY-15): it runs in the
template venv under runPython and under `fused app serve` for mcp.toml.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from typing import Any

DOC_TYPE = "fused-map"
DOC_VERSION = 1
DOC_SUFFIX = ".fmap"

BASEMAPS = ("light", "dark", "sat", "none")
KINDS = ("auto", "raster", "vector", "zarr", "pmtiles")

# Every style key a layer may carry, by the kind that reads it, with the JSON
# type it must have. A command naming any other key is refused rather than
# stored, so a typo from an agent fails on the call instead of silently doing
# nothing on the map. `null` always means "back to the default" and removes
# the key. Mirrored as STYLE_KEYS in template.html (the parity test compares).
STYLE_KEYS: dict[str, dict[str, str]] = {
    "raster": {
        "mode": "enum:rgb|single|palette|index",
        "bands": "int[]",
        "colormap": "str",
        "reversed": "bool",
        "rescale": "range",
        "nodata": "nodata",
        "gamma": "num",
        "stretch": "enum:linear|sqrt|log",
    },
    "vector": {
        "fill_color": "color",
        "fill_opacity": "unit",
        "line_color": "color",
        "line_width": "num",
        "circle_color": "color",
        "circle_radius": "num",
        "circle_opacity": "unit",
        "point_mode": "enum:circle|heatmap|cluster",
        "color_by": "str",
        "color_mode": "enum:graduated|categorized",
        "colormap": "str",
        "label_field": "str",
    },
    "zarr": {
        "colormap": "str",
        "reversed": "bool",
        "clim": "range",
    },
    "pmtiles": {
        "fill_color": "color",
        "fill_opacity": "unit",
        "line_color": "color",
        "line_width": "num",
        "circle_color": "color",
        "circle_radius": "num",
    },
}
# Options are about WHAT is read rather than how it is drawn.
OPTION_KEYS: dict[str, str] = {
    "variable": "str",        # zarr / netcdf variable
    "selector": "dict",       # zarr: {dim: index}
    "source_layer": "str",    # a layer inside a GeoPackage / multi-layer file
}

_HEX = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


class MapDocError(ValueError):
    """A command or document the map refuses; the message says why."""


def new_doc(title: str = "") -> dict[str, Any]:
    return {
        "type": DOC_TYPE,
        "version": DOC_VERSION,
        "title": title,
        "basemap": "light",
        "view": {"center": [0.0, 20.0], "zoom": 1.5, "bearing": 0.0, "pitch": 0.0},
        "layers": [],
    }


def is_doc(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == DOC_TYPE


# ---- value checks -----------------------------------------------------------

def _is_num(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _check(where: str, rule: str, value: Any) -> Any:
    """Validate one value against its rule; return it (normalized)."""
    ok = False
    if rule == "str":
        ok = isinstance(value, str) and value != ""
    elif rule == "bool":
        ok = isinstance(value, bool)
    elif rule == "num":
        ok = _is_num(value)
    elif rule == "unit":
        ok = _is_num(value) and 0 <= value <= 1
    elif rule == "color":
        ok = isinstance(value, str) and bool(_HEX.match(value))
    elif rule == "int[]":
        ok = (isinstance(value, list) and 1 <= len(value) <= 4
              and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
                      for v in value))
    elif rule == "range":
        # [lo, hi] for one channel, or [[lo, hi], ...] per channel.
        def pair(v):
            return (isinstance(v, list) and len(v) == 2
                    and all(_is_num(x) for x in v) and v[0] < v[1])
        ok = pair(value) or (isinstance(value, list) and 1 <= len(value) <= 4
                             and all(pair(v) for v in value))
    elif rule == "nodata":
        ok = value in ("auto", "off") or _is_num(value)
    elif rule == "dict":
        ok = isinstance(value, dict)
    elif rule.startswith("enum:"):
        ok = value in rule[5:].split("|")
    if not ok:
        expected = rule[5:].replace("|", " or ") if rule.startswith("enum:") else rule
        raise MapDocError(f"{where}: {value!r} is not a valid {expected}")
    return value


def _merge(where: str, current: dict, patch: Any, rules: dict[str, str]) -> dict:
    if not isinstance(patch, dict):
        raise MapDocError(f"{where} must be an object")
    merged = dict(current)
    for key, value in patch.items():
        if key not in rules:
            known = ", ".join(sorted(rules))
            raise MapDocError(f"{where}: unknown key {key!r} (known: {known})")
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = _check(f"{where}.{key}", rules[key], value)
    return merged


def _style_rules(kind: str) -> dict[str, str]:
    if kind == "auto":
        # Kind not settled yet: accept any key some kind knows. The page
        # settles the kind on first load and drops what that kind ignores.
        rules: dict[str, str] = {}
        for table in STYLE_KEYS.values():
            rules.update(table)
        return rules
    return STYLE_KEYS[kind]


def _basename(text: str) -> str:
    """The last path segment, splitting on both separators (a Windows path
    must name the same layer on every OS, and the page splits the same way).
    A URL's query and fragment are dropped first: a signed URL's token must
    never become part of a layer's name or id."""
    text = str(text)
    if "://" in text:
        text = re.split(r"[?#]", text, maxsplit=1)[0]
    return re.split(r"[\\/]", text.rstrip("/\\"))[-1] or text


def _slug(text: str) -> str:
    base = _basename(text)
    base = re.sub(r"\.[A-Za-z0-9]+$", "", base)
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:40].strip("-")
    return slug or "layer"


def _unique_id(doc: dict, wanted: str) -> str:
    taken = {layer["id"] for layer in doc["layers"]}
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted}-{n}" in taken:
        n += 1
    return f"{wanted}-{n}"


def _find(doc: dict, layer_id: Any) -> int:
    for index, layer in enumerate(doc["layers"]):
        if layer["id"] == layer_id:
            return index
    ids = ", ".join(layer["id"] for layer in doc["layers"]) or "none"
    raise MapDocError(f"no layer {layer_id!r} (layers: {ids})")


def _unit(where: str, value: Any) -> float:
    return _check(where, "unit", value)


def _view_patch(view: dict, command: dict) -> dict:
    out = dict(view)
    if "bounds" in command:
        b = command["bounds"]
        if not (isinstance(b, list) and len(b) == 4 and all(_is_num(v) for v in b)
                and b[0] < b[2] and b[1] < b[3]):
            raise MapDocError("bounds must be [west, south, east, north]")
        out["bounds"] = [float(v) for v in b]
    if "center" in command:
        c = command["center"]
        if not (isinstance(c, list) and len(c) == 2 and all(_is_num(v) for v in c)
                and -180 <= c[0] <= 180 and -90 <= c[1] <= 90):
            raise MapDocError("center must be [longitude, latitude]")
        out["center"] = [float(c[0]), float(c[1])]
        out.pop("bounds", None)
    for key, lo, hi in (("zoom", 0, 24), ("bearing", -360, 360), ("pitch", 0, 85)):
        if key in command:
            value = command[key]
            if not (_is_num(value) and lo <= value <= hi):
                raise MapDocError(f"{key} must be a number in [{lo}, {hi}]")
            out[key] = float(value)
            if key == "zoom":
                out.pop("bounds", None)
    return out


# ---- commands -----------------------------------------------------------------
# Each takes (doc, command) and mutates `doc` in place (apply() hands it a
# copy), returning the command's result payload.

def _add_layer(doc: dict, cmd: dict) -> dict:
    source = cmd.get("source")
    if not isinstance(source, str) or not source.strip():
        raise MapDocError("add_layer needs a source (a file path or URL)")
    source = source.strip()
    kind = cmd.get("kind", "auto")
    if kind not in KINDS:
        raise MapDocError(f"kind must be one of {', '.join(KINDS)}")
    name = cmd.get("name") or _basename(source)
    if not isinstance(name, str):
        raise MapDocError("name must be a string")
    wanted = cmd.get("id")
    if wanted is not None:
        if not (isinstance(wanted, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", wanted)):
            raise MapDocError("id must be lowercase letters, digits and dashes")
        if any(layer["id"] == wanted for layer in doc["layers"]):
            raise MapDocError(f"a layer {wanted!r} already exists")
        layer_id = wanted
    else:
        layer_id = _unique_id(doc, _slug(name))
    layer = {
        "id": layer_id,
        "name": name,
        "source": source,
        "kind": kind,
        "visible": True,
        "opacity": 1.0,
        "style": _merge("style", {}, cmd.get("style", {}), _style_rules(kind)),
        "options": _merge("options", {}, cmd.get("options", {}), OPTION_KEYS),
    }
    if "visible" in cmd:
        layer["visible"] = _check("visible", "bool", cmd["visible"])
    if "opacity" in cmd:
        layer["opacity"] = float(_unit("opacity", cmd["opacity"]))
    doc["layers"].append(layer)
    return {"id": layer_id}


def _remove_layer(doc: dict, cmd: dict) -> dict:
    index = _find(doc, cmd.get("id"))
    doc["layers"].pop(index)
    return {"id": cmd["id"]}


def _update_layer(doc: dict, cmd: dict) -> dict:
    layer = doc["layers"][_find(doc, cmd.get("id"))]
    if "kind" in cmd:
        if cmd["kind"] not in KINDS:
            raise MapDocError(f"kind must be one of {', '.join(KINDS)}")
        layer["kind"] = cmd["kind"]
        # Style keys the new kind does not read would be refused on the next
        # edit; drop them now instead of leaving the layer un-editable.
        rules = _style_rules(layer["kind"])
        layer["style"] = {k: v for k, v in layer["style"].items() if k in rules}
    if "name" in cmd:
        layer["name"] = _check("name", "str", cmd["name"])
    if "visible" in cmd:
        layer["visible"] = _check("visible", "bool", cmd["visible"])
    if "opacity" in cmd:
        layer["opacity"] = float(_unit("opacity", cmd["opacity"]))
    if "style" in cmd:
        layer["style"] = _merge("style", layer["style"], cmd["style"],
                                _style_rules(layer["kind"]))
    if "options" in cmd:
        layer["options"] = _merge("options", layer["options"], cmd["options"],
                                  OPTION_KEYS)
    return {"id": layer["id"]}


def _move_layer(doc: dict, cmd: dict) -> dict:
    index = _find(doc, cmd.get("id"))
    last = len(doc["layers"]) - 1
    to = cmd.get("to")
    if "index" in cmd:
        target = cmd["index"]
        if not isinstance(target, int) or isinstance(target, bool):
            raise MapDocError("index must be an integer (0 = bottom)")
        target = max(0, min(last, target))
    elif to in ("top", "bottom", "up", "down"):
        target = {"top": last, "bottom": 0,
                  "up": min(last, index + 1), "down": max(0, index - 1)}[to]
    else:
        raise MapDocError("move_layer needs index, or to: top|bottom|up|down")
    layer = doc["layers"].pop(index)
    doc["layers"].insert(target, layer)
    return {"id": layer["id"], "index": target}


def _set_view(doc: dict, cmd: dict) -> dict:
    if not any(k in cmd for k in ("center", "zoom", "bearing", "pitch", "bounds")):
        raise MapDocError("set_view needs center, zoom, bearing, pitch or bounds")
    doc["view"] = _view_patch(doc["view"], cmd)
    return {"view": doc["view"]}


def _set_basemap(doc: dict, cmd: dict) -> dict:
    basemap = cmd.get("basemap")
    if basemap not in BASEMAPS:
        raise MapDocError(f"basemap must be one of {', '.join(BASEMAPS)}")
    doc["basemap"] = basemap
    return {"basemap": basemap}


def _set_title(doc: dict, cmd: dict) -> dict:
    title = cmd.get("title")
    if not isinstance(title, str):
        raise MapDocError("title must be a string")
    doc["title"] = title
    return {"title": title}


def _clear_layers(doc: dict, cmd: dict) -> dict:
    removed = len(doc["layers"])
    doc["layers"] = []
    return {"removed": removed}


COMMANDS = {
    "add_layer": _add_layer,
    "remove_layer": _remove_layer,
    "update_layer": _update_layer,
    "move_layer": _move_layer,
    "set_view": _set_view,
    "set_basemap": _set_basemap,
    "set_title": _set_title,
    "clear_layers": _clear_layers,
}


def normalize(doc: Any) -> dict[str, Any]:
    """A document read from disk, checked and filled with defaults. Rebuilding
    it through the commands is what guarantees a hand-edited file obeys the
    same rules as one the commands wrote."""
    if not is_doc(doc):
        raise MapDocError(f"not a map document (expected \"type\": \"{DOC_TYPE}\")")
    if doc.get("version", DOC_VERSION) > DOC_VERSION:
        raise MapDocError(f"map document version {doc['version']} is newer than "
                          f"this viewer understands ({DOC_VERSION})")
    out = new_doc(doc.get("title") or "")
    if doc.get("basemap") is not None:
        _set_basemap(out, {"basemap": doc["basemap"]})
    if isinstance(doc.get("view"), dict):
        out["view"] = _view_patch(out["view"], doc["view"])
    layers = doc.get("layers") or []
    if not isinstance(layers, list):
        raise MapDocError("layers must be a list")
    for layer in layers:
        if not isinstance(layer, dict):
            raise MapDocError("each layer must be an object")
        fields = {k: layer[k] for k in
                  ("source", "name", "kind", "id", "visible", "opacity", "style", "options")
                  if k in layer}
        _add_layer(out, fields)
    return out


def apply(doc: dict, command: dict) -> tuple[dict, dict]:
    """Apply one command to a copy of `doc`: (new_doc, result). Raises
    MapDocError, leaving `doc` untouched, when the command is refused."""
    if not isinstance(command, dict):
        raise MapDocError("a command is an object with an \"op\"")
    op = command.get("op")
    handler = COMMANDS.get(op)
    if handler is None:
        raise MapDocError(f"unknown op {op!r} (known: {', '.join(COMMANDS)})")
    out = copy.deepcopy(doc)
    result = handler(out, command)
    return out, result


def apply_all(doc: dict, commands: list[dict]) -> tuple[dict, list[dict]]:
    """All-or-nothing: the first refused command refuses the batch."""
    results = []
    for index, command in enumerate(commands):
        try:
            doc, result = apply(doc, command)
        except MapDocError as error:
            raise MapDocError(f"command {index} ({command.get('op') if isinstance(command, dict) else '?'}): {error}") from None
        results.append(result)
    return doc, results


# ---- files ------------------------------------------------------------------

def load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        try:
            raw = json.load(handle)
        except ValueError as error:
            raise MapDocError(f"{path} is not valid JSON: {error}") from None
    return normalize(raw)


def load_or_new(path: str) -> dict[str, Any]:
    if os.path.exists(path):
        return load(path)
    title = re.sub(r"\.fmap$", "", _basename(path))
    return new_doc(title)


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def save(path: str, doc: dict) -> None:
    """Atomic write, so the page polling this file never reads half of it."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temporary = tempfile.mkstemp(prefix=".fmap-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dumps(doc))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def summary(doc: dict) -> dict[str, Any]:
    """What an agent needs to decide the next command, without the noise."""
    return {
        "title": doc.get("title", ""),
        "basemap": doc["basemap"],
        "view": doc["view"],
        "layers": [
            {"id": layer["id"], "name": layer["name"], "kind": layer["kind"],
             "source": layer["source"], "visible": layer["visible"],
             "opacity": layer["opacity"], "style": layer["style"],
             "options": layer["options"]}
            for layer in reversed(doc["layers"])  # top of the map first
        ],
    }
