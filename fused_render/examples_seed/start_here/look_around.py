"""runPython target for the filesystem demo on the Start Here page.

Read-only look at the real files around this project: the files in this
folder, and the sibling example projects next to it. Stdlib only.
"""
import os
import platform


def main() -> dict:
    """List this project's folder and its sibling projects. Reads only."""
    here = os.path.dirname(os.path.abspath(__file__))

    files = []
    for name in sorted(os.listdir(here)):
        path = os.path.join(here, name)
        if os.path.isfile(path):
            files.append({"name": name, "bytes": os.path.getsize(path)})

    parent = os.path.dirname(here)
    own = os.path.basename(here)
    siblings = []
    for name in sorted(os.listdir(parent)):
        if name == own:
            continue
        path = os.path.join(parent, name)
        if os.path.isdir(path):
            try:
                count = len(os.listdir(path))
            except OSError:
                count = 0
            siblings.append({"name": name, "entries": count})

    return {
        "folder": here,                       # the real absolute path on disk
        "files": files,                       # this page and the .py you're running
        "siblings": siblings,                 # the other example projects beside it
        "python": platform.python_version(),
    }
