"""MCP tools for the Map Viewer: an agent builds and edits maps as `.fmap`
documents, with or without the page open.

Each public function here is one tool, declared in this folder's `mcp.toml`
(SPEC §44), so `fused app serve <this folder>` publishes them; its schema is the
signature. Every edit is a `map_doc` command applied to the file and written
back atomically. A page showing that file polls it and reconciles, so an open
map moves as the agent works; a closed one simply opens in the new state.

Tools return JSON-native dicts and never raise: a refused command comes back as
`{"status": "error", "message": ...}` naming what to fix, which is what an agent
can act on.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "map_tools.py")
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import map_doc  # noqa: E402


def _path(map_path: str) -> str:
    path = os.path.abspath(os.path.expanduser(str(map_path or "").strip()))
    if not path or path == os.path.abspath(""):
        raise map_doc.MapDocError("map_path is required (a .fmap file)")
    if not path.endswith(map_doc.DOC_SUFFIX):
        raise map_doc.MapDocError(f"map_path must end in {map_doc.DOC_SUFFIX}")
    return path


def _edit(map_path: str, commands: list[dict]) -> dict[str, Any]:
    try:
        path = _path(map_path)
        doc = map_doc.load_or_new(path)
        doc, results = map_doc.apply_all(doc, commands)
        map_doc.save(path, doc)
        return {"status": "ok", "map_path": path, "results": results,
                "map": map_doc.summary(doc)}
    except (map_doc.MapDocError, OSError) as error:
        return {"status": "error", "message": str(error)}


def _drop_none(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _json_arg(value: Any) -> Any:
    """An object/list argument, or its JSON text. `fused app serve` derives
    each tool's schema from the signature and publishes an optional dict or
    list as a string, so an agent reading that schema sends JSON text."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise map_doc.MapDocError(f"not valid JSON: {text[:80]!r}") from None
    return value


def create_map(map_path: str, title: str = "", basemap: str = "light") -> dict:
    """Create a new, empty map document at map_path (a .fmap file). Refuses to
    overwrite an existing one. Open it in fused-render with the Map Viewer."""
    try:
        path = _path(map_path)
        if os.path.exists(path):
            return {"status": "error", "message": f"{path} already exists"}
        doc = map_doc.new_doc(title or os.path.basename(path)[: -len(map_doc.DOC_SUFFIX)])
        doc, _ = map_doc.apply(doc, {"op": "set_basemap", "basemap": basemap})
        map_doc.save(path, doc)
        return {"status": "ok", "map_path": path, "map": map_doc.summary(doc)}
    except (map_doc.MapDocError, OSError) as error:
        return {"status": "error", "message": str(error)}


def describe_map(map_path: str) -> dict:
    """The map's basemap, camera and layers (top of the map first), with each
    layer's id, source, kind, style and options."""
    try:
        return {"status": "ok", "map": map_doc.summary(map_doc.load(_path(map_path)))}
    except (map_doc.MapDocError, OSError) as error:
        return {"status": "error", "message": str(error)}


def add_layer(map_path: str, source: str, name: str = "", kind: str = "auto",
              style: dict | None = None, options: dict | None = None,
              visible: bool = True, opacity: float = 1.0) -> dict:
    """Add a data layer on top of the map. source is a local path or URL:
    GeoTIFF/COG, GeoJSON, GeoParquet, Shapefile, GeoPackage, FlatGeobuf, CSV,
    KML, NetCDF, HDF5, Zarr, PMTiles, LAS/LAZ/COPC point clouds (or an EPT
    ept.json), or a .py script returning geodata. kind is
    auto|raster|vector|zarr|pmtiles|pointcloud. See map_style_reference for
    style and options keys. Creates the map file if it does not exist. Returns
    the new layer's id, which the other layer tools take."""
    try:
        command = _drop_none(op="add_layer", source=source, name=name or None,
                             kind=kind, style=_json_arg(style),
                             options=_json_arg(options), visible=visible,
                             opacity=opacity)
    except map_doc.MapDocError as error:
        return {"status": "error", "message": str(error)}
    result = _edit(map_path, [command])
    if result["status"] == "ok":
        result["id"] = result["results"][0]["id"]
    return result


def update_layer(map_path: str, layer_id: str, name: str | None = None,
                 visible: bool | None = None, opacity: float | None = None,
                 style: dict | None = None, options: dict | None = None) -> dict:
    """Change a layer: rename, show/hide, opacity 0-1, or merge style/options
    keys (a key set to null returns to its default). See map_style_reference."""
    try:
        command = _drop_none(op="update_layer", id=layer_id, name=name,
                             visible=visible, opacity=opacity,
                             style=_json_arg(style), options=_json_arg(options))
    except map_doc.MapDocError as error:
        return {"status": "error", "message": str(error)}
    return _edit(map_path, [command])


def remove_layer(map_path: str, layer_id: str) -> dict:
    """Remove a layer from the map (the data file is untouched)."""
    return _edit(map_path, [{"op": "remove_layer", "id": layer_id}])


def move_layer(map_path: str, layer_id: str, to: str = "top",
               index: int | None = None) -> dict:
    """Reorder a layer: to is top|bottom|up|down, or give index (0 = bottom)."""
    command = {"op": "move_layer", "id": layer_id}
    if index is not None:
        command["index"] = index
    else:
        command["to"] = to
    return _edit(map_path, [command])


def set_view(map_path: str, center_lon: float | None = None,
             center_lat: float | None = None, zoom: float | None = None,
             bounds: list[float] | None = None, bearing: float | None = None,
             pitch: float | None = None) -> dict:
    """Move the camera: a center (lon, lat) and zoom 0-24, or bounds
    [west, south, east, north] to fit; bearing and pitch optional."""
    command: dict[str, Any] = {"op": "set_view"}
    if center_lon is not None or center_lat is not None:
        if center_lon is None or center_lat is None:
            return {"status": "error", "message": "give both center_lon and center_lat"}
        command["center"] = [center_lon, center_lat]
    try:
        command.update(_drop_none(zoom=zoom, bounds=_json_arg(bounds),
                                  bearing=bearing, pitch=pitch))
    except map_doc.MapDocError as error:
        return {"status": "error", "message": str(error)}
    return _edit(map_path, [command])


def set_basemap(map_path: str, basemap: str) -> dict:
    """Switch the basemap: light, dark, sat (satellite) or none."""
    return _edit(map_path, [{"op": "set_basemap", "basemap": basemap}])


def apply_commands(map_path: str, commands: list[dict]) -> dict:
    """Apply several map commands at once, all or nothing. Each is an object
    with "op" (add_layer, remove_layer, update_layer, move_layer, set_view,
    set_basemap, set_title, clear_layers) and that op's fields, e.g.
    {"op": "update_layer", "id": "roads", "style": {"line_color": "#ff0000"}}."""
    try:
        commands = _json_arg(commands)
    except map_doc.MapDocError as error:
        return {"status": "error", "message": str(error)}
    if not isinstance(commands, list):
        return {"status": "error", "message": "commands must be a list"}
    return _edit(map_path, commands)


def describe_source(source: str) -> dict:
    """What a data file holds before mapping it: raster bands/dtype/CRS/bounds,
    vector feature count/fields/geometry type, or a NetCDF/Zarr's gridded
    variables and dimensions. Use it to choose styles (bands, color_by)."""
    try:
        import prepare
    except Exception as error:  # the geo stack is the template venv's
        return {"status": "error", "message": f"describe_source needs the Map "
                f"Viewer's environment: {error}"}
    return prepare.main(source, action="inspect")


def download_source(source: str, dest_dir: str) -> dict:
    """Copy a remote layer source (http(s) or s3 URL) into the local folder
    dest_dir, e.g. to keep a COG, GeoParquet or PMTiles file, or a whole Zarr
    store. Never overwrites: an existing name gets a -1, -2... suffix. Returns
    the local path, which can then be added as a layer."""
    try:
        import download
    except Exception as error:  # requests is the template venv's
        return {"status": "error", "message": f"download_source needs the Map "
                f"Viewer's environment: {error}"}
    return download.main(source, dest_dir, action="run")


def style_reference() -> dict:
    """Every style and options key a layer accepts, by layer kind, with its
    type. Colors are "#rrggbb"; unit means 0-1; range is [min, max]; codes
    is a list of integers 0-255."""
    return {
        "status": "ok",
        "style": map_doc.STYLE_KEYS,
        "options": map_doc.OPTION_KEYS,
        "basemaps": list(map_doc.BASEMAPS),
        "kinds": list(map_doc.KINDS),
        "commands": list(map_doc.COMMANDS),
    }


def main(map_path: str = "", commands: list | None = None) -> dict:
    """runPython entry: apply `commands` to `map_path` (the page edits an open
    map file through `map_doc`'s browser twin; this is for scripts and tests)."""
    return apply_commands(map_path, commands or [])
