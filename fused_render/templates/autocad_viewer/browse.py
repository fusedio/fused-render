"""List the CAD drawings in a folder, for the viewer's Open panel.

Stdlib only (runs on the app interpreter, no dependencies) and returns
JSON-native values. A bad folder comes back as `error` rather than raising, so
the panel shows a message instead of a traceback overlay.

The folder is listed with a single os.scandir pass.
"""
import os
import re

_CAD_EXT = (".dxf", ".dwg")
_MAX = 2000  # bound a local scan so a pathological directory can't build a huge response


def _norm(p: str) -> str:
    # Mirror the shell's URL codec (router.ts urlForFsPath): normalize ONLY
    # drive-letter paths to forward slashes; on POSIX a backslash is a legal
    # filename character and must survive untouched.
    return p.replace("\\", "/") if re.match(r"^[A-Za-z]:[\\/]", p) else p


def _entry(folder, name, size):
    return {"name": name, "path": _norm(os.path.join(folder, name)), "size": size}


def _list_local(folder):
    files = []
    try:
        with os.scandir(folder) as it:
            for i, de in enumerate(it):
                if i >= _MAX:
                    break
                if not de.name.lower().endswith(_CAD_EXT):
                    continue
                try:
                    if de.is_file():
                        files.append(_entry(folder, de.name, de.stat().st_size))
                except OSError:
                    continue
    except OSError as e:
        return {"folder": _norm(folder), "files": [], "error": str(e)}
    files.sort(key=lambda f: f["name"].lower())
    return {"folder": _norm(folder), "files": files}


def main(folder: str) -> dict:
    return _list_local(folder)
