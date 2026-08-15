"""The workspace app recents store at ~/.fused-render/app_recents.json.

Extracted from server/routers/apps.py so the shell can record into it too:
the file recents endpoint (shell/recents.py) skips a workspace app's entry
page and forwards the open HERE instead — an entry opened through the file
tree must bump the app's recency exactly like a card click, or the open lands
in neither sidebar list. The router keeps its routes and delegates to this
module; shell↛server stays acyclic because this lives at the top level, like
registered_apps.

Entries identify an app by its WORKSPACE-RELATIVE path (`path`, e.g.
"local/demo" or "tag/shelf/app") — unique at every depth the walk lists,
where a (tag, name) key was not — newest-first, deduped, capped. The
workspace is always local, so plain isdir checks are safe here.
"""
import os
from datetime import datetime, timezone

from fused_render.shell.seed import fused_dir

# The store is the sort input for /home and /apps (opened_at in GET
# /api/apps), not just a short recents row — so the cap must comfortably
# exceed the number of apps a user actively cycles through, or open #N+1
# silently loses its rank.
APP_RECENTS_CAP = 200


def _path() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "app_recents.json")


def read_store() -> dict:
    from fused_render.shell import storage

    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return {"entries": []}
    entries = data.get("entries")
    return {
        "entries": [
            e
            for e in (entries if isinstance(entries, list) else [])
            if isinstance(e, dict) and isinstance(e.get("path"), str)
        ]
    }


def workspace_rel(root: str, path: str) -> str | None:
    """`path` as a workspace-relative, forward-slash key, or None when it isn't
    inside the workspace. The store's identity: unique at every depth the walk
    lists (1-3), where (tag, name) is not — two depth-3 apps under different
    shelves of one tag share both. Normalized to "/" so a key written on
    Windows matches the split in folder_exists; the replace is os.sep-
    conditional because on POSIX a backslash is a legal filename character."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        # Windows: relpath across drives has no relative form — that is just
        # "not inside the workspace", not an error.
        return None
    if rel == "." or rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/") if os.sep != "/" else rel


def folder_exists(rel: str) -> bool:
    """Does the workspace-relative app path currently resolve to a folder on
    disk? Rejects a key that would escape the workspace — the store is
    user-writable, so `rel` cannot be trusted to stay under it."""
    # Split on the OS separator too: a user-edited backslash key on Windows
    # must not smuggle `..` past a "/"-only split. Segments are then vetted
    # individually — a drive-relative segment like "C:foo" would make a
    # starred os.path.join discard the workspace base entirely, so anything
    # carrying a drive or absolute form is rejected, and the join happens as
    # ONE "/"-joined string (a legal separator on Windows as well) so no
    # segment can ever reset the base.
    parts = rel.replace(os.sep, "/").split("/")
    if os.path.isabs(rel) or rel.startswith(".") or ".." in parts:
        return False
    if any(not p or os.path.isabs(p) or os.path.splitdrive(p)[0] for p in parts):
        return False
    return os.path.isdir(os.path.join(fused_dir(), "/".join(parts)))


def record_open(path: str, title: str | None = None) -> bool:
    """Record an open of the workspace app folder at absolute `path` (bump to
    top, refresh openedAt). False when `path` isn't an existing app folder
    inside the workspace — the caller's benign no-op."""
    from fused_render.shell import storage

    rel = workspace_rel(fused_dir(), path)
    if rel is None or not folder_exists(rel):
        return False
    data = read_store()
    # Dedupe by path; a title-less re-record keeps the last known title.
    existing_title = None
    kept = []
    for e in data["entries"]:
        if e["path"] == rel:
            t = e.get("title")
            if existing_title is None and isinstance(t, str) and t:
                existing_title = t
            continue
        kept.append(e)
    entry = {
        "path": rel,
        "openedAt": datetime.now(timezone.utc).isoformat(),
    }
    if title is not None:
        entry["title"] = title
    elif existing_title is not None:
        entry["title"] = existing_title
    data["entries"] = [entry, *kept][:APP_RECENTS_CAP]
    storage.write_json(_path(), data)
    return True
