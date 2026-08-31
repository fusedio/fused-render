"""Re-root the server's own stores when one APP FOLDER moves (D548, SPEC §47).

`workspace_migration` solved this once for the whole workspace moving (D337):
bookmarks, shell recents, scheduled targets and the menu-bar pin all write the
absolute path of what they point at, and a move leaves every one of them
naming a folder that no longer exists. An individual app moving is the same
event at a smaller radius, so those four are rewritten by the same code
(`workspace_migration.rewrite_absolute_paths`), handed the app's old and new
roots instead of the workspace's.

Three more stores exist that a WORKSPACE move never had to touch, because
they key inside it or did not exist then:

* ``current_apps.json`` — the desk (D487). It only ever grows through new
  tasks, so a stale path would sit there as an `exists: false` row forever
  next to nothing: the moved app returns only when a NEW task runs under it.
* ``registered_apps.json`` — external apps. A re-open at the new path would
  re-register it, but the OLD entry would dangle until then, and the row is
  also the external app's `opened_at` record, which a move should not reset.
* ``app_recents.json`` — workspace apps' recency, keyed WORKSPACE-RELATIVE.
  An app moved within the workspace keeps its history under the new key; one
  moved out of the workspace loses the key entirely (its recency now lives in
  the registered store) and the dead entry is dropped.

Every rewrite is a prefix remap with a directory boundary (an app's SUBTREE
may be what a bookmark points into) and is idempotent — the caller
(`app_fused_dir._after_move`) re-runs on every open while a live session
holds the transcript half of the migration back, and a second pass over
already-rewritten state finds nothing left to change.

Best-effort like its caller: `rewrite_stores` never raises, and one store
failing does not stop the next.
"""
import logging
import os

from fused_render import workspace_migration
from fused_render._view_url_codec import canonical_fs_path
from fused_render.shell import storage
from fused_render.shell.seed import fused_dir

logger = logging.getLogger(__name__)


def rewrite_stores(old_root: str, new_root: str) -> None:
    """Point every store that names `old_root` (or anything under it) at
    `new_root`. Never raises."""
    for label, fn in (
            ("bookmarks/recents/schedule/pin",
             workspace_migration.rewrite_absolute_paths),
            ("current_apps.json", _rewrite_current_apps),
            ("registered_apps.json", _rewrite_registered),
            ("app_recents.json", _rewrite_app_recents)):
        try:
            fn(old_root, new_root)
        except Exception:  # noqa: BLE001 — the render path is behind this
            logger.exception("could not re-root %s after the app moved", label)


def _rewrite_current_apps(old_root: str, new_root: str) -> None:
    """The desk stores canonical absolute paths. If the new path is already a
    row (a task ran there before this open), the stale row just drops."""
    path = os.path.join(storage.home_dir(), "current_apps.json")
    data = storage.read_json(path)
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, list):
        return
    changed = False
    kept, have = [], set()
    for app in apps:
        remapped = workspace_migration._remap(
            app.get("path") if isinstance(app, dict) else None, old_root, new_root)
        if remapped is not None:
            app["path"] = canonical_fs_path(remapped)
            changed = True
        key = app.get("path") if isinstance(app, dict) else None
        if isinstance(key, str) and key in have:
            changed = True
            continue
        have.add(key)
        kept.append(app)
    if changed:
        data["apps"] = kept
        storage.write_json(path, data)
        logger.info("re-rooted moved app in %s", path)


def _rewrite_registered(old_root: str, new_root: str) -> None:
    """External apps' registry-and-recency. The remapped entry keeps its
    `openedAt`; one whose new home is inside the workspace is left for the
    read-side filter, which already drops workspace paths."""
    path = os.path.join(storage.home_dir(), "registered_apps.json")
    data = storage.read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return
    changed = False
    for entry in entries:
        remapped = workspace_migration._remap(
            entry.get("path") if isinstance(entry, dict) else None,
            old_root, new_root)
        if remapped is not None:
            entry["path"] = os.path.abspath(remapped)
            changed = True
    if changed:
        storage.write_json(path, data)
        logger.info("re-rooted moved app in %s", path)


def _workspace_rel(path: str) -> str | None:
    """`path` as the workspace-relative forward-slash key `app_recents.json`
    uses (routers/apps.py `_workspace_rel`), or None when outside."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(fused_dir()))
    except ValueError:
        return None
    if rel == "." or rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/") if os.sep != "/" else rel


def _rewrite_app_recents(old_root: str, new_root: str) -> None:
    path = os.path.join(storage.home_dir(), "app_recents.json")
    data = storage.read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return
    old_rel, new_rel = _workspace_rel(old_root), _workspace_rel(new_root)
    if old_rel is None:
        return  # the old home was outside the workspace; nothing keyed on it
    changed = False
    kept = []
    for entry in entries:
        rel = entry.get("path") if isinstance(entry, dict) else None
        tail = None
        if isinstance(rel, str) and (rel == old_rel or rel.startswith(old_rel + "/")):
            tail = rel[len(old_rel):]
        if tail is None:
            kept.append(entry)
            continue
        changed = True
        if new_rel is None:
            continue  # moved out of the workspace: the registered store owns it now
        entry["path"] = new_rel + tail
        kept.append(entry)
    if changed:
        data["entries"] = kept
        storage.write_json(path, data)
        logger.info("re-rooted moved app in %s", path)
