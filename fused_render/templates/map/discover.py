"""List runnable / renderable entries in a directory for the viewer sidebar.

main(dir="") -> {dir, parent, entries:[{name, path, kind, ext}]}
Default dir is the user's home. Pass any absolute directory to browse.
"""

import os

_DEFAULT_DIR = os.path.expanduser("~")

RASTER = (".tif", ".tiff", ".vrt", ".jp2", ".img")
VECTOR = (".geojson", ".json", ".shp", ".gpkg", ".fgb", ".kml", ".gml")
TABLE = (".parquet", ".geoparquet", ".csv")
PMTILES = (".pmtiles",)


def _kind(name):
    low = name.lower()
    if low.endswith(".py"):
        return "python"
    if low.endswith(PMTILES):
        return "pmtiles"
    if low.endswith(RASTER):
        return "raster"
    if low.endswith(VECTOR):
        return "vector"
    if low.endswith(TABLE):
        return "table"
    return "other"


def main(dir: str = ""):
    base = os.path.abspath(os.path.expanduser(dir.strip())) if dir else _DEFAULT_DIR
    if not os.path.isdir(base):
        return {"error": f"Not a directory: {base}", "dir": base, "entries": []}

    entries = []
    for name in sorted(os.listdir(base), key=str.lower):
        if name.startswith(".") or name in ("__pycache__",):
            continue
        full = os.path.join(base, name)
        if os.path.isdir(full):
            entries.append({"name": name, "path": full, "kind": "dir", "ext": ""})
            continue
        _, ext = os.path.splitext(name)
        k = _kind(name)
        if k == "other":
            continue  # only show things we might render / navigate
        entries.append({"name": name, "path": full, "kind": k, "ext": ext.lower()})

    # dirs first, then files
    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    return {"dir": base, "parent": os.path.dirname(base), "entries": entries}
