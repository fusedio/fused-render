"""Where specialists live on disk (PLAN B.1).

`root()` is `<workspace>/specialists`; each specialist's home is a direct child named by its slug; `.state/` beside
them is root state (not a home). Nothing here creates a folder: with the flag off no code path may make
`~/Fused/specialists` appear, and the predicates are called per transcript by the Tasks page and per start by the
project queue.
"""
from __future__ import annotations

import json
import os
import threading

STATE = ".state"
APP_DIR = ".specialist"
MANIFEST = "specialist.json"


def root() -> str:
    """`<workspace>/specialists`, not created."""
    from fused_render.bots.paths import workspace_dir

    return os.path.join(workspace_dir(), "specialists")


def state_root() -> str:
    """`<root>/.state`: root app state (seeded witness, scheduler lock, tokens, general inbox, signals, trash)."""
    return os.path.join(root(), STATE)


# id -> home folder. Keyed by the root too, since tests (and FUSED_RENDER_DIR) move the workspace.
_HOMES: dict[tuple[str, str], str] = {}
_LOCK = threading.Lock()


def _reset() -> None:
    """Forget cached id -> home lookups (tests, and after a create/delete)."""
    with _LOCK:
        _HOMES.clear()
        _SPEC_OF.clear()


def _scan(r: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        names = os.listdir(r)
    except OSError:
        return out
    for name in names:
        if name.startswith("."):
            continue
        folder = os.path.join(r, name)
        try:
            with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
                sid = json.load(f).get("id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(sid, str) and sid:
            out[sid] = folder
    return out


def home(spec_id: str) -> str:
    """The home folder of `spec_id`. Homes are named by slug, not id, so the id -> folder map is read from the
    manifests (cached; a miss rescans once). An unknown id answers `<root>/<id>`, a folder that does not exist,
    so callers can test `os.path.isdir` instead of handling None."""
    r = root()
    key = (r, spec_id)
    with _LOCK:
        hit = _HOMES.get(key)
    if hit and os.path.isfile(os.path.join(hit, MANIFEST)):
        return hit
    found = _scan(r)
    with _LOCK:
        for sid, folder in found.items():
            _HOMES[(r, sid)] = folder
    return found.get(spec_id) or os.path.join(r, spec_id)


def app_dir(spec_id: str) -> str:
    """`<home>/.specialist`: the home's untracked app state."""
    return os.path.join(home(spec_id), APP_DIR)


def is_inside_root(path: str) -> bool:
    """Pure string test: is `path` inside `<root>/` (a home or root state)? No disk access, no flag check."""
    if not path:
        return False
    r = os.path.normpath(root())
    p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    return p == r or p.startswith(r + os.sep)


_SPEC_OF: dict[tuple[str, str], str | None] = {}


def spec_of_path(path: str) -> str | None:
    """The id of the specialist whose home contains `path` (the home itself or anything under it), else None.
    String prefix test first, then the home's manifest; answers are cached per (root, top folder)."""
    if not path or not is_inside_root(path):
        return None
    r = os.path.normpath(root())
    p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    rel = p[len(r):].lstrip(os.sep)
    top = rel.split(os.sep, 1)[0] if rel else ""
    if not top or top.startswith("."):
        return None
    key = (r, top)
    with _LOCK:
        if key in _SPEC_OF and _SPEC_OF[key] is not None:
            return _SPEC_OF[key]
    try:
        with open(os.path.join(r, top, MANIFEST), encoding="utf-8") as f:
            sid = json.load(f).get("id")
    except (OSError, ValueError, AttributeError):
        sid = None
    sid = sid if isinstance(sid, str) and sid else None
    with _LOCK:
        _SPEC_OF[key] = sid
    return sid


def flag_on_and_inside(path: str) -> bool:
    """The seam `project_queue.queue_key` and the Tasks list ask: flag on and `path` inside a specialist's home.
    The string test runs first so the common case (a path nowhere near the root) costs no prefs read."""
    if not path or not is_inside_root(path):
        return False
    from fused_render.specialists import flag

    return flag.on() and spec_of_path(path) is not None
