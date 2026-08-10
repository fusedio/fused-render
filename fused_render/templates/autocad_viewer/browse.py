"""List the CAD drawings in a folder, for the viewer's Open panel.

Stdlib only (runs on the app interpreter, no dependencies) and returns
JSON-native values. The viewer calls this after the user picks or types a
folder, to offer the .dxf/.dwg files in it without a directory listing through
the shell. A bad folder comes back as `error` rather than raising, so the panel
can show a message instead of a traceback overlay.
"""
import os

_CAD_EXT = (".dxf", ".dwg")


def main(folder: str) -> dict:
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError as e:
        return {"folder": folder, "files": [], "error": str(e)}
    files = []
    for name in names:
        full = os.path.join(folder, name)
        if name.lower().endswith(_CAD_EXT) and os.path.isfile(full):
            files.append({
                "name": name,
                "path": full.replace("\\", "/"),
                "size": os.path.getsize(full),
            })
    return {"folder": folder.replace("\\", "/"), "files": files}
