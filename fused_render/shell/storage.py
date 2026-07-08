"""Shell user-data dir (~/.fused-render) and atomic JSON I/O.

Shared foundation for every shell state backend: one home dir, one pair of
read/write helpers. Adding a resource = a new module that resolves a path
under home_dir() and uses read_json/write_json.

The dir doubles as the user-template override channel (server.py's
USER_TEMPLATES_DIR points at the same path, D50/D73); the two concerns share
the directory but nothing else, and are kept decoupled to avoid a
server <-> shell import cycle.
"""
import json
import os
import tempfile


def home_dir() -> str:
    """User-data dir for shell state. FUSED_RENDER_HOME overrides the default
    ~/.fused-render — tests set it so they never touch the real home dir."""
    return os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")


def read_json(path: str):
    """Parse the JSON at `path`; return None if it is absent OR corrupt. The
    None-vs-value distinction lets a caller tell 'never written' from an empty
    resource (e.g. the bookmarks `exists` flag / one-time import gate)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def write_json(path: str, data) -> None:
    """Atomically write `data` as JSON to `path` (temp file in the same dir +
    os.replace), creating the home dir if needed. Last write wins — no locking
    (single local user, D3)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
