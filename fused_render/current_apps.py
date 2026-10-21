"""Current apps: the apps on the user's desk, as a STORE of their own.

``~/.fused-render/current_apps.json``::

    {"order": "top_first",
     "apps": [{"path": "/Users/me/Fused/local/foo", "addedAt": "<iso>",
               "openedAt": <epoch>, "unread": <bool>}],
     "seen": ["<task key>", ...]}

The sidebar's "Current apps" section (D487) used to DERIVE this list from the
task pulse — an app was current while any task under it was not archived, and
archiving the last task made the row vanish. That coupled two different
questions: "what is on my desk" and "what is filed". The owner's redesign
(2026-08-26) makes the desk its own table with these rules:

* A NEW task adds its app to the table if the app is not already there
  (`observe`, run inside the tasks listing so it sees every task on the
  machine, however it was started — chat, schedule, CLI).
* OPENING a task adds its app too (`ensure`, POST /api/current-apps/ensure —
  the task side peek calls it on mount; NOT the app page: an app the user
  took off the desk must stay off when its page is opened from a Recent or
  a link, as it always did). `observe` sees a task ONCE, and a task that MAKES the app — a Bots
  build (bots/bot.py `build`), "make me an app in this empty folder" — is
  first listed while the folder is still empty, so the one look finds no app
  and the folder never reached the desk: no sidebar row, no preview in the
  peek (shell/peek-preview.ts reads this table and only this table). The
  owner (2026-10-21) put the check where it is needed instead of a watch on
  the listing: the thing is re-asked at the door the user walks through, and
  an app whose tasks you never open is not on your desk yet. An archived
  task's door does not add (removing an app archives its tasks; opening one
  of those must not bring the app back).
* Nothing removes an app automatically. Archive every task under it and the
  row stays; the table grows through tasks — and through the explorer's
  "Open in project" button (`add`, POST /api/current-apps/add), which puts
  the folder on the desk before opening its app page.
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

`apps` is stored in DISPLAY order, top first (D1386). A new app — a task's
(`observe`) or the explorer's button (`add`) — goes in at index 0, and a drag
in the sidebar rewrites the whole order (`set_order`, POST
/api/current-apps/order). The order used to be the browser's: the sidebar kept
a per-app sequence map in localStorage and the server's list was only the
"added" seed. That lost the user's arrangement on every relaunch that landed
on a different port — the desktop app walks ports 1777..1787 then an ephemeral
one, and each port is a new origin with empty storage — so the order moved
here, and the client's old key is imported once and removed.

The file used to be ADDED order (oldest first, `append`). A file without the
``"order": "top_first"`` stamp is that legacy layout and is read REVERSED, so a
desk nobody ever dragged keeps showing newest on top; `write_state` writes the
stamp, so the first write after the upgrade settles the file. The two outside
writers that edit this file in place (`app_state_move`, `app_state_remove`)
keep whatever top-level keys they found, stamp included, and never reorder.

Paths are stored CANONICAL (forward-slash, `_view_url_codec.canonical_fs_path`)
— the form a task row's `project` already has, so the containment test is
plain string work on one spelling. Everything degrades: a corrupt file reads
as empty, a folder that vanished is listed with ``exists: false`` (the row is
the user's to remove — a table that prunes itself is the coupling this module
exists to end).
"""
import os
import threading
from datetime import datetime, timezone

from fused_render import app_listing, registered_apps
from fused_render._view_url_codec import canonical_fs_path
from fused_render.index.ignore import MountGuard
from fused_render.shell import storage

FILENAME = "current_apps.json"

#: The stamp that says `apps` is top-first display order (module docstring).
ORDER_STAMP = "top_first"

# Serialises every read-modify-write of the file in this process. Before the
# order moved here only the listing (`observe`) and one-off gestures wrote it;
# now a drag (`set_order`) rewrites the whole list, and a drag that read the
# desk just before an `observe` pass inserted a new app would otherwise write
# that app away — and `observe` never re-adds it, because its task key is
# already `seen`. Re-entrant so a mutator may call `read_state` under it.
_LOCK = threading.RLock()


def _path() -> str:
    return os.path.join(storage.home_dir(), FILENAME)


def read_state() -> dict:
    """The store, shape-checked: ``{"apps": [...], "seen": [...]}`` with every
    app a dict carrying a string ``path``, ``apps`` top first. Missing or
    corrupt reads as empty; an unstamped (legacy, oldest-first) file reads
    reversed — see the module docstring."""
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return {"apps": [], "seen": []}
    apps = data.get("apps")
    seen = data.get("seen")
    apps = [
        a for a in (apps if isinstance(apps, list) else [])
        if isinstance(a, dict) and isinstance(a.get("path"), str) and a["path"]
    ]
    if data.get("order") != ORDER_STAMP:
        apps.reverse()
    return {
        "apps": apps,
        "seen": [k for k in (seen if isinstance(seen, list) else []) if isinstance(k, str)],
    }


def write_state(state: dict) -> None:
    """Write the store, always stamped top-first (`state["apps"]` must be in
    display order — what `read_state` hands back)."""
    storage.write_json(_path(), {"order": ORDER_STAMP, "apps": state["apps"],
                                 "seen": state["seen"]})


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
    # A canvas CLONE (`~/Fused/canvas/<env>/<name>`), or a node file in it:
    # the clone is the project (the sidebar's Canvases section, 2026-10-20).
    # Before the workspace climb — a clone declares no page, so the climb
    # would answer None, and a clone is under the workspace root.
    from fused_render.canvases import clone_dir_for

    clone = clone_dir_for(project)
    if clone is not None:
        return canonical_fs_path(clone[2]).rstrip("/")
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


# ---- the unread dot ----------------------------------------------------------
#
# Each desk row carries its OWN unread state (owner, 2026-09-07): ``unread`` is
# true when a task under the folder FINISHED since the user last opened the app,
# and opening the app — nothing else — clears it. It is decoupled from the
# tasks' per-message read state on purpose: reading the task in the Tasks page,
# the chat, anywhere, leaves the app's dot alone, and opening the app leaves the
# task's messages unread.
#
# Two facts per row, both here, both the server's:
#
# * ``openedAt`` — epoch of the last open (`mark_opened`, POST
#   /api/current-apps/open). 0.0 for a row a task put on the desk (`observe`):
#   never opened, so the completion that put it there lights it. `add` (the
#   explorer's "Open in project", which navigates there at once) stamps now.
# * ``unread`` — RECOMPUTED on every `observe` pass, never incremented: any task
#   row under the folder that is not running or archived and whose
#   ``happened_at`` (tasks._row: the newest thing that actually ran or was
#   written, never a due time or a creation stamp) is later than ``openedAt``.
#   A recomputation rather than a transition detector because `observe` sees
#   snapshots per poll: a rerun that starts and ends between two polls shows no
#   transition, and a recurring task goes in_progress → upcoming without ever
#   touching Done. Comparing clocks catches both, and is idempotent — a missed
#   poll costs nothing, the next one gets the same answer.
#
# Running rows are left out so the flag means "finished", not "active": while a
# run is in flight the row wears the running dot (yellow outranks green, the
# Tasks rule), and the moment it ends the next pass lights the green.

# The lanes whose rows do NOT count as finished work. Every other status —
# `done`, `blocked` (a failed run has happened), `upcoming` (a recurring task
# whose last run finished and whose next is on the books) — does.
_NOT_FINISHED = frozenset({"in_progress", "needs_attention", "archived"})


def _unread(folder: str, opened_at: float, rows: list[dict]) -> bool:
    for row in rows:
        if row.get("status") in _NOT_FINISHED:
            continue
        project = str(row.get("project") or "")
        if not project or not is_under(project, folder):
            continue
        if float(row.get("happened_at") or 0.0) > opened_at:
            return True
    return False


def observe(rows: list[dict]) -> None:
    """Look at every task row once: a key not seen before is a new task, and a
    new task that is not archived adds its app. Then recompute every row's
    ``unread`` (see the section comment above). Called from the tasks listing,
    so the listing's own response already reflects the add. Writes only when
    something changed — an idle poll must not rewrite the file."""
    with _LOCK:
        _observe_locked(rows)


def _observe_locked(rows: list[dict]) -> None:
    state = read_state()
    seen = set(state["seen"])
    known = {a["path"] for a in state["apps"]}
    changed = False
    now = None
    live_keys: set[str] = set()
    # Rows from before the stamp shipped have no ``openedAt``. Stamp them NOW,
    # once: the quiet upgrade — nothing already on the desk lights until a task
    # finishes under it from here on (the alternative, 0.0, would light every
    # app with any finished task, which is most of them).
    for a in state["apps"]:
        if not isinstance(a.get("openedAt"), (int, float)):
            a["openedAt"] = _now_epoch()
            changed = True
    # OLDEST activity first: `_task_rows` sorts newest first, and every new
    # app is inserted at the TOP (index 0 — the table is display order, D1386).
    # Walking oldest → newest and inserting each at 0 leaves the newest on top;
    # walking the listing as given would seed a first-run desk — or any pass
    # that adds several apps — oldest on top, inverted (Bugbot on #867).
    for row in reversed(rows):
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
        # Never opened: the task that put it here lights it once it finishes.
        state["apps"].insert(0, {"path": folder, "addedAt": now, "openedAt": 0.0})
        known.add(folder)
    for a in state["apps"]:
        flag = _unread(a["path"], float(a["openedAt"]), rows)
        if a.get("unread") is not flag:
            a["unread"] = flag
            changed = True
    pruned = seen & live_keys
    if changed or pruned != set(state["seen"]):
        state["seen"] = sorted(pruned)
        write_state(state)


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def add(path: str) -> bool:
    """Put `path` on the desk by hand — the explorer's "Open in project"
    button — at the TOP (index 0, D1386). True when it was added, False when
    it was already there (the row keeps its place: the sidebar focuses it
    rather than re-inserting). The one way onto the desk besides a task;
    `observe`'s dedupe on `known` means a later task under the folder adds
    nothing."""
    folder = canonical_fs_path(os.path.abspath(path)).rstrip("/")
    with _LOCK:
        state = read_state()
        if any(a["path"] == folder for a in state["apps"]):
            return False
        state["apps"].insert(0, {
            "path": folder,
            "addedAt": datetime.now(timezone.utc).isoformat(),
            # The button navigates to the app at once: it IS an open.
            "openedAt": _now_epoch(),
            "unread": False,
        })
        write_state(state)
        return True


def ensure(project: str) -> tuple[str | None, bool]:
    """The app folder `project` belongs to (`app_dir_for`), put on the desk if
    it is not there — the door-time check (module docstring). `(folder, added)`:
    folder None when the project is no app (or its registered folder is gone),
    `added` True only when this call put the row there, so a caller announces
    a change only when there was one. `add`'s stamp: the row is born opened,
    so a build that finished weeks ago does not light the dot the moment its
    task is looked at."""
    folder = app_dir_for(project)
    if folder is None or not os.path.isdir(folder):
        return None, False
    return folder, add(folder)


def set_order(paths) -> list[dict]:
    """Reorder the desk after a drag in the sidebar: ``paths`` is the order
    the user arranged, top first (any spelling of each path). The new order
    is every desk app the list does NOT name, in its current relative order,
    followed by the named apps in ``paths`` order. Unknown paths (not on the
    desk, or not absolute) and repeats are dropped.

    Unnamed apps go FIRST on purpose: the one way an app reaches the desk
    without the client knowing is `observe` inserting it at the top while the
    drag was in flight, and that app belongs where `observe` put it — on top —
    not shuffled under the arrangement the user made of the older ones. This
    is the reverse of `dock.set_order`, whose stragglers are old pins.

    The order lives here, not in the browser, since D1386: localStorage is per
    origin and the desktop app's port walk (1777..1787, then ephemeral) makes
    a relaunch a new origin with an empty store. Writes only when the order
    changed. Returns `list_apps()`. ValueError when ``paths`` is not a list
    of strings."""
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("paths must be a list of app paths")
    with _LOCK:
        state = read_state()
        by_path = {a["path"]: a for a in state["apps"]}
        named: list[str] = []
        for p in paths:
            if not p or not os.path.isabs(p):
                continue
            folder = canonical_fs_path(os.path.abspath(p)).rstrip("/")
            if folder in by_path and folder not in named:
                named.append(folder)
        chosen = set(named)
        ordered = ([a for a in state["apps"] if a["path"] not in chosen]
                   + [by_path[p] for p in named])
        if [a["path"] for a in ordered] != [a["path"] for a in state["apps"]]:
            state["apps"] = ordered
            write_state(state)
    return list_apps()


def remove(path: str) -> bool:
    """Drop `path` from the desk. True when it was there. The archive
    side-effect is the ROUTER's (it owns the tasks) — this is only the table."""
    folder = canonical_fs_path(os.path.abspath(path)).rstrip("/")
    with _LOCK:
        state = read_state()
        kept = [a for a in state["apps"] if a["path"] != folder]
        if len(kept) == len(state["apps"]):
            return False
        state["apps"] = kept
        write_state(state)
        return True


def mark_opened(path: str, at: float) -> bool:
    """The user opened the app: stamp the row's ``openedAt`` with `at` (epoch,
    the server's clock) and clear its ``unread`` — the one gesture that clears
    the dot (see the unread section above). The next `observe` pass recomputes
    the flag against the new stamp, so only a task that finishes AFTER this
    lights it again. True when the row exists; False for a path not on the
    desk (nothing to clear on)."""
    folder = canonical_fs_path(os.path.abspath(path)).rstrip("/")
    with _LOCK:
        state = read_state()
        for a in state["apps"]:
            if a["path"] == folder:
                a["openedAt"] = float(at)
                a["unread"] = False
                write_state(state)
                return True
        return False


def _added_epoch(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


ICON_NAME = app_listing.ICON_NAME
ICON_NAMES = app_listing.ICON_NAMES


def app_icon(folder: str) -> dict | None:
    """The app's optional icon (``icon.svg``, else ``icon.png``) — ``{"icon":
    <canonical path>, "mtime": <epoch>}`` when one sits directly in the app
    folder, else None. The
    resolution itself lives in `app_listing.app_icon` (which the /apps listing
    reads through `app_dict`, so a card carries the same mark this row does);
    this wrapper only restates the path in the shell's canonical (forward-slash)
    form, which every other field of a desk row is already in."""
    found = app_listing.app_icon(folder)
    if found is None:
        return None
    return {"icon": canonical_fs_path(found["icon"]), "mtime": found["mtime"]}


def _canvas_of(path: str) -> dict | None:
    """`{env, name}` when the desk row IS a canvas clone (the clone dir
    itself, not a file in it), else None (see canvases.clone_dir_for)."""
    from fused_render.canvases import canvases_root as canvases_root_for, clone_dir_for

    clone = clone_dir_for(path)
    if clone is None:
        return None
    env, name, clone_dir = clone
    # The row must BE the clone (one level under the root), not a file in
    # it. Plain and resolved spellings of the root both name the clone;
    # nothing here resolves `path` (see clone_dir_for).
    real_root = os.path.realpath(canvases_root_for(env))
    if path.rstrip("/") not in (
        canonical_fs_path(clone_dir).rstrip("/"),
        canonical_fs_path(os.path.join(real_root, name)).rstrip("/"),
    ):
        return None
    return {"env": env, "name": name}


def list_apps() -> list[dict]:
    """The desk, in stored order — display order, top first (D1386: new apps
    are inserted at the top, a drag rewrites it via `set_order`). Each: ``path`` (canonical), ``name``
    (folder name), ``kind`` (``linked`` for a registry folder, ``workspace``
    otherwise), ``entry`` (the page to run, or None), ``exists``, ``icon`` /
    ``icon_mtime`` (the optional ``icon.svg`` / ``icon.png``, see `app_icon`), ``added_at``
    (epoch), ``opened_at`` (epoch of the last `mark_opened`; 0 for a row a task
    put here that has never been opened), ``unread`` (a task finished under it
    since that open — the sidebar's green dot, see the unread section). A
    folder that is gone or unreadable still lists — the row is the user's to
    remove — with ``exists`` false and no entry."""
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
        icon = None
        if not guard.blocks(path):
            try:
                exists = os.path.isdir(path)
                if exists:
                    entry = app_listing.app_entry(path)
                    icon = app_icon(path)
            except OSError:
                exists = False
        out.append({
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or path,
            "kind": "linked" if path in linked else "workspace",
            # Which canvas this row is (`{env, name}`), None for an app
            # folder: the sidebar splits the desk into Projects and Canvases
            # on it, and a canvas row opens the canvas's project page.
            "canvas": _canvas_of(path),
            "entry": canonical_fs_path(entry) if entry else None,
            "exists": exists,
            "icon": icon["icon"] if icon else None,
            "icon_mtime": icon["mtime"] if icon else None,
            "added_at": _added_epoch(a.get("addedAt")),
            "opened_at": (a["openedAt"] if isinstance(a.get("openedAt"), (int, float))
                          else None),
            "unread": bool(a.get("unread")),
        })
    return out
