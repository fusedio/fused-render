"""Drop every store row that names an app folder, when that app is removed.

The mirror of `app_state_move.rewrite_stores`: a MOVE re-roots the server's
stores onto the new path, and until now a DELETE did nothing — every store
filtered the dead path on read and kept it on disk, which is why the desk
showed struck-through rows and the registries only ever grew.

Same stores as the move, plus the three that only a delete has to care
about: the background-app autostart list, the bots builds ledger (a build
pointing at a folder that is gone is a dead button), and the Claude session
index, whose transcripts STAY on disk — they are archived, not erased, the
same gesture as DELETE /api/current-apps.

Best-effort like its mirror: never raises, one store failing does not stop
the next, and every rewrite is a prefix match with a directory boundary (a
bookmark may point INTO the app).
"""
from __future__ import annotations

import logging
import os

from fused_render import workspace_migration
from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import storage

logger = logging.getLogger(__name__)


def prune_stores(root: str) -> dict:
    """Forget `root` (and anything under it) everywhere. Returns a summary of
    what was dropped per store. Never raises."""
    root = os.path.abspath(root)
    out: dict = {}
    for label, fn in (
            ("bookmarks", _prune_bookmarks),
            ("recents", _prune_recents),
            # NOT the schedule store: its entries are the task HISTORY
            # tasks._collect lists ("one per scheduled entry"), the same rows
            # "Hide from projects" keeps. The pending ones are cancelled by
            # _archive_folder before this runs; the rest stay as the record.
            ("pin", _prune_pin),
            ("current_apps", _prune_current_apps),
            ("registered_apps", _prune_registered),
            ("app_recents", _prune_app_recents),
            ("background_apps", _prune_background),
            ("bot_builds", _prune_bot_builds)):
        try:
            out[label] = fn(root)
        except Exception:  # noqa: BLE001 — the delete path is behind this
            logger.exception("could not prune %s after the app was removed", label)
            out[label] = None
    return out


def _is_under(path: object, root: str) -> bool:
    if not isinstance(path, str) or not path:
        return False
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _url_is_under(url: object, root: str) -> bool:
    """A bookmark/recent stores a VIEW URL; `workspace_migration._remap_url`
    knows how to read the fs path out of one, so ask it whether the url
    would remap — a non-None answer means it names something under `root`."""
    return workspace_migration._remap_url(url, root, root + "__probe__") is not None


def _prune_bookmarks(root: str) -> int:
    path = os.path.join(storage.home_dir(), "bookmarks.json")
    items = storage.read_json(path)
    if not isinstance(items, list):
        return 0
    dropped = 0

    def walk(entries: list) -> list:
        nonlocal dropped
        kept = []
        for item in entries:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            if _url_is_under(item.get("url"), root):
                dropped += 1
                continue
            children = item.get("children")
            if isinstance(children, list):
                item["children"] = walk(children)
            kept.append(item)
        return kept

    items = walk(items)
    if dropped:
        storage.write_json(path, items)
    return dropped


def _prune_recents(root: str) -> int:
    path = os.path.join(storage.home_dir(), "recents.json")
    data = storage.read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0
    kept = [e for e in entries
            if not (isinstance(e, dict) and _url_is_under(e.get("url"), root))]
    dropped = len(entries) - len(kept)
    if dropped:
        data["entries"] = kept
        storage.write_json(path, data)
    return dropped


def _prune_pin(root: str) -> int:
    path = os.path.join(workspace_migration._app_support_dir(), "pin.json")
    data = storage.read_json(path)
    if not isinstance(data, dict):
        return 0
    if not _is_under(data.get("path"), root):
        return 0
    try:
        os.remove(path)
    except OSError:
        return 0
    return 1


def _prune_current_apps(root: str) -> int:
    path = os.path.join(storage.home_dir(), "current_apps.json")
    data = storage.read_json(path)
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, list):
        return 0
    kept = [a for a in apps if not (isinstance(a, dict) and _is_under(a.get("path"), root))]
    dropped = len(apps) - len(kept)
    if dropped:
        data["apps"] = kept
        storage.write_json(path, data)
    return dropped


def _prune_registered(root: str) -> int:
    path = os.path.join(storage.home_dir(), "registered_apps.json")
    data = storage.read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0
    kept = [e for e in entries if not (isinstance(e, dict) and _is_under(e.get("path"), root))]
    dropped = len(entries) - len(kept)
    if dropped:
        data["entries"] = kept
        storage.write_json(path, data)
    return dropped


def _prune_app_recents(root: str) -> int:
    from fused_render.app_state_move import _workspace_rel

    path = os.path.join(storage.home_dir(), "app_recents.json")
    data = storage.read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0
    rel = _workspace_rel(root)
    if rel is None:
        return 0
    kept = [e for e in entries
            if not (isinstance(e, dict) and isinstance(e.get("path"), str)
                    and (e["path"] == rel or e["path"].startswith(rel + "/")))]
    dropped = len(entries) - len(kept)
    if dropped:
        data["entries"] = kept
        storage.write_json(path, data)
    return dropped


def _prune_background(root: str) -> int:
    path = os.path.join(storage.home_dir(), "background_apps.json")
    data = storage.read_json(path)
    raw = data.get("autostart") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return 0
    kept = [p for p in raw if not _is_under(p, root)]
    dropped = len(raw) - len(kept)
    if dropped:
        data["autostart"] = kept
        storage.write_json(path, data)
    return dropped


def _prune_bot_builds(root: str) -> int:
    """A build row that names the folder loses its `dir`/`folder` pointer —
    the row stays (the Builds panel is the user's log), the dead link goes."""
    from fused_render.bots import store as bots_store

    builds = bots_store.builds_read()
    if not builds:
        return 0
    changed = 0
    for b in builds:
        if _is_under(b.get("dir"), root):
            b["dir"] = ""  # the panel's own "no folder" value (builds.ts)
            changed += 1
    if changed:
        bots_store.builds_write(builds)
    return changed


def canonical(path: str) -> str:
    return canonical_fs_path(os.path.abspath(path)).rstrip("/")
