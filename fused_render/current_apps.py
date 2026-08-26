"""Current apps: the apps on the user's desk, as a STORE of their own.

``~/.fused-render/current_apps.json``::

    {"apps": [{"path": "/Users/me/Fused/local/foo", "addedAt": "<iso>"}],
     "seen": ["<task key>", ...]}

The sidebar's "Current apps" section (D487) used to DERIVE this list from the
task pulse — an app was current while any task under it was not archived, and
archiving the last task made the row vanish. That coupled two different
questions: "what is on my desk" and "what is filed". The owner's redesign
(2026-08-26) makes the desk its own table with these rules:

* A NEW task adds its app to the table if the app is not already there
  (`observe`, run inside the tasks listing so it sees every task on the
  machine, however it was started — chat, schedule, CLI).
* Nothing removes an app automatically. Archive every task under it and the
  row stays; the table only grows through tasks.
* Removing an app (the row's cross, DELETE /api/current-apps) ARCHIVES every
  task under it as a side effect — the one place the coupling still runs, and
  it runs desk → tasks, not the other way.
* Every kind of app counts — `~/Fused/local`, `~/Fused/showcase`, any folder
  under the workspace with a declared page (`app_listing.app_entry`, the same
  marker the /apps hub trusts, D301), and LINKED apps: folders anywhere on
  disk in the registered-apps registry (registered_apps.py). A task on a
  folder that is none of those is a project, not an app, and adds nothing.

"New" is decided against `seen`: the task keys the observer has already looked
at. A key it has not seen before is a new task. The first observe ever (no
file) therefore seeds the desk with every app that has a non-archived task —
the migration of what the derived section showed, widened from `local/` to all
app kinds. `seen` is pruned to the keys still listed, so it is bounded by the
machine's transcripts and a deleted task's key does not live here forever.

Paths are stored CANONICAL (forward-slash, `_view_url_codec.canonical_fs_path`)
— the form a task row's `project` already has, so the containment test is
plain string work on one spelling. Everything degrades: a corrupt file reads
as empty, a folder that vanished is listed with ``exists: false`` (the row is
the user's to remove — a table that prunes itself is the coupling this module
exists to end).
"""
import os
from datetime import datetime, timezone

from fused_render import app_listing, registered_apps
from fused_render._view_url_codec import canonical_fs_path
from fused_render.index.ignore import MountGuard
from fused_render.shell import storage

FILENAME = "current_apps.json"


def _path() -> str:
    return os.path.join(storage.home_dir(), FILENAME)


def read_state() -> dict:
    """The store, shape-checked: ``{"apps": [...], "seen": [...]}`` with every
    app a dict carrying a string ``path``. Missing or corrupt reads as empty."""
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return {"apps": [], "seen": []}
    apps = data.get("apps")
    seen = data.get("seen")
    return {
        "apps": [
            a for a in (apps if isinstance(apps, list) else [])
            if isinstance(a, dict) and isinstance(a.get("path"), str) and a["path"]
        ],
        "seen": [k for k in (seen if isinstance(seen, list) else []) if isinstance(k, str)],
    }


def write_state(state: dict) -> None:
    storage.write_json(_path(), {"apps": state["apps"], "seen": state["seen"]})


def is_under(project: str, folder: str) -> bool:
    """Is `project` `folder` or inside it — exact on the folder boundary, so
    `foo` never claims `foo2`. Both arguments canonical."""
    return project == folder or project.startswith(folder.rstrip("/") + "/")


def _workspace_root() -> str:
    from fused_render.shell.seed import fused_dir

    return canonical_fs_path(os.path.abspath(fused_dir())).rstrip("/")


def app_dir_for(project: str) -> str | None:
    """The app folder a task's project belongs to, canonical, or None.

    Two sources, in order:

    1. The registered-apps registry — a linked app whose folder is the project
       or an ancestor of it. Registry first because a registered folder can sit
       anywhere, and "anywhere" includes nothing the workspace rule below would
       find.
    2. The workspace: climb from the project towards the workspace root and
       take the FIRST folder with a declared page — a task on `foo/sub` belongs
       to `foo` when `foo` is the app. The root itself is never an app (a task
       on `~/Fused/local` is a task on a shelf).

    Guarded before any syscall: a project on a wedged mount answers None
    rather than blocking the tasks listing."""
    if not project or not os.path.isabs(project):
        return None
    project = canonical_fs_path(os.path.abspath(project)).rstrip("/") or "/"
    for e in registered_apps.read_entries():
        folder = canonical_fs_path(os.path.abspath(e["path"])).rstrip("/")
        if folder and is_under(project, folder):
            return folder
    root = _workspace_root()
    if not is_under(project, root) or project == root:
        return None
    if MountGuard().blocks(project):
        return None
    folder = project
    while folder != root and len(folder) > len(root):
        try:
            if os.path.isdir(folder) and app_listing.app_entry(folder) is not None:
                return folder
        except OSError:
            return None
        parent = folder.rsplit("/", 1)[0]
        if parent == folder or not parent:
            break
        folder = parent
    return None


def observe(rows: list[dict]) -> None:
    """Look at every task row once: a key not seen before is a new task, and a
    new task that is not archived adds its app. Called from the tasks listing,
    so the listing's own response already reflects the add. Writes only when
    something changed — an idle poll must not rewrite the file."""
    state = read_state()
    seen = set(state["seen"])
    known = {a["path"] for a in state["apps"]}
    changed = False
    now = None
    live_keys: set[str] = set()
    for row in rows:
        key = str(row.get("key") or "")
        if not key:
            continue
        live_keys.add(key)
        if key in seen:
            continue
        seen.add(key)
        changed = True
        if row.get("status") == "archived":
            continue
        folder = app_dir_for(str(row.get("project") or ""))
        if folder is None or folder in known:
            continue
        now = now or datetime.now(timezone.utc).isoformat()
        state["apps"].append({"path": folder, "addedAt": now})
        known.add(folder)
    pruned = seen & live_keys
    if changed or pruned != set(state["seen"]):
        state["seen"] = sorted(pruned)
        write_state(state)


def remove(path: str) -> bool:
    """Drop `path` from the desk. True when it was there. The archive
    side-effect is the ROUTER's (it owns the tasks) — this is only the table."""
    folder = canonical_fs_path(os.path.abspath(path)).rstrip("/")
    state = read_state()
    kept = [a for a in state["apps"] if a["path"] != folder]
    if len(kept) == len(state["apps"]):
        return False
    state["apps"] = kept
    write_state(state)
    return True


def _added_epoch(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def list_apps() -> list[dict]:
    """The desk, in stored (added) order. Each: ``path`` (canonical), ``name``
    (folder name), ``kind`` (``linked`` for a registry folder, ``workspace``
    otherwise), ``entry`` (the page to run, or None), ``exists``, ``added_at``
    (epoch). A folder that is gone or unreadable still lists — the row is the
    user's to remove — with ``exists`` false and no entry."""
    linked = {
        canonical_fs_path(os.path.abspath(e["path"])).rstrip("/")
        for e in registered_apps.read_entries()
    }
    guard = MountGuard()
    out = []
    for a in read_state()["apps"]:
        path = a["path"]
        entry = None
        exists = False
        if not guard.blocks(path):
            try:
                exists = os.path.isdir(path)
                if exists:
                    entry = app_listing.app_entry(path)
            except OSError:
                exists = False
        out.append({
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or path,
            "kind": "linked" if path in linked else "workspace",
            "entry": canonical_fs_path(entry) if entry else None,
            "exists": exists,
            "added_at": _added_epoch(a.get("addedAt")),
        })
    return out
