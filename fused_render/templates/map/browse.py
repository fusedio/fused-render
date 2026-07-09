"""Directory listing for the map template's "add layer" picker.

Returns the sub-directories and files of `dir` so the HTML can render a
navigable list for adding another dataset as an overlay — no need to type
paths by hand. Stdlib only.
"""


def main(dir: str = "~", exts: str = ".geojson,.gpkg,.shp,.fgb,.kml,.tif,.tiff,.pmtiles,.parquet,.csv",
          show_all: bool = False):
    import os

    dir = os.path.abspath(os.path.expanduser(dir or "~"))
    if not os.path.isdir(dir):
        dir = os.path.dirname(dir) or "/"

    allow = tuple(e.strip().lower() for e in exts.split(",") if e.strip())
    dirs, files = [], []
    try:
        names = os.listdir(dir)
    except OSError as e:
        return {"error": f"cannot list {dir}: {e}", "dir": dir,
                "parent": os.path.dirname(dir)}

    for name in names:
        if name.startswith("."):            # hide dotfiles
            continue
        full = os.path.join(dir, name)
        try:
            is_dir = os.path.isdir(full)
            size = None if is_dir else os.path.getsize(full)
        except OSError:
            continue
        if is_dir:
            dirs.append({"name": name, "path": full, "is_dir": True})
        else:
            ext = os.path.splitext(name)[1].lower()
            loadable = ext in allow
            if loadable or show_all:
                files.append({"name": name, "path": full, "is_dir": False,
                              "size": size, "ext": ext, "loadable": loadable})

    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())

    parts, acc = [], ""
    for seg in dir.strip("/").split("/"):
        acc += "/" + seg
        parts.append({"label": seg, "path": acc})

    return {
        "dir": dir,
        "parent": os.path.dirname(dir),
        "crumbs": parts,
        "dirs": dirs,
        "files": files,
    }


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
