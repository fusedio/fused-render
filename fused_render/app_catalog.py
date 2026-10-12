"""One row shape over every source an app can be listed from.

The hub, Home, the launcher, the LAN share and the sidebar each read their
own stores and each key by PATH. That was enough until two folders could
carry one app: `app/<name>` is where a new app is created, `local/<slug>` is
where a `.fused` clones to, and the clone probe only ever looked in
`local/`, so cloning your own export made a second copy that listed as a
second app. Nothing read the entry page's `fused-app-id` (app_id.py), so
nothing could say the two were one.

This module is the missing middle: given listing rows (`app_listing.app_dict`
shape, any source), `annotate` adds

* ``app_id`` — the entry page's declared identity, or None;
* ``kind`` — the delete-policy class (`kind_for`), which is what the
  uninstall endpoint dispatches on;
* ``dupes`` — the OTHER paths in the same batch carrying the same id, so a
  card can say "2 copies" and a user can see which folder they are about to
  remove. Duplicates are shown, never merged: the showcase checkout and a
  clone of the same app are legitimately different folders.

Two small stores live here because listing is where they are consulted:

* ``hidden_apps.json`` — paths the user uninstalled that cannot be deleted
  (a showcase subfolder: deleting it dirties the clone and the next sync
  brings it back). Hidden rows are dropped from every listing.
* ``uninstalled.json`` — tombstones. `.fused/meta.json` decides move-from-copy
  by whether the recorded `app_dir` still exists (app_fused_dir._after_move);
  delete the original of a duplicate pair and the survivor would read as a
  MOVE and inherit the original's desk row, bookmarks and sessions. A
  tombstone says "that path was deleted on purpose", and the witness treats
  it as a copy. Entries expire after `TOMBSTONE_TTL_S`.
"""
from __future__ import annotations

import logging
import os
import time

from fused_render import app_id as app_identity
from fused_render import app_listing
from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import storage
from fused_render.shell.seed import fused_dir

logger = logging.getLogger(__name__)

TOMBSTONE_TTL_S = 30 * 24 * 3600

# The delete-policy classes. See the uninstall router for what each means.
KIND_OWN_REPO = "own-repo"      # <ws>/app/<name> and any other app with its own .git
KIND_MONOREPO = "monorepo"      # <ws>/local/<slug>, the shared repo (D626)
KIND_LINKED = "linked"          # outside the workspace (registered_apps)
KIND_APPFILE = "appfile"        # an exported .fused file
KIND_SHOWCASE = "showcase"      # a subfolder of the showcase clone
KIND_WORKSPACE = "workspace"    # any other folder under the workspace


def _canon(path: str) -> str:
    return canonical_fs_path(os.path.realpath(path)).rstrip("/")


def _under(path: str, root: str) -> bool:
    p, r = os.path.normcase(path), os.path.normcase(root)
    return p == r or p.startswith(r.rstrip("/") + "/")


def kind_for(path: str, *, is_file: bool | None = None) -> str:
    """The delete-policy class of `path`. Pure string work plus one `isdir`
    on `<path>/.git`; never raises."""
    if is_file is None:
        try:
            is_file = os.path.isfile(path)
        except OSError:
            is_file = False
    if is_file:
        return KIND_APPFILE
    p = _canon(path)
    root = _canon(fused_dir())
    if not _under(p, root) or p == root:
        return KIND_LINKED
    rel = p[len(root):].lstrip("/")
    first = rel.split("/", 1)[0]
    if first == "showcase":
        return KIND_SHOWCASE
    try:
        if os.path.isdir(os.path.join(path, ".git")):
            return KIND_OWN_REPO
    except OSError:
        pass
    if first == "local":
        return KIND_MONOREPO
    return KIND_WORKSPACE


def row_app_id(row: dict) -> str | None:
    """The id a listing row's entry page declares, or None. A `.fused` row
    carries none here — its id sits in the container manifest, which costs a
    zip read the hot listing does not pay."""
    entry = row.get("entry_html")
    if not isinstance(entry, str) or not entry:
        return None
    try:
        return app_identity.app_id(entry)
    except Exception:  # noqa: BLE001 — a listing degrades, never fails
        return None


def annotate(rows: list[dict]) -> list[dict]:
    """Add ``app_id``, ``kind`` and ``dupes`` to every row IN PLACE and drop
    the hidden ones. Returns the kept rows (a new list)."""
    hidden = hidden_paths()
    kept: list[dict] = []
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        path = r.get("path")
        if not isinstance(path, str):
            continue
        if hidden and _canon(path) in hidden:
            continue
        if "app_id" not in r:
            r["app_id"] = row_app_id(r)
        if "kind" not in r or r.get("kind") is None:
            r["kind"] = kind_for(path, is_file=r.get("kind") == KIND_APPFILE)
        kept.append(r)
        if r["app_id"]:
            by_id.setdefault(r["app_id"], []).append(r)
    for group in by_id.values():
        if len(group) < 2:
            for r in group:
                r["dupes"] = []
            continue
        for r in group:
            r["dupes"] = [o["path"] for o in group if o is not r]
    for r in kept:
        r.setdefault("dupes", [])
    return kept


def workspace_app_with_id(app_id: str) -> str | None:
    """The workspace folder whose entry page declares `app_id`, or None —
    the clone probe's question, asked of the WHOLE workspace walk rather
    than of `local/` alone (which is how the duplicate got made). One 4 KiB
    head read per app; bounded by the workspace size."""
    if not app_identity.is_valid(app_id):
        return None
    for row in app_listing.discover_workspace_apps(fused_dir()):
        if row_app_id(row) == app_id:
            return row["path"]
    return None


# ----------------------------------------------------------------- stores ---

def _hidden_path() -> str:
    return os.path.join(storage.home_dir(), "hidden_apps.json")


def hidden_paths() -> set[str]:
    data = storage.read_json(_hidden_path())
    raw = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return set()
    return {_canon(p) for p in raw if isinstance(p, str) and os.path.isabs(p)}


def hide(path: str) -> None:
    paths = hidden_paths()
    paths.add(_canon(path))
    storage.write_json(_hidden_path(), {"paths": sorted(paths)})


def unhide(path: str) -> bool:
    paths = hidden_paths()
    key = _canon(path)
    if key not in paths:
        return False
    paths.discard(key)
    storage.write_json(_hidden_path(), {"paths": sorted(paths)})
    return True


def _tombstone_path() -> str:
    return os.path.join(storage.home_dir(), "uninstalled.json")


def _read_tombstones() -> list[dict]:
    data = storage.read_json(_tombstone_path())
    raw = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    now = time.time()
    return [e for e in raw if isinstance(e, dict) and isinstance(e.get("path"), str)
            and isinstance(e.get("at"), (int, float)) and now - e["at"] < TOMBSTONE_TTL_S]


def tombstone(path: str, app_id: str | None) -> None:
    """Record that `path` was uninstalled on purpose."""
    key = _canon(path)
    entries = [e for e in _read_tombstones() if e["path"] != key]
    entries.insert(0, {"path": key, "app_id": app_id, "at": time.time()})
    storage.write_json(_tombstone_path(), {"entries": entries[:500]})


def is_tombstoned(path: str) -> bool:
    """Was `path` uninstalled (within the TTL)? Asked by the move witness."""
    try:
        key = _canon(path)
    except Exception:  # noqa: BLE001
        return False
    return any(e["path"] == key for e in _read_tombstones())
