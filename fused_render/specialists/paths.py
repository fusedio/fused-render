"""Where specialists live on disk.

`root()` is `<workspace>/specialists`; each specialist's home is a direct child named by its slug; `.state/` beside
them is root state (tokens, trash). Nothing here creates a folder: with the flag off no code path may make
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
    return os.path.join(root(), STATE)


# Keyed by the root too, since tests (and FUSED_RENDER_DIR) move the workspace.
_HOMES: dict[tuple[str, str], str] = {}
_SPEC_OF: dict[tuple[str, str], str | None] = {}
_LOCK = threading.Lock()


def _reset() -> None:
    """Forget cached lookups (tests, and after a create/delete)."""
    with _LOCK:
        _HOMES.clear()
        _SPEC_OF.clear()


def _manifest_id(folder: str) -> str | None:
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            sid = json.load(f).get("id")
    except (OSError, ValueError, AttributeError):
        return None
    return sid if isinstance(sid, str) and sid else None


def scan() -> dict[str, str]:
    """`{id: home}` for every home under the root."""
    r = root()
    try:
        names = os.listdir(r)
    except OSError:
        return {}
    out = {}
    for name in names:
        sid = None if name.startswith(".") else _manifest_id(os.path.join(r, name))
        if sid:
            out[sid] = os.path.join(r, name)
    return out


def home(spec_id: str) -> str:
    """The home folder of `spec_id` (homes are named by slug, so the map is read from the manifests; a miss
    rescans). An unknown id answers `<root>/<id>`, a folder that does not exist."""
    r = root()
    with _LOCK:
        hit = _HOMES.get((r, spec_id))
    if hit and os.path.isfile(os.path.join(hit, MANIFEST)):
        return hit
    found = scan()
    with _LOCK:
        for sid, folder in found.items():
            _HOMES[(r, sid)] = folder
    return found.get(spec_id) or os.path.join(r, spec_id)


def hidden_from_tasks(cwd: str) -> bool:
    """A session whose cwd is inside the specialists root is never a task, whether the flag is on or off, once the
    root exists. Never raises."""
    try:
        return bool(cwd) and is_inside_root(cwd) and os.path.isdir(root())
    except Exception:  # noqa: BLE001 — "cannot tell" must not hide a task
        return False


def is_inside_root(path: str) -> bool:
    """Pure string test: is `path` the root or inside it? No disk access, no flag check."""
    if not path:
        return False
    r = os.path.normpath(root())
    p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    return p == r or p.startswith(r + os.sep)


def spec_of_path(path: str) -> str | None:
    """The id of the specialist whose home contains `path`, else None (cached per top folder)."""
    if not is_inside_root(path):
        return None
    r = os.path.normpath(root())
    rel = os.path.normpath(os.path.abspath(os.path.expanduser(path)))[len(r):].lstrip(os.sep)
    top = rel.split(os.sep, 1)[0]
    if not top or top.startswith("."):
        return None
    key = (r, top)
    with _LOCK:
        if _SPEC_OF.get(key):
            return _SPEC_OF[key]
    sid = _manifest_id(os.path.join(r, top))
    with _LOCK:
        _SPEC_OF[key] = sid
    return sid


def flag_on_and_inside(path: str) -> bool:
    """Flag on and `path` inside a specialist's home (`project_queue.queue_key`). The string test runs first so a
    path nowhere near the root costs no prefs read."""
    if not is_inside_root(path):
        return False
    from fused_render.specialists import flag

    return flag.on() and spec_of_path(path) is not None
